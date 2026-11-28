"""Build and verify simulated workloads of agent-perf-env-v1."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path

from base.provenance import canonical_json, content_digest, file_digest

from . import tool_gap
from .tokenization import load_profile, validate_profile


ENV_SCHEMA = "agent-perf-env-v1"
JOB_SCHEMA = "dataforge-workload-job-v1"
GENERATOR_VERSION = 3  # Derives grid case IDs from the corpus manifest.
CAPABILITIES = {"tool_gap": ["parallel_tool_execution", "tool_latency_honoring"]}
FORBIDDEN = set("""
agent_hint session_id parent_session_id lifecycle reuse retention context_management
priority scheduling placement routing cache_policy eviction_policy ttl tier fallback
agent_hint_usage hint prefill offload evict metrics ttft throughput latency_slo reference_script replay
action_sequence ground_truth_action_sequence manage_request osl
""".split())


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _check_fields(value):
    if isinstance(value, dict):
        forbidden = FORBIDDEN.intersection(value)
        _require(not forbidden, f"forbidden_field: {sorted(forbidden)}")
        for child in value.values():
            _check_fields(child)
    elif isinstance(value, list):
        for child in value:
            _check_fields(child)


def _read(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(value, dict), f"expected JSON object: {path.name}")
    _check_fields(value)
    return value


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    formatted = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
    path.write_bytes((formatted + "\n").encode("utf-8"))


def _only_keys(value, allowed, where):
    unexpected = value.keys() - set(allowed.split())
    _require(not unexpected, f"unsupported fields in {where}: {sorted(unexpected)}")


def _seconds(value, name, maximum):
    _require(type(value) in (int, float) and 0 < value <= maximum,
             f"{name} must be greater than 0 and at most {maximum}")
    return float(value)


def _text(value, name):
    _require(isinstance(value, str) and value.strip(), f"{name} must be a nonempty string")
    return value


def _latency_class(seconds):
    if seconds < 1:
        return "fast"
    return "medium" if seconds < 10 else "slow"


def _compile(job, plan=None):
    """Compile explicit cases into workload, tool, resource, and coverage files."""
    scenario_key = "tool_gap"
    _only_keys(job, "schema_version generator_version set_id seed cases environment allow_fast_baseline", "job")
    _require(job.get("schema_version") == JOB_SCHEMA, "unsupported job schema")
    _require(type(job.get("allow_fast_baseline", False)) is bool, "allow_fast_baseline must be boolean")
    _text(job.get("set_id"), "set_id")
    seed = job.get("seed")
    _require(type(seed) is int, "seed must be an integer")
    cases = job.get("cases")
    _require(isinstance(cases, list) and cases, "cases must be a nonempty list")
    files, coverage, ids = {}, {}, set()
    latency_classes = []
    for case in cases:
        _require(isinstance(case, dict), "expected case object")
        case_keys = "workload_id task_text context context_repetitions rounds tools"
        if plan is not None:
            case_keys += " tokenizer_profile context_tokens result_token_target source"
        _only_keys(case, case_keys, "case")
        case_id = case.get("workload_id")
        _require(isinstance(case_id, str) and len(case_id) <= tool_gap.MAX_WORKLOAD_ID_LENGTH
                 and re.fullmatch(r"[a-z0-9][a-z0-9._-]*", case_id),
                 "invalid workload_id")
        _require(case_id not in ids, f"duplicate workload_id: {case_id}")
        ids.add(case_id)
        rounds = case.get("rounds", 1)
        _require(type(rounds) is int and rounds >= 1, "rounds must be a positive integer")
        tools = case.get("tools")
        _require(isinstance(tools, list) and len(tools) >= 2, "tool_gap requires at least two tools")
        names, durations, refs = [], [], []
        fixture_sizes = {}
        for tool in tools:
            _require(isinstance(tool, dict), "expected tool object")
            tool_keys = "name description latency_seconds result parameters sample_arguments"
            if plan is not None:
                tool_keys += " result_tokens"
            _only_keys(tool, tool_keys, "tool")
            name = tool.get("name")
            _require(isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", name),
                     "invalid tool name")
            _require(name not in names, f"duplicate tool name: {name}")
            delay = _seconds(tool.get("latency_seconds"), "latency_seconds", 120)
            fixture = {"content": _text(tool.get("result"), "tool result")}
            digest = content_digest(fixture)
            files[f"resources/{digest.removeprefix('sha256:')}.json"] = fixture
            tool_id = f"{case_id}.{name}"
            tool_path = f"tools/{tool_id}.json"
            _require(tool_path not in files, f"duplicate tool_id: {tool_id}")
            result_fixture = {"content_ref": digest}
            parameters = tool.get("parameters", {"type": "object", "properties": {}, "additionalProperties": False})
            if plan is not None:
                fixture_sizes[tool_id] = result_fixture["token_count"] = tool["result_tokens"]
            files[tool_path] = {
                "schema_version": "agent-workload-tool-v1", "tool_id": tool_id, "name": name,
                "description": _text(tool.get("description"), "tool description"),
                "parameters": parameters,
                "latency": {"mode": "fixed", "seconds": delay, "class": _latency_class(delay),
                            "p50_seconds": delay, "p99_seconds": delay},
                "timeout_seconds": delay + 5, "independence": {"depends_on": []},
                "result": {"kind": "fixture_set", "fixtures": [result_fixture]},
            }
            files[tool_path]["sample_arguments"] = tool.get("sample_arguments", {})
            tool_arguments(files[tool_path])
            names.append(name)
            durations.append(delay)
            refs.append({"tool_ref": tool_id})
        width = len(names)
        gap = _latency_class(max(durations))
        _require(gap != "fast" or job.get("allow_fast_baseline") is True,
                 "fast gaps require allow_fast_baseline")
        task = _text(case.get("task_text"), "task_text")
        context = _text(case.get("context"), "context")
        repetitions = case.get("context_repetitions", 1)
        _require(type(repetitions) is int and 1 <= repetitions <= 2048,
                 "context_repetitions must be an integer between 1 and 2048")
        if repetitions > 1:
            context = "\n\n".join(f"Archive entry {i + 1}\n{context}" for i in range(repetitions))
        workload = {
            "schema_version": "agent-workload-v1", "workload_id": case_id, "scenario": scenario_key,
            "task": {"task_id": case_id, "task_text": task, "tools_available": names,
                     "initial_messages": [{"role": "system", "content": context},
                                          {"role": "user", "content": task}]},
            "tools": refs,
            "structure": {"kind": "tool_gap", "gap_class": gap, "rounds": rounds,
                          "parallel_intent": {"min_parallel_calls": width,
                                              "max_parallel_calls": width, "independent": True}},
            "load_hypothesis": {"parallel_calls": width, "gap_class": gap, "turns": rounds},
            "sampling": {"seed": seed},
        }
        if plan is None:
            cell = f"width={width},gap={gap},rounds={rounds}"
        else:
            target = case["result_token_target"]
            _require(target * 0.95 <= max(fixture_sizes.values()) <= target, "token_budget_mismatch: tool results")
            cell = tool_gap.cell_key(width, gap, rounds, target)
            workload["tokenizer_profile"] = case["tokenizer_profile"]
            workload["structure"]["result_tokens"] = fixture_sizes
            workload["structure"]["induction"] = {
                "task_phrasing": "simultaneous_independent_lookups",
                "tool_description_phrasing": "independent_of_other_tools",
                "no_shared_parameters": True,
            }
            workload["load_hypothesis"]["context_tokens"] = case["context_tokens"]
            workload["metadata"] = {"cell": cell, "source": case["source"], "result_token_target": target}
        workload["content_digest"] = content_digest(workload)
        files[f"workloads/{scenario_key}/{case_id}.json"] = workload
        coverage[cell] = coverage.get(cell, 0) + 1
        latency_classes.append(gap)
    files["coverage.json"] = (
        {scenario_key: coverage} if plan is None else tool_gap.coverage_report(
            plan, job["set_id"], coverage, latency_classes)
    )
    return files, len(ids)


def _compile_grid(set_id, seed, generation, tokenizer):
    """Expand a seeded generation plan and embed the assets needed to rebuild it."""
    _only_keys(generation, "scenarios coverage corpus workload_id_version generator_version", "generation")
    plan = tool_gap.normalize_plan(generation["scenarios"], generation["coverage"])
    _text(set_id, "set_id")
    _require(type(seed) is int, "seed must be an integer")
    try:
        cases = tool_gap.generate_cases(plan, generation["corpus"], tokenizer, seed)
    except (KeyError, IndexError) as error:
        raise ValueError(f"invalid tool_gap corpus field or template placeholder: {error}") from error
    files, count = _compile({
        "schema_version": JOB_SCHEMA, "set_id": set_id, "seed": seed, "cases": cases,
        "allow_fast_baseline": plan["scenarios"]["tool_gap"]["allow_fast_baseline"],
    }, plan)
    profile_id = tokenizer["profile"]["profile_id"]
    files["generation/tool_gap.json"] = {
        **plan, "corpus": generation["corpus"],
        "workload_id_version": GENERATOR_VERSION, "generator_version": GENERATOR_VERSION,
    }
    files[f"tokenizers/{profile_id}.json"] = tokenizer
    for value in files.values():
        _check_fields(value)
    return files, count, [profile_id]


def _build_files(job, root):
    """Select the package compiler for the supplied job format."""
    scenarios = job.get("scenarios")
    version = job.get("generator_version", GENERATOR_VERSION)
    _require(type(version) is int and version == GENERATOR_VERSION, "unsupported job generator_version")
    if "scenarios" not in job:
        files, count = _compile(job)
        return files, count, []
    _only_keys(job, "schema_version generator_version set_id seed environment scenarios input tokenizer_profiles coverage", "job")
    _require(job.get("schema_version") == JOB_SCHEMA, "unsupported job schema")
    plan = tool_gap.normalize_plan(scenarios, job.get("coverage", {}))
    corpus = tool_gap.load_corpus(job.get("input"), root)
    tokenizer = load_profile(job.get("tokenizer_profiles"), root)
    generation = {**plan, "corpus": corpus, "workload_id_version": version, "generator_version": version}
    return _compile_grid(job.get("set_id"), job.get("seed"), generation, tokenizer)


def build_workload_set(config, output):
    """Publish a deterministic package without replacing an existing package."""
    config = Path(config)
    job = _read(config)
    environment = job.get("environment", {})
    _require(isinstance(environment, dict) and environment.get("delivery_mode", "simulated") == "simulated",
             "workload generation supports simulated delivery")
    _only_keys(environment, "delivery_mode time_scale", "environment")
    scale = _seconds(environment.get("time_scale", 1), "time_scale", 1)
    files, count, profiles = _build_files(job, config.parent)
    scenario = "tool_gap"
    capabilities = CAPABILITIES[scenario]
    realization = "data_induced"
    for value in files.values():
        _check_fields(value)
    output = Path(output)
    _require(not output.exists() and not output.is_symlink(), f"package already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".perfworkload-", dir=output.parent) as temporary:
        stage = Path(temporary) / "package"
        for name, value in files.items():
            _write(stage / name, value)
        inventory = [
            {"path": name, "sha256": file_digest(stage / name), "bytes": (stage / name).stat().st_size}
            for name in sorted(files)
        ]
        manifest = {
            "generator_version": job.get("generator_version", GENERATOR_VERSION),
            "schema_version": ENV_SCHEMA, "set_id": job["set_id"], "seed": job["seed"],
            "delivery_mode": "simulated", "time_scale": scale, "capabilities": {scenario: capabilities},
            "realization": {scenario: realization}, "counts": {scenario: count},
            "files": inventory, "workload_digest": content_digest(inventory), "substrate": None,
        }
        if profiles:
            manifest["tokenizer_profiles"] = profiles
        elif job.get("allow_fast_baseline") is True:
            manifest["allow_fast_baseline"] = True
        manifest["env_digest"] = content_digest(manifest)
        _write(stage / "env.json", manifest)
        _require(not output.exists() and not output.is_symlink(), f"package already exists: {output}")
        stage.rename(output)
    return manifest


def tool_arguments(tool):
    """Validate and return the tool inputs stored in the package."""
    schema = tool["parameters"]
    _require(isinstance(schema, dict), "invalid tool parameters")
    required = schema.get("required", [])
    properties = schema.get("properties")
    _require(schema.get("type") == "object" and schema.get("additionalProperties") is False
             and isinstance(required, list) and isinstance(properties, dict)
             and all(isinstance(key, str) for key in required) and set(required) == properties.keys()
             and all(isinstance(prop, dict) and prop.get("type") == "string" for prop in properties.values()),
             "invalid tool parameters")
    arguments = tool.get("sample_arguments")
    _require(isinstance(arguments, dict) and arguments.keys() == properties.keys()
             and all(isinstance(value, str) and value for value in arguments.values()), "invalid sample_arguments")
    return arguments


def _check_tool(tool, tool_id, files, referenced):
    """Validate a tool fixture and record the resource it references."""
    _require(tool["schema_version"] == "agent-workload-tool-v1" and tool["tool_id"] == tool_id
             and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", tool["name"])
             and tool["independence"] == {"depends_on": []}, "invalid tool declaration")
    _text(tool["description"], "tool description")
    delay = _seconds(tool["latency"]["seconds"], "latency_seconds", 120)
    _require(tool["latency"] == {"mode": "fixed", "class": _latency_class(delay), "seconds": delay,
                                 "p50_seconds": delay, "p99_seconds": delay}
             and tool["timeout_seconds"] == delay + 5, "invalid tool timing")
    result = tool["result"]
    _require(result["kind"] == "fixture_set" and len(result["fixtures"]) == 1, "invalid tool result")
    fixture = result["fixtures"][0]
    digest = fixture["content_ref"]
    path = f"resources/{digest.removeprefix('sha256:')}.json"
    if path not in referenced:
        resource = files[path]
        _require(resource.keys() == {"content"} and content_digest(resource) == digest, "invalid fixture digest")
        _text(resource["content"], "tool result")
        referenced.add(path)
    tool_arguments(tool)
    return delay, fixture.get("token_count")


def _verify_structure(manifest, files):
    """Check the stored graph and its coverage without generating text or counting tokens."""
    scenario_key = "tool_gap"
    version = manifest.get("generator_version")
    _require(type(version) is int and version == GENERATOR_VERSION, "unsupported tool_gap generator_version")
    _text(manifest["set_id"], "set_id")
    _require(type(manifest["seed"]) is int, "invalid seed")
    referenced = {"coverage.json"}
    plan = None
    profiles = manifest.get("tokenizer_profiles", [])
    if profiles:
        _require(isinstance(profiles, list) and len(profiles) == 1, "expected one tokenizer profile")
        recipe_path = f"generation/{scenario_key.lower()}.json"
        profile_path = f"tokenizers/{profiles[0]}.json"
        recipe = files[recipe_path]
        _require(recipe["scenarios"].keys() == {scenario_key} and recipe.get("generator_version") == GENERATOR_VERSION
                 and recipe.get("workload_id_version") == GENERATOR_VERSION,
                 "inconsistent generation recipe")
        plan = tool_gap.normalize_plan(recipe["scenarios"], recipe["coverage"])
        validate_profile(files[profile_path])
        _require(files[profile_path]["profile"]["profile_id"] == profiles[0], "invalid tokenizer reference")
        referenced.update([recipe_path, profile_path])
    allow_fast = (plan["scenarios"]["tool_gap"]["allow_fast_baseline"] if plan
                  else manifest.get("allow_fast_baseline", False))
    _require(type(allow_fast) is bool, "allow_fast_baseline must be boolean")
    counts, gaps = {}, []
    for path, work in files.items():
        if not path.startswith("workloads/"):
            continue
        case_id = work["workload_id"]
        _require(path == f"workloads/{scenario_key}/{case_id}.json" and work["scenario"] == scenario_key
                 and work["schema_version"] == "agent-workload-v1" and work["sampling"] == {"seed": manifest["seed"]}
                 and work["content_digest"] == content_digest({k: v for k, v in work.items() if k != "content_digest"}),
                 "invalid workload identity")
        referenced.add(path)
        task = work["task"]
        messages = task["initial_messages"]
        _require(task["task_id"] == case_id and [msg["role"] for msg in messages] == ["system", "user"]
                 and messages[1]["content"] == task["task_text"], "invalid initial messages")
        for msg in messages:
            _text(msg["content"], "message content")
        names, durations, sizes = [], [], {}
        for ref in work["tools"]:
            tool_id = ref["tool_ref"]
            tool_path = f"tools/{tool_id}.json"
            tool = files[tool_path]
            _require(tool_id == f"{case_id}.{tool['name']}" and tool_path not in referenced, "invalid tool reference")
            referenced.add(tool_path)
            delay, size = _check_tool(tool, tool_id, files, referenced)
            names.append(tool["name"])
            durations.append(delay)
            if size is not None:
                _require(type(size) is int and size > 0, "invalid result token count")
                sizes[tool_id] = size
        width = len(names)
        _require(width >= 2 and names == task["tools_available"], "invalid available tools")
        gap = _latency_class(max(durations))
        _require(gap != "fast" or allow_fast, "fast gaps require allow_fast_baseline")
        structure = work["structure"]
        rounds = structure["rounds"]
        _require(type(rounds) is int and rounds >= 1 and structure["kind"] == "tool_gap"
                 and structure["gap_class"] == gap and structure["parallel_intent"] == {
                     "min_parallel_calls": width, "max_parallel_calls": width, "independent": True
                 }, "invalid parallel tool group")
        hypothesis = {"parallel_calls": width, "gap_class": gap, "turns": rounds}
        if plan:
            target = work["metadata"]["result_token_target"]
            scenario = plan["scenarios"]["tool_gap"]
            grid = scenario["grid"]
            _require(width in grid["parallel_width"] and gap in grid["latency_class"] and rounds in grid["rounds"]
                     and target in grid["result_tokens"], "workload outside coverage grid")
            cell = tool_gap.cell_key(width, gap, rounds, target)
            context_size = work["load_hypothesis"]["context_tokens"]
            _require(type(context_size) is int and scenario["context_tokens"] * .95 <= context_size <= scenario["context_tokens"]
                     and work["metadata"]["cell"] == cell and work["tokenizer_profile"] == profiles[0]
                     and len(sizes) == width and structure["result_tokens"] == sizes
                     and target * .95 <= max(sizes.values()) <= target, "invalid workload token declarations")
            hypothesis["context_tokens"] = context_size
        else:
            cell = f"width={width},gap={gap},rounds={rounds}"
        _require(work["load_hypothesis"] == hypothesis, "invalid load hypothesis")
        counts[cell] = counts.get(cell, 0) + 1
        gaps.append(gap)
    _require(bool(gaps) and manifest["counts"] == {scenario_key: len(gaps)}, "invalid workload counts")
    expected = (tool_gap.coverage_report(plan, manifest["set_id"], counts, gaps)
                if plan else {scenario_key: counts})
    _require(files["coverage.json"] == expected, "invalid tool_gap generated data or coverage")
    _require(referenced == files.keys(), "unreferenced package files")


def _verify_rebuild(manifest, files):
    """Recompile the tool-gap package and compare its contents and coverage."""
    scenario_key = "tool_gap"
    profiles = manifest.get("tokenizer_profiles", [])
    recipe_path = f"generation/{scenario_key.lower()}.json"
    if profiles or recipe_path in files:
        _require(isinstance(profiles, list) and len(profiles) == 1, "expected one tool_gap tokenizer profile")
        expected, count, checked_profiles = _compile_grid(
            manifest["set_id"], manifest["seed"], files[recipe_path],
            files[f"tokenizers/{profiles[0]}.json"],
        )
        _require(profiles == checked_profiles and canonical_json(expected) == canonical_json(files)
                 and manifest.get("counts") == {scenario_key: count}, "invalid tool_gap generated data or coverage")
        return
    # Recompile the verified fixtures to check all references and derived fields.
    cases = []
    for name, work in files.items():
        if not name.startswith(f"workloads/{scenario_key}/"):
            continue
        tools = []
        for ref in work["tools"]:
            tool = files[f"tools/{ref['tool_ref']}.json"]
            digest = tool["result"]["fixtures"][0]["content_ref"].removeprefix("sha256:")
            tools.append({"name": tool["name"], "description": tool["description"],
                          "latency_seconds": tool["latency"]["seconds"],
                          "parameters": tool["parameters"], "sample_arguments": tool["sample_arguments"],
                          "result": files[f"resources/{digest}.json"]["content"]})
        cases.append({"workload_id": work["workload_id"], "rounds": work["structure"]["rounds"],
                      "context": work["task"]["initial_messages"][0]["content"],
                      "task_text": work["task"]["task_text"], "tools": tools})
    expected, count = _compile({"schema_version": JOB_SCHEMA, "set_id": manifest["set_id"],
                                "seed": manifest["seed"], "cases": cases,
                                "allow_fast_baseline": manifest.get("allow_fast_baseline", False)})
    _require(canonical_json(expected) == canonical_json(files) and manifest.get("counts") == {scenario_key: count},
             "invalid tool_gap structure or coverage")


def load_workload_set(package, *, deep=False):
    """Load and verify a package, optionally rebuilding it to check reproducibility."""
    root = Path(package).resolve()
    _require(not (root / "env.json").is_symlink(), "env.json cannot be a symlink")
    manifest = _read(root / "env.json")
    realization = manifest.get("realization")
    scenario_key = "tool_gap"
    capabilities = CAPABILITIES[scenario_key]
    expected_realization = "data_induced"
    _require(manifest.get("schema_version") == ENV_SCHEMA and manifest.get("delivery_mode") == "simulated"
             and manifest.get("capabilities") == {scenario_key: capabilities}
             and manifest.get("substrate") is None
             and realization == {scenario_key: expected_realization}, "unsupported workload environment")
    _seconds(manifest.get("time_scale"), "time_scale", 1)
    signed = dict(manifest)
    _require(signed.pop("env_digest", None) == content_digest(signed), "env_digest_mismatch")
    inventory = manifest.get("files")
    _require(isinstance(inventory, list) and manifest.get("workload_digest") == content_digest(inventory),
             "workload_digest_mismatch")
    files = {}
    for entry in inventory:
        _require(isinstance(entry, dict), "invalid inventory entry")
        name = entry.get("path")
        _require(isinstance(name, str) and re.fullmatch(
            rf"(coverage|generation/{scenario_key.lower()}|tokenizers/[a-z0-9][a-z0-9_-]*|"
            rf"workloads/{scenario_key}/[a-z0-9][a-z0-9._-]*|tools/[A-Za-z0-9._-]+|resources/[a-f0-9]{{64}})\.json",
            name) and name not in files, "invalid or duplicate inventory path")
        path = root / name
        _require(path.resolve() == path and path.is_file(), f"invalid package file: {name}")
        value = path.read_bytes()
        # Verify and parse the same bytes, including on a concurrently modified tree.
        _require(len(value) == entry.get("bytes"), f"file size mismatch: {name}")
        _require("sha256:" + hashlib.sha256(value).hexdigest() == entry.get("sha256"),
                 f"file integrity mismatch: {name}")
        files[name] = json.loads(value.decode("utf-8"))
        _check_fields(files[name])
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() or path.is_symlink()}
    _require(actual == {"env.json", *files}, "package contains unlisted files")
    try:
        _verify_structure(manifest, files)
        if deep:
            _verify_rebuild(manifest, files)
    except (KeyError, TypeError, IndexError, AttributeError) as error:
        raise ValueError(f"invalid {scenario_key} package structure") from error
    return manifest, files
