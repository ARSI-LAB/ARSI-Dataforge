#!/usr/bin/env python
"""Load, clean, label, and save a trajectory without leaking audit columns."""

from pathlib import Path
import sys
import tempfile

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

import ray

from curator.operations.cleaning import (
    DegenerateTrajectoryDetectionOperation,
    PIIScrubbingOperation,
    ReasoningStripOperation,
    ToolReceiptTruncationOperation,
    TrajectoryNormalizationOperation,
)
from curator.operations.loading import TextLoadingOperation
from curator.operations.saving import TextSavingOperation


def main():
    owns_ray = not ray.is_initialized()
    if owns_ray:
        ray.init(ignore_reinit_error=True, num_cpus=2)
    try:
        dataset = TextLoadingOperation({
            "data_paths": [str(SCRIPT_DIR / "trajectory_cleaning_input.jsonl")],
        }).execute()
        operations = [
            TrajectoryNormalizationOperation(),
            ReasoningStripOperation(),
            ToolReceiptTruncationOperation({"max_chars": 80}),
            PIIScrubbingOperation(),
            DegenerateTrajectoryDetectionOperation(),
        ]
        for operation in operations:
            dataset = operation.execute(dataset)

        internal = dataset.dataset.take_all()[0]
        with tempfile.TemporaryDirectory() as output_dir:
            output_path = str(Path(output_dir) / "cleaned.parquet")
            TextSavingOperation({"output_path": output_path, "format": "parquet"}).execute(dataset)
            saved = ray.data.read_parquet(output_path).take_all()[0]
            assert not any(key.startswith("_trajectory_") for key in saved)

        print(f"Cleaned trajectory: {saved['trajectory_id']}")
        print(f"Recorded omissions: {len(internal['_trajectory_omissions'])}")
        print(f"Degenerate flags: {sorted(internal['_trajectory_degenerate_flags'])}")
        print(f"Saved business columns: {sorted(saved)}")
    finally:
        if owns_ray and ray.is_initialized():
            ray.shutdown()


if __name__ == "__main__":
    main()
