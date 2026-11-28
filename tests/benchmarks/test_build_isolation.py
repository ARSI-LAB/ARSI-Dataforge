"""R4/R2/R6/R9 probes: item quarantine, seed ledger, build metrics, checkpoint.

The fixture is the workbuddy artifact-compare chain used elsewhere in this
suite: a manifest pins a real runner invocation against reference/negative
artifact roots, so native audit takes the ordinary subprocess path.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

import data_designer.pipelines.benchmarks.build as builder
from data_designer.pipelines.benchmarks.contracts import (
    BenchmarkError,
    file_sha256,
)
from data_designer.storage.catalog import ArtifactCatalog


@pytest.fixture(autouse=True)
def quality_gate(monkeypatch):
    monkeypatch.setattr(
        builder, "assess_taskset", lambda items: {"quality_overall_score": 1.0}
    )


def _sha(fill: str) -> str:
    return fill * 64


def _runner_runtime(tmp_path, name="runtime"):
    from data_designer.pipelines.benchmarks.runners import workbuddy

    runner = Path(workbuddy.__file__).resolve()
    environment = tmp_path / f"environment-{name}.json"
    environment.write_text("{}")
    return {
        "environment": {"path": str(environment), "sha256": file_sha256(environment)},
        "verifier": {
            "path": str(runner),
            "sha256": file_sha256(runner),
            "command": [sys.executable, str(runner)],
            "version": workbuddy.VERSION,
        },
        "assets": [],
    }


def _audit_roots(tmp_path, name, total):
    reference = tmp_path / f"reference-{name}"
    negative = tmp_path / f"negative-{name}"
    for root, value in ((reference, {"total": 42}), (negative, {"total": total})):
        (root / "output").mkdir(parents=True, exist_ok=True)
        (root / "output/result.json").write_text(json.dumps(value))
    return str(reference), str(negative)


def _variant(
    source_task_id,
    *,
    task_text,
    runtime_args,
    variant_id="v1",
    success_criteria="Write output/result.json with the computed total.",
):
    reference, negative = runtime_args
    return {
        "source_task_id": source_task_id,
        "variant_id": variant_id,
        "operation": "mutation",
        "public": {
            "task_text": task_text,
            "success_criteria": success_criteria,
            "tools_available": ["files"],
            "difficulty": "easy",
        },
        "private": {
            "initial_state_sha256": _sha("c"),
            "expected_state_sha256": _sha("d"),
            "verifier_spec": {
                "kind": "artifact_compare",
                "generator_ref": "owned-fixture@1",
                "payload": {
                    "artifact": {"path": "output/result.json"},
                    "gold": {"total": 42},
                },
            },
        },
        "audit": {"reviewed": True, "reference_root": reference, "negative_root": negative},
    }


def _workbuddy_fixture(tmp_path, variants, *, runtime=None, cases=None):
    runtime = runtime or _runner_runtime(tmp_path)
    cases = cases if cases is not None else [
        {
            "source_task_id": variant["source_task_id"],
            "task_text": f"Original {variant['source_task_id']} exercise.",
            "initial_state_sha256": _sha("a"),
            "expected_state_sha256": _sha("b"),
        }
        for variant in variants
    ]
    source = {
        "schema_version": "benchmark-source-v1",
        "family": "workbuddy",
        "release": "fixture",
        "split": "development",
        "license": {"id": "owned-fixture", "derive_allowed": True},
        "cases": cases,
        **runtime,
    }
    config = {
        "schema_version": "benchmark-augment-v1",
        "family": "workbuddy",
        "release": "fixture",
        "split": "development",
        "variants": variants,
    }
    source_path = tmp_path / "source.json"
    config_path = tmp_path / "augment.json"
    source_path.write_text(json.dumps(source))
    config_path.write_text(json.dumps(config))
    return source_path, config_path


def _build(tmp_path, variants, *, store=None, ref_id="fixture-ref", **kwargs):
    source_path, config_path = _workbuddy_fixture(tmp_path, variants)
    store = store or ArtifactCatalog()
    return builder.build_benchmark_taskset(
        store,
        str(source_path),
        str(config_path),
        str(tmp_path / "trusted"),
        ref_id,
        **kwargs,
    )


def _single(tmp_path, *, task_text="Compute the total.", negative_total=0, **kwargs):
    runtime_args = _audit_roots(tmp_path, "one", negative_total)
    variant = _variant("acct-1", task_text=task_text, runtime_args=runtime_args)
    return _build(tmp_path, [variant], **kwargs)


def test_duplicate_text_is_quarantined_and_others_publish(tmp_path):
    store = ArtifactCatalog()
    first_roots = _audit_roots(tmp_path, "first", 0)
    second_roots = _audit_roots(tmp_path, "second", 0)
    variants = [
        _variant("acct-1", task_text="Same prompt text.", runtime_args=first_roots),
        _variant("acct-2", task_text="Same prompt text.", runtime_args=second_roots),
    ]
    result = _build(tmp_path, variants, store=store)
    payload = store.get_artifact("taskset", "fixture-ref")["payload"]

    assert payload["item_count"] == 1
    assert result["build_report"]["accepted_count"] == 1
    assert result["build_report"]["quarantined_count"] == 1
    assert payload["quarantine"]["by_reason"] == {"duplicate_text": 1}
    quarantine_path = Path(payload["quarantine"]["path"])
    assert file_sha256(quarantine_path) == payload["quarantine"]["sha256"]
    body = json.loads(quarantine_path.read_text())
    assert body["records"][0]["task_id"].endswith(":acct-2:v1")


def test_negative_passed_item_is_quarantined(tmp_path):
    store = ArtifactCatalog()
    good_roots = _audit_roots(tmp_path, "good", 0)
    bad_roots = _audit_roots(tmp_path, "bad", 42)  # negative equals the gold
    variants = [
        _variant("acct-1", task_text="Good prompt.", runtime_args=good_roots),
        _variant("acct-2", task_text="Bad prompt.", runtime_args=bad_roots),
    ]
    result = _build(tmp_path, variants, store=store)
    payload = store.get_artifact("taskset", "fixture-ref")["payload"]

    assert payload["item_count"] == 1
    assert payload["quarantine"]["by_reason"] == {"native_negative_passed": 1}
    assert payload["quarantine"]["task_ids"][0].endswith(":acct-2:v1")

    with pytest.raises(BenchmarkError, match="positive/negative native audit failed"):
        _build(tmp_path, variants, on_item_rejection="fail")


def test_private_boundary_violation_does_not_leak_values(tmp_path):
    store = ArtifactCatalog()
    good_roots = _audit_roots(tmp_path, "leak-good", 0)
    leak_roots = _audit_roots(tmp_path, "leak", 0)
    variants = [
        _variant("acct-1", task_text="Clean prompt.", runtime_args=good_roots),
        _variant(
            "acct-2", task_text="Leaky prompt.", runtime_args=leak_roots,
            success_criteria=" ",
        ),
    ]
    result = _build(tmp_path, variants, store=store)
    payload = store.get_artifact("taskset", "fixture-ref")["payload"]
    assert payload["item_count"] == 1
    assert payload["quarantine"]["by_reason"] == {
        "private_boundary_violation": 1
    }
    quarantine_text = Path(payload["quarantine"]["path"]).read_text()
    assert str(leak_roots[0]) not in quarantine_text


def test_all_items_quarantined_leaves_no_taskset(tmp_path):
    store = ArtifactCatalog()
    roots = _audit_roots(tmp_path, "only", 42)
    variant = _variant("acct-1", task_text="Bad prompt.", runtime_args=roots)

    with pytest.raises(BenchmarkError, match="no accepted benchmark items remain"):
        _build(tmp_path, [variant], store=store)
    assert store.list_artifacts("taskset") == []


def test_published_text_collision_is_rejected(tmp_path):
    catalog_dir = tmp_path / "catalog"
    store = ArtifactCatalog(str(catalog_dir))
    roots = _audit_roots(tmp_path, "collision", 0)
    variant = _variant("acct-1", task_text="Published prompt.", runtime_args=roots)
    _build(tmp_path, [variant], store=store, ref_id="first")
    assert store.get_artifact("taskset", "first")["payload"]["item_count"] == 1

    # Same text, new source case and ref: must not be published twice.
    second_roots = _audit_roots(tmp_path, "collision-2", 0)
    second = _variant("acct-2", task_text="Published prompt.", runtime_args=second_roots)
    with pytest.raises(BenchmarkError, match="no accepted benchmark items remain"):
        _build(tmp_path, [second], store=store, ref_id="second")
    assert store.get_artifact("taskset", "second") is None


def test_curator_removal_counts_as_quarantine(tmp_path, monkeypatch):
    store = ArtifactCatalog()
    roots = _audit_roots(tmp_path, "curator", 0)
    variant = _variant("acct-1", task_text="Curated prompt.", runtime_args=roots)
    removed = "workbuddy:fixture:acct-1:v1"
    monkeypatch.setattr(
        builder,
        "assess_taskset",
        lambda items: {
            "quality_overall_score": 1.0,
            "curator_removed_task_ids": [removed],
        },
    )
    with pytest.raises(BenchmarkError, match="after deduplication"):
        _build(tmp_path, [variant], store=store)
    assert store.list_artifacts("taskset") == []


def test_build_metrics_and_operations(tmp_path):
    roots_one = _audit_roots(tmp_path, "metric-1", 0)
    roots_two = _audit_roots(tmp_path, "metric-2", 0)
    variants = [
        _variant(
            "acct-1",
            task_text="Metric one.",
            runtime_args=roots_one,
            variant_id="v1",
        ),
        _variant(
            "acct-1",
            task_text="Metric two.",
            runtime_args=roots_two,
            variant_id="v2",
        ),
    ]
    source_path, config_path = _workbuddy_fixture(
        tmp_path,
        variants,
        cases=[
            {
                "source_task_id": "acct-1",
                "task_text": "Original acct-1 exercise.",
                "initial_state_sha256": _sha("a"),
                "expected_state_sha256": _sha("b"),
            }
        ],
    )
    # One seed (the shared source case) yields two accepted variants.
    store = ArtifactCatalog()
    result = builder.build_benchmark_taskset(
        store,
        str(source_path),
        str(config_path),
        str(tmp_path / "trusted"),
        "metrics",
    )
    report = result["build_report"]
    assert report["seed_count"] == 1
    assert report["accepted_count"] == 2
    assert report["expansion_ratio"] == 2.0
    assert report["admission_rate"] == 1.0
    assert report["operations"] == {"mutation": {"input": 2, "accepted": 2, "quarantined": 0}}
    assert report["grid"]["dimensions"] == ["family", "difficulty"]
    assert report["grid"]["accepted"] == {"workbuddy/easy": 2}
    assert report["gates"] == {
        "min_expansion_ratio": None,
        "expansion_passed": None,
        "require_grid_gain": False,
        "grid_gain_passed": None,
    }

    with pytest.raises(BenchmarkError, match="expansion ratio"):
        builder.build_benchmark_taskset(
            ArtifactCatalog(),
            str(source_path),
            str(config_path),
            str(tmp_path / "trusted"),
            "metrics-gated",
            min_expansion_ratio=3.0,
        )

    # Variants only cover the already-seeded difficulty cell, so the grid gain
    # is zero and the optional gate must reject the build.
    with pytest.raises(BenchmarkError, match="grid coverage gain"):
        builder.build_benchmark_taskset(
            ArtifactCatalog(),
            str(source_path),
            str(config_path),
            str(tmp_path / "trusted"),
            "metrics-grid-gated",
            require_grid_gain=True,
        )


def test_checkpoint_reuses_accepted_and_quarantined_outcomes(tmp_path, monkeypatch):
    calls = []
    real_run_case = builder.run_case

    def counted(*args, **kwargs):
        calls.append(args[1])
        return real_run_case(*args, **kwargs)

    monkeypatch.setattr(builder, "run_case", counted)
    checkpoint = tmp_path / "trusted" / "checkpoint.jsonl"
    good_roots = _audit_roots(tmp_path, "cp-good", 0)
    bad_roots = _audit_roots(tmp_path, "cp-bad", 42)
    variants = [
        _variant("acct-1", task_text="Checkpoint good.", runtime_args=good_roots),
        _variant("acct-2", task_text="Checkpoint bad.", runtime_args=bad_roots),
    ]
    first = _build(
        tmp_path,
        variants,
        store=ArtifactCatalog(),
        checkpoint=checkpoint,
        ref_id="cp-first",
    )
    first_calls = len(calls)
    assert first_calls == 4  # reference + negative for each variant
    assert first["build_report"]["accepted_count"] == 1

    second = _build(
        tmp_path,
        variants,
        store=ArtifactCatalog(),
        checkpoint=checkpoint,
        ref_id="cp-second",
    )
    assert len(calls) == first_calls  # no re-audit
    assert second["taskset_sha256"] == first["taskset_sha256"]
    # Enabling the checkpoint must not change the frozen taskset bytes.
    third = _build(
        tmp_path,
        variants,
        store=ArtifactCatalog(),
        ref_id="cp-third",
    )
    assert third["taskset_sha256"] == first["taskset_sha256"]
    entries = [
        json.loads(line)
        for line in checkpoint.read_text().splitlines()
        if line.strip()
    ]
    assert {entry["outcome"] for entry in entries} == {"accepted", "quarantined"}


def test_checkpoint_tolerates_half_line_and_pin_drift(tmp_path, monkeypatch):
    calls = []
    real_run_case = builder.run_case

    def counted(*args, **kwargs):
        calls.append(args[1])
        return real_run_case(*args, **kwargs)

    monkeypatch.setattr(builder, "run_case", counted)
    checkpoint = tmp_path / "trusted" / "checkpoint.jsonl"
    roots = _audit_roots(tmp_path, "torn", 0)
    variant = _variant("acct-1", task_text="Torn prompt.", runtime_args=roots)
    _build(tmp_path, [variant], checkpoint=checkpoint)
    calls_before = len(calls)

    with checkpoint.open("a", encoding="utf-8") as stream:
        stream.write('{"schema_version":"benchmark-checkpoint-v1","case_key":')
    _build(tmp_path, [variant], checkpoint=checkpoint)
    assert len(calls) == calls_before  # torn tail ignored, entry reused

    source_path, _ = _workbuddy_fixture(tmp_path, [variant])
    source_path.write_text(source_path.read_text() + "\n")
    store = ArtifactCatalog()
    builder.build_benchmark_taskset(
        store,
        str(source_path),
        str(tmp_path / "augment.json"),
        str(tmp_path / "trusted"),
        "drifted",
        checkpoint=checkpoint,
    )
    assert len(calls) == calls_before + 2  # source pin drift invalidates the key

    # Appends after a torn tail must remain standalone lines: the repair drops
    # the fragment so the first post-repair entry is readable on restart.
    entries = [
        json.loads(line)
        for line in checkpoint.read_text().splitlines()
        if line.strip()
    ]
    assert len(entries) == 2
    assert all(isinstance(entry.get("case_key"), str) for entry in entries)


def test_audit_concurrency_keeps_declaration_order(tmp_path, monkeypatch):
    real_run_case = builder.run_case

    def slow_first(*args, **kwargs):
        # Reverse completion order without changing any audit result.
        time.sleep(0.05 if args[1].endswith(":acct-1:v1") else 0.0)
        return real_run_case(*args, **kwargs)

    monkeypatch.setattr(builder, "run_case", slow_first)
    variants = []
    for index in range(1, 5):
        roots = _audit_roots(tmp_path, f"conc-{index}", 0)
        variants.append(
            _variant(
                f"acct-{index}",
                task_text=f"Concurrent prompt {index}.",
                runtime_args=roots,
            )
        )
    serial = _build(tmp_path, variants, ref_id="serial")
    concurrent_store = ArtifactCatalog()
    concurrent = _build(
        tmp_path,
        variants,
        store=concurrent_store,
        ref_id="concurrent",
        audit_concurrency=4,
    )
    assert serial["taskset_sha256"] == concurrent["taskset_sha256"]
    order = [
        item["task_id"]
        for item in concurrent_store.get_artifact("taskset", "concurrent")["payload"][
            "taskset"
        ]
    ]
    assert order == [f"workbuddy:fixture:acct-{i}:v1" for i in range(1, 5)]


def test_checkpoint_under_temporary_trusted_dir_warns(tmp_path):
    roots = _audit_roots(tmp_path, "warn", 0)
    variant = _variant("acct-1", task_text="Warning prompt.", runtime_args=roots)
    with pytest.warns(RuntimeWarning, match="temporary trusted directory"):
        _build(
            tmp_path,
            [variant],
            checkpoint=tmp_path / "trusted" / "checkpoint.jsonl",
        )


def test_interrupted_build_registers_no_artifact(tmp_path, monkeypatch):
    real_run_case = builder.run_case

    def interrupt(*args, **kwargs):
        if args[1].endswith(":acct-2:v1"):
            raise RuntimeError("simulated interruption")
        return real_run_case(*args, **kwargs)

    monkeypatch.setattr(builder, "run_case", interrupt)
    variants = [
        _variant(
            "acct-1",
            task_text="Interrupt good.",
            runtime_args=_audit_roots(tmp_path, "int-good", 0),
        ),
        _variant(
            "acct-2",
            task_text="Interrupt bad.",
            runtime_args=_audit_roots(tmp_path, "int-bad", 0),
        ),
    ]
    store = ArtifactCatalog()
    checkpoint = tmp_path / "trusted" / "checkpoint.jsonl"
    with pytest.raises(RuntimeError, match="simulated interruption"):
        _build(tmp_path, variants, store=store, ref_id="interrupted",
               checkpoint=checkpoint)
    assert store.get_artifact("taskset", "interrupted", 1) is None
    assert checkpoint.is_file()


def test_seed_ledger_merges_owned_and_official_cases(tmp_path):
    runtime = _runner_runtime(tmp_path)
    seed_rows = [
        {"id": "acct-001", "task_text": "Owned seed one.", "success_criteria": "One."},
        {"id": "acct-002", "task_text": "Owned seed two.", "success_criteria": "Two."},
    ]
    seed_path = tmp_path / "seeds.jsonl"
    seed_path.write_text("".join(json.dumps(row) + "\n" for row in seed_rows))
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "benchmark-runtime-registry-v1",
                "cases": {"acct-001": runtime, "acct-002": runtime},
            }
        )
    )
    official = {
        "source_task_id": "official-1",
        "task_text": "Official exercise.",
        "initial_state_sha256": _sha("a"),
        "expected_state_sha256": _sha("b"),
        "runtime": runtime,
    }
    source = {
        "schema_version": "benchmark-source-v1",
        "family": "workbuddy",
        "release": "fixture",
        "split": "development",
        "license": {"id": "owned-fixture", "derive_allowed": True},
        "seeds": {
            "path": str(seed_path),
            "sha256": file_sha256(seed_path),
            "runtime_registry": {
                "path": str(registry_path),
                "sha256": file_sha256(registry_path),
            },
            "materialization": {
                "cases": {
                    "acct-001": {
                        "initial_state_sha256": _sha("1"),
                        "expected_state_sha256": _sha("2"),
                    },
                    "acct-002": {
                        "initial_state_sha256": _sha("3"),
                        "expected_state_sha256": _sha("4"),
                    },
                }
            },
            "passthrough_columns": ["customer_tag"],
        },
        "cases": [official],
    }
    variants = []
    for index, source_task_id in enumerate(
        ("seed:acct-001", "seed:acct-002", "official-1")
    ):
        roots = _audit_roots(tmp_path, f"seed-{index}", 0)
        variants.append(
            _variant(
                source_task_id,
                task_text=f"Derived prompt {index}.",
                runtime_args=roots,
                variant_id=f"v{index}",
            )
        )
    source_path = tmp_path / "source-seeds.json"
    source_path.write_text(json.dumps(source))
    config = {
        "schema_version": "benchmark-augment-v1",
        "family": "workbuddy",
        "release": "fixture",
        "split": "development",
        "variants": variants,
    }
    config_path = tmp_path / "augment-seeds.json"
    config_path.write_text(json.dumps(config))

    store = ArtifactCatalog()
    result = builder.build_benchmark_taskset(
        store, str(source_path), str(config_path), str(tmp_path / "trusted"), "seeded"
    )
    payload = store.get_artifact("taskset", "seeded")["payload"]
    assert payload["item_count"] == 3
    assert payload["source_grade_counts"] == {
        "owned_native": 2,
        "official_native": 1,
    }
    ledger_path = Path(payload["source_ledger_path"])
    assert file_sha256(ledger_path) == payload["source_ledger_sha256"]
    assert payload["seed_rows_sha256"] == file_sha256(seed_path)
    records = [
        json.loads(line) for line in ledger_path.read_text().splitlines() if line
    ]
    assert [record["source_task_id"] for record in records] == [
        "seed:acct-001",
        "seed:acct-002",
    ]
    assert all(record["admission"] == "expressible" for record in records)
    assert result["build_report"]["source_grade_counts"] == payload["source_grade_counts"]


def test_seed_without_runtime_is_excluded_not_fatal(tmp_path):
    runtime = _runner_runtime(tmp_path)
    seed_rows = [
        {"id": "acct-001", "task_text": "Runnable seed.", "success_criteria": "One."},
        {"id": "acct-002", "task_text": "Missing runtime seed.", "success_criteria": "Two."},
    ]
    seed_path = tmp_path / "seeds.jsonl"
    seed_path.write_text("".join(json.dumps(row) + "\n" for row in seed_rows))
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "benchmark-runtime-registry-v1",
                "cases": {"acct-001": runtime},
            }
        )
    )
    materialization = {
        "cases": {
            "acct-001": {
                "initial_state_sha256": _sha("1"),
                "expected_state_sha256": _sha("2"),
            },
            "acct-002": {
                "initial_state_sha256": _sha("3"),
                "expected_state_sha256": _sha("4"),
            },
        }
    }
    source = {
        "schema_version": "benchmark-source-v1",
        "family": "workbuddy",
        "release": "fixture",
        "split": "development",
        "license": {"id": "owned-fixture", "derive_allowed": True},
        "seeds": {
            "path": str(seed_path),
            "sha256": file_sha256(seed_path),
            "runtime_registry": {
                "path": str(registry_path),
                "sha256": file_sha256(registry_path),
            },
            "materialization": materialization,
        },
        "cases": [],
    }
    roots = _audit_roots(tmp_path, "runnable", 0)
    config = {
        "schema_version": "benchmark-augment-v1",
        "family": "workbuddy",
        "release": "fixture",
        "split": "development",
        "variants": [
            _variant("seed:acct-001", task_text="Runnable prompt.", runtime_args=roots)
        ],
    }
    source_path = tmp_path / "source-exclude.json"
    config_path = tmp_path / "augment-exclude.json"
    source_path.write_text(json.dumps(source))
    config_path.write_text(json.dumps(config))

    store = ArtifactCatalog()
    result = builder.build_benchmark_taskset(
        store, str(source_path), str(config_path), str(tmp_path / "trusted"), "excluded"
    )
    payload = store.get_artifact("taskset", "excluded")["payload"]
    assert payload["item_count"] == 1
    assert payload["source_grade_counts"] == {"owned_native": 1, "official_native": 0}
    records = [
        json.loads(line)
        for line in Path(payload["source_ledger_path"]).read_text().splitlines()
        if line
    ]
    excluded = [record for record in records if record["admission"] == "excluded"]
    assert excluded[0]["source_task_id"] == "seed:acct-002"
    assert excluded[0]["exclusion_reason"] == "runtime_unavailable"


def test_all_seeds_excluded_and_sha_drift_fail(tmp_path):
    seed_rows = [
        {"id": "acct-001", "task_text": "No runtime seed.", "success_criteria": "One."}
    ]
    seed_path = tmp_path / "seeds.jsonl"
    seed_path.write_text("".join(json.dumps(row) + "\n" for row in seed_rows))
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps({"schema_version": "benchmark-runtime-registry-v1", "cases": {}})
    )
    source = {
        "schema_version": "benchmark-source-v1",
        "family": "workbuddy",
        "release": "fixture",
        "split": "development",
        "license": {"id": "owned-fixture", "derive_allowed": True},
        "seeds": {
            "path": str(seed_path),
            "sha256": file_sha256(seed_path),
            "runtime_registry": {
                "path": str(registry_path),
                "sha256": file_sha256(registry_path),
            },
            "materialization": {
                "cases": {
                    "acct-001": {
                        "initial_state_sha256": _sha("c"),
                        "expected_state_sha256": _sha("d"),
                    }
                }
            },
        },
        "cases": [],
    }
    config = {
        "schema_version": "benchmark-augment-v1",
        "family": "workbuddy",
        "release": "fixture",
        "split": "development",
        "variants": [],
    }
    source_path = tmp_path / "source-empty.json"
    config_path = tmp_path / "augment-empty.json"
    source_path.write_text(json.dumps(source))
    config_path.write_text(json.dumps(config))

    with pytest.raises(BenchmarkError, match="no expressible seed cases"):
        builder.build_benchmark_taskset(
            ArtifactCatalog(),
            str(source_path),
            str(config_path),
            str(tmp_path / "trusted"),
            "none",
        )

    seed_path.write_text(seed_path.read_text() + " ")
    with pytest.raises(BenchmarkError, match="SHA-256 drift"):
        builder.build_benchmark_taskset(
            ArtifactCatalog(),
            str(source_path),
            str(config_path),
            str(tmp_path / "trusted"),
            "drift",
        )
