from base.dataset import MultimodalDataset
from .trajectory_cleaning_operator import TrajectoryCleaningOperation
from .trajectory_primitives import tool_receipt_truncation


class ToolReceiptTruncationOperation(TrajectoryCleaningOperation):
    """Truncate tool receipts while preserving indices and error markers."""

    PARAMS_SCHEMA = {
        "category": "cleaning",
        "description": "Truncate tool-result observations",
        "params": {
            "max_chars": {
                "type": "integer", "required": False, "default": 300,
                "description": "Maximum characters to retain in tool-result observations",
            },
        },
        "example": {"max_chars": 300},
    }

    def execute(self, dataset: MultimodalDataset) -> MultimodalDataset:
        max_chars = self.params["max_chars"]

        def truncate(row):
            omissions = []
            result = tool_receipt_truncation(row, max_chars=max_chars, omissions=omissions)
            return self._append_omissions(result, omissions)

        return dataset.map(truncate)
