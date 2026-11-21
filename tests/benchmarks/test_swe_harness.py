"""No-Docker contracts for the official SWE-bench resolved derivation mode."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from data_designer.contracts.provenance import canonical_json
from data_designer.pipelines.benchmarks.contracts import (
    BenchmarkError,
    canonical_sha256,
    file_sha256,
)
from data_designer.pipelines.benchmarks.boundaries import (
    EvaluationFailure,
    _swe_harness_boundary,
)
from data_designer.pipelines.benchmarks.families.registry import get_family
from data_designer.pipelines.benchmarks.families.swe import (
    HARNESS_PROTOCOL,
    assert_swe_build,
    assert_swe_harness_build,
    derive_swe_harness_spec,
)
from data_designer.pipelines.benchmarks.build import build_benchmark_taskset
from data_designer.pipelines.benchmarks.families.swe import import_swe_jsonl
from data_designer.pipelines.benchmarks.runners.swe import VERSION as HARNESS_VERSION
from data_designer.storage.catalog import ArtifactCatalog


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fixture(tmp_path: Path):
    instance_id = "sqlfluff__sqlfluff-1625"
    row = {
        "instance_id": instance_id,
        "repo": "sqlfluff/sqlfluff",
        "base_commit": "14e1a23a3166b9a645a16de96f694c77a5d4abb7",
        "problem_statement": "Fix L031 alias handling.",
        "patch": "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n",
        "test_patch": "diff --git a/t.py b/t.py\n--- a/t.py\n+++ b/t.py\n@@ -1 +1 @@\n-x\n+y\n",
        "FAIL_TO_PASS": ["tests/test_a.py::test_fix"],
        "PASS_TO_PASS": ["tests/test_a.py::test_keep"],
        "created_at": "2023-01-01",
        "version": "1.0",
    }
    dataset = tmp_path / "instance.json"
    dataset.write_text(json.dumps([row]), encoding="utf-8")
    case = {
        "source_task_id": instance_id,
        "task_text": row["problem_statement"],
        "initial_state_sha256": canonical_sha256(
            {"repo": row["repo"], "base_commit": row["base_commit"]}
        ),
        "expected_state_sha256": canonical_sha256(
            {
                "test_patch_sha256": _sha_text(row["test_patch"]),
                "FAIL_TO_PASS": row["FAIL_TO_PASS"],
                "PASS_TO_PASS": row["PASS_TO_PASS"],
            }
        ),
        "swe": {
            "repo": row["repo"],
            "base_commit": row["base_commit"],
            "created_at": row["created_at"],
            "version": row["version"],
            "gold_patch_sha256": _sha_text(row["patch"]),
            "test_patch_sha256": _sha_text(row["test_patch"]),
            "FAIL_TO_PASS": row["FAIL_TO_PASS"],
            "PASS_TO_PASS": row["PASS_TO_PASS"],
        },
    }
    directive = {
        "dataset_path": str(dataset),
        "dataset_sha256": file_sha256(dataset),
        "instance_id": instance_id,
        "protocol": HARNESS_PROTOCOL,
    }
    return row, dataset, case, directive


def test_derive_and_gate_official_harness_spec(tmp_path):
    _row, _dataset, case, directive = _fixture(tmp_path)
    private = derive_swe_harness_spec({}, case, directive)
    assert private["swe_harness"] == {
        "dataset_path": directive["dataset_path"],
        "dataset_sha256": directive["dataset_sha256"],
        "instance_id": directive["instance_id"],
        "protocol": HARNESS_PROTOCOL,
    }
    assert private["initial_state_sha256"] != case["initial_state_sha256"]
    assert private["expected_state_sha256"] != case["expected_state_sha256"]
    runtime = {"verifier": {"version": HARNESS_VERSION}}
    assert_swe_harness_build(runtime, private, case)
    assert_swe_build(runtime, private, case)
    with pytest.raises(BenchmarkError, match="harness runner"):
        assert_swe_harness_build({"verifier": {"version": "other"}}, private, case)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.update(protocol="offline"), "protocol"),
        (lambda d: d.update(dataset_sha256="0" * 64), "SHA-256"),
        (lambda d: d.update(instance_id="other"), "disagrees"),
    ],
)
def test_harness_directive_drift_is_rejected(tmp_path, mutate, message):
    _row, _dataset, case, directive = _fixture(tmp_path)
    bad = dict(directive)
    mutate(bad)
    with pytest.raises(BenchmarkError, match=message):
        derive_swe_harness_spec({}, case, bad)


def test_harness_dataset_content_drift_is_rejected(tmp_path):
    _row, dataset, case, directive = _fixture(tmp_path)
    row = json.loads(dataset.read_text(encoding="utf-8"))[0]
    row["patch"] = row["patch"].replace("+new", "+tampered")
    dataset.write_text(json.dumps([row]), encoding="utf-8")
    bad = dict(directive)
    bad["dataset_sha256"] = file_sha256(dataset)
    with pytest.raises(BenchmarkError, match="gold patch SHA-256 drift"):
        derive_swe_harness_spec({}, case, bad)


def test_registry_rejects_ambiguous_derivation_blocks(tmp_path):
    _row, _dataset, case, directive = _fixture(tmp_path)
    rules = get_family("swe")
    with pytest.raises(ValueError, match="multiple derivation blocks"):
        rules.materialize(
            {},
            case,
            {"swe_offline": {"deliverable": "patch.diff"}, "swe_harness": directive},
        )


def test_harness_boundary_rejects_candidate_root_and_dataset_drift(tmp_path):
    _row, dataset, case, directive = _fixture(tmp_path)
    private = derive_swe_harness_spec({}, case, directive)
    spec = private["swe_harness"]
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    _swe_harness_boundary(spec, "task", candidate_root)

    nested = candidate_root / "dataset.json"
    nested.write_bytes(dataset.read_bytes())
    inside = dict(spec, dataset_path=str(nested), dataset_sha256=file_sha256(nested))
    with pytest.raises(EvaluationFailure, match="candidateRoot"):
        _swe_harness_boundary(inside, "task", candidate_root)

    dataset.write_text("[]", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="SHA-256 drift"):
        _swe_harness_boundary(spec, "task", candidate_root)

def test_official_source_text_is_not_treated_as_derived_duplicate(tmp_path, monkeypatch):
    row, dataset, _case, directive = _fixture(tmp_path)
    snapshot = tmp_path / "snapshot.jsonl"
    snapshot.write_text(canonical_json(row) + "\n", encoding="utf-8")
    environment = tmp_path / "environment.json"
    environment.write_text("{}", encoding="utf-8")
    verifier = Path(
        "data_designer/pipelines/benchmarks/runners/swe.py"
    ).resolve()
    runtime = {
        "environment": {"path": str(environment), "sha256": file_sha256(environment)},
        "verifier": {
            "path": str(verifier),
            "sha256": file_sha256(verifier),
            "command": [str(Path(__import__("sys").executable)), str(verifier)],
            "version": HARNESS_VERSION,
            "launcher": {
                "path": str(Path(__import__("sys").executable)),
                "sha256": file_sha256(Path(__import__("sys").executable)),
            },
        },
        "assets": [],
    }
    registry = tmp_path / "runtime-registry.json"
    registry.write_text(
        canonical_json(
            {"schema_version": "benchmark-runtime-registry-v1", "cases": {row["instance_id"]: runtime}}
        ),
        encoding="utf-8",
    )
    source = import_swe_jsonl(
        str(snapshot),
        file_sha256(snapshot),
        release="official-test",
        split="development",
        upstream_split="dev",
        license_record={"id": "official-test", "derive_allowed": True},
        runtime_registry_path=str(registry),
        runtime_registry_sha256=file_sha256(registry),
    )
    source_path = tmp_path / "source.json"
    source_path.write_text(canonical_json(source), encoding="utf-8")
    reference = tmp_path / "reference"
    negative = tmp_path / "negative"
    for root, patch in ((reference, row["patch"]), (negative, row["patch"])):
        root.mkdir()
        (root / "patch.diff").write_text(patch, encoding="utf-8")
    augment = tmp_path / "augment.json"
    augment.write_text(
        canonical_json(
            {
                "schema_version": "benchmark-augment-v1",
                "family": "swe",
                "release": "official-test",
                "split": "development",
                "variants": [
                    {
                        "source_task_id": row["instance_id"],
                        "variant_id": "official-resolved-v1",
                        "operation": "issue_fix",
                        "public": {
                            "task_text": row["problem_statement"],
                            "success_criteria": "Official resolved must be 1.",
                            "tools_available": ["repository"],
                            "difficulty": "hard",
                        },
                        "swe_harness": directive,
                        "audit": {
                            "reviewed": True,
                            "reference_root": str(reference),
                            "negative_root": str(negative),
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    import data_designer.pipelines.benchmarks.build as builder

    monkeypatch.setattr(
        builder,
        "_audit",
        lambda prepared, checkpoint: {
            **prepared,
            "positive": {"status": "completed", "passed": True, "verifier_version": HARNESS_VERSION},
            "negative": {"status": "completed", "passed": False, "verifier_version": HARNESS_VERSION},
        },
    )
    monkeypatch.setattr(builder, "assess_taskset", lambda items: {"quality_overall_score": 1.0})
    store = ArtifactCatalog(str(tmp_path / "catalog"))
    built = build_benchmark_taskset(
        store,
        str(source_path),
        str(augment),
        str(tmp_path / "trusted"),
        "official-test",
        on_item_rejection="fail",
        quality_threshold=0.0,
    )
    assert built["item_count"] == 1
    assert built["build_report"]["quarantined_count"] == 0

