"""Generate tool_gap workloads from a frozen local corpus and coverage grid."""

from __future__ import annotations

import itertools
import json
import random
import re
from collections import Counter

from base.provenance import canonical_json

from .tokenization import local_path, text_digest, token_counter


GRID = {
    "parallel_width": (2, 4, 8),
    "latency_class": ("medium", "slow"),
    "rounds": (2,),
    "result_tokens": (1024, 8192),
}
ALLOWED_GRID = {**GRID, "latency_class": ("fast", "medium", "slow"),
                "rounds": (1, 2, 4), "result_tokens": (1024, 8192, 32768)}
LATENCIES = (0.3, 0.5, 2.0, 3.0, 1.5, 2.5, 0.4, 1.2)
LONGEST = {"fast": 0.8, "medium": 6.0, "slow": 20.0}
MAX_WORKLOAD_ID_LENGTH = 80


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _keys(value, required, optional=()):
    _require(isinstance(value, dict) and set(required) <= value.keys()
             and value.keys() <= set(required) | set(optional), f"invalid tool_gap configuration, expected {required}")


def load_corpus(input_config, root):
    """Load the corpus text and its provenance from a local manifest."""
    _keys(input_config, ("corpus_manifest",))
    manifest_path = local_path(root, input_config["corpus_manifest"])
    manifest = json.loads(manifest_path.read_bytes())
    _keys(manifest, ("schema_version", "documents"))
    _require(manifest["schema_version"] == "agent-workload-content-v1", "unsupported corpus schema")
    documents = manifest["documents"]
    _require(isinstance(documents, list) and len(documents) == 1,
             "tool_gap requires one corpus document")
    document = documents[0]
    _keys(document, ("content_id", "path", "sha256", "license", "source"))
    text = local_path(manifest_path.parent, document["path"]).read_bytes().decode("utf-8")
    asset = {key: value for key, value in document.items() if key != "path"}
    return {**asset, "text": text}


def normalize_plan(scenarios, coverage):
    """Validate the tool-gap grid and resolve its coverage defaults."""
    _keys(scenarios, ("tool_gap",))
    scenario = scenarios["tool_gap"]
    _keys(scenario, ("enabled",), ("grid", "context_tokens", "allow_fast_baseline"))
    _require(scenario["enabled"] is True, "tool_gap must be enabled")
    requested = scenario.get("grid", {})
    _keys(requested, (), GRID)
    grid = {}
    for key, default in GRID.items():
        values = requested.get(key, list(default))
        allowed = ALLOWED_GRID[key]
        _require(isinstance(values, list) and values
                 and all(type(value) is type(allowed[0]) and value in allowed for value in values)
                 and len(set(values)) == len(values), f"invalid tool_gap grid axis: {key}")
        grid[key] = sorted(values, key=allowed.index)
    allow_fast = scenario.get("allow_fast_baseline", False)
    _require(type(allow_fast) is bool and (allow_fast or "fast" not in grid["latency_class"]),
             "fast gaps require allow_fast_baseline")
    context_tokens = scenario.get("context_tokens", 4096)
    _require(type(context_tokens) is int and 1024 <= context_tokens <= 32768,
             "context_tokens must be an integer between 1024 and 32768")
    _keys(coverage, (), ("require_complete", "min_per_cell"))
    complete = coverage.get("require_complete", True)
    minimum = coverage.get("min_per_cell", 1)
    _require(type(complete) is bool and type(minimum) is int and minimum >= 1,
             "invalid tool_gap coverage configuration")
    return {
        "scenarios": {"tool_gap": {"enabled": True, "grid": grid, "context_tokens": context_tokens,
                             "allow_fast_baseline": allow_fast}},
        "coverage": {"require_complete": complete, "min_per_cell": minimum},
    }


def cell_key(width, gap, rounds, result_tokens):
    return f"width={width}|latency={gap}|rounds={rounds}|result_tokens={result_tokens}"


def _cells(plan):
    grid = plan["scenarios"]["tool_gap"]["grid"]
    return itertools.product(*(grid[key] for key in GRID))


def coverage_report(plan, set_id, actual, latency):
    """Summarize generated cases against the requested grid and latency classes."""
    minimum = plan["coverage"]["min_per_cell"]
    expected = {cell_key(*cell): minimum for cell in _cells(plan)}
    missing = sorted(key for key, count in expected.items() if actual.get(key, 0) < count)
    _require(not (plan["coverage"]["require_complete"] and missing), f"coverage_gap: {missing}")
    return {
        "schema_version": "agent-workload-coverage-v1",
        "set_id": set_id,
        "scenarios": {"tool_gap": {"planned_cells": expected, "cells": dict(actual), "missing": missing,
                             "status": "incomplete" if missing else "complete"}},
        "realization": {"tool_gap": "data_induced"},
        "latency_bins": {"tool_gap": dict(Counter(latency))},
    }


def _read_corpus(asset):
    _keys(asset, ("content_id", "sha256", "license", "source", "text"))
    _require(all(isinstance(asset[key], str) and asset[key].strip() for key in asset),
             "corpus provenance must be nonempty")
    _require(text_digest(asset["text"]) == asset["sha256"], "corpus_digest_mismatch")
    corpus = json.loads(asset["text"])
    _keys(corpus, ("task_template", "context_template", "history_templates", "tools"))
    for key in ("task_template", "context_template"):
        _require(isinstance(corpus[key], str) and corpus[key].strip(), f"invalid corpus {key}")
    history = corpus["history_templates"]
    _require(isinstance(history, list) and history and all(isinstance(line, str) and line.strip() for line in history),
             "corpus requires nonempty history templates")
    tools = corpus["tools"]
    _require(isinstance(tools, list) and len(tools) == len(LATENCIES), "corpus requires eight tools")
    parameters = set()
    for tool in tools:
        _keys(tool, ("name", "description", "parameter", "parameter_description", "title", "details"))
        for key in ("name", "description", "parameter", "parameter_description", "title"):
            _require(isinstance(tool[key], str) and tool[key].strip(), f"invalid corpus tool {key}")
        _require(tool["parameter"] not in parameters, "tool_gap tool parameter names must be distinct")
        parameters.add(tool["parameter"])
        details = tool["details"]
        _require(isinstance(details, list) and details
                 and all(isinstance(detail, str) and detail.strip() for detail in details),
                 "corpus tool requires fixture details")
    return corpus


def _fit(render, count, target, minimum_ratio=0.95):
    """Choose complete records and verify the resulting text against the token budget."""
    lower = 0
    upper = 1
    while upper < 32768 and count(render(upper)) <= target:
        lower = upper
        upper *= 2
    while lower + 1 < upper:
        middle = (lower + upper) // 2
        if count(render(middle)) <= target:
            lower = middle
        else:
            upper = middle
    text = render(lower)
    size = count(text)
    _require(target * minimum_ratio <= size <= target, f"token_budget_mismatch: target={target}, actual={size}")
    return text, size


def _make_tool(tool, identifier, delay, target, count, rng):
    """Build a fixed tool fixture, fitting its result text when a token target is set."""
    parameter = tool["parameter"]
    details = tool["details"]
    offset = rng.randrange(len(details))

    def render(size):
        return canonical_json({
            parameter: identifier,
            "report": tool["title"],
            "observations": [
                {
                    "check_id": f"CHK-{index + 1:05d}",
                    "outcome": "needs_review" if (index + offset) % 11 == 0 else "passed",
                    "detail": details[(index + offset) % len(details)],
                }
                for index in range(size)
            ],
        })

    result, result_tokens = _fit(render, count, target, minimum_ratio=0.90 if target == 512 else 0.95)
    return {
        "name": tool["name"],
        "description": f'{tool["description"]} This source is independent of the other tools.',
        "latency_seconds": delay,
        "result": result,
        "sample_arguments": {parameter: identifier},
        "parameters": {
            "type": "object",
            "properties": {parameter: {"type": "string", "description": tool["parameter_description"]}},
            "required": [parameter],
            "additionalProperties": False,
        },
        "result_tokens": result_tokens,
    }


def _case_suffix(width, gap, rounds, result_tokens, number):
    seconds = LONGEST[gap]
    wait = f"{seconds:g}s" if seconds >= 1 else f"{seconds * 1000:g}ms"
    round_unit = "round" if rounds == 1 else "rounds"
    return (f"_{width}-tools_wait-{wait}_{rounds}-{round_unit}"
            f"_result-{result_tokens}-tokens_case-{number:03d}")


def _corpus_prefix(content_id, limit):
    if len(content_id) <= limit and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", content_id):
        return content_id
    # Preserve the original ID in metadata. Distinguish IDs that shorten to the same filename.
    slug = re.sub(r"[^a-z0-9]+", "-", content_id.lower()).strip("-") or "corpus"
    digest = text_digest(content_id).removeprefix("sha256:")[:10]
    return f"{slug[:limit - len(digest) - 1].rstrip('-')}-{digest}"


def generate_cases(plan, corpus_asset, tokenizer_asset, seed):
    """Expand the tool-gap grid into deterministic cases from a corpus and seed."""
    corpus = _read_corpus(corpus_asset)
    count = token_counter(tokenizer_asset)
    profile_id = tokenizer_asset["profile"]["profile_id"]
    scenario = plan["scenarios"]["tool_gap"]
    last = plan["coverage"]["min_per_cell"]
    longest_suffix = max(len(_case_suffix(*cell, last)) for cell in _cells(plan))
    prefix = _corpus_prefix(corpus_asset["content_id"], MAX_WORKLOAD_ID_LENGTH - longest_suffix)
    cases = []
    for width, gap, rounds, result_tokens in _cells(plan):
        cell = cell_key(width, gap, rounds, result_tokens)
        for instance in range(plan["coverage"]["min_per_cell"]):
            rng = random.Random(f"{seed}:tool_gap:{cell}:{instance}")
            release = f"REL-{rng.randrange(100000, 1000000)}"
            project = f"Atlas-{rng.randrange(100, 1000)}"
            version = f"v{rng.randrange(2, 6)}.{rng.randrange(1, 9)}.0"
            tools = corpus["tools"][:width]
            inputs = [f'{tool["parameter"]}={release}-{index + 1}' for index, tool in enumerate(tools)]
            parameters = ", ".join(inputs)
            context_header = corpus["context_template"].format(project=project, release=release, version=version,
                                                        inputs=parameters)
            history = corpus["history_templates"]
            offset = rng.randrange(len(history))

            def render_context(size):
                return "\n".join([context_header, *(
                    history[(offset + index) % len(history)].format(
                        project=project, release=release, item=index + 1, version=version
                    ) for index in range(size)
                )])

            context, context_size = _fit(render_context, count, scenario["context_tokens"])
            specs = []
            for index, tool in enumerate(tools):
                delay = LONGEST[gap] if index == 1 else LATENCIES[index]
                if gap == "fast" and index != 1:
                    delay = min(delay, 0.7)
                specs.append(_make_tool(tool, inputs[index].split("=", 1)[1], delay,
                                        result_tokens if index == 1 else 512, count, rng))
            task_text = corpus["task_template"].format(
                project=project, release=release, version=version, inputs=parameters,
                sources=", ".join(tool["name"] for tool in specs), rounds=rounds,
            )
            number = instance + 1
            workload_id = prefix + _case_suffix(width, gap, rounds, result_tokens, number)
            cases.append({
                "workload_id": workload_id,
                "task_text": task_text,
                "context": context,
                "context_tokens": context_size,
                "rounds": rounds,
                "tools": specs,
                "tokenizer_profile": profile_id,
                "result_token_target": result_tokens,
                "source": {key: corpus_asset[key] for key in ("content_id", "source", "license", "sha256")},
            })
    return cases
