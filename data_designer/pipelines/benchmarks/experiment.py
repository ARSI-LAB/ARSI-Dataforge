"""Real candidate execution and multi-round experiment orchestration.

A solver is a local program invoked as::

    python solver.py <request.json> <result.json>

The request carries the public task item, a fresh workspace, a fresh candidate
root, and the attempt index/seed. The solver writes its deliverable under
``candidateRoot`` and acknowledges the attempt in ``result.json``. The frozen
verifier then scores that candidate root.

``run_experiment`` executes at least two rounds against the same loaded suite,
persists every evaluation record, and emits a comparison report with per-task
deltas, macro/micro metrics, runtime-version checks and an explicit decision.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from data_designer.contracts.provenance import canonical_json

from .contracts import BenchmarkError, file_sha256
from .report import (
    CandidateFailure,
    compare_evaluations,
    evaluate_suite,
)

SOLVER_SCHEMA_VERSION = "benchmark-solver-v1"
EXPERIMENT_SCHEMA_VERSION = "benchmark-experiment-v1"
DEFAULT_MIN_SAMPLE_SIZE = 2
DEFAULT_ALPHA = 0.05
_EPS = 1e-9


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
    )


@dataclass(frozen=True)
class Solver:
    path: str
    sha256: str
    command: List[str]
    timeout: float = 600.0
    env: Mapping[str, str] = field(default_factory=dict)

    def __call__(
        self,
        task: Dict[str, Any],
        workspace: Path,
        candidate_root: Path,
        attempt: Dict[str, int],
    ) -> Dict[str, Any]:
        request = {
            "schemaVersion": SOLVER_SCHEMA_VERSION,
            "taskId": task["task_id"],
            "publicItem": task,
            "workspace": str(workspace),
            "candidateRoot": str(candidate_root),
            "attempt": attempt,
        }
        with tempfile.TemporaryDirectory(prefix="benchmark-solver-") as directory:
            request_path = Path(directory) / "request.json"
            result_path = Path(directory) / "result.json"
            request_path.write_text(
                json.dumps(request, ensure_ascii=False, allow_nan=False),
                encoding="utf-8",
            )
            environment = {**os.environ, **dict(self.env)} if self.env else None
            try:
                process = subprocess.run(
                    [*self.command, str(request_path), str(result_path)],
                    cwd=str(workspace),
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    check=False,
                    env=environment,
                )
            except subprocess.TimeoutExpired as error:
                raise CandidateFailure(
                    "timeout", f"candidate solver timed out: {error}"
                ) from error
            except OSError as error:
                raise CandidateFailure(
                    "unavailable", f"candidate solver unavailable: {error}"
                ) from error
            if process.returncode != 0:
                raise CandidateFailure(
                    "failed",
                    "candidate solver failed "
                    f"({process.returncode}): {process.stderr[-500:]}",
                )
            try:
                result = _load_json(result_path)
            except (OSError, ValueError) as error:
                raise CandidateFailure(
                    "protocol", f"candidate solver result invalid: {error}"
                ) from error
        if result.get("schema_version") != SOLVER_SCHEMA_VERSION:
            raise CandidateFailure(
                "protocol", "candidate solver used an unknown result schema"
            )
        if result.get("task_id") != task["task_id"]:
            raise CandidateFailure("protocol", "candidate solver task_id mismatch")
        if (
            result.get("attempt_index") != attempt["index"]
            or result.get("seed") != attempt["seed"]
        ):
            raise CandidateFailure(
                "protocol", "candidate solver did not acknowledge attempt seed"
            )
        version = result.get("solver_version")
        if not isinstance(version, str) or not version.strip():
            raise CandidateFailure(
                "protocol", "candidate solver did not declare solver_version"
            )
        return {
            "solver_version": version,
            "solver_sha256": self.sha256,
            "solver_path": self.path,
        }


def load_solver(
    path: str | Path,
    *,
    timeout: float = 600.0,
    env: Optional[Mapping[str, str]] = None,
) -> Solver:
    solver_path = Path(path)
    if not solver_path.is_absolute():
        solver_path = solver_path.resolve()
    if not solver_path.is_file():
        raise BenchmarkError(f"candidate solver unavailable: {solver_path}")
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise BenchmarkError("candidate solver timeout must be finite and positive")
    return Solver(
        path=str(solver_path),
        sha256=file_sha256(solver_path),
        command=[sys.executable, str(solver_path)],
        timeout=float(timeout),
        env=dict(env or {}),
    )


def run_round(
    store,
    suite: Dict[str, Any],
    solver: Solver | str | Path,
    round_root: str | Path,
    *,
    phase: str = "evaluation",
    allow_holdout: bool = False,
    solver_env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Execute one candidate over the frozen suite and return its v2 record."""
    round_root = Path(round_root)
    solver_obj = (
        solver
        if isinstance(solver, Solver)
        else load_solver(
            solver,
            timeout=float(suite.get("timeout_seconds", 600)),
            env=solver_env,
        )
    )
    record = evaluate_suite(
        store,
        suite,
        round_root / "candidates",
        phase=phase,
        allow_holdout=allow_holdout,
        solver=solver_obj,
        workspace_root=round_root / "workspaces",
    )
    record["candidate"] = {
        "solver_path": solver_obj.path,
        "solver_sha256": solver_obj.sha256,
        "solver_versions": sorted(
            {
                attempt.get("solver_version")
                for item in record["items"]
                for attempt in item.get("attempts") or []
                if attempt.get("solver_version")
            }
        ),
    }
    record["round_root"] = str(round_root)
    return record


def _item_score(item: Dict[str, Any]) -> Optional[float]:
    scores = [
        float(attempt["score"])
        for attempt in item.get("attempts") or []
        if attempt.get("score") is not None
    ]
    return sum(scores) / len(scores) if scores else None


def _item_pass_rate(item: Dict[str, Any]) -> Optional[float]:
    values = [
        1.0 if attempt.get("passed") else 0.0
        for attempt in item.get("attempts") or []
        if attempt.get("passed") is not None
    ]
    return sum(values) / len(values) if values else None


def _paired_deltas(
    baseline: Dict[str, Any], candidate: Dict[str, Any]
) -> List[Dict[str, Any]]:
    graded = {"passed", "failed"}
    left = {
        item["task_id"]: item
        for item in baseline["items"]
        if item.get("status") in graded
    }
    right = {
        item["task_id"]: item
        for item in candidate["items"]
        if item.get("status") in graded
    }
    rows: List[Dict[str, Any]] = []
    for task_id in sorted(set(left) & set(right)):
        baseline_score = _item_score(left[task_id])
        candidate_score = _item_score(right[task_id])
        baseline_rate = _item_pass_rate(left[task_id])
        candidate_rate = _item_pass_rate(right[task_id])
        if None in (baseline_score, candidate_score, baseline_rate, candidate_rate):
            continue
        rows.append(
            {
                "task_id": task_id,
                "family": left[task_id]["family"],
                "baseline_score": baseline_score,
                "candidate_score": candidate_score,
                "score_delta": candidate_score - baseline_score,
                "baseline_pass_rate": baseline_rate,
                "candidate_pass_rate": candidate_rate,
                "pass_rate_delta": candidate_rate - baseline_rate,
            }
        )
    return rows


def _sign_test_p_value(improved: int, regressed: int) -> float:
    total = improved + regressed
    if total == 0:
        return 1.0
    tail = min(improved, regressed)
    probability = sum(math.comb(total, index) for index in range(tail + 1))
    return min(1.0, 2.0 * probability / (2**total))


def _decision(
    *,
    comparison,
    paired: List[Dict[str, Any]],
    baseline: Dict[str, Any],
    candidate: Dict[str, Any],
    min_sample_size: int,
    alpha: float,
) -> Dict[str, Any]:
    improved = sum(1 for row in paired if row["pass_rate_delta"] > _EPS)
    regressed = sum(1 for row in paired if row["pass_rate_delta"] < -_EPS)
    tied = len(paired) - improved - regressed
    p_value = _sign_test_p_value(improved, regressed)
    infra = comparison.infra_summary
    has_infra = bool(infra.get("baseline") or infra.get("candidate"))
    if has_infra:
        decision = "invalid"
        reason = "infrastructure failures are present; no promotion decision"
    elif len(paired) < min_sample_size:
        decision = "inconclusive_small_sample"
        reason = (
            f"only {len(paired)} paired graded tasks; "
            f"minimum sample size is {min_sample_size}"
        )
    elif improved > regressed and p_value <= alpha:
        decision = "promote"
        reason = (
            f"{improved} improved / {regressed} regressed, "
            f"sign-test p={p_value:.6f} <= alpha={alpha}"
        )
    elif regressed > improved and p_value <= alpha:
        decision = "keep_incumbent"
        reason = (
            f"{improved} improved / {regressed} regressed, "
            f"sign-test p={p_value:.6f} <= alpha={alpha}"
        )
    else:
        decision = "inconclusive"
        reason = (
            f"{improved} improved / {regressed} regressed / {tied} tied, "
            f"sign-test p={p_value:.6f} > alpha={alpha}"
        )
    return {
        "decision": decision,
        "reason": reason,
        "sample_size": len(paired),
        "improved": improved,
        "regressed": regressed,
        "tied": tied,
        "p_value": p_value,
        "min_sample_size": min_sample_size,
        "alpha": alpha,
        "macro_baseline": comparison.macro_baseline,
        "macro_candidate": comparison.macro_candidate,
        "macro_delta": comparison.macro_delta,
        "micro_baseline": comparison.micro_baseline,
        "micro_candidate": comparison.micro_candidate,
        "micro_delta": comparison.micro_delta,
        "baseline_infra_failed": baseline["metrics"].get("infra_failed_total", 0),
        "candidate_infra_failed": candidate["metrics"].get("infra_failed_total", 0),
        "baseline_cancelled": baseline["metrics"].get("cancelled_total", 0),
        "candidate_cancelled": candidate["metrics"].get("cancelled_total", 0),
    }


def run_experiment(
    store,
    suite: Dict[str, Any],
    rounds: List[Dict[str, Any]],
    output_root: str | Path,
    *,
    phase: str = "evaluation",
    min_sample_size: int = DEFAULT_MIN_SAMPLE_SIZE,
    alpha: float = DEFAULT_ALPHA,
    allow_holdout: bool = False,
) -> Dict[str, Any]:
    """Run at least two rounds on one frozen suite and write the comparison."""
    if not isinstance(rounds, list) or len(rounds) < 2:
        raise BenchmarkError("an experiment needs at least two rounds")
    if type(min_sample_size) is not int or min_sample_size < 1:
        raise BenchmarkError("min_sample_size must be an integer >= 1")
    if (
        type(alpha) not in (int, float)
        or not math.isfinite(alpha)
        or not 0 < alpha < 1
    ):
        raise BenchmarkError("alpha must be in (0, 1)")
    output_root = Path(output_root)
    executed: List[Dict[str, Any]] = []
    for config in rounds:
        if (
            not isinstance(config, dict)
            or not isinstance(config.get("id"), str)
            or not config["id"].strip()
            or not config.get("solver")
        ):
            raise BenchmarkError("each round needs an id and a solver path")
        round_root = output_root / "rounds" / config["id"]
        record = run_round(
            store,
            suite,
            config["solver"],
            round_root,
            phase=phase,
            allow_holdout=allow_holdout,
            solver_env=config.get("solver_env"),
        )
        record_path = round_root / "eval-record.json"
        record_path.write_text(canonical_json(record), encoding="utf-8")
        executed.append(
            {
                "id": config["id"],
                "solver": str(config["solver"]),
                "eval_record": str(record_path),
                "record": record,
            }
        )
    comparison = compare_evaluations(
        executed[0]["record"], executed[-1]["record"]
    )
    comparison_path = output_root / "comparison.json"
    comparison.write_json(comparison_path)
    paired = _paired_deltas(executed[0]["record"], executed[-1]["record"])
    decision = _decision(
        comparison=comparison,
        paired=paired,
        baseline=executed[0]["record"],
        candidate=executed[-1]["record"],
        min_sample_size=min_sample_size,
        alpha=alpha,
    )
    summary = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "suite": {
            "ref_id": suite["ref_id"],
            "version": suite["version"],
            "suite_sha256": suite["suite_sha256"],
            "split": suite["split"],
            "repeats": suite["repeats"],
            "seed": suite.get("seed", 0),
        },
        "rounds": [
            {
                "id": item["id"],
                "solver": item["solver"],
                "solver_sha256": item["record"]["candidate"]["solver_sha256"],
                "eval_record": item["eval_record"],
                "metrics": item["record"]["metrics"],
                "verifier_versions": item["record"]["runtime"].get(
                    "verifier_versions", {}
                ),
                "harness_versions": item["record"]["runtime"].get(
                    "harness_versions", {}
                ),
                "protocols": item["record"]["runtime"].get("protocols", {}),
            }
            for item in executed
        ],
        "tasks": executed[0]["record"]["tasks"],
        "paired_deltas": paired,
        "comparison": comparison.to_dict(),
        "comparison_path": str(comparison_path),
        "decision": decision,
    }
    if store is not None:
        from .promotion import register_experiment

        summary["artifact"] = register_experiment(store, summary)
    (output_root / "summary.json").write_text(
        canonical_json(summary), encoding="utf-8"
    )
    return summary


__all__ = [
    "DEFAULT_ALPHA",
    "DEFAULT_MIN_SAMPLE_SIZE",
    "EXPERIMENT_SCHEMA_VERSION",
    "SOLVER_SCHEMA_VERSION",
    "Solver",
    "load_solver",
    "run_experiment",
    "run_round",
]
