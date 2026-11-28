from __future__ import annotations

import importlib
from typing import Any

_OPERATIONS = {
    "CleaningOperation": "cleaning_operator",
    "InvalidDataRemovalOperation": "invalid_data_removal_operator",
    "CorruptedDataFixOperation": "corrupted_data_fix_operator",
    "FormatStandardizationOperation": "format_standardization_operator",
    "TrajectoryCleaningOperation": "trajectory_cleaning_operator",
    "TrajectoryNormalizationOperation": "trajectory_normalization_operation",
    "ReasoningStripOperation": "reasoning_strip_operation",
    "ToolReceiptTruncationOperation": "tool_receipt_truncation_operation",
    "PIIScrubbingOperation": "pii_scrubbing_operation",
    "DegenerateTrajectoryDetectionOperation": "degenerate_trajectory_detection_operation",
}


def __getattr__(name: str) -> Any:
    module_name = _OPERATIONS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f"{__name__}.{module_name}")
    value = getattr(module, name)
    globals()[name] = value
    return value


__all__ = list(_OPERATIONS)
