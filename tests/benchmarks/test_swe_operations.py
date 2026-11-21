"""RFC §3.3.2(3): operation labels are proven against the pinned gold diff."""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path

import pytest

from data_designer.pipelines.benchmarks.contracts import (
    BenchmarkError,
    file_sha256,
)
from data_designer.pipelines.benchmarks.families.swe import verify_swe_operation
from data_designer.pipelines.benchmarks.families.swe import import_swe_jsonl
from data_designer.pipelines.benchmarks.build import build_benchmark_taskset
from data_designer.storage.catalog import ArtifactCatalog
from tests.benchmarks.swe_offline_fixture import (
    INSTANCE_ID,
    prepare_area,
    swe_variant,
    write_audit_roots,
)


def _diff(relative: str, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
        )
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fixture(
    tmp_path: Path,
    gold: str,
    *,
    test_patch: str = "PRIVATE TARGET TEST",
    fail_to_pass=("tests/test_counter.py::test_fix",),
):
    row = {
        "instance_id": "fixture__repo-1",
        "repo": "fixture/repo",
        "base_commit": "a" * 40,
        "problem_statement": "Resolve the counter fault.",
        "patch": gold,
        "test_patch": test_patch,
        "FAIL_TO_PASS": json.dumps(list(fail_to_pass)),
        "PASS_TO_PASS": [],
        "created_at": "2026-01-02T00:00:00Z",
        "version": "1.0",
    }
    raw = json.dumps(row) + "\n"
    snapshot = tmp_path / "swe.jsonl"
    snapshot.write_text(raw, encoding="utf-8")
    source = {
        "release": "fixture",
        "source_snapshot": {
            "path": str(snapshot),
            "sha256": file_sha256(snapshot),
            "format": "swe-jsonl-v1",
        },
    }
    case = {
        "source_task_id": row["instance_id"],
        "source_row": {"line": 1, "sha256": hashlib.sha256(raw.encode()).hexdigest()},
        "swe": {
            "repo": row["repo"],
            "base_commit": row["base_commit"],
            "gold_patch_sha256": _sha(gold),
            "test_patch_sha256": _sha(test_patch),
            "FAIL_TO_PASS": list(fail_to_pass),
        },
    }
    return source, case, row


def _offline_variant(operation: str) -> dict:
    return {
        "source_task_id": "fixture__repo-1",
        "variant_id": "v1",
        "operation": operation,
        "swe_offline": {"deliverable": "patch.diff", "timeout_s": 30},
    }


def test_issue_fix_accepts_one_source_file(tmp_path):
    gold = _diff("src/counter.py", "value = 1\n", "value = 2\n")
    source, case, _ = _fixture(tmp_path, gold)
    evidence = verify_swe_operation(source, case, _offline_variant("issue_fix"), "issue_fix")
    assert evidence["source_files"] == ["src/counter.py"]
    assert evidence["touched_files"] == ["src/counter.py"]


def test_cross_file_rejects_a_single_file(tmp_path):
    gold = _diff("src/counter.py", "value = 1\n", "value = 2\n")
    source, case, _ = _fixture(tmp_path, gold)
    with pytest.raises(BenchmarkError, match="at least two distinct source files"):
        verify_swe_operation(source, case, _offline_variant("cross_file"), "cross_file")


def test_cross_file_accepts_two_source_files(tmp_path):
    gold = _diff("src/counter.py", "value = 1\n", "value = 2\n") + _diff(
        "src/ledger.py", "total = 0\n", "total = 1\n"
    )
    source, case, _ = _fixture(tmp_path, gold)
    evidence = verify_swe_operation(source, case, _offline_variant("cross_file"), "cross_file")
    assert evidence["source_files"] == ["src/counter.py", "src/ledger.py"]


def test_regression_requires_a_fail_to_pass_test(tmp_path):
    gold = _diff("src/counter.py", "value = 1\n", "value = 2\n")
    source, case, _ = _fixture(tmp_path, gold, fail_to_pass=())
    with pytest.raises(BenchmarkError, match="FAIL_TO_PASS"):
        verify_swe_operation(source, case, _offline_variant("regression"), "regression")


def test_harness_operation_reads_the_pinned_dataset(tmp_path):
    gold = _diff("src/counter.py", "value = 1\n", "value = 2\n") + _diff(
        "src/ledger.py", "total = 0\n", "total = 1\n"
    )
    source, case, row = _fixture(tmp_path, gold)
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps([row]), encoding="utf-8")
    variant = {
        "source_task_id": "fixture__repo-1",
        "variant_id": "v1",
        "operation": "cross_file",
        "swe_harness": {
            "dataset_path": str(dataset),
            "dataset_sha256": file_sha256(dataset),
            "instance_id": "fixture__repo-1",
            "protocol": "swebench_resolved_0_1",
        },
    }
    evidence = verify_swe_operation(source, case, variant, "cross_file")
    assert len(evidence["source_files"]) == 2


def test_unknown_operation_is_rejected(tmp_path):
    gold = _diff("src/counter.py", "value = 1\n", "value = 2\n")
    source, case, _ = _fixture(tmp_path, gold)
    with pytest.raises(BenchmarkError, match="unsupported swe operation"):
        verify_swe_operation(source, case, _offline_variant("paraphrase"), "paraphrase")


def test_operation_mismatch_is_quarantined_by_build(tmp_path):
    pins = prepare_area(tmp_path)
    source = import_swe_jsonl(
        pins["snapshot_path"],
        pins["snapshot_sha256"],
        release="fixture-release-1",
        split="development",
        upstream_split="dev",
        license_record={"id": "fixture-only", "derive_allowed": True},
        runtime_registry_path=pins["runtime_registry_path"],
        runtime_registry_sha256=pins["runtime_registry_sha256"],
        offline_base_registry_path=pins["offline_registry_path"],
        offline_base_registry_sha256=pins["offline_registry_sha256"],
    )
    reference, negative = write_audit_roots(tmp_path, pins)
    variant = swe_variant(
        INSTANCE_ID,
        reference_root=reference,
        negative_root=negative,
    )
    variant["operation"] = "cross_file"
    source_path = tmp_path / "source.json"
    config_path = tmp_path / "augment.json"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "benchmark-augment-v1",
                "family": "swe",
                "release": "fixture-release-1",
                "split": "development",
                "variants": [variant],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkError, match="operation_mismatch"):
        build_benchmark_taskset(
            ArtifactCatalog(),
            str(source_path),
            str(config_path),
            str(tmp_path / "trusted"),
            "mismatch",
        )
