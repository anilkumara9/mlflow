import json
import os
import shutil
import stat
import sys
from pathlib import Path


def main(stage: Path, output: Path):
    # Run after process exit: successful stream results can precede worker completions.
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
            elif (
                event.get("type") == "user"
                and isinstance(event.get("tool_use_result"), dict)
                and event["tool_use_result"].get("status") == "completed"
            ):
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_result" and not block.get("is_error"):
                        completed_calls.add(block["tool_use_id"])
            elif event.get("type") == "system":
                key = (event.get("task_id"), event.get("run_id"))
                if (
                    event.get("subtype") == "task_started"
                    and event.get("task_type") == "local_agent"
                ):
                    agent_activity = True
                    active_workers.add(key)
                    peak_workers = max(peak_workers, len(active_workers))
                elif event.get("subtype") == "task_notification":
                    active_workers.discard(key)
    if result is None or result.get("is_error") is not False:
        raise ValueError("Nightly agent did not finish successfully")
    stats = result.get("subagent_stats")
    if stats is None and not agent_activity:
        stats = {
            "spawned": 0,
            "completed": 0,
            "failed": 0,
            "killed": {"parent": 0, "user": 0, "system": 0},
        }
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
                        "New nightly issues require a completed Agent or Task investigation "
                        "in a worktree"
                    )
            source.seek(0)
            with (stage / "report.json").open("xb") as target:
                shutil.copyfileobj(source, target)
    finally:
        os.close(directory_fd)


if __name__ == "__main__":
    main(*map(Path, sys.argv[1:]))
