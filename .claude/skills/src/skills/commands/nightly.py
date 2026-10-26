from __future__ import annotations

import argparse
import hashlib
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
    return {"processed": [], "issues": {}, "reruns": []}


def restore_state(default_branch: str) -> dict[str, Any]:
    artifacts = paginated(
        f"repos/{REPOSITORY}/actions/artifacts?name=nightly-state&per_page=100", "artifacts"
    )
    for artifact in sorted(artifacts, key=lambda item: item["id"], reverse=True):
        if artifact["expired"]:
            continue
        run = api(f"repos/{REPOSITORY}/actions/runs/{artifact['workflow_run']['id']}")
        if (
            run["path"].split("@")[0] != NIGHTLY_WORKFLOW
            or run["head_branch"] != default_branch
            or run["event"] not in {"schedule", "workflow_dispatch"}
            or run["status"] != "completed"
            or run["head_repository"]["full_name"] != REPOSITORY
        ):
            continue
        result = subprocess.run(
            ["gh", "api", f"repos/{REPOSITORY}/actions/artifacts/{artifact['id']}/zip"],
            capture_output=True,
            check=True,
        )
        with zipfile.ZipFile(io.BytesIO(result.stdout)) as archive:
            return cast(dict[str, Any], json.loads(archive.read("snapshot.json"))["state"])
    return empty_state()


def load_issues(*, state: str = "open") -> list[dict[str, Any]]:
    return [
        issue
        for issue in paginated(
            f"repos/{REPOSITORY}/issues?state={state}&labels=nightly&per_page=100"
        )
        if "pull_request" not in issue
    ]


def source_run(run: dict[str, Any], default_branch: str) -> bool:
    return bool(
        run["path"].split("@")[0] == f".github/workflows/{SOURCE_WORKFLOW}"
        and run["event"] == "schedule"
        and run["head_branch"] == default_branch
        and run["head_repository"]["full_name"] == REPOSITORY
        and run["status"] == "completed"
        and run["conclusion"] == "failure"
    )


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


def public_job(job: dict[str, Any]) -> dict[str, str]:
    return {key: job[key] for key in ("workflow", "job", "url")}


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
        and source_run(candidate, default_branch)
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


def nightly_issue(issue: dict[str, Any]) -> bool:
    return (
        issue["state"] == "open"
        and "pull_request" not in issue
        and any(label["name"] == "nightly" for label in issue["labels"])
    )


def validate_report(report: dict[str, Any], snapshot: dict[str, Any]) -> None:
    Draft202012Validator(yaml.safe_load(SCHEMA_PATH.read_text())).validate(report)
    known_jobs = {job["url"]: public_job(job) for run in snapshot["runs"] for job in run["jobs"]}
    retry_available = {
        job["url"]: run["run_attempt"] == 1 for run in snapshot["runs"] for job in run["jobs"]
    }
    open_issues = {issue["number"] for issue in snapshot["issues"] if nightly_issue(issue)}
    seen = set()
    drafts = set()
    for group in report["groups"]:
        for job in group["jobs"]:
            if known_jobs.get(job["url"]) != public_job(job):
                raise ValueError(f"Unknown job or changed metadata: {job['url']}")
            if group["verdict"] == "transient" and not retry_available[job["url"]]:
                raise ValueError("Retry budget exhausted; investigate the persistent failure")
            seen.add(job["url"])
        if group["verdict"] == "already_reported" and group["issue_number"] not in open_issues:
            raise ValueError("Existing issue must be an open issue labeled nightly")
        if group["verdict"] == "new":
            key = issue_key(group)
            if key in drafts:
                raise ValueError("Duplicate new issue drafts; reconcile groups before publishing")
            drafts.add(key)
    if seen != known_jobs.keys():
        raise ValueError("Report does not cover every input failure")


def fresh_run(run: dict[str, Any]) -> dict[str, Any]:
    current = latest_run()
    if (
        current is None
        or current["id"] != run["id"]
        or not source_run(current, run["head_branch"])
        or current["run_attempt"] != run["run_attempt"]
        or current["head_sha"] != run["head_sha"]
    ):
        raise ValueError(f"Source run changed; recollect run {run['id']}")
    jobs = failed_jobs(current)
    if {job["id"] for job in jobs} != {job["id"] for job in run["jobs"]}:
        raise ValueError(f"Failed job set changed for run {run['id']}")
    return current


def issue_key(group: dict[str, Any]) -> str:
    value = {
        "jobs": sorted(job["url"] for job in group["jobs"]),
        "title": group["title"],
        "body": group["body"],
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def marker(key: str) -> str:
    return f"<!-- nightly-report:{key} -->"


def issue_body(group: dict[str, Any]) -> str:
    context = "\n".join(
        f"- [{job['workflow']} / {job['job']}]({job['url']})" for job in group["jobs"]
    )
    receipt = "\n\n" + marker(issue_key(group))
    body = f"{group['body']}\n\n### CI jobs\n\n{context}"
    if len(body) + len(receipt) > 60000:
        notice = "\n\n[Report truncated.]"
        body = body[: 60000 - len(receipt) - len(notice)] + notice
    return body + receipt


def post_check(
    report: dict[str, Any],
    snapshot: dict[str, Any],
    snapshot_path: Path,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    validate_report(report, snapshot)
    state = snapshot["state"]
    pending = [run for run in snapshot["runs"] if run_key(run) not in state["processed"]]
    plan: dict[str, Any] = {
        "dry_run": not apply,
        "issues": [],
        "reruns": [],
        "blocked_reruns": [],
        "findings": [],
    }
    if not pending:
        return plan
    for run in pending:
        fresh_run(run)

    current_issues = load_issues()
    current_numbers = {issue["number"] for issue in current_issues if nightly_issue(issue)}
    receipts = {marker(issue_key(group)) for group in report["groups"] if group["verdict"] == "new"}
    original_issues = {issue["number"]: issue for issue in snapshot["issues"]}
    if any(
        (
            issue["number"] not in original_issues
            or any(
                issue.get(field) != original_issues[issue["number"]].get(field)
                for field in ("title", "body")
            )
        )
        and not any(receipt in (issue["body"] or "") for receipt in receipts)
        for issue in current_issues
    ):
        raise ValueError("Nightly issues changed during analysis; recollect before publishing")
    # Receipts recover a completed write, including an issue subsequently closed by a maintainer.
    receipt_issues = load_issues(state="all") if receipts else []
    for group in report["groups"]:
        if group["verdict"] == "already_reported" and group["issue_number"] not in current_numbers:
            raise ValueError("Existing issue changed; recollect before publishing")

    if apply and receipts:
        labels = paginated(f"repos/{REPOSITORY}/labels?per_page=100")
        if not any(label["name"] == "nightly" for label in labels):
            api(
                f"repos/{REPOSITORY}/labels",
                method="POST",
                data={
                    "name": "nightly",
                    "description": "Failures detected by scheduled CI jobs.",
                    "color": "5319e7",
                },
            )

    for group in report["groups"]:
        if group["verdict"] != "new":
            plan["findings"].append(group)
            continue
        key = issue_key(group)
        number = state["issues"].get(key)
        if number is None:
            number = next(
                (
                    issue["number"]
                    for issue in receipt_issues
                    if marker(key) in (issue["body"] or "")
                ),
                None,
            )
        action = {"title": group["title"], "body": issue_body(group), "issue_number": number}
        plan["issues"].append(action)
        if apply:
            if number is None:
                group_urls = {job["url"] for job in group["jobs"]}
                for source in snapshot["runs"]:
                    if any(job["url"] in group_urls for job in source["jobs"]):
                        fresh_run(source)
                created = api(
                    f"repos/{REPOSITORY}/issues",
                    method="POST",
                    data={"title": group["title"], "body": action["body"], "labels": ["nightly"]},
                )
                number = created["number"]
                action["issue_number"] = number
            state["issues"][key] = number
            write_json(snapshot_path, snapshot)

    verdicts: dict[str, set[str]] = {}
    for group in report["groups"]:
        for job in group["jobs"]:
            verdicts.setdefault(job["url"], set()).add(group["verdict"])
    for run in pending:
        key = run_key(run)
        transient = any("transient" in verdicts[job["url"]] for job in run["jobs"])
        if transient and not all(verdicts[job["url"]] == {"transient"} for job in run["jobs"]):
            plan["blocked_reruns"].append({"run_id": run["id"], "reason": "mixed_causes"})
            continue
        if not transient or run["run_attempt"] != 1 or key in state["reruns"]:
            continue
        plan["reruns"].append({"run_id": run["id"], "expected_run_attempt": run["run_attempt"]})
        if apply:
            # Publishing issues can take time; check the attempt again just before the request.
            fresh_run(run)
            api(f"repos/{REPOSITORY}/actions/runs/{run['id']}/rerun-failed-jobs", method="POST")
            state["reruns"].append(key)
            write_json(snapshot_path, snapshot)

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
