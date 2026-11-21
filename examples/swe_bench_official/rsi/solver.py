#!/usr/bin/env python3
"""RSI candidate adapter for the official SWE-bench resolved suite.

The evaluator invokes::

    python solver.py <request.json> <result.json>

The adapter only reads the public task item and writes ``patch.diff`` under
``candidateRoot``. Gold patches and trusted manifests are never read.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCHEMA_VERSION = "benchmark-solver-v1"


def _patch_path(request: dict) -> Path:
    single = os.environ.get("SWE_CANDIDATE_PATCH_FILE")
    if single:
        return Path(single)
    directory = os.environ.get("SWE_CANDIDATE_PATCH_DIR")
    if not directory:
        raise SystemExit(
            "SWE_CANDIDATE_PATCH_FILE or SWE_CANDIDATE_PATCH_DIR is required"
        )
    source_id = request["publicItem"]["benchmark"]["source_task_id"]
    return Path(directory) / f"{source_id}.diff"


def main() -> int:
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    patch_path = _patch_path(request)
    if not patch_path.is_file():
        raise SystemExit(f"candidate patch missing: {patch_path}")
    patch = patch_path.read_text(encoding="utf-8")
    if not patch.strip():
        raise SystemExit(f"candidate patch is empty: {patch_path}")

    candidate = Path(request["candidateRoot"])
    candidate.mkdir(parents=True, exist_ok=True)
    (candidate / "patch.diff").write_text(patch, encoding="utf-8")

    result = {
        "schema_version": SCHEMA_VERSION,
        "task_id": request["taskId"],
        "attempt_index": request["attempt"]["index"],
        "seed": request["attempt"]["seed"],
        "solver_version": os.environ.get("SWE_SOLVER_VERSION", "rsi-candidate-v1"),
    }
    Path(sys.argv[2]).write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())