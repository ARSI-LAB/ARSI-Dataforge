"""Select a tool_gap package and measure real harness/tool executions."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from itertools import count
from os.path import relpath
from pathlib import Path
import re
from statistics import median
import subprocess
import sys
from threading import BoundedSemaphore, Lock, Thread
import time
import uuid
from urllib.parse import quote, urlsplit

from data_designer.pipelines.perfworkload.api import load_workload_set
from markdown_report import report_paths, write_markdown
from measure import ENGINE_METRICS, open_bridge

HERE = Path(__file__).resolve().parent


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def read_events(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("{")]


def dsh_command(args, spec, folder, env):
    """Write the DSH plugin configuration and build the headless command."""
    def scalar(value):
        return json.dumps(str(value), ensure_ascii=False)

    config = f"""- insert:
    - id: agent-hints
      name: {scalar(args.hint_plugin)}
      config:
        baseURL: {scalar(spec["bridge_url"])}
        apiKeyEnv: TOOL_GAP_LOOPBACK_KEY
        models:
          - id: {scalar(args.model)}
            name: {scalar(args.model)}
            contextWindow: {args.context_window}
            maxTokens: {args.max_tokens}
        agentHint:
          enabled: {str(spec["hint"]).lower()}
          cacheControl: {{ttl_s: {args.ttl}}}
          sessionControl: {{enabled: false}}
    - id: tool-gap-package-tools
      name: {scalar(HERE / "dsh-tools.mjs")}
- id: agent-default-model
  config:
    provider: deepseek-hint
    model: {scalar(args.model)}
"""
    for plugin in ("tool-bash", "tool-pwsh", "tool-fs", "tool-fs-search", "tool-working-directory",
                   "tool-jobs", "tool-skill", "tool-subagent", "tool-subagent-control", "tool-subagent-fork",
                   "tool-workflow", "tool-todo", "tool-goal", "tool-web", "tool-plugin-manager", "session-title-llm"):
        config += f"- id: {plugin}\n  disabled: true\n"
    patch = folder / "patch.yml"
    patch.write_text(config, encoding="utf-8")
    env.update(DSH_HOME=str(folder / "home"), TOOL_GAP_LOOPBACK_KEY="loopback", DSH_PERMISSION_MODE="read-only")
    return [args.node, str(args.dsh_bin), "--profile", "headless", "--patch", str(patch), "--json", spec["task"]]


def langchain_command(args, folder, env):
    if args.harness_dir:
        source = args.harness_dir / "src"
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(source), env.get("PYTHONPATH")]))
    return [sys.executable, str(HERE / "langchain_host.py"), str(folder / "run-spec.json")]


def run_once(args, manifest, workload, tools, folder, hint):
    """Run one harness session and collect model requests, tool spans, and continuations."""
    folder.mkdir()
    workspace = folder / "workspace"
    workspace.mkdir()
    log = folder / "requests.jsonl"
    server = open_bridge(args.endpoint)
    server.log_path = log
    server.lock = Lock()
    server.metrics_lock = Lock()
    server.request_limiter = getattr(args, "request_limiter", None)
    server.metrics_endpoint = args.engine_metrics_url
    # Initial and continuation requests share a salt within this run.
    server.salt = uuid.uuid4().hex
    server.isolate_cache = args.isolate_cache
    server.transport = args.hint_transport
    server.expect_hint = hint
    server.ttl = args.ttl
    server.context_window = args.context_window
    server.upstream_timeout = args.request_timeout
    spec = {
        "python": sys.executable, "worker": str(HERE / "tool_worker.py"), "package": str(args.package),
        "case_id": workload["workload_id"], "background": workload["task"]["initial_messages"][0]["content"],
        "task": workload["task"]["task_text"], "tool_log": str(folder / "tools.jsonl"), "tools": tools,
        "time_scale": manifest["time_scale"], "bridge_url": f"http://127.0.0.1:{server.server_port}/v1",
        "model": args.model, "hint": hint, "ttl": args.ttl, "session": server.salt, "max_tokens": args.max_tokens,
    }
    write_json(folder / "run-spec.json", spec)
    env = {key: value for key, value in os.environ.items() if key != "OPENAI_API_KEY"}
    env["TOOL_GAP_SPEC"] = str(folder / "run-spec.json")
    command = (dsh_command(args, spec, folder, env) if args.harness == "dsh"
               else langchain_command(args, folder, env))
    listener = Thread(target=server.serve_forever)
    listener.start()
    started = time.monotonic()
    failure = None
    code = None
    try:
        with (folder / "harness.log").open("w", encoding="utf-8") as stdout, (folder / "stderr.log").open("w", encoding="utf-8") as stderr:
            code = subprocess.run(command, cwd=workspace, env=env, stdout=stdout, stderr=stderr,
                                  timeout=args.run_timeout).returncode
    except (OSError, subprocess.TimeoutExpired) as error:
        failure = type(error).__name__
    finally:
        elapsed = time.monotonic() - started
        server.shutdown()
        server.server_close()
        listener.join()
    requests = sorted(read_events(log), key=lambda row: row["started_monotonic_seconds"])
    events = sorted(read_events(folder / "tools.jsonl"), key=lambda row: row["started"])
    rounds = collect_rounds(requests, events)
    report = {
        "harness": args.harness, "workload_id": workload["workload_id"],
        "workload_digest": manifest["workload_digest"], "env_digest": manifest["env_digest"],
        "model": args.model, "hint_enabled": hint, "hint_ttl_seconds": args.ttl if hint else None,
        "hint_transport": args.hint_transport, "cache_salt_enabled": args.isolate_cache,
        "engine_metrics_source": "vllm_prometheus_delta" if args.engine_metrics_url else None,
        "elapsed_seconds": elapsed, "harness_exit_code": code, "harness_error": failure,
        "time_scale": manifest["time_scale"], "max_tokens": args.max_tokens,
        "declared_tools": [{"name": tool["name"], "latency_seconds": tool["latency"]["seconds"]} for tool in tools],
        "expected_tool_rounds": workload["structure"]["rounds"], "requests": requests,
        "tools": events, "rounds": rounds,
    }
    write_json(folder / "report.json", report)
    return report


def collect_rounds(requests, events):
    """Join successful tool worker spans to the model request that consumes them."""
    rounds = []
    for index, request in enumerate(requests):
        if not request["tool_results"] or index == 0:
            continue
        previous = requests[index - 1]
        # Only attribute tool spans between the preceding stream and this continuation.
        group = [
            event for event in events
            if previous["finished_monotonic_seconds"]
            <= event["started"] <= event["finished"] <= request["started_monotonic_seconds"]
        ]
        if not group:
            continue
        previous_results = previous["tool_results"]
        new_results = request["tool_results"] - previous_results
        first = min(event["started"] for event in group)
        last = max(event["finished"] for event in group)
        overlap = min(event["finished"] for event in group) - max(event["started"] for event in group)
        rounds.append({
            "request_index": index, "tool_names": [event["name"] for event in group],
            "tool_gap_seconds": last - first, "all_tools_overlap_seconds": max(0, overlap) if len(group) > 1 else 0,
            "tool_results_in_request": request["tool_results"],
            "matched_tool_results": new_results == len(group), "ttft_seconds": request["ttft_seconds"],
            "tpot_seconds": request["tpot_seconds"], "cached_tokens": request["cached_tokens"],
            "valid_stream": request["stream_done"] and request["http_status"] == 200,
            **{field: request.get(field) for field in ENGINE_METRICS},
        })
    return rounds


def summarize(reports, tools):
    """Compute per-round medians from valid parallel samples for each Hint mode."""
    expected_tools = sorted(tool["name"] for tool in tools)
    groups = []
    for enabled in (False, True):
        runs = [r for r in reports if r["hint_enabled"] == enabled]
        if not runs:
            continue
        for index in range(max(len(run["rounds"]) for run in runs)):
            round_samples = (
                run["rounds"][index] for run in runs if index < len(run["rounds"])
            )
            # A usable continuation can still miss the intended parallel tool shape.
            eligible = [
                row for row in round_samples
                if row["valid_stream"]
                and row["matched_tool_results"]
                and sorted(row["tool_names"]) == expected_tools
                and row["all_tools_overlap_seconds"] > 0
            ]
            stats = {}
            for field in ("ttft_seconds", "tpot_seconds", "cached_tokens", "tool_gap_seconds", *ENGINE_METRICS):
                values = [row[field] for row in eligible if row[field] is not None]
                stats[field] = {"count": len(values), "median": median(values) if values else None}
            groups.append({"hint_enabled": enabled, "round": index + 1, "runs": len(runs),
                           "parallel_samples": len(eligible), "metrics": stats})
    return groups


def create_experiment_output(package, package_name, harness, output, report_dir):
    """Reserve a fresh run directory and check for report path collisions."""
    if output is None:
        safe_name = re.sub(r"[^\w-]+", "-", package_name).strip("-_")[:100] or "package"
        candidates = (Path("artifacts") / f"{safe_name}_{harness}_{number:03d}" for number in count(1))
    else:
        candidates = (output,)

    for candidate in candidates:
        candidate = candidate.resolve()
        markdown_paths = [path.resolve() for path in report_paths(candidate, report_dir)]
        if candidate.is_relative_to(package) or any(path.is_relative_to(package) for path in markdown_paths):
            raise ValueError("Experiment output and Markdown report must be outside the environment package")
        existing_report = next((path for path in markdown_paths if path.exists()), None)
        if existing_report:
            if output is None:
                continue
            raise ValueError(f"Markdown report already exists: {existing_report}")
        try:
            candidate.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            if output is not None:
                raise
        else:
            return candidate


def hint_sequence(args):
    """Yield the configured Hint modes in experiment order."""
    sequence = []
    if args.hint == "both":
        # Alternate which mode runs first to reduce fixed-order effects.
        for pair in range(args.repeat):
            sequence.extend((False, True) if pair % 2 == 0 else (True, False))
    else:
        sequence = [args.hint == "on"] * args.repeat
    return sequence


def run_case(args, manifest, workload, tools, output, report_dir):
    """Execute the requested Hint modes and write reports for one workload."""
    reports = []
    for index, hint in enumerate(hint_sequence(args), 1):
        folder = output / f"{index:02d}-hint-{'on' if hint else 'off'}"
        report = run_once(args, manifest, workload, tools, folder, hint)
        reports.append(report)
        print(f"{folder}: {len(report['requests'])} model requests, {len(report['tools'])} tools, exit {report['harness_exit_code']}", flush=True)
    return finish_case(reports, tools, output, report_dir)


def finish_case(reports, tools, output, report_dir):
    """Publish metrics and both Markdown reports for a finished workload."""
    summary = summarize(reports, tools)
    write_json(output / "summary.json", summary)
    destinations = write_markdown(output, summary, report_dir)
    for destination in destinations:
        print(f"Markdown report: {destination}")
    valid = bool(summary) and all(
        any(row["valid_stream"] and row["matched_tool_results"] and row["ttft_seconds"] is not None
            for row in report["rounds"])
        for report in reports
    )
    return valid, destinations


def select_dataset_packages(root, case_id):
    """Resolve dataset index entries and optionally select one workload ID."""
    index = json.loads((root / "index.json").read_bytes())
    selected = []
    seen_ids = set()
    for entry in index["packages"]:
        package = (root / entry["package"]).resolve()
        if not package.is_relative_to(root) or package == root:
            raise ValueError(f"Package must be inside the dataset: {package}")
        ids = [case["workload_id"] for case in entry["cases"]]
        for identifier in ids:
            if not re.fullmatch(r"[\w][\w.-]*", identifier) or identifier in seen_ids:
                raise ValueError(f"Invalid or repeated workload ID: {identifier}")
            seen_ids.add(identifier)
        matching_ids = ids if case_id is None else [identifier for identifier in ids if identifier == case_id]
        if matching_ids:
            selected.append((package, entry, matching_ids))
    if not selected:
        raise ValueError(f"No matching tool_gap cases in {root}; see index.json for workload IDs")
    return selected


def write_collection_reports(root, output, report_dir, results):
    """Link per-case reports from the bilingual dataset summaries."""
    write_json(output / "summary.json", results)
    passed = sum(result["valid"] for result in results)
    for destination, english in zip(report_paths(output, report_dir), (False, True), strict=True):
        destination = destination.resolve()
        title = "Dataset run" if english else "数据集运行报告"
        header = "| Case | Result |\n| --- | --- |" if english else "| 用例 | 结果 |\n| --- | --- |"
        totals = (f"{passed}/{len(results)} cases met the continuation checks."
                  if english else f"{passed}/{len(results)} 条用例通过续跑检查。")
        lines = [f"# {title} · {root.name}", "", totals, "", header]
        for result in results:
            path = result["reports"][destination.name]
            link = quote(Path(relpath(path, destination.parent)).as_posix())
            valid = result["valid"]
            status = ("Passed" if valid else "Check run details") if english else ("通过" if valid else "查看运行详情")
            lines.append(f"| [{result['workload_id']}]({link}) | {status} |")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Markdown report: {destination}")


def load_dataset_package(package, entry):
    """Verify a package and confirm it matches the dataset index when supplied."""
    manifest, files = load_workload_set(package)
    if entry is not None and (
        manifest["env_digest"] != entry["env_digest"] or manifest["workload_digest"] != entry["workload_digest"]
    ):
        raise ValueError(f"Package digests differ from index.json: {package}")
    return manifest, files


def run_collection(args, packages):
    """Run selected workloads serially and return the dataset exit status."""
    root = args.package
    output = create_experiment_output(root, root.name, args.harness, args.output, args.report_dir)
    # Mirror the experiment directory under a separate Markdown output root.
    case_report_root = args.report_dir / output.name if args.report_dir else None
    results = []
    for package, entry, ids in packages:
        manifest, files = load_dataset_package(package, entry)
        package_args = argparse.Namespace(**(vars(args) | {"package": package}))
        for identifier in ids:
            workload = files[f"workloads/tool_gap/{identifier}.json"]
            tools = [files[f"tools/{ref['tool_ref']}.json"] for ref in workload["tools"]]
            case_output = output / identifier
            case_output.mkdir()
            valid, reports = run_case(package_args, manifest, workload, tools, case_output, case_report_root)
            results.append({"workload_id": identifier, "valid": valid,
                            "reports": {path.name: str(path) for path in reports}})
    write_collection_reports(root, output, args.report_dir, results)
    return 0 if all(result["valid"] for result in results) else 1


def run_concurrent_collection(args, packages, selection):
    """Run one Hint mode at a time with a shared limit on inference streams."""
    root = args.package
    if selection:
        package_by_id = {identifier: (package, entry) for package, entry, ids in packages for identifier in ids}
        selected = [(identifier, *package_by_id[identifier])
                    for identifier in (case["workload_id"] for case in selection["cases"])]
    else:
        selected = [(identifier, package, entry) for package, entry, ids in packages for identifier in ids]
    output = create_experiment_output(root, root.name, args.harness, args.output, args.report_dir)
    report_dir = args.report_dir / output.name if args.report_dir else None
    if selection:
        write_json(output / "concurrent-set.json", selection)
    limiter = BoundedSemaphore(args.concurrency)
    cases = {}
    for identifier, package, entry in selected:
        manifest, files = load_dataset_package(package, entry)
        workload = files[f"workloads/tool_gap/{identifier}.json"]
        tools = [files[f"tools/{ref['tool_ref']}.json"] for ref in workload["tools"]]
        case_args = argparse.Namespace(**(vars(args) | {"package": package, "request_limiter": limiter}))
        cases[identifier] = (case_args, manifest, workload, tools)
        (output / identifier).mkdir()

    # Start extra sessions so that tool waits leave runnable work for the engine.
    workers = min(len(cases), args.concurrency * 4)
    records = {identifier: [] for identifier in cases}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for index, hint in enumerate(hint_sequence(args), 1):
            run_name = f"{index:02d}-hint-{'on' if hint else 'off'}"
            print(f"{run_name}: {len(cases)} workloads, up to {args.concurrency} inference streams", flush=True)
            pending = {identifier: executor.submit(run_once, *case, output / identifier / run_name, hint)
                       for identifier, case in cases.items()}
            for identifier, future in pending.items():
                records[identifier].append(future.result())

    results = []
    for identifier, reports in records.items():
        valid, destinations = finish_case(reports, cases[identifier][3], output / identifier, report_dir)
        results.append({"workload_id": identifier, "valid": valid,
                        "reports": {path.name: str(path) for path in destinations}})
    write_collection_reports(root, output, args.report_dir, results)
    return 0 if all(result["valid"] for result in results) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path,
                        help="Dataset root with index.json, or an individual package with env.json")
    parser.add_argument("--case", help="Run only this workload ID (dataset default: all cases)")
    parser.add_argument("--harness", choices=("dsh", "langchain"))
    parser.add_argument("--harness-dir", type=Path, help="Local Hint plugin checkout")
    parser.add_argument("--dsh-bin", type=Path, help="DSH JavaScript CLI entry")
    parser.add_argument("--hint-plugin", type=Path, help="Compiled DSH plugin entry; defaults to HARNESS_DIR/lib/types/index.js")
    parser.add_argument("--node", default="node")
    parser.add_argument("--endpoint", help="Inference server Chat Completions URL")
    parser.add_argument("--engine-metrics-url", help="vLLM Prometheus /metrics URL for an isolated engine")
    parser.add_argument("--model")
    parser.add_argument("--estimate-only", action="store_true", help="Print planned input lengths as JSON without running inference")
    parser.add_argument("--tokenizer-profile", type=Path, help="Local tokenizer job profile for --estimate-only")
    parser.add_argument("--output", type=Path, help="New experiment directory (default: ./artifacts/PACKAGE_HARNESS_NUMBER)")
    parser.add_argument("--report-dir", type=Path, help="Markdown report root directory (default: save in --output)")
    parser.add_argument("--hint", choices=("off", "on", "both"), default="both",
                        help="Hint mode (default: both, off then on)")
    parser.add_argument("--hint-transport", choices=("header", "ascend-body"), default="header")
    parser.add_argument("--isolate-cache", action="store_true", help="Use cache_salt (requires engine support)")
    parser.add_argument("--repeat", type=int, default=1, help="Runs per selected mode (default: 1)")
    parser.add_argument("--ttl", type=int, default=35)
    parser.add_argument("--context-window", type=int, default=16384)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--request-timeout", type=int, default=120)
    parser.add_argument("--run-timeout", type=int, default=900)
    parser.add_argument("--concurrency", type=int, help="Maximum simultaneous model requests in dataset mode")
    parser.add_argument("--concurrent-set", type=Path, help="Reuse a saved concurrent-set.json")
    parser.add_argument("--sample-size", type=int, help="Randomly select this many distinct SWE-bench tasks")
    parser.add_argument("--sample-seed", type=int, default=42)
    parser.add_argument("--gap-seconds", type=float, nargs="+", help="Candidate declared tool gaps")
    parser.add_argument("--target-resume-tokens", type=int, help="Target estimated first continuation prompt size")
    parser.add_argument("--resume-tolerance", type=float, default=0.1, help="Allowed relative deviation (default: 0.1)")
    args = parser.parse_args()
    args.package = args.package.resolve()
    if args.estimate_only:
        from estimate import estimate_workloads

        try:
            if (args.package / "env.json").is_file():
                _, files = load_workload_set(args.package)
                ids = sorted(
                    item["workload_id"] for name, item in files.items()
                    if name.startswith("workloads/tool_gap/") and (args.case is None or item["workload_id"] == args.case)
                )
                if not ids:
                    raise ValueError("No matching tool_gap cases in this package")
                packages = [(args.package, None, ids)]
            else:
                packages = select_dataset_packages(args.package, args.case)
            print(json.dumps(list(estimate_workloads(packages, load_dataset_package, args.tokenizer_profile)), indent=2))
        except (KeyError, ValueError, OSError) as error:
            parser.error(str(error))
        return 0
    if args.concurrency is not None and args.concurrency < 1:
        parser.error("--concurrency must be positive")
    if args.sample_size is not None and args.sample_size < 1:
        parser.error("--sample-size must be positive")
    if args.target_resume_tokens is not None and (
        args.target_resume_tokens < 1 or not 0 <= args.resume_tolerance < 1
    ):
        parser.error("Expected a positive token target and a tolerance in [0, 1)")
    if args.concurrent_set and (args.sample_size or args.gap_seconds or args.target_resume_tokens):
        parser.error("--concurrent-set cannot be combined with sampling filters")
    if (args.gap_seconds or args.target_resume_tokens) and not args.sample_size:
        parser.error("Sampling filters require --sample-size")
    if args.tokenizer_profile and not args.target_resume_tokens:
        parser.error("--tokenizer-profile requires --estimate-only or --target-resume-tokens")
    batch = args.concurrency is not None or args.concurrent_set is not None or args.sample_size is not None
    if batch and (args.case or (args.package / "env.json").is_file()):
        parser.error("Concurrent batches require a dataset root and cannot be combined with --case")
    if batch and args.engine_metrics_url:
        parser.error("Per-request Prometheus deltas are unavailable for concurrent batches")
    if batch:
        args.concurrency = args.concurrency or 1
    if not (args.harness and args.endpoint and args.model):
        parser.error("--harness, --endpoint and --model are required for inference runs")
    if min(args.repeat, args.ttl, args.context_window, args.max_tokens, args.request_timeout, args.run_timeout) <= 0:
        parser.error("repeat, TTL, token limits and timeouts must be positive")
    if args.engine_metrics_url:
        url = urlsplit(args.engine_metrics_url)
        if (url.scheme not in ("http", "https") or not url.hostname or not url.path
                or url.username or url.password or url.query or url.fragment):
            parser.error("--engine-metrics-url must be an HTTP(S) URL without credentials or query")
        args.engine_metrics_url = url
    if args.harness_dir:
        args.harness_dir = args.harness_dir.resolve()
    if args.harness == "dsh":
        args.hint_plugin = args.hint_plugin or (args.harness_dir / "lib/types/index.js" if args.harness_dir else None)
        for key in ("dsh_bin", "hint_plugin"):
            value = getattr(args, key)
            if not value or not value.is_file():
                parser.error(f"DSH requires an existing --{key.replace('_', '-')}")
            setattr(args, key, value.resolve())

    if not (args.package / "env.json").is_file():
        try:
            packages = select_dataset_packages(args.package, args.case)
            if batch:
                from sampling import select_concurrent_set

                selection = None
                if args.concurrent_set or args.sample_size:
                    estimates = {}
                    if args.target_resume_tokens:
                        from estimate import estimate_workloads

                        estimates = {row["workload_id"]: row for row in
                                     estimate_workloads(packages, load_dataset_package, args.tokenizer_profile)}
                    selection = select_concurrent_set(packages, args, estimates)
                return run_concurrent_collection(args, packages, selection)
            return run_collection(args, packages)
        except (KeyError, ValueError, OSError) as error:
            parser.error(str(error))

    manifest, files = load_workload_set(args.package)
    workloads = {item["workload_id"]: item for name, item in files.items() if name.startswith("workloads/tool_gap/")}
    if args.case is None and len(workloads) == 1:
        args.case = next(iter(workloads))
    if args.case not in workloads:
        parser.error("select a case: " + ", ".join(sorted(workloads)))
    workload = workloads[args.case]
    tools = [files[f"tools/{ref['tool_ref']}.json"] for ref in workload["tools"]]
    try:
        args.output = create_experiment_output(
            args.package, manifest["set_id"], args.harness, args.output, args.report_dir
        )
    except (ValueError, OSError) as error:
        parser.error(str(error))
    return 0 if run_case(args, manifest, workload, tools, args.output, args.report_dir)[0] else 1


if __name__ == "__main__":
    raise SystemExit(main())
