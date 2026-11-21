"""Snapshot import, semantic augmentation, native audit, and taskset freeze."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List

from data_designer.contracts.provenance import canonical_json
from data_designer.contracts.provenance import content_digest as fingerprint_of
from data_designer.storage.catalog import ArtifactCatalog

from .augment import materialize_json_state_variant
from .contracts import (
    BenchmarkError,
    ItemRejection,
    NativeFailure,
    canonical_sha256,
    file_sha256,
    make_public_item,
    normalized_text_sha256,
    require_family,
    require_nonempty,
    require_sha,
    require_split,
)
from .families.registry import family_operations, get_family
from .native import run_case
from .quality import assess_taskset
from .quarantine import write_quarantine
from .runtime import lock_runtime, runtime_digests
from .seeds import import_seed_cases

# The operation names are owned by each family (RFC §1.4). Upstream
# environment mutations are supplied by an authorized snapshot and must pass
# both the family operation contract and that runner's native audit.
AUGMENTATIONS = family_operations()

CHECKPOINT_SCHEMA_VERSION = "benchmark-checkpoint-v1"


def _failure_trace(value: Any) -> Dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("sanitized") is not True:
        raise BenchmarkError("failure trace requires sanitization attestation")
    if value.get("split") != "development":
        raise BenchmarkError("only development failure traces may seed augmentation")
    trace_path = Path(require_nonempty(value.get("path"), "failure_trace.path"))
    trace_sha = require_sha(value.get("sha256"), "failure_trace.sha256")
    if not trace_path.is_absolute() or not trace_path.is_file():
        raise BenchmarkError("failure trace snapshot unavailable")
    if file_sha256(trace_path) != trace_sha:
        raise BenchmarkError("failure trace SHA-256 drift")
    return {
        "trace_ref": require_nonempty(value.get("trace_ref"), "trace_ref"),
        "path": str(trace_path),
        "sha256": trace_sha,
        "split": "development",
        "sanitized": True,
        "lesson_ref": value.get("lesson_ref"),
        "cluster_ref": value.get("cluster_ref"),
    }


def _load_json(path: str) -> Dict[str, Any]:
    try:
        value = json.loads(
            Path(path).read_text(encoding="utf-8"),
            parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)),
        )
    except (OSError, ValueError) as error:
        raise BenchmarkError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise BenchmarkError(f"JSON {path} must be an object")
    return value


def _augment(
    source: Dict[str, Any], variant: Dict[str, Any], family: str, release: str
) -> Dict[str, Any]:
    if not isinstance(source, dict) or not isinstance(variant, dict):
        raise BenchmarkError("source and variant must be objects")
    operation = variant.get("operation")
    if operation not in AUGMENTATIONS[family]:
        raise ItemRejection(
            "unsupported_operation",
            f"unsupported semantic augmentation {operation!r} for {family}",
            {"operation": operation, "family": family},
        )
    source_id = require_nonempty(source.get("source_task_id"), "source_task_id")
    if variant.get("source_task_id") != source_id:
        raise BenchmarkError("variant source_task_id mismatch")
    variant_id = require_nonempty(variant.get("variant_id"), "variant_id")
    public = variant.get("public")
    private = variant.get("private")
    if not isinstance(public, dict) or not isinstance(private, dict):
        raise BenchmarkError("variant must provide public and private objects")
    for key in ("initial_state_sha256", "expected_state_sha256"):
        require_sha(private.get(key), f"private.{key}")
        require_sha(source.get(key), f"source.{key}")
    if (
        private["initial_state_sha256"] == source["initial_state_sha256"]
        or private["expected_state_sha256"] == source["expected_state_sha256"]
    ):
        raise ItemRejection(
            "state_unchanged",
            "augmentation must change initial and expected state",
            {
                "source_initial": source["initial_state_sha256"],
                "source_expected": source["expected_state_sha256"],
            },
        )
    task_id = f"{family}:{release}:{source_id}:{variant_id}"
    fields = {
        "task_id": task_id,
        "family": family,
        "source_release": release,
        "source_task_id": source_id,
        "variant_id": variant_id,
        "task_text": public.get("task_text"),
        "tools_available": public.get("tools_available"),
        "success_criteria": public.get("success_criteria"),
        "difficulty": public.get("difficulty"),
    }
    # Source and variant metadata are intentionally retained only on the trusted
    # side. The item is constructed from an allowlist, never a source dict copy.
    return {
        "public": fields,
        "private": private,
        "operation": operation,
        "source_ref": source_id,
        "audit": variant.get("audit"),
    }


class _Checkpoint:
    """Append-only JSONL checkpoint tolerant of a torn final line (R9)."""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        if self.path.is_file():
            self._repair_torn_tail()
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and isinstance(entry.get("case_key"), str):
                    self._entries[entry["case_key"]] = entry

    def _repair_torn_tail(self) -> None:
        # A crash mid-append leaves a fragment with no trailing newline. Drop
        # it before any new append, otherwise that append concatenates onto
        # the fragment and is unreadable on the next restart.
        data = self.path.read_bytes()
        if not data or data.endswith(b"\n"):
            return
        with self.path.open("r+b") as stream:
            stream.truncate(data.rfind(b"\n") + 1)

    @staticmethod
    def _manifest_valid(entry: Dict[str, Any], manifest_path: str, manifest_sha: str) -> bool:
        if entry.get("manifest_sha256") != manifest_sha:
            return False
        recorded = entry.get("case_manifest_path")
        if not isinstance(recorded, str) or not Path(recorded).is_file():
            return False
        return file_sha256(Path(recorded)) == manifest_sha

    def lookup(
        self, case_key: str, manifest_path: str, manifest_sha: str
    ) -> Dict[str, Any] | None:
        entry = self._entries.get(case_key)
        if entry is None or entry.get("outcome") not in ("accepted", "quarantined"):
            return None
        if not self._manifest_valid(entry, manifest_path, manifest_sha):
            return None
        return entry

    def append(self, entry: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = canonical_json(entry) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
            self._entries[entry["case_key"]] = entry


def _raise_fail(rejection: ItemRejection) -> None:
    if rejection.cause is not None:
        raise rejection.cause
    raise BenchmarkError(rejection.detail)


def _cell(family: str, difficulty: Any) -> str:
    return f"{family}/{str(difficulty or 'unspecified')}"


def _count_cells(pairs) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for family, difficulty in pairs:
        cell = _cell(family, difficulty)
        counts[cell] = counts.get(cell, 0) + 1
    return dict(sorted(counts.items()))


def _grid_report(
    family: str,
    source_cases: List[Dict[str, Any]],
    items: List[Dict[str, Any]],
    grid_config: Any,
) -> Dict[str, Any]:
    grid_config = grid_config if isinstance(grid_config, dict) else {}
    minimum = grid_config.get("min_count", 1)
    if type(minimum) is not int or minimum < 0:
        raise BenchmarkError("grid.min_count must be a non-negative integer")
    quotas = grid_config.get("quotas", {})
    if not isinstance(quotas, dict) or any(
        type(value) is not int or value < 0 for value in quotas.values()
    ):
        raise BenchmarkError("grid.quotas must map cells to non-negative integers")
    seed_counts = _count_cells(
        (family, case.get("difficulty")) for case in source_cases
    )
    accepted_counts = _count_cells(
        (family, item.get("difficulty")) for item in items
    )
    cells = sorted(set(seed_counts) | set(accepted_counts) | set(quotas))
    gap_report = []
    for cell in cells:
        required = max(minimum, quotas.get(cell, 0))
        if accepted_counts.get(cell, 0) < required:
            gap_report.append(
                {
                    "cell": cell,
                    "count": accepted_counts.get(cell, 0),
                    "suggestion_operations": sorted(AUGMENTATIONS.get(family, set())),
                }
            )
    return {
        "dimensions": ["family", "difficulty"],
        "seed": seed_counts,
        "accepted": accepted_counts,
        "seed_covered_cells": len(seed_counts),
        "covered_cells": len(accepted_counts),
        "coverage_gain": len(accepted_counts) - len(seed_counts),
        "gap_report": gap_report,
    }


def _case_protocol(private: Any) -> str | None:
    """Judging protocol implied by the trusted derivation block."""
    if not isinstance(private, dict):
        return None
    harness = private.get("swe_harness")
    if isinstance(harness, dict):
        return harness.get("protocol")
    spec = private.get("verifier_spec")
    if isinstance(spec, dict):
        return spec.get("kind")
    return None


def _operation_evidence_summary(evidence: Any) -> Dict[str, Any] | None:
    """Counts only: file paths from a gold diff stay on the trusted side."""
    if not isinstance(evidence, dict):
        return None
    summary = {"operation": evidence.get("operation")}
    for key in ("touched_files", "source_files", "test_files"):
        value = evidence.get(key)
        if isinstance(value, list):
            summary[key] = len(value)
    if "fail_to_pass" in evidence:
        summary["fail_to_pass"] = evidence["fail_to_pass"]
    return summary


def _write_seed_ledger(trusted_root: Path, ledger: Dict[str, Any]) -> Dict[str, Any]:
    # Content-addressed so rebuilt jobs sharing one trusted dir never overwrite
    # the ledger an earlier published taskset points at.
    directory = trusted_root / "seed-ledger"
    directory.mkdir(parents=True, exist_ok=True)
    lines = "".join(
        canonical_json(record) + "\n" for record in ledger["records"]
    ).encode("utf-8")
    sha256 = hashlib.sha256(lines).hexdigest()
    path = directory / f"{sha256}.jsonl"
    path.write_bytes(lines)
    return {
        "path": str(path),
        "sha256": sha256,
        "source_grade_counts": dict(ledger["source_grade_counts"]),
        "seed_rows_sha256": ledger["seed_rows_sha256"],
    }


def _prepare(
    index: int,
    variant: Any,
    context: Dict[str, Any],
    checkpoint: _Checkpoint | None,
    on_item_rejection: str,
) -> Dict[str, Any]:
    source = context["source"]
    family = context["family"]
    release = context["release"]
    split = context["split"]
    rules = context["rules"]
    trusted_root = context["trusted_root"]
    by_id = context["by_id"]
    plain_variant = variant if isinstance(variant, dict) else {}
    fallback_task_id = None
    source_id = plain_variant.get("source_task_id")
    variant_id = plain_variant.get("variant_id")
    if isinstance(source_id, str) and isinstance(variant_id, str):
        fallback_task_id = f"{family}:{release}:{source_id}:{variant_id}"
    try:
        source_case = by_id.get(source_id)
        if source_case is None:
            raise BenchmarkError("variant references an unknown source case")
        if "state_patch" in plain_variant:
            if not rules.state_patch_allowed:
                raise BenchmarkError(f"{family} derivation cannot use JSON state_patch")
            plain_variant = {
                **plain_variant,
                "private": materialize_json_state_variant(
                    source_case, plain_variant, trusted_root
                ),
            }
        plain_variant = rules.materialize(source, source_case, plain_variant)
        case = _augment(source_case, plain_variant, family, release)
        try:
            operation_evidence = rules.verify_operation(
                source, source_case, plain_variant, case["operation"]
            )
        except BenchmarkError as error:
            raise ItemRejection(
                "operation_mismatch",
                str(error),
                {
                    "operation": case["operation"],
                    "task_id": case["public"]["task_id"],
                },
            ) from error
        case["operation_evidence"] = operation_evidence
        public = case["public"]
        runtime_record = (
            plain_variant["runtime"]
            if "runtime" in plain_variant
            else source_case["runtime"]
            if "runtime" in source_case
            else source
        )
        runtime = lock_runtime(runtime_record)
        rules.check_build(source, source_case, runtime, case["private"])
        environment_sha = runtime["environment"]["sha256"]
        verifier_sha = runtime["verifier"]["sha256"]
        verifier_version = runtime["verifier"]["version"]
        command = runtime["verifier"]["command"]
        asset_sha = canonical_sha256(runtime["assets"])
        case_audit = case["audit"]
        if (
            not isinstance(case_audit, dict)
            or case_audit.get("reviewed") is not True
            or not isinstance(case_audit.get("reference_root"), str)
            or not isinstance(case_audit.get("negative_root"), str)
        ):
            raise BenchmarkError(
                "independent review, reference and negative roots required"
            )
        for root in (case_audit["reference_root"], case_audit["negative_root"]):
            if not Path(root).is_absolute() or not Path(root).is_dir():
                raise BenchmarkError(f"audit root unavailable: {root}")
        manifest = {
            "schema_version": "benchmark-case-v1",
            "task_id": public["task_id"],
            "family": family,
            "source_release": release,
            "split": split,
            "source_sha256": context["source_sha"],
            "augment_config_sha256": context["config_sha"],
            "source_ref": case["source_ref"],
            "source_case": source_case,
            "source_snapshot": context["source_snapshot"],
            "failure_trace": _failure_trace(plain_variant.get("failure_trace")),
            "operation": case["operation"],
            "operation_evidence": case.get("operation_evidence"),
            "license_id": context["license_record"]["id"],
            "private": case["private"],
            "environment_sha256": environment_sha,
            "verifier_sha256": verifier_sha,
            "asset_manifest_sha256": asset_sha,
            "audit": case_audit,
        }
        manifest_sha = canonical_sha256(manifest)
        manifest_path = trusted_root / "cases" / f"{manifest_sha}.json"
        contents = canonical_json(manifest).encode("utf-8")
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        if manifest_path.exists() and manifest_path.read_bytes() != contents:
            raise BenchmarkError("trusted manifest content mismatch")
        manifest_path.write_bytes(contents)
        case_key = canonical_sha256(
            {
                "source_sha256": context["source_sha"],
                "augment_config_sha256": context["config_sha"],
                "source_task_id": public["source_task_id"],
                "variant": plain_variant,
                "runtime": {
                    "environment": environment_sha,
                    "verifier": verifier_sha,
                    "assets": asset_sha,
                },
            }
        )
        entry_base = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "case_key": case_key,
            "task_id": public["task_id"],
            "manifest_sha256": manifest_sha,
            "case_manifest_path": str(manifest_path),
            "verifier_version": verifier_version,
        }
        try:
            item = make_public_item(public, manifest_sha, environment_sha, verifier_sha)
        except BenchmarkError as error:
            rejection = ItemRejection(
                "private_boundary_violation",
                str(error),
                {"task_id": public["task_id"]},
            )
            _checkpoint_rejection(checkpoint, {"base_entry": entry_base}, rejection)
            raise rejection from error
        reuse = None
        if checkpoint is not None:
            reuse = checkpoint.lookup(case_key, str(manifest_path), manifest_sha)
            if reuse is not None and reuse.get("outcome") == "quarantined":
                rejection = ItemRejection(
                    reuse.get("reason") or "native_reference_failed",
                    reuse.get("detail") or "checkpointed item rejection",
                    reuse.get("evidence") or {},
                )
                raise rejection
        text_sha = normalized_text_sha256(public["task_text"])
        own_native_text = (
            context["native_build"] or context.get("harness_build")
        ) and text_sha == context["source_text_by_id"].get(case["source_ref"])
        if public["task_id"] in context["seen_ids"]:
            raise ItemRejection(
                "duplicate_task_id",
                "duplicate derived task ID or task text",
                {"task_id": public["task_id"]},
            )
        duplicate_text = text_sha in context["seen_texts"] and not (
            own_native_text and text_sha not in context["published_texts"]
        )
        if duplicate_text:
            reason = (
                "duplicate_published_text"
                if text_sha in context["published_texts"]
                else "duplicate_text"
            )
            raise ItemRejection(
                reason,
                "duplicate derived task ID or task text",
                {"text_sha256": text_sha},
            )
        context["seen_ids"].add(public["task_id"])
        context["seen_texts"].add(text_sha)
        return {
            "index": index,
            "variant": plain_variant,
            "source_case": source_case,
            "operation": case["operation"],
            "operation_evidence": case.get("operation_evidence"),
            "protocol": _case_protocol(case["private"]),
            "item": item,
            "runtime": runtime,
            "manifest_path": str(manifest_path),
            "manifest_sha": manifest_sha,
            "command": command,
            "timeout": context["timeout"],
            "audit": case_audit,
            "case_key": case_key,
            "base_entry": entry_base,
            "reuse": reuse,
        }
    except ItemRejection as rejection:
        if on_item_rejection == "fail":
            _raise_fail(rejection)
        record = {
            "index": index,
            "task_id": fallback_task_id,
            "reason": rejection.reason,
            "detail": rejection.detail,
            "evidence": rejection.evidence,
            "source_task_id": source_id,
            "variant_id": variant_id,
            "operation": plain_variant.get("operation"),
        }
        if checkpoint is not None and fallback_task_id is not None:
            # Duplicate/ordering rejections are intentionally not checkpointed:
            # they depend on the rest of the batch, so reusing them on a changed
            # batch could drop an item that is no longer a duplicate. Rejections
            # that own a manifest are checkpointed where the manifest is written.
            pass
        raise _PreparedRejection(record) from rejection


class _PreparedRejection(Exception):
    def __init__(self, record: Dict[str, Any]):
        super().__init__(record.get("detail"))
        self.record = record


def _audit(
    prepared: Dict[str, Any],
    checkpoint: _Checkpoint | None,
) -> Dict[str, Any]:
    reuse = prepared.get("reuse")
    if reuse is not None:
        positive = reuse.get("reference")
        negative = reuse.get("negative")
        if isinstance(positive, dict) and isinstance(negative, dict):
            return {**prepared, "positive": positive, "negative": negative}
    task_id = prepared["item"]["task_id"]
    try:
        positive = run_case(
            prepared["command"],
            task_id,
            prepared["manifest_path"],
            prepared["audit"]["reference_root"],
            phase="reference",
            timeout=prepared["timeout"],
            public_item=prepared["item"],
        )
        negative = run_case(
            prepared["command"],
            task_id,
            prepared["manifest_path"],
            prepared["audit"]["negative_root"],
            phase="negative",
            timeout=prepared["timeout"],
            public_item=prepared["item"],
        )
    except NativeFailure as failure:
        if failure.category == "timeout":
            rejection = ItemRejection(
                "audit_timeout",
                f"positive/negative native audit failed: {task_id}",
                {"category": failure.category, "timeout_seconds": prepared["timeout"]},
                cause=failure,
            )
        else:
            raise
        _checkpoint_rejection(checkpoint, prepared, rejection)
        raise rejection from failure
    except BenchmarkError as error:
        rejection = ItemRejection(
            "native_reference_failed",
            f"positive/negative native audit failed: {task_id}",
            {"error": str(error)},
            cause=error,
        )
        _checkpoint_rejection(checkpoint, prepared, rejection)
        raise rejection from error

    pinned_version = prepared["runtime"]["verifier"]["version"]
    if (
        positive["verifier_version"] != pinned_version
        or negative["verifier_version"] != pinned_version
    ):
        rejection = ItemRejection(
            "verifier_version_mismatch",
            f"positive/negative native audit failed: {task_id}",
            {
                "pinned": pinned_version,
                "observed": [
                    positive["verifier_version"],
                    negative["verifier_version"],
                ],
            },
        )
        _checkpoint_rejection(checkpoint, prepared, rejection)
        raise rejection
    if positive["status"] != "completed" or not positive["passed"]:
        rejection = ItemRejection(
            "native_reference_failed",
            f"positive/negative native audit failed: {task_id}",
            {"reference_digest": canonical_sha256(positive)},
        )
        _checkpoint_rejection(checkpoint, prepared, rejection)
        raise rejection
    if negative["status"] != "completed" or negative["passed"]:
        rejection = ItemRejection(
            "native_negative_passed",
            f"positive/negative native audit failed: {task_id}",
            {"negative_digest": canonical_sha256(negative)},
        )
        _checkpoint_rejection(checkpoint, prepared, rejection)
        raise rejection
    if checkpoint is not None:
        checkpoint.append(
            {
                **prepared["base_entry"],
                "outcome": "accepted",
                "reason": None,
                "detail": None,
                "evidence": None,
                "reference": positive,
                "negative": negative,
                "reference_digest": canonical_sha256(positive),
                "negative_digest": canonical_sha256(negative),
            }
        )
    return {**prepared, "positive": positive, "negative": negative}


def _checkpoint_rejection(
    checkpoint: _Checkpoint | None,
    prepared: Dict[str, Any],
    rejection: ItemRejection,
) -> None:
    if checkpoint is None:
        return
    checkpoint.append(
        {
            **prepared["base_entry"],
            "outcome": "quarantined",
            "reason": rejection.reason,
            "detail": rejection.detail,
            "evidence": rejection.evidence,
            "reference": None,
            "negative": None,
        }
    )


def build_benchmark_taskset(
    store: ArtifactCatalog,
    source_path: str,
    config_path: str,
    trusted_dir: str,
    ref_id: str,
    *,
    quality_threshold: float = 0.6,
    on_item_rejection: str = "quarantine",
    checkpoint: str | Path | None = None,
    audit_concurrency: int = 1,
    min_expansion_ratio: float | None = None,
    require_grid_gain: bool = False,
    seeds_base_dir: str | Path | None = None,
    expected_family: str | None = None,
    expected_release: str | None = None,
    expected_split: str | None = None,
    data_boundary: str | None = None,
) -> Dict[str, Any]:
    if (
        type(quality_threshold) not in (int, float)
        or not math.isfinite(quality_threshold)
        or not 0 <= quality_threshold <= 1
    ):
        raise BenchmarkError("quality_threshold must be in [0,1]")
    if on_item_rejection not in ("quarantine", "fail"):
        raise BenchmarkError("on_item_rejection must be 'quarantine' or 'fail'")
    if type(audit_concurrency) is not int or not 1 <= audit_concurrency <= 16:
        raise BenchmarkError("audit_concurrency must be an integer in [1,16]")
    if min_expansion_ratio is not None and (
        type(min_expansion_ratio) not in (int, float)
        or not math.isfinite(min_expansion_ratio)
        or min_expansion_ratio < 0
    ):
        raise BenchmarkError("min_expansion_ratio must be finite and non-negative")
    boundary = None
    if data_boundary is not None:
        boundary = Path(require_nonempty(data_boundary, "data_boundary"))
        if not boundary.is_absolute() or not boundary.is_dir():
            raise BenchmarkError(
                "data_boundary must be an existing absolute directory"
            )
        boundary = boundary.resolve()
    source = _load_json(source_path)
    config = _load_json(config_path)
    if (
        source.get("schema_version") != "benchmark-source-v1"
        or config.get("schema_version") != "benchmark-augment-v1"
    ):
        raise BenchmarkError("wrong source or augmentation schema")
    family = require_family(source.get("family"))
    rules = get_family(family)
    release = require_nonempty(source.get("release"), "release")
    split = require_split(source.get("split"))
    if (
        (expected_family is not None and family != expected_family)
        or (expected_release is not None and release != expected_release)
        or (expected_split is not None and split != expected_split)
    ):
        raise BenchmarkError("source pins do not match build request")
    if (config.get("family"), config.get("release"), config.get("split")) != (
        family,
        release,
        split,
    ):
        raise BenchmarkError("source and augment config pins differ")

    source, ledger = import_seed_cases(
        source,
        base_directory=seeds_base_dir or Path(source_path).parent,
        storage_root=boundary,
    )
    sources = source.get("cases")
    variants = config.get("variants")
    if not isinstance(sources, list) or not isinstance(variants, list) or not variants:
        raise BenchmarkError("source cases and at least one variant required")
    operations = {
        variant.get("operation")
        for variant in variants
        if isinstance(variant, dict)
    }
    native_build = "native_ingest" in operations
    harness_build = any(
        isinstance(variant, dict) and isinstance(variant.get("swe_harness"), dict)
        for variant in variants
    )
    if native_build and operations != {"native_ingest"}:
        raise BenchmarkError(
            "augment config must not mix native_ingest with other operations"
        )
    license_record = source.get("license")
    if (
        not isinstance(license_record, dict)
        or not require_nonempty(license_record.get("id"), "license.id")
    ):
        raise BenchmarkError("source license decision required")
    if native_build:
        if license_record.get("native_ingest_allowed") is not True:
            raise BenchmarkError("source license does not authorize native ingest")
    elif license_record.get("derive_allowed") is not True:
        raise BenchmarkError("source license does not authorize derivation")
    if split == "holdout":
        raise BenchmarkError("holdout cannot be an augmentation source")
    source_snapshot = source.get("source_snapshot")
    if source_snapshot is not None:
        if not isinstance(source_snapshot, dict):
            raise BenchmarkError("source_snapshot must be an object")
        snapshot_path = Path(
            require_nonempty(source_snapshot.get("path"), "source_snapshot.path")
        )
        snapshot_sha = require_sha(
            source_snapshot.get("sha256"), "source_snapshot.sha256"
        )
        if (
            not snapshot_path.is_absolute()
            or not snapshot_path.is_file()
            or file_sha256(snapshot_path) != snapshot_sha
        ):
            raise BenchmarkError("source snapshot missing or SHA-256 drift")
    source_sha = file_sha256(source_path)
    config_sha = file_sha256(config_path)
    by_id = {
        case.get("source_task_id"): case for case in sources if isinstance(case, dict)
    }
    if len(by_id) != len(sources):
        raise BenchmarkError("duplicate or invalid source case IDs")
    rules.verify_source(source)
    excluded = source.get("exclusion_text_sha256", [])
    if not isinstance(excluded, list):
        raise BenchmarkError("exclusion_text_sha256 must be a list")
    seen_texts = {require_sha(value, "exclusion_text_sha256") for value in excluded}
    published_texts: set[str] = set()
    source_text_by_id: Dict[str, str] = {}
    for case in sources:
        text_sha = normalized_text_sha256(case.get("task_text"))
        seen_texts.add(text_sha)
        source_text_by_id[case.get("source_task_id")] = text_sha
    if not Path(trusted_dir).is_absolute():
        raise BenchmarkError("trusted_dir must be an absolute directory")
    trusted_root = Path(trusted_dir).resolve()
    if boundary is not None and not trusted_root.is_relative_to(boundary):
        raise BenchmarkError("trusted_dir must stay inside data_boundary")
    if checkpoint is not None:
        temporary_root = Path(tempfile.gettempdir()).resolve()
        try:
            trusted_root.relative_to(temporary_root)
        except ValueError:
            pass
        else:
            warnings.warn(
                "checkpoint under a temporary trusted directory cannot be "
                "resumed across processes",
                RuntimeWarning,
                stacklevel=2,
            )
    seen_ids: set[str] = set()
    for previous in store.list_artifacts("taskset"):
        for old in previous["payload"].get("taskset", []):
            metadata = old.get("benchmark") if isinstance(old, dict) else None
            if isinstance(metadata, dict):
                seen_ids.add(str(old.get("task_id")))
                text_sha = normalized_text_sha256(old.get("task_text"))
                seen_texts.add(text_sha)
                published_texts.add(text_sha)

    timeout_value = config.get("audit_timeout_seconds", 600)
    if (
        type(timeout_value) not in (int, float)
        or not math.isfinite(timeout_value)
        or timeout_value <= 0
    ):
        raise BenchmarkError("audit_timeout_seconds must be finite and positive")
    timeout = float(timeout_value)
    context = {
        "source": source,
        "family": family,
        "release": release,
        "split": split,
        "rules": rules,
        "trusted_root": trusted_root,
        "by_id": by_id,
        "source_sha": source_sha,
        "config_sha": config_sha,
        "source_snapshot": source_snapshot,
        "license_record": license_record,
        "native_build": native_build,
        "harness_build": harness_build,
        "source_text_by_id": source_text_by_id,
        "seen_ids": seen_ids,
        "seen_texts": seen_texts,
        "published_texts": published_texts,
        "timeout": timeout,
    }
    checkpoint_log = _Checkpoint(checkpoint) if checkpoint is not None else None

    prepared: List[Dict[str, Any] | None] = []
    prepare_rejections: Dict[int, Dict[str, Any]] = {}
    for index, variant in enumerate(variants):
        try:
            prepared.append(_prepare(
                index, variant, context, checkpoint_log, on_item_rejection
            ))
        except _PreparedRejection as rejection:
            prepared.append(None)
            prepare_rejections[index] = rejection.record

    audited: Dict[int, Dict[str, Any]] = {}
    audit_rejections: Dict[int, Dict[str, Any]] = {}

    def run_one(index: int, value: Dict[str, Any]) -> None:
        try:
            audited[index] = _audit(value, checkpoint_log)
        except ItemRejection as rejection:
            if on_item_rejection == "fail":
                _raise_fail(rejection)
            audit_rejections[index] = {
                "index": index,
                "task_id": value["item"]["task_id"],
                "reason": rejection.reason,
                "detail": rejection.detail,
                "evidence": rejection.evidence,
                "source_task_id": value["item"]["benchmark"]["source_task_id"],
                "variant_id": value["item"]["benchmark"]["variant_id"],
                "operation": value["operation"],
            }

    if audit_concurrency == 1:
        for index, value in enumerate(prepared):
            if value is not None:
                run_one(index, value)
    else:
        with ThreadPoolExecutor(max_workers=audit_concurrency) as pool:
            futures = {
                index: pool.submit(run_one, index, value)
                for index, value in enumerate(prepared)
                if value is not None
            }
            for future in futures.values():
                future.result()

    def public_grade_counts() -> Dict[str, int]:
        if ledger is not None:
            return dict(ledger["source_grade_counts"])
        return {"owned_native": 0, "official_native": len(by_id)}

    quarantine_records: List[Dict[str, Any]] = []
    accepted: List[Dict[str, Any]] = []
    operation_counts: Dict[str, Dict[str, int]] = {}
    for index, variant in enumerate(variants):
        operation = (
            variant.get("operation") if isinstance(variant, dict) else None
        )
        operation = str(operation)
        counts = operation_counts.setdefault(
            operation, {"input": 0, "accepted": 0, "quarantined": 0}
        )
        counts["input"] += 1
        if index in prepare_rejections:
            quarantine_records.append(prepare_rejections[index])
            counts["quarantined"] += 1
        elif index in audit_rejections:
            quarantine_records.append(audit_rejections[index])
            counts["quarantined"] += 1
        else:
            accepted.append(audited[index])
            counts["accepted"] += 1

    if not accepted:
        reasons: Dict[str, int] = {}
        for record in quarantine_records:
            reasons[record["reason"]] = reasons.get(record["reason"], 0) + 1
        raise BenchmarkError(f"no accepted benchmark items remain: {reasons}")

    quality_report = assess_taskset([entry["item"] for entry in accepted])
    curator_removed = list(quality_report.get("curator_removed_task_ids") or [])
    if curator_removed:
        removed = set(curator_removed)
        by_task = {entry["item"]["task_id"]: entry for entry in accepted}
        for task_id in curator_removed:
            entry = by_task[task_id]
            quarantine_records.append(
                {
                    "index": entry["index"],
                    "task_id": task_id,
                    "reason": "duplicate_text",
                    "detail": "Curator exact dedup removed an audited item",
                    "evidence": {"curator_removed": True},
                    "source_task_id": entry["item"]["benchmark"]["source_task_id"],
                    "variant_id": entry["item"]["benchmark"]["variant_id"],
                    "operation": entry["operation"],
                }
            )
            operation_counts[str(entry["operation"])]["accepted"] -= 1
            operation_counts[str(entry["operation"])]["quarantined"] += 1
        accepted = [entry for entry in accepted if entry["item"]["task_id"] not in removed]
    if not accepted:
        raise BenchmarkError("no accepted benchmark items remain after deduplication")

    taskset = [entry["item"] for entry in accepted]
    runtime_by_task = {entry["item"]["task_id"]: entry["runtime"] for entry in accepted}
    protocols = sorted(
        {entry["protocol"] for entry in accepted if entry.get("protocol")}
    )
    if family in ("swe", "deepswe"):
        missing_protocols = [
            entry["item"]["task_id"] for entry in accepted if not entry.get("protocol")
        ]
        if missing_protocols:
            raise BenchmarkError(
                "SWE taskset entries must declare a judging protocol: "
                + ", ".join(missing_protocols)
            )
    if family in ("swe", "deepswe") and len(protocols) > 1:
        raise BenchmarkError(
            "one taskset must use one judging protocol; got " + ", ".join(protocols)
        )
    audit = [
        {
            "task_id": entry["item"]["task_id"],
            "operation": entry["operation"],
            "operation_evidence": _operation_evidence_summary(
                entry.get("operation_evidence")
            ),
            "protocol": entry.get("protocol"),
            "reference": entry["positive"],
            "negative": entry["negative"],
            "case_manifest_sha256": entry["manifest_sha"],
        }
        for entry in accepted
    ]
    if quality_report["quality_overall_score"] < quality_threshold:
        raise BenchmarkError(
            "benchmark quality score "
            f"{quality_report['quality_overall_score']:.4f} below threshold "
            f"{quality_threshold:.4f}: {quality_report['quality_issues']}"
        )

    seed_count = len(by_id)
    accepted_count = len(taskset)
    quarantined_count = len(quarantine_records)
    expansion_ratio = accepted_count / seed_count
    admission_denominator = accepted_count + quarantined_count
    admission_rate = (
        accepted_count / admission_denominator if admission_denominator else 0.0
    )
    pre_removed = sum(
        1
        for record in quarantine_records
        if record["reason"]
        in ("duplicate_task_id", "duplicate_text", "duplicate_published_text")
        and not (record.get("evidence") or {}).get("curator_removed")
    )
    published_collisions = sum(
        1
        for record in quarantine_records
        if record["reason"] == "duplicate_published_text"
    )
    curator_removed_count = len(curator_removed)
    grid = _grid_report(family, sources, taskset, source.get("grid") or config.get("grid"))
    gates = {
        "min_expansion_ratio": min_expansion_ratio,
        "expansion_passed": (
            None if min_expansion_ratio is None
            else expansion_ratio >= min_expansion_ratio
        ),
        "require_grid_gain": bool(require_grid_gain),
        "grid_gain_passed": (
            None if not require_grid_gain else grid["coverage_gain"] > 0
        ),
    }
    if min_expansion_ratio is not None and expansion_ratio < min_expansion_ratio:
        raise BenchmarkError(
            f"expansion ratio {expansion_ratio:.4f} below required "
            f"{min_expansion_ratio:.4f}; grid gaps: {grid['gap_report']}"
        )
    if require_grid_gain and grid["coverage_gain"] <= 0:
        raise BenchmarkError(
            f"grid coverage gain {grid['coverage_gain']} is not positive; "
            f"gaps: {grid['gap_report']}"
        )

    quarantine_summary: Dict[str, Any] | None = None
    if quarantine_records:
        quarantine_summary = write_quarantine(
            trusted_root / "quarantine", quarantine_records
        )
    seed_ledger_info = (
        _write_seed_ledger(trusted_root, ledger) if ledger is not None else None
    )

    native_pins: Dict[str, Any] | None = None
    if native_build:
        if not rules.native_pins:
            raise BenchmarkError("family does not support native ingest")
        native_pins = rules._call(
            rules.native_pins,
            config,
            {variant["source_task_id"] for variant in variants},
        )
    taskset_fp = fingerprint_of(taskset)
    taskset_sha = canonical_sha256(taskset)
    runtime_hashes = runtime_digests(taskset, runtime_by_task)
    version = store.next_artifact_version("taskset", ref_id)

    report = {
        "recipe": "benchmark_case",
        "schema_version": "benchmark-case-v1",
        **{
            key: value
            for key, value in quality_report.items()
            if key != "curator_removed_task_ids"
        },
        "audit": audit,
        "seed_count": seed_count,
        "accepted_count": accepted_count,
        "quarantined_count": quarantined_count,
        "admission_rate": admission_rate,
        "expansion_ratio": expansion_ratio,
        "dedup": {
            "pre_removed": pre_removed,
            "curator_removed": curator_removed_count,
            "published_collisions": published_collisions,
        },
        "dedup_removed": pre_removed + curator_removed_count,
        "dedup_kept": accepted_count,
        "grid": grid,
        "operations": dict(sorted(operation_counts.items())),
        "protocols": protocols,
        "gap_report": grid["gap_report"],
        "gates": gates,
        "source_grade_counts": public_grade_counts(),
    }
    if quarantine_summary is not None:
        report["quarantine_by_reason"] = quarantine_summary["by_reason"]
    payload = {
        "ref_id": ref_id,
        "version": version,
        "family": family,
        "split": split,
        "source_release": release,
        "source_sha256": source_sha,
        "augment_config_sha256": config_sha,
        **runtime_hashes,
        "case_manifest_sha256": canonical_sha256(
            [x["case_manifest_sha256"] for x in audit]
        ),
        "taskset": taskset,
        "taskset_fingerprint": taskset_fp,
        "taskset_sha256": taskset_sha,
        "item_count": len(taskset),
        "protocols": protocols,
        "build_report": report,
        "source_grade_counts": public_grade_counts(),
        # Trusted-side lookup only; never passed through enumerate or task_file.
        "trusted_manifest_dir": str(trusted_root / "cases"),
        "runtime_by_task": runtime_by_task,
    }
    if store.uri:
        try:
            payload["trusted_manifest_rel"] = (
                (trusted_root / "cases").relative_to(Path(store.uri)).as_posix()
            )
        except ValueError:
            pass
    if seed_ledger_info is not None:
        payload["source_ledger_sha256"] = seed_ledger_info["sha256"]
        payload["source_ledger_path"] = seed_ledger_info["path"]
        payload["seed_rows_sha256"] = seed_ledger_info["seed_rows_sha256"]
    if quarantine_summary is not None:
        payload["quarantine"] = quarantine_summary
    if native_pins is not None:
        # Public-input manifest is path+sha references only (D5); the report pin
        # freezes the per-task ingest evidence. Keys are source_task_id.
        payload["ingest_report_sha256"] = native_pins["ingest_report_sha256"]
        payload["public_inputs_dir"] = native_pins["public_inputs_dir"]
        payload["public_inputs_by_task"] = native_pins["public_inputs_by_task"]
    store.put_artifact("taskset", ref_id, version, payload, fingerprint=taskset_fp)
    reread = store.get_artifact("taskset", ref_id, version)
    if (
        reread is None
        or reread["payload"]["taskset_sha256"] != taskset_sha
        or reread["payload"]["taskset_fingerprint"] != taskset_fp
    ):
        raise BenchmarkError("taskset readback failed")
    return {
        "catalog_uri": store.uri,
        "taskset": {"ref_id": ref_id, "version": version, "fingerprint": taskset_fp},
        "item_count": len(taskset),
        "build_report": report,
        "source_release": release,
        "case_manifest_sha256": payload["case_manifest_sha256"],
        **runtime_hashes,
        "taskset_sha256": taskset_sha,
    }
