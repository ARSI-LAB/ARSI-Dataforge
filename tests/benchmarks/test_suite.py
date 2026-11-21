"""benchmark suite rule tests: create_suite pin freezing, release state machine, load_suite gate,
and comparability assertions, all directly against the new API (ArtifactCatalog + suite module), without going through the deleted
worker shell protocol. tasksets are frozen with the reasoning_qa offline fixture (deterministic unit_test,
no judge required)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_designer.contracts.provenance import canonical_json
from data_designer.pipelines.benchmarks.build import build_benchmark_taskset
from data_designer.pipelines.benchmarks.contracts import BenchmarkError, file_sha256
from data_designer.pipelines.benchmarks.suite import (
    assert_comparable_evaluations,
    create_suite,
    load_suite,
    set_publication_status,
)
from data_designer.sources.hf_single_turn_seed import BENCHMARK_FAMILIES
from data_designer.storage.catalog import ArtifactCatalog
from tests.benchmarks.benchmark_review import approve_fixture_suite
from tests.benchmarks.test_single_turn_source import (
    build_config,
    derive_rows,
    prepare_family,
)


@pytest.fixture(scope="module")
def frozen(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("suite-fixture")
    prepared = prepare_family(tmp_path, "reasoning_qa")
    rows = derive_rows(
        prepared,
        tuple(name for name, fam in BENCHMARK_FAMILIES.items()
              if fam == "reasoning_qa"),
    )
    assert rows
    config, config_path = build_config(tmp_path, prepared, rows)
    source_path = tmp_path / "source.json"
    source_path.write_text(
        json.dumps(prepared["source"], ensure_ascii=False), encoding="utf-8"
    )
    store = ArtifactCatalog(str(tmp_path / "catalog"))
    snapshot = build_benchmark_taskset(
        store, str(source_path), config_path, str(tmp_path / "trusted"),
        "suite-fixture",
        expected_family="reasoning_qa",
        expected_release=prepared["source"]["release"],
        expected_split="development",
    )
    yield store, snapshot
    store.close()


def test_create_suite_freezes_pins_and_rejects_invalid_policy(frozen):
    store, snapshot = frozen
    suite = create_suite(
        store, "suite-rules-ok", [snapshot["taskset"]], repeats=1
    )
    assert suite["suite_sha256"]
    assert suite["split"] == "development"
    assert suite["tasksets"][0]["ref_id"] == snapshot["taskset"]["ref_id"]
    assert suite["tasksets"][0]["fingerprint"] == snapshot["taskset"]["fingerprint"]

    with pytest.raises(BenchmarkError, match="duplicate suite family"):
        create_suite(store, "suite-rules-dup",
                     [snapshot["taskset"], snapshot["taskset"]])
    with pytest.raises(BenchmarkError, match="weights"):
        create_suite(store, "suite-rules-w1", [snapshot["taskset"]],
                     weights={"code_gen": 1.0})
    with pytest.raises(BenchmarkError, match="weights"):
        create_suite(store, "suite-rules-w2", [snapshot["taskset"]],
                     weights={"reasoning_qa": 1.5})
    with pytest.raises(BenchmarkError, match="configuration invalid"):
        create_suite(store, "suite-rules-r", [snapshot["taskset"]], repeats=0)
    with pytest.raises(BenchmarkError, match="configuration invalid"):
        create_suite(store, "suite-rules-t", [snapshot["taskset"]],
                     timeout_seconds=0)


def test_publication_state_machine_and_load_gate(frozen, tmp_path):
    store, snapshot = frozen
    suite = create_suite(store, "suite-pub", [snapshot["taskset"]], repeats=1)
    ref_id, version, sha = (
        suite["ref_id"], suite["version"], suite["suite_sha256"]
    )

    # candidates cannot be evaluated directly; SHA drift is rejected explicitly.
    with pytest.raises(BenchmarkError, match="not approved"):
        load_suite(store, ref_id, version, sha)
    with pytest.raises(BenchmarkError, match="drift"):
        load_suite(store, ref_id, version, "0" * 64, require_approved=False)

    # Review evidence files changed after the fact → approval is rejected; no forged approval is recorded.
    from tests.benchmarks.benchmark_review import write_fixture_review
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    pin = write_fixture_review(suite, evidence_dir)
    Path(pin["path"]).write_text("tampered", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="review evidence"):
        set_publication_status(
            store, ref_id, version, "approved", "tampered evidence",
            review_evidence=pin,
        )

    ok_dir = tmp_path / "evidence-ok"
    ok_dir.mkdir()
    approve_fixture_suite(store, suite, ok_dir)
    assert load_suite(store, ref_id, version, sha)["suite_sha256"] == sha

    # approved → quarantined is allowed; quarantined is terminal and cannot be flipped again.
    set_publication_status(store, ref_id, version, "quarantined",
                           "fixture quarantine drill")
    with pytest.raises(BenchmarkError, match="invalid suite publication"):
        set_publication_status(store, ref_id, version, "approved",
                               "quarantine is terminal")


def test_comparable_evaluations_require_same_tasks_and_suite(frozen, tmp_path):
    store, snapshot = frozen
    suite = create_suite(store, "suite-comparable", [snapshot["taskset"]], repeats=1)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    approve_fixture_suite(store, suite, evidence_dir)
    loaded = load_suite(
        store, suite["ref_id"], suite["version"], suite["suite_sha256"]
    )
    eval_shape = {
        "suite_sha256": loaded["suite_sha256"],
        "suite_ref": {"ref_id": loaded["ref_id"], "version": loaded["version"]},
        "tasksets": loaded["tasksets"],
        "split": loaded["split"],
        "tasks": ["reasoning_qa:task-a", "reasoning_qa:task-b"],
    }
    assert_comparable_evaluations(dict(eval_shape), dict(eval_shape))
    drifted = dict(eval_shape, tasks=["reasoning_qa:task-a"])
    with pytest.raises(BenchmarkError, match="different suites"):
        assert_comparable_evaluations(eval_shape, drifted)
    with pytest.raises(BenchmarkError, match="non-empty task list"):
        assert_comparable_evaluations(
            dict(eval_shape, tasks=[]), dict(eval_shape, tasks=[])
        )


def test_owned_native_seed_requires_six_review_pins(frozen, tmp_path):
    """R2.6: an owned-native pin upgrades any review scope to native."""
    store, snapshot = frozen
    pin = snapshot["taskset"]
    payload = store.get_artifact("taskset", pin["ref_id"], pin["version"])["payload"]
    owned = {
        **payload,
        "source_grade_counts": {"owned_native": 1, "official_native": 0},
    }
    store.put_artifact(
        "taskset", "owned-seeded", 1, owned,
        fingerprint=payload["taskset_fingerprint"],
    )
    suite = create_suite(
        store,
        "owned-review",
        [{
            "ref_id": "owned-seeded",
            "version": 1,
            "fingerprint": payload["taskset_fingerprint"],
        }],
    )

    # The fixture attestation claims synthetic scope; the owned-native seed
    # must still force the six evidence pins.
    with pytest.raises(BenchmarkError, match="six evidence pins"):
        approve_fixture_suite(store, suite, tmp_path)

    evidence_dir = tmp_path / "native-evidence"
    evidence_dir.mkdir()
    pins = {}
    for label in (
        "source_release", "license", "native_environment",
        "reference", "negative", "isolation",
    ):
        path = evidence_dir / f"{label}.txt"
        path.write_text(f"{label} acceptance evidence\n", encoding="utf-8")
        pins[label] = {"path": str(path), "sha256": file_sha256(path)}
    review = evidence_dir / "review.json"
    review.write_text(
        canonical_json({
            "schema_version": "benchmark-suite-review-v1",
            "scope": "synthetic_fixture",
            "suite_sha256": suite["suite_sha256"],
            "tasksets": suite["tasksets"],
            "reviewer": "owned-native-test",
            "evidence_refs": ["six offline evidence files"],
            "native_positive_negative_verified": True,
            "private_boundary_verified": True,
            "license_checked": True,
            "environment_reset_verified": True,
            "evidence_pins": pins,
        }),
        encoding="utf-8",
    )
    set_publication_status(
        store, suite["ref_id"], suite["version"], "approved",
        "owned native evidence",
        review_evidence={"path": str(review), "sha256": file_sha256(review)},
    )
