"""No-Docker contracts for solver execution and multi-round orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from data_designer.pipelines.benchmarks import experiment
from data_designer.pipelines.benchmarks.experiment import (
    CandidateFailure,
    _paired_deltas,
    _sign_test_p_value,
    load_solver,
    run_experiment,
)
from data_designer.storage.catalog import ArtifactCatalog


def _solver_script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "solver.py"
    path.write_text(body, encoding="utf-8")
    return path


def test_solver_protocol_accepts_acknowledged_attempt(tmp_path):
    solver = _solver_script(
        tmp_path,
        "import json,sys\n"
        "req=json.load(open(sys.argv[1]))\n"
        "open(sys.argv[2],'w').write(json.dumps({"
        "'schema_version':'benchmark-solver-v1',"
        "'task_id':req['taskId'],"
        "'attempt_index':req['attempt']['index'],"
        "'seed':req['attempt']['seed'],"
        "'solver_version':'unit-solver-v1'}))\n",
    )
    workspace = tmp_path / "workspace"
    candidate = tmp_path / "candidate"
    workspace.mkdir()
    candidate.mkdir()
    loaded = load_solver(solver, timeout=30)
    info = loaded({"task_id": "t1"}, workspace, candidate, {"index": 0, "seed": 7})
    assert info["solver_version"] == "unit-solver-v1"
    assert info["solver_sha256"] == loaded.sha256


def test_solver_failure_maps_to_candidate_failure(tmp_path):
    solver = _solver_script(tmp_path, "raise SystemExit(3)\n")
    workspace = tmp_path / "workspace"
    candidate = tmp_path / "candidate"
    workspace.mkdir()
    candidate.mkdir()
    loaded = load_solver(solver, timeout=30)
    with pytest.raises(CandidateFailure) as error:
        loaded({"task_id": "t1"}, workspace, candidate, {"index": 0, "seed": 0})
    assert error.value.category == "failed"


def test_sign_test_and_paired_deltas():
    assert _sign_test_p_value(6, 0) == pytest.approx(0.03125)
    assert _sign_test_p_value(0, 0) == 1.0

    baseline = {
        "items": [
            {
                "task_id": "t1",
                "family": "swe",
                "status": "failed",
                "attempts": [{"score": 0.0, "passed": False}],
            }
        ]
    }
    candidate = {
        "items": [
            {
                "task_id": "t1",
                "family": "swe",
                "status": "passed",
                "attempts": [{"score": 1.0, "passed": True}],
            }
        ]
    }
    rows = _paired_deltas(baseline, candidate)
    assert rows == [
        {
            "task_id": "t1",
            "family": "swe",
            "baseline_score": 0.0,
            "candidate_score": 1.0,
            "score_delta": 1.0,
            "baseline_pass_rate": 0.0,
            "candidate_pass_rate": 1.0,
            "pass_rate_delta": 1.0,
        }
    ]


def _record(identifier: str, task_ids, passed: bool):
    return {
        "schema_version": "benchmark-eval-record-v2",
        "suite_sha256": "a" * 64,
        "suite_ref": {"ref_id": "suite", "version": 1},
        "tasksets": [{"family": "swe", "ref_id": "t", "version": 1}],
        "split": "development",
        "tasks": list(task_ids),
        "repeats": 1,
        "seed": 0,
        "policy": {"weights": {"swe": 1.0}},
        "runtime": {
            "verifier_versions": {task: "v" for task in task_ids},
            "harness_versions": {task: ["2.1.7"] for task in task_ids},
            "protocols": {task: ["swebench_resolved_0_1"] for task in task_ids},
        },
        "items": [
            {
                "task_id": task,
                "family": "swe",
                "status": "passed" if passed else "failed",
                "attempts": [
                    {"score": 1.0 if passed else 0.0, "passed": passed}
                ],
            }
            for task in task_ids
        ],
        "metrics": {
            "macro_pass_rate": 1.0 if passed else 0.0,
            "micro_pass_rate": 1.0 if passed else 0.0,
            "infra_failed_total": 0,
            "cancelled_total": 0,
        },
        "candidate": {"solver_sha256": identifier},
    }


def test_run_experiment_writes_eval_records_comparison_and_summary(tmp_path, monkeypatch):
    task_ids = [f"t{index}" for index in range(1, 7)]
    records = {
        "baseline": _record("a", task_ids, False),
        "evolved": _record("b", task_ids, True),
    }

    def fake_round(store, suite, solver, round_root, **kwargs):
        round_root = Path(round_root)
        round_root.mkdir(parents=True, exist_ok=True)
        return records[str(solver)]

    class FakeComparison:
        macro_baseline = 0.0
        macro_candidate = 1.0
        macro_delta = 1.0
        micro_baseline = 0.0
        micro_candidate = 1.0
        micro_delta = 1.0
        changed_tasks = [
            {"task_id": task, "family": "swe", "baseline": "failed", "candidate": "passed"}
            for task in task_ids
        ]
        infra_summary = {"baseline": [], "candidate": []}
        graded_totals = {"baseline": 6, "candidate": 6}

        def to_dict(self):
            return {
                "macro_baseline": self.macro_baseline,
                "macro_candidate": self.macro_candidate,
                "macro_delta": self.macro_delta,
                "micro_baseline": self.micro_baseline,
                "micro_candidate": self.micro_candidate,
                "micro_delta": self.micro_delta,
                "changed_tasks": self.changed_tasks,
                "infra_summary": self.infra_summary,
                "graded_totals": self.graded_totals,
            }

        def write_json(self, path):
            Path(path).write_text(json.dumps(self.to_dict()), encoding="utf-8")

    monkeypatch.setattr(experiment, "run_round", fake_round)
    monkeypatch.setattr(experiment, "compare_evaluations", lambda a, b: FakeComparison())
    suite = {"ref_id": "suite", "version": 1, "suite_sha256": "a" * 64,
             "split": "development", "repeats": 1, "seed": 0}
    summary = run_experiment(
        None,
        suite,
        [{"id": "baseline", "solver": "baseline"}, {"id": "evolved", "solver": "evolved"}],
        tmp_path / "out",
        min_sample_size=2,
    )
    assert (tmp_path / "out" / "rounds" / "baseline" / "eval-record.json").is_file()
    assert (tmp_path / "out" / "rounds" / "evolved" / "eval-record.json").is_file()
    assert (tmp_path / "out" / "comparison.json").is_file()
    assert (tmp_path / "out" / "summary.json").is_file()
    assert summary["rounds"][1]["metrics"]["macro_pass_rate"] == 1.0
    assert summary["decision"]["decision"] == "promote"


def test_run_experiment_registers_an_immutable_artifact(tmp_path, monkeypatch):
    task_ids = [f"t{index}" for index in range(1, 7)]
    records = {
        "baseline": _record("a", task_ids, False),
        "evolved": _record("b", task_ids, True),
    }

    def fake_round(store, suite, solver, round_root, **kwargs):
        round_root = Path(round_root)
        round_root.mkdir(parents=True, exist_ok=True)
        return records[str(solver)]

    monkeypatch.setattr(experiment, "run_round", fake_round)
    suite = {
        "ref_id": "suite",
        "version": 1,
        "suite_sha256": "a" * 64,
        "split": "development",
        "repeats": 1,
        "seed": 0,
        "tasksets": [{"family": "swe", "ref_id": "t", "version": 1}],
        "weights": {"swe": 1.0},
    }
    store = ArtifactCatalog(str(tmp_path / "catalog"))
    summary = run_experiment(
        store,
        suite,
        [{"id": "baseline", "solver": "baseline"}, {"id": "evolved", "solver": "evolved"}],
        tmp_path / "out",
        min_sample_size=2,
    )
    artifact = summary["artifact"]
    registered = store.get_artifact(
        "benchmark_experiment", artifact["ref_id"], artifact["version"]
    )["payload"]
    assert registered["experiment_sha256"] == artifact["experiment_sha256"]
    assert registered["decision"]["decision"] == "promote"
    assert registered["suite"]["suite_sha256"] == suite["suite_sha256"]
