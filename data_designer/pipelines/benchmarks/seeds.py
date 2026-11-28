"""Custom seed import and source ledger (R2).

``benchmark-source-v1.seeds`` turns a customer-owned JSONL seed file into
ordinary source cases. The conversion reuses the shared ``agent-task-v1``
validator from :mod:`data_designer.contracts.seeds`; admission failures
(``runtime_unavailable`` / ``materialization_unavailable``) are recorded in the
ledger and excluded without aborting the batch.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from data_designer.contracts.seeds import (
    SEED_SIDECAR_PASSTHROUGH_KEY,
    SEED_SIDECAR_SOURCE_META_KEY,
    SeedValidationError,
    validate_seed_rows,
)

from .contracts import (
    BenchmarkError,
    canonical_sha256,
    file_sha256,
    require_nonempty,
    require_sha,
)
from .runtime import lock_runtime

SEED_LEDGER_SCHEMA_VERSION = "benchmark-seed-ledger-v1"
RUNTIME_REGISTRY_SCHEMA_VERSION = "benchmark-runtime-registry-v1"
TEXT_FAMILIES = frozenset({"reasoning_qa", "knowledge_qa", "code_gen", "health_qa"})
_SOURCE_TASK_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_SEEDS_KEYS = frozenset(
    {
        "path",
        "sha256",
        "runtime_template",
        "runtime_registry",
        "materialization",
        "passthrough_columns",
    }
)


def _validate_seeds_block(block: Any) -> Dict[str, Any]:
    if not isinstance(block, dict):
        raise BenchmarkError("source.seeds must be an object")
    unknown = sorted(set(block) - _SEEDS_KEYS)
    if unknown:
        raise BenchmarkError(f"source.seeds has unknown field(s): {', '.join(unknown)}")
    has_template = "runtime_template" in block
    has_registry = "runtime_registry" in block
    if has_template == has_registry:
        raise BenchmarkError(
            "source.seeds requires exactly one of runtime_template or runtime_registry"
        )
    passthrough = block.get("passthrough_columns", [])
    if not isinstance(passthrough, list) or not all(
        isinstance(column, str) and column for column in passthrough
    ):
        raise BenchmarkError("source.seeds.passthrough_columns must be a list of strings")
    return block


def _resolve(
    base: Path, value: Any, name: str, root: Path | None = None
) -> Path:
    path = Path(require_nonempty(value, name))
    resolved = (base / path).resolve() if not path.is_absolute() else path.resolve()
    if root is not None and not resolved.is_relative_to(root):
        raise BenchmarkError(f"{name} escapes data_boundary: {resolved}")
    return resolved


def _read_rows(path: Path) -> List[Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise BenchmarkError(f"cannot read seed file {path}: {error}") from error
    rows: List[Any] = []
    errors: List[str] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as error:
            errors.append(f"line {line_no}: invalid JSON: {error}")
    if errors:
        raise SeedValidationError(errors)
    return rows


def _load_registry(
    block: Dict[str, Any], base: Path, root: Path | None = None
) -> Dict[str, Any]:
    record = block.get("runtime_registry")
    if not isinstance(record, dict):
        raise BenchmarkError("source.seeds.runtime_registry must be an object")
    path = _resolve(base, record.get("path"), "runtime_registry.path", root)
    sha = require_sha(record.get("sha256"), "runtime_registry.sha256")
    if not path.is_file() or file_sha256(path) != sha:
        raise BenchmarkError("runtime registry missing or SHA-256 drift")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BenchmarkError(f"cannot read runtime registry {path}: {error}") from error
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != RUNTIME_REGISTRY_SCHEMA_VERSION
        or not isinstance(value.get("cases"), dict)
    ):
        raise BenchmarkError("runtime registry has an unsupported schema")
    return value["cases"]


def _text_state(item: Dict[str, Any], family: str, release: str) -> Tuple[str, str]:
    initial = canonical_sha256(
        {
            "stage": "seed-prompt",
            "task_text": item["task_text"],
            "family": family,
            "release": release,
        }
    )
    expected = canonical_sha256(
        {
            "success_criteria": item["success_criteria"],
            "scoring_rubric": item["scoring_rubric"],
            "ground_truth_action_sequence": item["ground_truth_action_sequence"],
        }
    )
    return initial, expected


def _artifact_state(
    block: Dict[str, Any], original_id: str
) -> Tuple[str, str] | None:
    cases = block.get("cases")
    record = cases.get(original_id) if isinstance(cases, dict) else None
    if record is None:
        record = block
    if not isinstance(record, dict):
        return None
    initial = record.get("initial_state_sha256")
    expected = record.get("expected_state_sha256")
    if initial is None or expected is None:
        return None
    try:
        return (
            require_sha(initial, "materialization.initial_state_sha256"),
            require_sha(expected, "materialization.expected_state_sha256"),
        )
    except BenchmarkError:
        return None


def import_seed_cases(
    source: Dict[str, Any],
    *,
    base_directory: str | Path | None = None,
    storage_root: str | Path | None = None,
) -> Tuple[Dict[str, Any], Dict[str, Any] | None]:
    """Return ``(expanded_source, ledger)``.

    ``ledger`` is ``None`` when the source has no ``seeds`` block. Otherwise it
    contains ``records``, ``seed_rows_sha256`` and ``source_grade_counts``; the
    build writes the JSONL and records its digest in the taskset payload.
    """
    if not isinstance(source, dict):
        raise BenchmarkError("source must be an object")
    block = source.get("seeds")
    if block is None:
        return source, None
    block = _validate_seeds_block(block)
    base = Path(base_directory or Path.cwd())
    root = None
    if storage_root is not None:
        root = Path(storage_root)
        if not root.is_absolute() or not root.is_dir():
            raise BenchmarkError(
                "data_boundary must be an existing absolute directory"
            )
        root = root.resolve()
    family = require_nonempty(source.get("family"), "family")
    release = require_nonempty(source.get("release"), "release")
    text_family = family in TEXT_FAMILIES

    seed_path = _resolve(base, block.get("path"), "seeds.path", root)
    seed_sha = require_sha(block.get("sha256"), "seeds.sha256")
    if not seed_path.is_file() or file_sha256(seed_path) != seed_sha:
        raise BenchmarkError("seed file missing or SHA-256 drift")

    passthrough_columns = tuple(block.get("passthrough_columns") or ())
    rows = _read_rows(seed_path)
    items = validate_seed_rows(
        rows, strict=True, passthrough_columns=passthrough_columns
    )

    template_runtime = None
    registry_cases = None
    if "runtime_template" in block:
        runtime_source = "template"
        template_runtime = lock_runtime(block["runtime_template"])
    else:
        runtime_source = "registry"
        registry_cases = _load_registry(block, base, root)
        for entry in registry_cases.values():
            lock_runtime(entry)
    materialization = block.get("materialization")
    if materialization is not None and not isinstance(materialization, dict):
        raise BenchmarkError("source.seeds.materialization must be an object")

    official = source.get("cases") or []
    if not isinstance(official, list):
        raise BenchmarkError("source.cases must be a list")
    merged: List[Dict[str, Any]] = []
    official_ids = set()
    for case in official:
        if not isinstance(case, dict):
            raise BenchmarkError("source case must be an object")
        source_task_id = require_nonempty(case.get("source_task_id"), "source_task_id")
        if source_task_id in official_ids:
            raise BenchmarkError("duplicate or invalid source case IDs")
        official_ids.add(source_task_id)
        merged.append({**case, "source_grade": "official_native"})

    records: List[Dict[str, Any]] = []
    owned_ids = set()
    for line_no, (raw, item) in enumerate(zip(rows, items), 1):
        original_id = item["seed_id"]
        if _SOURCE_TASK_ID_RE.fullmatch(original_id):
            source_task_id = "seed:" + original_id
        else:
            source_task_id = "seed:" + canonical_sha256(original_id)[:12]
        if source_task_id in owned_ids or source_task_id in official_ids:
            raise BenchmarkError(
                f"duplicate seed source_task_id at line {line_no}: {source_task_id}"
            )
        owned_ids.add(source_task_id)

        admission = "expressible"
        exclusion_reason = None
        runtime = None
        state = None
        if not text_family and materialization is None:
            admission = "excluded"
            exclusion_reason = "materialization_unavailable"
        elif not text_family:
            state = _artifact_state(materialization, original_id)
            if state is None:
                admission = "excluded"
                exclusion_reason = "materialization_unavailable"
        if admission == "expressible":
            if runtime_source == "template":
                runtime = template_runtime
            else:
                runtime = registry_cases.get(original_id)
                if runtime is None:
                    admission = "excluded"
                    exclusion_reason = "runtime_unavailable"
        if admission == "expressible" and state is None:
            state = _text_state(item, family, release)

        passthrough = item.get(SEED_SIDECAR_PASSTHROUGH_KEY, {})
        source_meta = item.get(SEED_SIDECAR_SOURCE_META_KEY, {})
        record = {
            "schema_version": SEED_LEDGER_SCHEMA_VERSION,
            "line": line_no,
            "source_task_id": source_task_id,
            "original_seed_id": original_id,
            "source_grade": "owned_native",
            "admission": admission,
            "runtime_source": runtime_source,
            "seed_sha256": canonical_sha256(raw),
            "source_meta": source_meta,
            "passthrough": passthrough,
        }
        if exclusion_reason is not None:
            record["exclusion_reason"] = exclusion_reason
        records.append(record)
        if admission != "expressible":
            continue
        case = {
            "source_task_id": source_task_id,
            "task_text": item["task_text"],
            "initial_state_sha256": state[0],
            "expected_state_sha256": state[1],
            "source_grade": "owned_native",
            "seed_id": original_id,
            "success_criteria": item["success_criteria"],
            "tools_available": item["tools_available"],
            "difficulty": item["difficulty"],
            "source_meta": source_meta,
            "runtime": runtime,
        }
        merged.append(case)

    expressible = sum(1 for record in records if record["admission"] == "expressible")
    if expressible == 0:
        counts: Dict[str, int] = {}
        for record in records:
            key = record.get("exclusion_reason") or "unknown"
            counts[key] = counts.get(key, 0) + 1
        raise BenchmarkError(f"no expressible seed cases: {counts}")

    ledger = {
        "schema_version": SEED_LEDGER_SCHEMA_VERSION,
        "records": records,
        "seed_rows_sha256": seed_sha,
        "source_grade_counts": {
            "owned_native": expressible,
            "official_native": len(official),
        },
    }
    return {**source, "cases": merged}, ledger
