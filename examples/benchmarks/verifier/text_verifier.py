#!/usr/bin/env python3
"""Minimal offline verifier for the benchmark examples.

Implements the ``dataforge-benchmark`` native runner protocol:

    text_verifier.py <request.json> <result.json>

The request carries ``taskId``, ``caseManifestPath``, ``candidateRoot``,
``phase`` and (during evaluation) an ``attempt`` object. The trusted case
manifest declares ``private.verifier_spec.payload.artifact.path`` plus the
expected ``gold`` JSON object. The candidate writes that relative artifact
under ``candidateRoot``; scoring is exact JSON equality.

No third-party imports and no network access: the examples run anywhere a
Python interpreter is available. Replace this file through the example's
``runtimes.json`` to plug in a real SWE or terminal harness.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

VERSION = "example-text-verifier-v1"


def _load(path: Path):
    return json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
    )


def _result(request, *, status, passed, score, detail):
    response = {
        "task_id": request["taskId"],
        "status": status,
        "passed": passed,
        "score": score,
        "native_metrics": {"artifact_bytes": float(detail)},
        "verifier_version": VERSION,
        "evidence_refs": [],
        "runtime_seconds": 0.0,
    }
    if "attempt" in request:
        response["attempt_index"] = request["attempt"]["index"]
        response["seed"] = request["attempt"]["seed"]
    return response


def main(argv) -> int:
    started = time.monotonic()
    request = _load(Path(argv[1]))
    manifest = _load(Path(request["caseManifestPath"]))
    spec = manifest.get("private", {}).get("verifier_spec") or {}
    relative = spec.get("payload", {}).get("artifact", {}).get("path")
    gold = spec.get("payload", {}).get("gold")
    if not relative or gold is None:
        raise SystemExit("case manifest lacks an artifact_compare spec")
    candidate = Path(request["candidateRoot"]).resolve() / relative
    try:
        artifact = _load(candidate)
        size = candidate.stat().st_size
    except (OSError, ValueError):
        response = _result(
            request, status="invalid_candidate", passed=False, score=0.0, detail=0
        )
    else:
        passed = artifact == gold
        response = _result(
            request,
            status="completed",
            passed=passed,
            score=1.0 if passed else 0.0,
            detail=size,
        )
    response["runtime_seconds"] = time.monotonic() - started
    Path(argv[2]).write_text(
        json.dumps(response, sort_keys=True), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
