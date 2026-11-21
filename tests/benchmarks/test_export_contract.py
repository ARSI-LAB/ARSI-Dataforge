"""R7 probes: export contract alignment (suite policy, degradation, capabilities)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from data_designer.integrations.evaluator.export import export_evaluator
from data_designer.integrations.gym.export import export_gym
from data_designer.pipelines.benchmarks.suite import create_suite
from tests.benchmarks.benchmark_review import approve_fixture_suite
from tests.benchmarks.test_eval_policy import _synthetic_taskset
from tests.benchmarks.test_report import build_workbuddy_suite


def _pin_version(suite):
    return f"{suite['ref_id']}@{suite['version']}"


def test_suite_export_records_policy_and_suite_sha(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    approve_fixture_suite(store, suite, tmp_path)

    output = tmp_path / "export"
    value = export_evaluator(
        output=output, catalog=store.uri, suite=_pin_version(suite)
    )
    assert value["degraded"] == []
    capabilities = json.loads((output / "capabilities.json").read_text())
    assert capabilities["source_suite_sha256"] == suite["suite_sha256"]
    assert capabilities["source_digest"].startswith("sha256:")
    assert capabilities["policy"]["repeats"] == 1
    assert capabilities["policy"]["aggregation"] == "micro"
    assert capabilities["policy"]["weights"] == suite["weights"]
    assert capabilities["policy"]["timeout_seconds"] == suite["timeout_seconds"]
    assert capabilities["degraded"] == []
    assert not (output / "degraded.json").exists()

    package = json.loads((output / "package" / "manifest.json").read_text())
    assert package["protocol_version"] == "dataforge-benchmark-v1"
    config = json.loads((output / "benchmark.json").read_text())
    assert config["benchmark"]["config"]["digest"] == capabilities["source_digest"]


def test_repeats_require_explicit_degradation(tmp_path, monkeypatch):
    store, seed_suite = build_workbuddy_suite(tmp_path, monkeypatch)
    suite = create_suite(
        store, "repeat-export", [seed_suite["tasksets"][0]], repeats=2, seed=9
    )
    approve_fixture_suite(store, suite, tmp_path)

    output = tmp_path / "rejected"
    with pytest.raises(ValueError, match="allow_degraded"):
        export_evaluator(output=output, catalog=store.uri, suite=_pin_version(suite))
    assert not output.exists()

    degraded_output = tmp_path / "degraded"
    value = export_evaluator(
        output=degraded_output,
        catalog=store.uri,
        suite=_pin_version(suite),
        allow_degraded=True,
    )
    assert value["degraded"] == ["repeats=2 -> 1"]
    capabilities = json.loads((degraded_output / "capabilities.json").read_text())
    assert capabilities["degraded"] == ["repeats=2 -> 1"]
    assert capabilities["attempts"] == 1
    assert capabilities["policy"]["seed"] == 9
    assert json.loads((degraded_output / "degraded.json").read_text())["degraded"] == [
        "repeats=2 -> 1"
    ]


def test_multi_family_suite_must_export_per_family(tmp_path, monkeypatch):
    store, workbuddy_suite = build_workbuddy_suite(tmp_path, monkeypatch)
    pinned = workbuddy_suite["tasksets"][0]
    runtime = next(
        iter(
            store.get_artifact("taskset", pinned["ref_id"], pinned["version"])[
                "payload"
            ]["runtime_by_task"].values()
        )
    )
    terminal = _synthetic_taskset(
        store, "export-terminal", "terminal", runtime, ["Terminal one."]
    )
    suite = create_suite(
        store,
        "multi-family-export",
        [
            pinned,
            {
                "ref_id": "export-terminal",
                "version": 1,
                "fingerprint": terminal["taskset_fingerprint"],
            },
        ],
        weights={"workbuddy": 0.5, "terminal": 0.5},
    )
    approve_fixture_suite(store, suite, tmp_path)
    output = tmp_path / "multi"
    with pytest.raises(ValueError, match="one family per export"):
        export_evaluator(output=output, catalog=store.uri, suite=_pin_version(suite))
    assert not output.exists()


def test_manifest_and_suite_are_mutually_exclusive(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    approve_fixture_suite(store, suite, tmp_path)
    with pytest.raises(ValueError, match="exactly one"):
        export_evaluator(
            manifest=tmp_path / "package",
            output=tmp_path / "both",
            catalog=store.uri,
            suite=_pin_version(suite),
        )
    with pytest.raises(ValueError, match="exactly one"):
        export_evaluator(output=tmp_path / "neither")
    with pytest.raises(ValueError, match="requires catalog"):
        export_evaluator(output=tmp_path / "no-catalog", suite=_pin_version(suite))


def test_gym_rejects_unsupported_family_with_supported_set(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    from data_designer.pipelines.benchmarks.package import freeze_package

    pin = suite["tasksets"][0]
    payload = store.get_artifact("taskset", pin["ref_id"], pin["version"])["payload"]
    package = freeze_package(payload, tmp_path / "package")
    host_config = tmp_path / "host.json"
    host_config.write_text("{}")
    output = tmp_path / "gym"
    with pytest.raises(ValueError) as error:
        export_gym(
            package["manifest_uri"],
            output,
            config_paths=[str(host_config)],
        )
    message = str(error.value)
    for family in ("health_qa", "knowledge_qa", "reasoning_qa"):
        assert family in message
    assert not output.exists()


def test_cli_gym_export_does_not_forward_evaluator_default_flag(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    from data_designer.pipelines.benchmarks.package import freeze_package

    pin = suite["tasksets"][0]
    payload = store.get_artifact("taskset", pin["ref_id"], pin["version"])["payload"]
    frozen = freeze_package(payload, tmp_path / "package")
    host_config = tmp_path / "host.json"
    host_config.write_text("{}")
    config = {
        "manifest": Path(frozen["manifest_uri"]).relative_to(tmp_path).as_posix(),
        "output": "gym",
        "config_paths": ["host.json"],
    }
    config_path = tmp_path / "export-gym.json"
    config_path.write_text(json.dumps(config))
    result = subprocess.run(
        [sys.executable, "-m", "data_designer.cli.main", "export", "gym",
         "--config", str(config_path)],
        capture_output=True, text=True, cwd=tmp_path,
    )
    # validate_config injects allow_degraded=false; it must not reach the Gym
    # adapter as an unexpected keyword, so the host-specific error survives.
    assert result.returncode == 1
    assert "unexpected keyword argument" not in result.stderr
    for family in ("health_qa", "knowledge_qa", "reasoning_qa"):
        assert family in result.stderr


def test_cli_suite_export_and_config_validation(tmp_path, monkeypatch):
    store, suite = build_workbuddy_suite(tmp_path, monkeypatch)
    approve_fixture_suite(store, suite, tmp_path)
    config = {
        "catalog": Path(store.uri).name,
        "suite": _pin_version(suite),
        "output": "cli-export",
    }
    config_path = tmp_path / "export.json"
    config_path.write_text(json.dumps(config))
    result = subprocess.run(
        [sys.executable, "-m", "data_designer.cli.main", "export", "evaluator",
         "--config", str(config_path)],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["degraded"] == []
    assert (tmp_path / "cli-export" / "capabilities.json").is_file()

    both = {**config, "manifest": "package", "output": "conflict"}
    config_path.write_text(json.dumps(both))
    result = subprocess.run(
        [sys.executable, "-m", "data_designer.cli.main", "export", "evaluator",
         "--config", str(config_path)],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert result.returncode == 1
    assert "exactly one" in result.stderr
