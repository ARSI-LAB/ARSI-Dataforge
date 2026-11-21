"""Shared solver I/O for the official SWE-bench acceptance pair."""

from __future__ import annotations

import json
from pathlib import Path


def read_request(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_result(path: str, request: dict, version: str) -> None:
    Path(path).write_text(
        json.dumps(
            {
                "schema_version": "benchmark-solver-v1",
                "task_id": request["taskId"],
                "attempt_index": request["attempt"]["index"],
                "seed": request["attempt"]["seed"],
                "solver_version": version,
            }
        ),
        encoding="utf-8",
    )


def write_patch(request: dict, patch: str) -> None:
    root = Path(request["candidateRoot"])
    root.mkdir(parents=True, exist_ok=True)
    (root / "patch.diff").write_text(patch, encoding="utf-8")
