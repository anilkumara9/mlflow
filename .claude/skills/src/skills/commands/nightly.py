from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, cast

import yaml
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

REPOSITORY = "mlflow/mlflow"
# TODO: Support additional nightly workflows.
SOURCE_WORKFLOW = "cross-version-tests.yml"
NIGHTLY_WORKFLOW = ".github/workflows/nightly.yml"
SCHEMA_PATH = Path(__file__).parents[3] / "nightly" / "output.schema.yaml"
FAILED = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}


def api(endpoint: str, *, method: str = "GET", data: Any = None) -> dict[str, Any]:
    command = ["gh", "api", endpoint, "--method", method]
    if data is not None:
        command.extend(["--input", "-"])
    result = subprocess.run(
        command,
        input=json.dumps(data) if data is not None else None,
        capture_output=True,
        text=True,
        check=True,
    )
    return cast(dict[str, Any], json.loads(result.stdout)) if result.stdout.strip() else {}


def paginated(endpoint: str, key: str | None = None) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["gh", "api", endpoint, "--paginate", "--slurp"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [item for page in json.loads(result.stdout) for item in (page[key] if key else page)]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def empty_state() -> dict[str, Any]:
    return {"processed": []}


def restore_state(default_branch: str) -> dict[str, Any]:
    artifacts = paginated(
        f"repos/{REPOSITORY}/actions/artifacts?name=nightly-state&per_page=100", "artifacts"
    )
    for artifact in sorted(artifacts, key=lambda item: item["id"], reverse=True):
        if artifact["expired"]:
            continue
        run = api(f"repos/{REPOSITORY}/actions/runs/{artifact['workflow_run']['id']}")
        if run["path"].split("@")[0] != NIGHTLY_WORKFLOW or run["head_branch"] != default_branch:
            continue
        result = subprocess.run(
            ["gh", "api", f"repos/{REPOSITORY}/actions/artifacts/{artifact['id']}/zip"],
            capture_output=True,
            check=True,
        )
        with zipfile.ZipFile(io.BytesIO(result.stdout)) as archive:
            return cast(dict[str, Any], json.loads(archive.read("snapshot.json"))["state"])
    return empty_state()


def load_issues() -> list[dict[str, Any]]:
    return [
        issue
        for issue in paginated(f"repos/{REPOSITORY}/issues?state=open&labels=nightly&per_page=100")
        if "pull_request" not in issue
    ]


def run_key(run: dict[str, Any]) -> str:
    return f"{run['id']}:{run['run_attempt']}"


def failed_jobs(run: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        job
        for job in paginated(
            f"repos/{REPOSITORY}/actions/runs/{run['id']}"
            f"/attempts/{run['run_attempt']}/jobs?per_page=30",
            "jobs",
        )
        if job["conclusion"] in FAILED
    ]


def latest_run() -> dict[str, Any] | None:
    runs = api(
        f"repos/{REPOSITORY}/actions/workflows/{SOURCE_WORKFLOW}/runs?event=schedule&per_page=1"
    )["workflow_runs"]
    return cast(dict[str, Any], runs[0]) if runs else None


def collect(out_dir: Path) -> None:
    default_branch = api(f"repos/{REPOSITORY}")["default_branch"]
    state = restore_state(default_branch)
    runs = []
    candidate = latest_run()
    if (
        candidate is not None
        and candidate["conclusion"] == "failure"
        and run_key(candidate) not in state["processed"]
    ):
        if jobs := failed_jobs(candidate):
            run = {
                key: candidate[key]
                for key in (
                    "id",
                    "run_attempt",
                    "head_sha",
                    "name",
                    "path",
                    "event",
                    "head_branch",
                    "status",
                    "conclusion",
                )
            }
            run["html_url"] = (
                f"https://github.com/{REPOSITORY}/actions/runs/{run['id']}"
                f"/attempts/{run['run_attempt']}"
            )
            run["retry_available"] = run["run_attempt"] == 1
            run["jobs"] = [
                {
                    "id": job["id"],
                    "workflow": run["name"],
                    "job": job["name"],
                    "url": f"{run['html_url']}/job/{job['id']}",
                }
                for job in jobs
            ]
            runs.append(run)
    write_json(
        out_dir / "snapshot.json",
        {"repository": REPOSITORY, "runs": runs, "issues": load_issues(), "state": state},
    )


def issue_body(group: dict[str, Any]) -> str:
    evidence = "\n".join(f"- {item}" for item in group["evidence"])
    body = f"{group['summary']}\n\n### Evidence\n\n{evidence}"
    if group["uncertainties"]:
        uncertainties = "\n".join(f"- {item}" for item in group["uncertainties"])
        body += f"\n\n### Uncertainties\n\n{uncertainties}"
    jobs = "\n".join(f"- [{job['workflow']} / {job['job']}]({job['url']})" for job in group["jobs"])
    return f"{body}\n\n### CI jobs\n\n{jobs}"


def post_check(
    report: dict[str, Any],
    snapshot: dict[str, Any],
    snapshot_path: Path,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    Draft202012Validator(yaml.safe_load(SCHEMA_PATH.read_text())).validate(report)
    state = snapshot["state"]
    pending = [run for run in snapshot["runs"] if run_key(run) not in state["processed"]]
    plan: dict[str, Any] = {
        "dry_run": not apply,
        "issues": [],
        "reruns": [],
        "findings": [],
    }
    if not pending:
        return plan

    for group in report["groups"]:
        if group["verdict"] != "new":
            plan["findings"].append(group)
            continue
        body = issue_body(group)
        action = {"title": group["title"], "body": body, "issue_number": None}
        plan["issues"].append(action)
        if apply:
            created = api(
                f"repos/{REPOSITORY}/issues",
                method="POST",
                data={"title": group["title"], "body": body, "labels": ["nightly"]},
            )
            action["issue_number"] = created["number"]

    verdicts: dict[str, set[str]] = {}
    for group in report["groups"]:
        for job in group["jobs"]:
            verdicts.setdefault(job["url"], set()).add(group["verdict"])
    for run in pending:
        if run["run_attempt"] != 1 or not all(
            verdicts.get(job["url"]) == {"transient"} for job in run["jobs"]
        ):
            continue
        plan["reruns"].append({"run_id": run["id"], "expected_run_attempt": run["run_attempt"]})
        if apply:
            api(f"repos/{REPOSITORY}/actions/runs/{run['id']}/rerun-failed-jobs", method="POST")

    if apply:
        state["processed"] = sorted(set(state["processed"]) | {run_key(run) for run in pending})
        write_json(snapshot_path, snapshot)
    return plan


def run(args: argparse.Namespace) -> None:
    if args.nightly_command == "collect":
        collect(args.out_dir)
        return
    report = json.loads(args.report.read_text())
    snapshot = json.loads(args.snapshot.read_text())
    plan = post_check(report, snapshot, args.snapshot, apply=args.apply)
    rendered = json.dumps(plan, indent=2) + "\n"
    if args.plan:
        args.plan.write_text(rendered)
    sys.stdout.write(rendered)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("nightly", help="Collect and publish nightly CI findings")
    commands = parser.add_subparsers(dest="nightly_command", required=True)
    collect_parser = commands.add_parser("collect")
    collect_parser.add_argument("--out-dir", type=Path, required=True)
    check_parser = commands.add_parser("post-check")
    for name in ("snapshot", "report"):
        check_parser.add_argument(f"--{name}", type=Path, required=True)
    check_parser.add_argument("--plan", type=Path)
    check_parser.add_argument("--apply", action="store_true")
    parser.set_defaults(func=run)
