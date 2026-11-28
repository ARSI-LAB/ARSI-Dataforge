"""Library-level evaluation execution and cross-round comparison reports.

Collapses the e2e ``eval_arm`` pattern into reusable primitives: per-attempt
records, infra failures recorded instead of raised (design D8), suite-policy
execution, and per-task delta reports over
``assert_comparable_evaluations``-comparable records.
"""
from __future__ import annotations

import hashlib
import math
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from data_designer.contracts.provenance import canonical_json

from .contracts import BenchmarkError, NativeFailure, canonical_sha256
from .native import run_case
from .runtime import verify_runtime_files
from .suite import (
    _checked_taskset,
    assert_comparable_evaluations,
    suite_body,
)

EVAL_RECORD_SCHEMA_VERSION = "benchmark-eval-record-v2"
EVAL_METRICS_SCHEMA_VERSION = "benchmark-metrics-v2"
LEGACY_EVAL_RECORD_SCHEMA_VERSION = "benchmark-eval-record-v1"

_GRADED_STATUSES = ("passed", "failed")
_GRADED_ATTEMPT_STATUSES = ("completed", "invalid_candidate")
# NativeFailure.category → record status (design §7.3). Timeout and an
# explicit runner "cancelled" are environment-side non-verdicts; every other
# category means the native infrastructure could not score the task.
_CANCELLED_CATEGORIES = ("timeout", "cancelled")



class CandidateFailure(BenchmarkError):
    """The candidate solver failed before producing a scorable deliverable."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


def _safe_component(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "-_." else "_"
        for character in value
    )
    return cleaned[:120] or "task"


def _map_failure(failure: NativeFailure) -> Dict[str, Any]:
    status = (
        "cancelled"
        if failure.category in _CANCELLED_CATEGORIES
        else "infra_failed"
    )
    return {
        "status": status,
        "passed": None,
        "detail": f"{failure.category}: {failure}",
    }


def aggregate(record_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Legacy v1 metrics over eval items.

    Denominator = passed + failed only; infra failures and cancellations are
    counted separately (design D10). ``macro_pass_rate`` here is the v1 pooled
    rate; v2 records use task-weighted macro from ``suite.weights``.
    """
    graded = [item for item in record_items if item["status"] in _GRADED_STATUSES]
    passed_total = sum(1 for item in graded if item["status"] == "passed")
    by_family: Dict[str, List[int]] = {}
    for item in graded:
        counts = by_family.setdefault(item["family"], [0, 0])
        counts[0] += 1 if item["status"] == "passed" else 0
        counts[1] += 1
    metrics: Dict[str, Any] = {
        "macro_pass_rate": passed_total / len(graded) if graded else 0.0,
        "graded_total": len(graded),
        "passed_total": passed_total,
        "infra_failed_total": sum(
            1 for item in record_items if item["status"] == "infra_failed"
        ),
        "cancelled_total": sum(
            1 for item in record_items if item["status"] == "cancelled"
        ),
    }
    metrics.update({
        f"pass_rate_{family}": counts[0] / counts[1] if counts[1] else 0.0
        for family, counts in sorted(by_family.items())
    })
    return metrics


def attempt_seed(suite_seed: int, task_id: str, attempt_index: int) -> int:
    """Deterministic, cross-round attempt seed (design §5.3)."""
    material = canonical_json(
        {"suite_seed": suite_seed, "task_id": task_id, "attempt": attempt_index}
    )
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


def _effective_timeout(suite: Dict[str, Any], limit: Optional[Dict[str, Any]]) -> float:
    value = float(suite["timeout_seconds"])
    if isinstance(limit, dict) and "timeout_seconds" in limit:
        value = min(value, float(limit["timeout_seconds"]))
    return value


def _attempt_record(index: int, seed: int, result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "index": index,
        "seed": seed,
        "status": result["status"],
        "passed": result["passed"],
        "score": result["score"],
        "verifier_version": result["verifier_version"],
        "runtime_seconds": result["runtime_seconds"],
        "resource_usage": result.get("resource_usage"),
        "evidence_refs": list(result["evidence_refs"]),
        "harness_version": result.get("harness_version"),
        "protocol": result.get("protocol"),
        "solver_version": result.get("solver_version"),
        "solver_sha256": result.get("solver_sha256"),
    }


def _failure_attempt(index: int, seed: int, failure: NativeFailure) -> Dict[str, Any]:
    status = "cancelled" if failure.category in _CANCELLED_CATEGORIES else "infra_failed"
    return {
        "index": index,
        "seed": seed,
        "status": status,
        "passed": None,
        "score": None,
        "verifier_version": None,
        "runtime_seconds": None,
        "resource_usage": None,
        "evidence_refs": [],
        "harness_version": None,
        "protocol": None,
        "solver_version": None,
        "solver_sha256": None,
        "detail": f"{failure.category}: {failure}",
    }


def _version_mismatch_attempt(
    index: int, seed: int, result: Dict[str, Any]
) -> Dict[str, Any]:
    return {
        "index": index,
        "seed": seed,
        "status": "infra_failed",
        "passed": None,
        "score": None,
        "verifier_version": result["verifier_version"],
        "runtime_seconds": result["runtime_seconds"],
        "resource_usage": result.get("resource_usage"),
        "evidence_refs": list(result["evidence_refs"]),
        "harness_version": result.get("harness_version"),
        "protocol": result.get("protocol"),
        "solver_version": result.get("solver_version"),
        "solver_sha256": result.get("solver_sha256"),
        "detail": "verifier_version_mismatch",
    }


def _summarize_task(
    task_id: str,
    family: str,
    attempts: List[Dict[str, Any]],
    *,
    detail: Optional[str] = None,
) -> Dict[str, Any]:
    graded = [
        attempt
        for attempt in attempts
        if attempt["status"] in _GRADED_ATTEMPT_STATUSES
    ]
    graded_passed = [attempt for attempt in graded if attempt["passed"] is True]
    infra_attempts = sum(
        1 for attempt in attempts if attempt["status"] not in _GRADED_ATTEMPT_STATUSES
    )
    if graded_passed:
        status, passed = "passed", True
    elif graded:
        status, passed = "failed", False
    elif any(
        attempt["status"] == "infra_failed" for attempt in attempts
    ):
        status, passed = "infra_failed", None
    else:
        status, passed = "cancelled", None
    if detail is None and status not in _GRADED_STATUSES:
        detail = next(
            (attempt.get("detail") for attempt in attempts if attempt.get("detail")),
            None,
        )
    scores = [attempt["score"] for attempt in graded]
    variance = statistics.pvariance(scores) if len(scores) >= 2 else None
    return {
        "task_id": task_id,
        "family": family,
        "status": status,
        "passed": passed,
        "pass_at_1": len(graded_passed) / len(graded) if graded else 0.0,
        "pass_at_k": bool(graded_passed),
        "graded_attempts": len(graded),
        "infra_attempts": infra_attempts,
        "score_variance": variance,
        "attempts": attempts,
        "detail": detail,
    }


def _aggregate_v2(
    items: List[Dict[str, Any]],
    suite: Dict[str, Any],
    families: List[str],
) -> Dict[str, Any]:
    graded = [item for item in items if item["status"] in _GRADED_STATUSES]
    passed_total = sum(1 for item in graded if item["status"] == "passed")
    by_family: Dict[str, List[int]] = {}
    for item in graded:
        counts = by_family.setdefault(item["family"], [0, 0])
        counts[0] += 1 if item["status"] == "passed" else 0
        counts[1] += 1
    weights = suite["weights"]
    used = [family for family in families if by_family.get(family, [0, 0])[1] > 0]
    weight_sum = sum(weights[family] for family in used)
    macro = (
        sum(
            weights[family]
            * (by_family[family][0] / by_family[family][1])
            for family in used
        )
        / weight_sum
        if used and weight_sum > 0
        else 0.0
    )
    micro = passed_total / len(graded) if graded else 0.0
    variances = [
        item["score_variance"]
        for item in items
        if item.get("score_variance") is not None
    ]
    metrics: Dict[str, Any] = {
        "macro_pass_rate": macro,
        "micro_pass_rate": micro,
        "graded_total": len(graded),
        "passed_total": passed_total,
        "attempts_total": sum(len(item["attempts"]) for item in items),
        "infra_failed_total": sum(
            1 for item in items if item["status"] == "infra_failed"
        ),
        "cancelled_total": sum(
            1 for item in items if item["status"] == "cancelled"
        ),
        "weight_renormalized": set(used) != {f for f in families if f in weights},
        "mean_score_variance": (
            statistics.mean(variances) if variances else None
        ),
        "max_score_variance": max(variances) if variances else None,
        "no_graded_tasks": not graded,
    }
    metrics.update({
        f"pass_rate_{family}": by_family[family][0] / by_family[family][1]
        for family in sorted(by_family)
    })
    return metrics


def _resolve_manifest(payload: Dict[str, Any], store, case_sha: str) -> Path:
    name = f"{case_sha}.json"
    relative = payload.get("trusted_manifest_rel")
    if isinstance(relative, str) and getattr(store, "uri", None):
        candidate = Path(store.uri) / relative / name
        if candidate.is_file():
            return candidate
    absolute = Path(payload["trusted_manifest_dir"]) / name
    if absolute.is_file():
        return absolute
    raise BenchmarkError(
        f"trusted case manifest missing for {case_sha[:12]}: "
        "rebuild with a persistent trusted directory"
    )


def evaluate_suite(
    store,
    suite: Dict[str, Any],
    candidate_root: str | Path,
    *,
    timeout: float | None = None,
    phase: str = "evaluation",
    allow_holdout: bool = False,
    promotion: Optional[Dict[str, Any]] = None,
    solver: Callable[..., Dict[str, Any]] | None = None,
    workspace_root: str | Path | None = None,
) -> Dict[str, Any]:
    """Execute the suite's evaluation plan and produce a v2 record.

    ``suite`` is the authority for repeats, seed, weights, timeout and
    resource limits. Infra failures are recorded, never raised (design D8);
    the caller decides whether they are fatal.

    When ``solver`` is supplied, each attempt gets a fresh workspace and
    candidate root; the solver writes the deliverable and the frozen verifier
    scores that root. This is the real cross-round candidate path.
    """
    if canonical_sha256(suite_body(suite)) != suite.get("suite_sha256"):
        raise BenchmarkError("suite SHA-256 drift")
    if suite.get("split") == "holdout" and (
        not allow_holdout or phase != "final"
    ):
        raise BenchmarkError("holdout suites are final-evaluation only")
    if suite.get("split") == "holdout":
        if promotion is None:
            raise BenchmarkError("holdout evaluation requires a promotion record")
        from .promotion import verify_promotion

        verify_promotion(store, promotion, suite)
    elif promotion is not None:
        raise BenchmarkError("promotion records only authorize holdout suites")
    if phase not in ("evaluation", "final"):
        raise BenchmarkError("phase must be 'evaluation' or 'final'")

    limits = suite.get("resource_limits") or {}
    families: List[str] = []
    selected: List[tuple] = []  # (family, item, payload, runtime)
    runtime_prechecked = set()
    selection: Dict[str, Dict[str, int]] = {}
    for pin in suite["tasksets"]:
        payload = _checked_taskset(store, pin)
        family = payload["family"]
        if family in families:
            raise BenchmarkError(f"duplicate suite family {family}")
        families.append(family)
        items = payload["taskset"]
        limit = limits.get(family)
        count = (
            limit["max_tasks"]
            if isinstance(limit, dict) and "max_tasks" in limit
            else len(items)
        )
        if isinstance(limit, dict):
            selection[family] = {"max_tasks": count}
        for item in items[:count]:
            task_id = item["task_id"]
            runtime = payload["runtime_by_task"][task_id]
            key = canonical_sha256(runtime)
            if key not in runtime_prechecked:
                verify_runtime_files(runtime)
                runtime_prechecked.add(key)
            selected.append((family, item, payload, runtime))

    if timeout is not None and (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise BenchmarkError("timeout override must be finite and positive")

    candidate_root = Path(candidate_root)
    workspace_root = (
        Path(workspace_root) if workspace_root is not None else None
    )
    repeats = suite["repeats"]
    suite_seed = suite.get("seed", 0)
    items: List[Dict[str, Any]] = []
    tasks: List[str] = []
    submitted: Dict[str, Dict[str, Any]] = {}
    for family, item, payload, runtime in selected:
        submitted[item["task_id"]] = {
            "family": family,
            "item": item,
            "runtime": runtime,
            "payload": payload,
        }

    for family in families:
        # Selection preserves frozen order; dict insertion order follows suite order.
        family_tasks = [
            task_id
            for task_id in submitted
            if submitted[task_id]["family"] == family
        ]
        limit = limits.get(family)
        effective = _effective_timeout(suite, limit)
        if timeout is not None and timeout != effective:
            raise BenchmarkError("timeout override breaks comparability")
        wall_seconds = (
            float(limit["wall_seconds"]) if isinstance(limit, dict) else None
        )
        max_cost = (
            float(limit["max_cost_usd"])
            if isinstance(limit, dict) and "max_cost_usd" in limit
            else None
        )
        started = time.monotonic()
        cost = 0.0
        budget_detail = None
        for task_id in family_tasks:
            record = submitted[task_id]
            item, runtime = record["item"], record["runtime"]
            pinned_version = runtime["verifier"]["version"]
            manifest_path = _resolve_manifest(
                record["payload"], store,
                item["benchmark"]["case_manifest_sha256"],
            )
            attempts: List[Dict[str, Any]] = []
            detail = None
            if (
                budget_detail is None
                and wall_seconds is not None
                and time.monotonic() - started > wall_seconds
            ):
                budget_detail = "wall_clock_exhausted"
            if (
                budget_detail is None
                and max_cost is not None
                and cost >= max_cost
            ):
                budget_detail = "cost_budget_exhausted"
            if budget_detail is not None:
                detail = budget_detail
            else:
                for attempt_index in range(repeats):
                    seed = attempt_seed(suite_seed, task_id, attempt_index)
                    attempt_candidate = candidate_root
                    solver_info: Dict[str, Any] = {}
                    if solver is not None:
                        attempt_root = (
                            (workspace_root or candidate_root.parent)
                            / _safe_component(task_id)
                            / f"attempt-{attempt_index}"
                        )
                        workspace = attempt_root / "workspace"
                        workspace.mkdir(parents=True, exist_ok=True)
                        attempt_candidate = attempt_root / "candidate"
                        attempt_candidate.mkdir(parents=True, exist_ok=True)
                        try:
                            solver_info = solver(
                                item,
                                workspace,
                                attempt_candidate,
                                {"index": attempt_index, "seed": seed},
                            ) or {}
                        except CandidateFailure as failure:
                            attempts.append(
                                {
                                    "index": attempt_index,
                                    "seed": seed,
                                    "status": "failed",
                                    "passed": False,
                                    "score": 0.0,
                                    "verifier_version": None,
                                    "runtime_seconds": 0.0,
                                    "resource_usage": None,
                                    "evidence_refs": [],
                                    "harness_version": None,
                                    "protocol": None,
                                    "solver_version": None,
                                    "solver_sha256": None,
                                    "detail": (
                                        f"solver_failed: {failure.category}: "
                                        f"{failure}"
                                    ),
                                }
                            )
                            continue
                    try:
                        result = run_case(
                            runtime["verifier"]["command"],
                            task_id,
                            str(manifest_path),
                            str(attempt_candidate),
                            phase=phase,
                            timeout=effective,
                            public_item=item,
                            attempt={"index": attempt_index, "seed": seed},
                        )
                    except NativeFailure as failure:
                        attempts.append(
                            _failure_attempt(attempt_index, seed, failure)
                        )
                        continue
                    if result["verifier_version"] != pinned_version:
                        attempts.append(
                            _version_mismatch_attempt(attempt_index, seed, result)
                        )
                        continue
                    attempt = _attempt_record(attempt_index, seed, result)
                    if solver_info:
                        attempt["solver_version"] = solver_info.get(
                            "solver_version"
                        )
                        attempt["solver_sha256"] = solver_info.get(
                            "solver_sha256"
                        )
                    attempts.append(attempt)
                    usage = result.get("resource_usage") or {}
                    cost += float(usage.get("cost_usd", 0.0))
            summary = _summarize_task(
                task_id, family, attempts, detail=detail
            )
            tasks.append(task_id)
            items.append(summary)

    runtime_block = {
        "verifier_versions": {},
        "observed_verifier_versions": {},
        "verifier_sha256": {},
        "environment_sha256": {},
        "harness_versions": {},
        "protocols": {},
    }
    items_by_task = {item["task_id"]: item for item in items}
    for task_id in tasks:
        record = submitted[task_id]
        runtime_block["verifier_versions"][task_id] = record["runtime"][
            "verifier"
        ]["version"]
        runtime_block["observed_verifier_versions"][task_id] = [
            attempt["verifier_version"]
            for attempt in items_by_task[task_id]["attempts"]
            if attempt["verifier_version"] is not None
        ]
        benchmark = record["item"]["benchmark"]
        runtime_block["verifier_sha256"][task_id] = benchmark["verifier_sha256"]
        runtime_block["environment_sha256"][task_id] = benchmark[
            "environment_sha256"
        ]
        runtime_block["harness_versions"][task_id] = sorted(
            {
                attempt["harness_version"]
                for attempt in items_by_task[task_id]["attempts"]
                if attempt.get("harness_version")
            }
        )
        runtime_block["protocols"][task_id] = sorted(
            {
                attempt["protocol"]
                for attempt in items_by_task[task_id]["attempts"]
                if attempt.get("protocol")
            }
        )
    excluded = {
        "infra_failed": [
            item["task_id"] for item in items if item["status"] == "infra_failed"
        ],
        "cancelled": [
            item["task_id"] for item in items if item["status"] == "cancelled"
        ],
    }
    policy = {
        "weights": suite["weights"],
        "repeats": repeats,
        "seed": suite_seed,
        "timeout_seconds": suite["timeout_seconds"],
        "resource_limits": suite.get("resource_limits"),
        "selection": selection,
    }
    record = {
        "schema_version": EVAL_RECORD_SCHEMA_VERSION,
        "metrics_schema": EVAL_METRICS_SCHEMA_VERSION,
        "suite_sha256": suite["suite_sha256"],
        "suite_ref": {"ref_id": suite["ref_id"], "version": suite["version"]},
        "tasksets": suite["tasksets"],
        "split": suite["split"],
        "phase": phase,
        "policy": policy,
        "runtime": runtime_block,
        "tasks": tasks,
        "items": items,
        "metrics": _aggregate_v2(items, suite, families),
        "excluded": excluded,
    }
    if promotion is not None:
        from .promotion import consume_promotion

        consumed = consume_promotion(
            store, promotion, suite, canonical_sha256(record)
        )
        record["promotion"] = {
            "ref_id": consumed["promotion_ref"]["ref_id"],
            "version": consumed["promotion_ref"]["version"],
            "promotion_sha256": consumed["promotion_sha256"],
        }
    return record


@dataclass
class ComparisonReport:
    """Cross-round report: macro/micro deltas, per-family rates, per-task
    changes, score/variance summaries and policy equality. Only tasks graded
    (passed/failed) on both sides enter the delta denominators; infra sides
    are listed separately."""

    suite_sha256: str
    split: str
    baseline_ref: Dict[str, Any]
    candidate_ref: Dict[str, Any]
    macro_baseline: float
    macro_candidate: float
    macro_delta: float
    per_family: Dict[str, Dict[str, float]]
    changed_tasks: List[Dict[str, Any]]
    infra_summary: Dict[str, List[str]]
    graded_totals: Dict[str, int]
    micro_baseline: Optional[float] = None
    micro_candidate: Optional[float] = None
    micro_delta: Optional[float] = None
    score_changes: List[Dict[str, Any]] = field(default_factory=list)
    variance_summary: Dict[str, Optional[float]] = field(default_factory=dict)
    policy_equal: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "suite_sha256": self.suite_sha256,
            "split": self.split,
            "baseline_ref": self.baseline_ref,
            "candidate_ref": self.candidate_ref,
            "macro_baseline": self.macro_baseline,
            "macro_candidate": self.macro_candidate,
            "macro_delta": self.macro_delta,
            "micro_baseline": self.micro_baseline,
            "micro_candidate": self.micro_candidate,
            "micro_delta": self.micro_delta,
            "per_family": self.per_family,
            "changed_tasks": self.changed_tasks,
            "score_changes": self.score_changes,
            "variance_summary": self.variance_summary,
            "policy_equal": self.policy_equal,
            "infra_summary": self.infra_summary,
            "graded_totals": self.graded_totals,
        }

    def write_json(self, path: str | Path) -> None:
        Path(path).write_text(canonical_json(self.to_dict()), encoding="utf-8")



def _normalized_runtime_versions(record: Dict[str, Any]) -> Dict[str, Dict[str, List[str]]]:
    runtime = record.get("runtime")
    if not isinstance(runtime, dict):
        return {}
    normalized: Dict[str, Dict[str, List[str]]] = {}
    for key in ("verifier_versions", "harness_versions", "protocols"):
        raw = runtime.get(key)
        if not isinstance(raw, dict):
            continue
        values: Dict[str, List[str]] = {}
        for task_id, value in raw.items():
            sequence = value if isinstance(value, list) else [value]
            cleaned = sorted({str(item) for item in sequence if item})
            if cleaned:
                values[task_id] = cleaned
        if values:
            normalized[key] = values
    return normalized


def _graded_items(record: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    if not isinstance(record.get("items"), list):
        raise BenchmarkError(
            "evaluation record is missing per-item records (benchmark-eval-record-v1)"
        )
    return {
        item["task_id"]: item
        for item in record["items"]
        if item["status"] in _GRADED_STATUSES
    }


def _family_rates(record: Dict[str, Any]) -> Dict[str, float]:
    graded = [item for item in record["items"] if item["status"] in _GRADED_STATUSES]
    by_family: Dict[str, List[int]] = {}
    for item in graded:
        counts = by_family.setdefault(item["family"], [0, 0])
        counts[0] += 1 if item["status"] == "passed" else 0
        counts[1] += 1
    return {
        family: counts[0] / counts[1] if counts[1] else 0.0
        for family, counts in by_family.items()
    }


def _is_v2(record: Dict[str, Any]) -> bool:
    return record.get("schema_version") == EVAL_RECORD_SCHEMA_VERSION


def _mean_score(item: Dict[str, Any]) -> Optional[float]:
    scores = [
        attempt["score"]
        for attempt in item.get("attempts") or []
        if attempt.get("score") is not None
    ]
    return statistics.mean(scores) if scores else None


def compare_evaluations(
    baseline: Dict[str, Any], candidate: Dict[str, Any]
) -> ComparisonReport:
    if _is_v2(baseline) != _is_v2(candidate):
        raise BenchmarkError("metric semantics differ")
    if (baseline.get("split") == "holdout") != (
        candidate.get("split") == "holdout"
    ):
        raise BenchmarkError(
            "holdout and experiment evaluations are not comparable"
        )
    assert_comparable_evaluations(baseline, candidate)
    for label, record in (("baseline", baseline), ("candidate", candidate)):
        runtime = record.get("runtime") or {}
        protocols = runtime.get("protocols")
        families = {
            item.get("family") for item in record.get("items") or []
            if isinstance(item, dict)
        }
        if families & {"swe", "deepswe"} and (
            not isinstance(protocols, dict)
            or not protocols
            or not runtime.get("verifier_versions")
        ):
            raise BenchmarkError(
                f"{label} SWE evaluation is missing verifier/protocol runtime"
            )
        if not isinstance(protocols, dict):
            continue
        mixed = sorted(
            task_id
            for task_id, values in protocols.items()
            if isinstance(values, (list, tuple))
            and len({str(value) for value in values if value}) > 1
        )
        if mixed:
            raise BenchmarkError(
                f"{label} used multiple judging protocols for tasks: {mixed}"
            )
        declared = {
            str(value)
            for values in protocols.values()
            if isinstance(values, (list, tuple))
            for value in values
            if value
        }
        if len(declared) > 1:
            raise BenchmarkError(
                f"{label} mixes judging protocols: {sorted(declared)}"
            )
    baseline_runtime = _normalized_runtime_versions(baseline)
    candidate_runtime = _normalized_runtime_versions(candidate)
    if baseline_runtime != candidate_runtime:
        raise BenchmarkError(
            "verifier/harness runtime drift between evaluation rounds"
        )
    baseline_graded = _graded_items(baseline)
    candidate_graded = _graded_items(candidate)

    changed_tasks = []
    for task_id in sorted(set(baseline_graded) & set(candidate_graded)):
        left, right = baseline_graded[task_id], candidate_graded[task_id]
        if left["status"] != right["status"]:
            changed_tasks.append({
                "task_id": task_id,
                "family": left["family"],
                "baseline": left["status"],
                "candidate": right["status"],
            })

    families = sorted(
        {item["family"] for item in baseline["items"]}
        | {item["family"] for item in candidate["items"]}
    )
    baseline_rates = _family_rates(baseline)
    candidate_rates = _family_rates(candidate)
    per_family = {
        family: {
            "baseline": baseline_rates.get(family, 0.0),
            "candidate": candidate_rates.get(family, 0.0),
            "delta": candidate_rates.get(family, 0.0) - baseline_rates.get(family, 0.0),
        }
        for family in families
    }

    infra_summary = {
        "baseline": [
            item["task_id"] for item in baseline["items"]
            if item["status"] not in _GRADED_STATUSES
        ],
        "candidate": [
            item["task_id"] for item in candidate["items"]
            if item["status"] not in _GRADED_STATUSES
        ],
    }
    macro_baseline = baseline["metrics"]["macro_pass_rate"]
    macro_candidate = candidate["metrics"]["macro_pass_rate"]
    micro_baseline = baseline["metrics"].get("micro_pass_rate")
    micro_candidate = candidate["metrics"].get("micro_pass_rate")
    micro_delta = (
        micro_candidate - micro_baseline
        if micro_baseline is not None and micro_candidate is not None
        else None
    )
    score_changes = []
    variance_summary: Dict[str, Optional[float]] = {
        "baseline": baseline["metrics"].get("mean_score_variance"),
        "candidate": candidate["metrics"].get("mean_score_variance"),
    }
    policy_equal: Optional[bool] = None
    if _is_v2(baseline):
        for task_id in sorted(set(baseline_graded) & set(candidate_graded)):
            left = _mean_score(baseline_graded[task_id])
            right = _mean_score(candidate_graded[task_id])
            if left is not None and right is not None:
                score_changes.append({
                    "task_id": task_id,
                    "family": baseline_graded[task_id]["family"],
                    "baseline_score": left,
                    "candidate_score": right,
                    "delta": right - left,
                })
        if isinstance(baseline.get("policy"), dict) and isinstance(
            candidate.get("policy"), dict
        ):
            policy_equal = baseline["policy"] == candidate["policy"]
    return ComparisonReport(
        suite_sha256=baseline["suite_sha256"],
        split=baseline["split"],
        baseline_ref=baseline["suite_ref"],
        candidate_ref=candidate["suite_ref"],
        macro_baseline=macro_baseline,
        macro_candidate=macro_candidate,
        macro_delta=macro_candidate - macro_baseline,
        per_family=per_family,
        changed_tasks=changed_tasks,
        infra_summary=infra_summary,
        graded_totals={
            "baseline": len(baseline_graded),
            "candidate": len(candidate_graded),
        },
        micro_baseline=micro_baseline,
        micro_candidate=micro_candidate,
        micro_delta=micro_delta,
        score_changes=score_changes,
        variance_summary=variance_summary,
        policy_equal=policy_equal,
    )
