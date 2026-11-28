from __future__ import annotations

import copy

from base.dataset import MultimodalDataset
from base.trajectories import TRAJECTORY_NORMALIZED_COLUMN
from .trajectory_cleaning_operator import TrajectoryCleaningOperation
from .trajectory_primitives import normalize_trajectory


class TrajectoryNormalizationOperation(TrajectoryCleaningOperation):
    """Normalize host trajectory dictionaries into the canonical schema."""

    PARAMS_SCHEMA = {
        "category": "cleaning",
        "description": "Normalize host trajectory dictionaries",
        "params": {
            "default_harness": {
                "type": "string", "required": False, "default": "unknown",
                "description": "Host identifier used when the record does not declare a harness",
            },
            "require_core_meta": {
                "type": "boolean", "required": False, "default": True,
                "description": "Require non-empty agent_version and model_version",
            },
        },
        "example": {"default_harness": "unknown", "require_core_meta": True},
    }

    def execute(self, dataset: MultimodalDataset) -> MultimodalDataset:
        default_harness = self.params["default_harness"]
        require_core_meta = self.params["require_core_meta"]

        def normalize(row):
            if row.get(TRAJECTORY_NORMALIZED_COLUMN):
                return copy.deepcopy(row)
            normalized = normalize_trajectory(
                row,
                default_harness=default_harness,
                require_core_meta=require_core_meta,
            )
            preserved = {
                key: copy.deepcopy(value)
                for key, value in row.items()
                if key not in normalized
            }
            return {**row, **preserved, **normalized, TRAJECTORY_NORMALIZED_COLUMN: True}

        return dataset.map(normalize)
