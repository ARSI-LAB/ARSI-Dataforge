"""R10 probes: offline examples and the full build -> evaluate -> export chain."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "benchmark_acceptance_e2e.py"


def _env():
    return {**os.environ, "PYTHONPATH": str(ROOT)}


def _acceptance(work: Path, example: str):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--example", example, "--work", str(work)],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        text=True,
    )


def _summary(result):
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout)


@pytest.mark.parametrize("example", ["swe-mini", "seed-mini"])
def test_acceptance_chain_is_complete_and_offline(tmp_path, example):
    summary = _summary(_acceptance(tmp_path / example, example))
    assert summary["rounds"]["reference"]["macro_pass_rate"] == 1.0
    assert summary["rounds"]["baseline"]["macro_pass_rate"] == 0.0
    for round_name in ("reference", "baseline"):
        assert summary["rounds"][round_name]["infra_failed_total"] == 0
        assert Path(summary["rounds"][round_name]["record"]).is_file()
    assert summary["macro_delta"] == 1.0
    assert summary["export"]["source_suite_sha256"] == summary["suite"]["suite_sha256"]
    assert summary["export"]["aggregation"] == "micro"
    assert summary["export"]["degraded"] == []
    assert summary["suite"]["visibility"] == "experiment"

    work = Path(summary["work"])
    assert Path(summary["manifest_uri"]).is_file()
    assert (work / "comparison.json").is_file()
    assert (Path(summary["export"]["output"]) / "benchmark.json").is_file()
    assert (Path(summary["export"]["output"]) / "package" / "manifest.json").is_file()
    capabilities = json.loads(
        (Path(summary["export"]["output"]) / "capabilities.json").read_text()
    )
    assert capabilities["policy"] == {
        "repeats": 1,
        "seed": 0,
        "weights": {"workbuddy": 1.0},
        "aggregation": "micro",
        "timeout_seconds": 600,
    }

    if example == "seed-mini":
        assert summary["source_grade_counts"] == {
            "official_native": 1,
            "owned_native": 1,
        }
        assert len(summary["tasks"]) == 2
        assert any("seed:customer-001" in task for task in summary["tasks"])


def test_acceptance_evaluation_is_repeatable(tmp_path):
    summary = _summary(_acceptance(tmp_path / "run", "swe-mini"))
    from data_designer.pipelines.benchmarks.api import evaluate, load_suite

    handle = load_suite(
        Path(summary["work"]) / "catalog",
        summary["suite"]["ref_id"],
        summary["suite"]["version"],
    )
    first = json.loads(
        Path(summary["rounds"]["reference"]["record"]).read_text()
    )
    second = evaluate(
        handle, ROOT / "examples/benchmarks/swe-mini/candidates/reference"
    )
    assert first["tasks"] == second["tasks"]
    for left, right in zip(first["items"], second["items"]):
        assert left["status"] == right["status"] == "passed"
        assert left["pass_at_1"] == right["pass_at_1"] == 1.0
        assert [
            (attempt["index"], attempt["seed"]) for attempt in left["attempts"]
        ] == [
            (attempt["index"], attempt["seed"]) for attempt in right["attempts"]
        ]
    assert second["metrics"]["macro_pass_rate"] == 1.0


def test_readme_build_command_runs_from_cli(tmp_path):
    prepared = subprocess.run(
        [sys.executable, str(ROOT / "examples/benchmarks/swe-mini/prepare.py"),
         "--work", str(tmp_path / "run")],
        cwd=ROOT, env=_env(), capture_output=True, text=True,
    )
    assert prepared.returncode == 0, prepared.stderr[-2000:]
    result = subprocess.run(
        [sys.executable, "-m", "data_designer.cli.main", "benchmark", "build",
         "--config", str(tmp_path / "run" / "job.yaml")],
        cwd=ROOT, env=_env(), capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    body = json.loads(result.stdout)
    assert Path(body["manifest_uri"]).is_file()
    assert body["digest"].startswith("sha256:")
