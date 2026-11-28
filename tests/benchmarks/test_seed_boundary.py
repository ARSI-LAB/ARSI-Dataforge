"""RFC §3.3.1/§4: customer seed assets must stay inside the declared boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_designer.pipelines.benchmarks.contracts import BenchmarkError, file_sha256
from data_designer.pipelines.benchmarks.seeds import import_seed_cases


def _seed_file(path: Path) -> str:
    path.write_text(
        json.dumps(
            {
                "task_id": "customer-001",
                "task_text": "Compute the ledger total.",
                "success_criteria": "Write output/result.json.",
                "tools_available": ["files"],
                "difficulty": "easy",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return file_sha256(path)


def _source(seed_path: Path, seed_sha: str, seeds_extra: dict | None = None) -> dict:
    return {
        "family": "reasoning_qa",
        "release": "fixture",
        "split": "development",
        "license": {"id": "owned-fixture", "derive_allowed": True},
        "seeds": {
            "path": str(seed_path),
            "sha256": seed_sha,
            "runtime_template": {},
            **(seeds_extra or {}),
        },
    }


def test_seed_file_outside_boundary_is_rejected(tmp_path):
    boundary = tmp_path / "boundary"
    boundary.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    seed = outside / "seed.jsonl"
    sha = _seed_file(seed)
    with pytest.raises(BenchmarkError, match="escapes data_boundary"):
        import_seed_cases(
            _source(seed, sha), base_directory=boundary, storage_root=boundary
        )


def test_runtime_registry_outside_boundary_is_rejected(tmp_path):
    boundary = tmp_path / "boundary"
    boundary.mkdir()
    seed = boundary / "seed.jsonl"
    sha = _seed_file(seed)
    outside = tmp_path / "outside"
    outside.mkdir()
    registry = outside / "runtimes.json"
    registry.write_text("{}", encoding="utf-8")
    source = _source(
        seed,
        sha,
        {
            "runtime_template": None,
            "runtime_registry": {
                "path": str(registry),
                "sha256": file_sha256(registry),
            },
        },
    )
    source["seeds"].pop("runtime_template")
    with pytest.raises(BenchmarkError, match="escapes data_boundary"):
        import_seed_cases(source, base_directory=boundary, storage_root=boundary)


def test_boundary_must_be_an_absolute_directory(tmp_path):
    seed = tmp_path / "seed.jsonl"
    sha = _seed_file(seed)
    with pytest.raises(BenchmarkError, match="absolute directory"):
        import_seed_cases(_source(seed, sha), storage_root="relative/path")
