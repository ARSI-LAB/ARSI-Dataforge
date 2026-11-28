"""Build and verify a collection of tool_gap packages from a Harbor Git snapshot."""

import argparse
from collections import Counter, deque, defaultdict
from itertools import product
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import tempfile

from base.provenance import canonical_json
from data_designer.pipelines.perfworkload.api import build_workload_set, load_workload_set
from data_designer.pipelines.perfworkload.tokenization import load_profile, token_counter
from data_designer.pipelines.perfworkload.tool_gap import MAX_WORKLOAD_ID_LENGTH, _corpus_prefix, _fit

DATASET = "datasets/swebench-verified"
REVISION = "86723674f04e4209ac479d0fb75d9d9f44b4377e"
WIDTHS = (2, 4, 8)
ROUNDS = (1, 2, 4)
RESULT_TARGETS = (1024, 8192, 32768)
# Within each latency class, alternate durations across instances.
DURATIONS = {"fast": (0.8,), "medium": (2, 6, 9), "slow": (12, 20, 45, 90, 120)}


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def instance_paths(repo, revision, count):
    """Select issue records from a Git snapshot in repository round-robin order."""
    paths = git(repo, "ls-tree", "-r", "--name-only", revision, DATASET).decode().splitlines()
    groups = defaultdict(deque)
    for name in sorted(paths):
        if not name.endswith("/tests/config.json"):
            continue
        instance_id = PurePosixPath(name).parts[-3]
        groups[instance_id.rsplit("-", 1)[0]].append(name)
    selected = []
    while len(selected) < count:
        size = len(selected)
        for group in groups.values():
            if group:
                selected.append(group.popleft())
                if len(selected) == count:
                    break
        if len(selected) == size:
            raise ValueError(f"Only {size} benchmark instances available")
    return selected


def build_case(record, shape, duration, calibrated_result, count):
    """Build one tool-gap workload and its index entry from an issue record."""
    width, rounds, target = shape
    issue = record["problem_statement"]
    identity = {key: record[key] for key in ("instance_id", "repo", "base_commit")}
    # Expose benchmark inputs only. Issue sections come from the original report.
    paragraphs = issue.replace("\r\n", "\n").split("\n\n")
    sections = [part.strip() for part in paragraphs if part.strip()]
    sources = [
        ("get_repository_revision", "repository name and base commit", canonical_json(identity)),
        ("get_issue_excerpt", "fixed-size extract from the original issue", calibrated_result),
        ("get_issue_title", "title line from the issue", issue.strip().splitlines()[0]),
        ("get_issue_created_at", "issue creation timestamp", str(record.get("created_at") or "Unavailable")),
        ("get_benchmark_version", "project release version", str(record.get("version") or "Unavailable")),
        ("get_issue_opening", "opening section of the issue", sections[0]),
        ("get_issue_closing", "closing section of the issue", sections[-1]),
        ("get_issue_middle", "middle section of the issue", sections[len(sections) // 2]),
    ]
    tools = [
        {"name": name, "description": f"Retrieve {description}. Independent lookup.",
         "latency_seconds": duration if index == 1 else 0.3, "result": result}
        for index, (name, description, result) in enumerate(sources[:width])
    ]
    names = ", ".join(tool["name"] for tool in tools)
    suffix = f"-{width}-tools-{duration:g}s-{rounds}r-{target}t"
    prefix = _corpus_prefix(identity["instance_id"], MAX_WORKLOAD_ID_LENGTH - len(suffix))
    case = {
        "workload_id": prefix + suffix,
        "task_text": (
            f"{issue}\n\nIndependently query these sources in parallel: {names}. "
            f"Do {rounds} review pass(es), then summarize the defect and outline a fix."
        ),
        "context": f"You are reviewing a SWE-bench Verified issue. Checkout: {canonical_json(identity)}.",
        "rounds": rounds,
        "tools": tools,
    }
    # Supplementary sizing is kept outside the environment package.
    actual = {tool["name"]: count(tool["result"]) for tool in tools}
    if not 0.95 * target <= max(actual.values()) <= target:
        raise ValueError(f"Result token budget outside target for {record['instance_id']}: {actual}")
    return case, actual


def calibrated_excerpt(issue, target, count):
    # Cycle source fragments to produce a controlled response without introducing
    # facts absent from the issue. Short fragments give fine token increments.
    """Repeat issue excerpts to fit a target tool-result token budget."""
    issue = issue.strip()
    parts = [issue[i:i + 12] for i in range(0, len(issue), 12)]
    if not parts:
        raise ValueError("Empty SWE-bench issue")
    def render(size):
        return canonical_json({
            "format": "issue excerpts; repeated to reach the test token budget",
            "issue_fragments": [parts[i % len(parts)] for i in range(size)],
        })
    return _fit(render, count, target)[0]


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_collection(repo, revision, output, number, tokenizer_job, *, long_resume=False):
    """Build, verify, and publish a collection of SWE-bench environment packages."""
    paths = instance_paths(repo, revision, number)
    shapes = list(product(WIDTHS, ROUNDS, RESULT_TARGETS))
    profile = load_profile(json.loads(tokenizer_job.read_bytes())["tokenizer_profiles"], tokenizer_job.parent)
    count = token_counter(profile)
    packages, coverage = [], Counter()
    workload_ids = set()
    # Publish the collection only after all packages pass deep verification.
    with tempfile.TemporaryDirectory(prefix=".swebench-", dir=output.parent) as tmp:
        stage = Path(tmp) / "collection"
        stage.mkdir()
        for i, path in enumerate(paths):
            source_bytes = git(repo, "show", f"{revision}:{path}")
            record = json.loads(source_bytes)
            instance_id = PurePosixPath(path).parts[-3]
            if instance_id != record["instance_id"]:
                raise ValueError(f"Instance ID mismatch: {path}")
            if long_resume:
                # Leave room for the short initial prompt, tool schemas, and chat framing.
                result_budget = 39_000 - count(record["problem_statement"])
                shape = (2, 1, result_budget)
                gaps = (("slow", 12), ("slow", 20), ("slow", 45))
            else:
                shape = shapes[i % len(shapes)]
                gaps = ((latency, durations[i % len(durations)]) for latency, durations in DURATIONS.items())
            width, rounds, target = shape
            result = calibrated_excerpt(record["problem_statement"], target, count)
            cases, details = [], []
            folder = stage / instance_id
            folder.mkdir()
            for latency, duration in gaps:
                case, actual = build_case(record, shape, duration, result, count)
                if case["workload_id"] in workload_ids:
                    raise ValueError(f"Duplicate workload ID: {case['workload_id']}")
                workload_ids.add(case["workload_id"])
                cases.append(case)
                details.append(dict(workload_id=case["workload_id"], gap_class=latency,
                                    gap_seconds=duration, parallel_width=width, rounds=rounds,
                                    target_result_tokens=target, result_tokens=actual))
                coverage[(width, latency, rounds, target)] += 1
            job = {
                "schema_version": "dataforge-workload-job-v1", "generator_version": 3,
                "set_id": f"tool-gap-swebench-{instance_id}", "seed": 20261124,
                "environment": {"delivery_mode": "simulated", "time_scale": 1.0},
                "allow_fast_baseline": not long_resume, "cases": cases,
            }
            write_json(folder / "job.json", job)
            build_workload_set(folder / "job.json", folder / "package")
            manifest, files = load_workload_set(folder / "package", deep=True)
            for detail in details:
                workload = files[f"workloads/tool_gap/{detail['workload_id']}.json"]
                if workload["structure"]["gap_class"] != detail["gap_class"]:
                    raise ValueError(f"Unexpected gap classification: {instance_id}")
            packages.append({
                "instance_id": instance_id, "repo": record["repo"], "base_commit": record["base_commit"],
                "source_path": path, "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "package": f"{instance_id}/package", "workload_digest": manifest["workload_digest"],
                "env_digest": manifest["env_digest"], "cases": details,
            })
            print(f"{i + 1}/{number} {instance_id}", flush=True)
        if not long_resume and number >= len(shapes) and len(coverage) != len(shapes) * len(DURATIONS):
            raise ValueError("Incomplete collection coverage")
        write_json(stage / "index.json", {
            "dataset": "SWE-bench Verified (Harbor)", "snapshot": revision,
            "tokenizer_profile": profile["profile"], "package_count": len(packages),
            "workload_count": sum(len(x["cases"]) for x in packages),
            "coverage": [dict(parallel_width=w, gap_class=g, rounds=r, target_result_tokens=t, count=n)
                         for (w, g, r, t), n in sorted(coverage.items())],
            "packages": packages,
        })
        if output.exists() or output.is_symlink():
            raise FileExistsError(output)
        stage.rename(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path, help="Local harbor-datasets Git checkout")
    parser.add_argument("--revision", default=REVISION)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--count", default=64, type=int)
    parser.add_argument("--tokenizer-job", type=Path, default=Path(__file__).with_name("tokenizer-profile.json"))
    parser.add_argument("--long-resume", action="store_true",
                        help="Build 12-, 20-, and 45-second cases with an approximately 40K continuation input")
    args = parser.parse_args()
    if args.count < 1 or args.output.exists() or args.output.is_symlink():
        parser.error("count must be positive and output must be a new directory")
    revision = git(args.repo, "rev-parse", "--verify", args.revision + "^{commit}").decode().strip()
    build_collection(args.repo, revision, args.output, args.count, args.tokenizer_job, long_resume=args.long_resume)


if __name__ == "__main__":
    main()
