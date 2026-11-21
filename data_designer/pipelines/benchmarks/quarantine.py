"""Trusted-side quarantine artifact for item-level rejections (R4).

The file is written once per build under ``<trusted>/quarantine/<sha256>.json``.
Only the summary (counts and task ids) is copied into the taskset payload and
thence into the frozen package ``admission``; rejection evidence stays on the
trusted side.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List

from data_designer.contracts.provenance import canonical_json

QUARANTINE_SCHEMA_VERSION = "benchmark-quarantine-v1"


def write_quarantine(directory: str | Path, records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Write ``records`` as a content-addressed quarantine artifact.

    The file lands at ``<directory>/<sha256>.json`` so rebuilding into the same
    trusted directory never overwrites a prior build's artifact. Records keep
    the caller's (declaration) order so the digest is deterministic. An empty
    list produces no file and an empty summary.
    """
    by_reason: Dict[str, int] = {}
    task_ids: List[str] = []
    for record in records:
        reason = str(record.get("reason"))
        by_reason[reason] = by_reason.get(reason, 0) + 1
        task_id = record.get("task_id")
        if isinstance(task_id, str) and task_id not in task_ids:
            task_ids.append(task_id)
    summary: Dict[str, Any] = {
        "count": len(records),
        "by_reason": dict(sorted(by_reason.items())),
        "task_ids": task_ids,
    }
    if not records:
        return summary
    body = {
        "schema_version": QUARANTINE_SCHEMA_VERSION,
        "records": records,
    }
    encoded = canonical_json(body).encode("utf-8")
    sha256 = hashlib.sha256(encoded).hexdigest()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sha256}.json"
    path.write_bytes(encoded)
    summary["sha256"] = sha256
    summary["path"] = str(path)
    return summary
