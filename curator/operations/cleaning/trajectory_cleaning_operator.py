"""Shared validation and audit helpers for trajectory cleaning Operations."""
from __future__ import annotations

import copy
from typing import Any, Dict, List

from base.trajectories import OmissionEntry, TRAJECTORY_OMISSIONS_COLUMN
from .cleaning_operator import CleaningOperation


class TrajectoryCleaningOperation(CleaningOperation):
    """Base class for Operations that preserve trajectory and upstream fields."""

    @classmethod
    def _apply_param_defaults(cls, params: Dict[str, Any]) -> Dict[str, Any]:
        schema_params = cls.PARAMS_SCHEMA.get("params", {})
        unknown = sorted(set(params) - set(schema_params))
        if unknown:
            raise ValueError(f"{cls.__name__}: unknown parameter(s): {', '.join(unknown)}")

        result = copy.deepcopy(super()._apply_param_defaults(params))
        for key, spec in schema_params.items():
            if key not in result:
                continue
            value = result[key]
            expected = spec.get("type")
            valid = (
                (expected == "integer" and type(value) is int)
                or (expected == "string" and isinstance(value, str))
                or (expected == "boolean" and type(value) is bool)
                or (expected == "array" and isinstance(value, list))
            )
            if not valid:
                raise ValueError(f"{cls.__name__}: parameter '{key}' must be of type {expected}")
        return result

    @staticmethod
    def _append_omissions(row: Dict[str, Any], omissions: List[OmissionEntry]) -> Dict[str, Any]:
        if not omissions:
            return row
        row[TRAJECTORY_OMISSIONS_COLUMN] = [
            *row.get(TRAJECTORY_OMISSIONS_COLUMN, []),
            *(entry.as_dict() for entry in omissions),
        ]
        return row
