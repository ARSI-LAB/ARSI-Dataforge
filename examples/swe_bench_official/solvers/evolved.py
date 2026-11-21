#!/usr/bin/env python3
"""Evolved candidate: emits a precomputed candidate patch.

The patch is produced by an external RSI policy. It is never read from the
gold/trusted side or the evaluation workspace, so the evaluator boundary is
preserved. Two transport forms are supported:

* ``SWE_EVOLVED_PATCH_FILE`` for a single-task run;
* ``SWE_EVOLVED_PATCH_DIR`` with one ``<source_task_id>.diff`` per task for a
  multi-task suite run.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from common import read_request, write_patch, write_result


def _patch_for(request: dict) -> str:
    single = os.environ.get("SWE_EVOLVED_PATCH_FILE")
    if single:
        return Path(single).read_text(encoding="utf-8")
    directory = os.environ.get("SWE_EVOLVED_PATCH_DIR")
    if not directory:
        raise SystemExit(
            "SWE_EVOLVED_PATCH_FILE or SWE_EVOLVED_PATCH_DIR is required"
        )
    source_id = request["publicItem"]["benchmark"]["source_task_id"]
    path = Path(directory) / f"{source_id}.diff"
    if not path.is_file():
        raise SystemExit(f"evolved candidate patch missing: {path}")
    return path.read_text(encoding="utf-8")


def main() -> int:
    request = read_request(sys.argv[1])
    patch = _patch_for(request)
    if not patch.strip():
        raise SystemExit("evolved candidate patch is empty")
    write_patch(request, patch)
    write_result(sys.argv[2], request, "swe-official-evolved-v1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
