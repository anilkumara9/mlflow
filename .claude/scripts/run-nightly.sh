#!/usr/bin/env bash
# Run after setup-claude and setup-gateway, with their gateway outputs in the environment.
set -euo pipefail

: "${RUNNER_TEMP:?}"
: "${GATEWAY_TOKEN:?}"
: "${GATEWAY_URL:?}"
: "${GH_TOKEN:?}"

context_dir=/tmp/nightly-context
out_dir=/tmp/nightly-out
trusted_bundle="$RUNNER_TEMP/nightly-bundle"
stage_dir="$trusted_bundle/report"
sandbox_settings="$RUNNER_TEMP/nightly-srt.json"
proxy_log="$RUNNER_TEMP/nightly-gateway-proxy.log"
source_sha=$(git rev-parse HEAD)
proxy_pid=
command_token=$(cat /proc/sys/kernel/random/uuid)

cleanup() {
  status=$?
  trap - EXIT
  if [ -n "$proxy_pid" ]; then
    kill "$proxy_pid" 2>/dev/null || true
    wait "$proxy_pid" 2>/dev/null || true
  fi
  if [ -f "$proxy_log" ]; then
    cat "$proxy_log"
  fi
  printf '::%s::\n' "$command_token"
  exit "$status"
}

umask 077
proxy_token=$(openssl rand -hex 32)
echo "::add-mask::$proxy_token"
echo "::stop-commands::$command_token"
trap cleanup EXIT

mkdir -p "$stage_dir" "$out_dir" ~/.claude ~/.cache/uv ~/.npm
if [ ! -d "$trusted_bundle/context" ]; then
  echo 'Nightly collector snapshot was not staged before analysis' >&2
  exit 1
fi
echo '{}' > ~/.claude.json
printf '%s\n%s\n' "$GATEWAY_TOKEN" "$proxy_token" | env -u GATEWAY_TOKEN -u GH_TOKEN \
  GATEWAY_URL="$GATEWAY_URL" node .claude/sandbox/gateway-proxy.mjs > "$proxy_log" 2>&1 &
proxy_pid=$!
unset GATEWAY_TOKEN
for _ in $(seq 1 30); do
  kill -0 "$proxy_pid" 2>/dev/null || break
  if curl --noproxy '*' -fsS http://127.0.0.1:8080/healthz > /dev/null; then
    break
  fi
  sleep 1
done
curl --noproxy '*' -fsS http://127.0.0.1:8080/healthz > /dev/null

# analyze-ci follows GitHub's signed log-storage redirects.
gh api meta > "$RUNNER_TEMP/nightly-github-meta.json"
jq --exit-status --arg trusted_bundle "$trusted_bundle" \
  --slurpfile meta "$RUNNER_TEMP/nightly-github-meta.json" '
  ($meta[0].domains.actions
    | if type == "array" then . else error("Invalid Actions domains in GitHub /meta") end
    | map(select(test("^productionresultssa[0-9]+\\.blob\\.core\\.windows\\.net$")))
    | unique) as $domains
  | if ($domains | length) == 0 then
      error("GitHub /meta returned no Actions log-storage domains")
    else
      .network.allowedDomains += ($domains | map(. + ":443"))
    end
  | .filesystem.denyWrite += [$trusted_bundle]
' .claude/sandbox/srt.json > "$sandbox_settings"

srt --settings "$sandbox_settings" -- true
host_processes=$(ps -e -o comm=)
if ! grep -Fxq 'Runner.Worker' <<< "$host_processes"; then
  echo 'Runner.Worker was not found on the host' >&2
  exit 1
fi
srt --settings "$sandbox_settings" -- bash -c '
  set -euo pipefail
  processes=$(ps -e -o comm=)
  if grep -Fxq "Runner.Worker" <<< "$processes"; then
    echo "Runner.Worker is visible inside the sandbox" >&2
    exit 1
  fi
'
if [ ! -r "/proc/$proxy_pid/environ" ]; then
  echo 'Gateway proxy environment is unreadable' >&2
  exit 1
fi
if grep -azq '^GATEWAY_TOKEN=' "/proc/$proxy_pid/environ"; then
  echo 'Gateway proxy inherited GATEWAY_TOKEN' >&2
  exit 1
elif [ "$?" -ne 1 ]; then
  echo 'Could not check gateway proxy environment' >&2
  exit 1
fi
for command in \
  'curl -sf https://example.com' \
  'curl -sf --unix-socket /var/run/docker.sock http://localhost/version' \
  'curl -sf --unix-socket /run/docker.sock http://localhost/version' \
  'ls /home/runner/work/_temp/_runner_file_commands | grep -q .'; do
  if srt --settings "$sandbox_settings" -- bash -c "$command"; then
    echo 'Sandbox allowed a forbidden operation' >&2
    exit 1
  fi
done

probe="$trusted_bundle/.sandbox-write-probe"
touch "$probe"
rm "$probe"
if srt --settings "$sandbox_settings" -- touch "$probe" || [ -e "$probe" ]; then
  rm -f "$probe"
  echo 'Sandbox allowed writes to the trusted Nightly bundle' >&2
  exit 1
fi

export ANTHROPIC_BASE_URL=http://localhost:8080/anthropic
export ANTHROPIC_AUTH_TOKEN="$proxy_token"
# The auto permission classifier requires this alias; investigators inherit the main model.
export ANTHROPIC_DEFAULT_SONNET_MODEL=databricks-claude-sonnet-5
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
export CLAUDE_CODE_FORK_SUBAGENT=0
export CLAUDE_CODE_ENABLE_TELEMETRY=0
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=0
export OTEL_TRACES_EXPORTER=none
export OTEL_LOGS_EXPORTER=none
export OTEL_METRICS_EXPORTER=none
export XDG_DATA_HOME=/tmp/nightly-xdg-data
export XDG_CONFIG_HOME=/tmp/nightly-xdg-config
export XDG_CACHE_HOME=/tmp/nightly-xdg-cache

agent_status=0
timeout --kill-after=10s 30m srt --settings "$sandbox_settings" -- \
  env -u NO_PROXY -u no_proxy -u GITHUB_OUTPUT -u GITHUB_ENV -u GITHUB_PATH \
    -u GITHUB_STATE -u GITHUB_STEP_SUMMARY claude \
    --model databricks-claude-sonnet-5-5 \
    --effort medium \
    --permission-mode auto \
    --setting-sources project \
    --settings '{"worktree":{"baseRef":"head"}}' \
    --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
    --tools Agent,Bash,Read,Grep,Glob,Write \
    --no-session-persistence \
    --print --verbose --output-format stream-json --forward-subagent-text \
    "/nightly $context_dir/inventory.json $context_dir/issues.json $out_dir" \
  | .claude/scripts/stream.sh "$stage_dir/stream.jsonl" || agent_status=$?

if [ "$agent_status" -ne 0 ]; then
  exit "$agent_status"
fi
if [ "$(git rev-parse HEAD)" != "$source_sha" ] || \
  [ -n "$(git status --porcelain --untracked-files=all)" ]; then
  echo 'Nightly analysis changed the coordinator checkout' >&2
  exit 1
fi

# Wait for process exit above: stream-json can emit successful results between worker completions.
python - "$stage_dir" "$out_dir" <<'PY'
import json
import os
import shutil
import stat
import sys
from pathlib import Path

stage, output = map(Path, sys.argv[1:])
result = None
agent_activity = False
isolated_calls = set()
completed_calls = set()
active_workers = set()
peak_workers = 0
with (stage / "stream.jsonl").open() as stream:
    for line in stream:
        event = json.loads(line)
        if event.get("type") == "result":
            result = event
        elif event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use" and block.get("name") in {"Agent", "Task"}:
                    agent_activity = True
                    if block.get("input", {}).get("isolation") == "worktree":
                        isolated_calls.add(block["id"])
        elif event.get("type") == "user" and event.get("tool_use_result", {}).get("status") == "completed":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_result" and not block.get("is_error"):
                    completed_calls.add(block["tool_use_id"])
        elif event.get("type") == "system":
            key = (event.get("task_id"), event.get("run_id"))
            if event.get("subtype") == "task_started" and event.get("task_type") == "local_agent":
                agent_activity = True
                active_workers.add(key)
                peak_workers = max(peak_workers, len(active_workers))
            elif event.get("subtype") == "task_notification":
                active_workers.discard(key)
if result is None or result.get("is_error") is not False:
    raise ValueError("Nightly agent did not finish successfully")
stats = result.get("subagent_stats")
if stats is None and not agent_activity:
    stats = {"spawned": 0, "completed": 0, "failed": 0, "killed": {"parent": 0, "user": 0, "system": 0}}
if not isinstance(stats, dict) or any(
    type(stats.get(field)) is not int or stats[field] < 0
    for field in ("spawned", "completed", "failed")
):
    raise ValueError("Nightly agent returned invalid worker completion metadata")
killed = stats.get("killed")
if not isinstance(killed, dict) or not {"parent", "user", "system"} <= killed.keys():
    raise ValueError("Nightly agent returned invalid worker termination metadata")
if any(type(value) is not int or value < 0 for value in killed.values()):
    raise ValueError("Nightly agent returned invalid worker termination counts")
if peak_workers > 3:
    raise ValueError("Nightly agent exceeded the three-concurrent-worker limit")
finished = stats["completed"] + stats["failed"] + sum(killed.values())
if active_workers or stats["spawned"] > finished:
    raise ValueError("Nightly agent exited before all workers finished")

flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
directory_fd = os.open(output, flags | os.O_DIRECTORY)
try:
    report_fd = os.open("report.json", flags, dir_fd=directory_fd)
    with os.fdopen(report_fd, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 2 * 1024 * 1024:
            raise ValueError("Nightly report must be a regular file no larger than 2 MiB")
        report = json.load(source)
        if any(group["verdict"] == "new" for group in report["groups"]):
            if stats["completed"] < 1 or not isolated_calls.intersection(completed_calls):
                raise ValueError(
                    "New nightly issues require a completed Agent or Task investigation in a worktree"
                )
        source.seek(0)
        with (stage / "report.json").open("xb") as target:
            shutil.copyfileobj(source, target)
finally:
    os.close(directory_fd)
PY
