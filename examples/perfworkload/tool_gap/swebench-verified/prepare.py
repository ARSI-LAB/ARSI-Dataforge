"""Turn a local Harbor SWE-bench Verified instance into a tool_gap job."""

import argparse
import json
from pathlib import Path


def build_job(instance_dir):
    # Project only task inputs. The Harbor config also contains reference answers.
    """Convert one Harbor issue into an explicit tool-gap job."""
    record = json.loads((instance_dir / "tests/config.json").read_text(encoding="utf-8"))
    issue = record["problem_statement"]
    checkout = {key: record[key] for key in ("instance_id", "repo", "base_commit")}
    instance_id = checkout["instance_id"]
    cases = []
    for seconds in (6, 20):
        cases.append({
            "workload_id": f"swebench-{instance_id}-wait-{seconds}s",
            "task_text": issue + (
                "\n\nBefore proposing a fix, run get_repository_revision and "
                "get_issue_description in parallel for two review passes. "
                "Then summarize the defect and outline a fix."
            ),
            "context": (
                "You are investigating a SWE-bench Verified issue. "
                f"Repository: {checkout['repo']}. Base commit: {checkout['base_commit']}. "
                f"Instance: {instance_id}."
            ),
            "rounds": 2,
            "tools": [
                {
                    "name": "get_repository_revision",
                    "description": "Return the repository and base commit for this issue.",
                    "latency_seconds": 0.3,
                    "result": json.dumps(checkout, ensure_ascii=False, sort_keys=True),
                },
                {
                    "name": "get_issue_description",
                    "description": "Return the original issue report.",
                    "latency_seconds": seconds,
                    "result": issue,
                },
            ],
        })
    return {
        "schema_version": "dataforge-workload-job-v1",
        "generator_version": 3,
        "set_id": f"tool-gap-swebench-{instance_id}",
        "seed": 20261124,
        "environment": {"delivery_mode": "simulated", "time_scale": 1.0},
        "cases": cases,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    job = build_job(args.instance_dir)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(job, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
