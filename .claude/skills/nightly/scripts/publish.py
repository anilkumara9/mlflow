import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

REPOSITORY = "mlflow/mlflow"


def post(endpoint: str, data: Any = None) -> dict[str, Any]:
    result = subprocess.run(
        ["gh", "api", f"repos/{REPOSITORY}/{endpoint}", "--method", "POST", "--input", "-"],
        input=json.dumps(data or {}),
        capture_output=True,
        text=True,
        check=True,
    )
    return cast(dict[str, Any], json.loads(result.stdout)) if result.stdout.strip() else {}


def issue_body(group: dict[str, Any]) -> str:
    evidence = "\n".join(f"- {item}" for item in group["evidence"])
    body = f"{group['summary']}\n\n### Evidence\n\n{evidence}"
    if group["uncertainties"]:
        uncertainties = "\n".join(f"- {item}" for item in group["uncertainties"])
        body += f"\n\n### Uncertainties\n\n{uncertainties}"
    jobs = "\n".join(f"- [{job['workflow']} / {job['job']}]({job['url']})" for job in group["jobs"])
    return f"{body}\n\n### CI jobs\n\n{jobs}"


def publish(report: dict[str, Any], *, apply: bool = False) -> dict[str, Any]:
    plan: dict[str, Any] = {"dry_run": not apply, "issues": [], "findings": []}
    for group in report["groups"]:
        if group["verdict"] != "new":
            plan["findings"].append(group)
            continue
        body = issue_body(group)
        action = {"title": group["title"], "body": body, "issue_number": None}
        plan["issues"].append(action)
        if apply:
            action["issue_number"] = post(
                "issues", {"title": group["title"], "body": body, "labels": ["nightly"]}
            )["number"]
    return plan


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Publish nightly CI findings")
    parser.add_argument("report", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    plan = publish(json.loads(args.report.read_text()), apply=args.apply)
    sys.stdout.write(json.dumps(plan, indent=2) + "\n")
