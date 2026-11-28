from base.dataset import MultimodalDataset
from .trajectory_cleaning_operator import TrajectoryCleaningOperation
from .trajectory_primitives import reasoning_strip


class ReasoningStripOperation(TrajectoryCleaningOperation):
    """Remove model-internal reasoning and record every lossy transformation."""

    PARAMS_SCHEMA = {
        "category": "cleaning",
        "description": "Strip model-internal reasoning from trajectory observations",
        "params": {},
        "example": {},
    }

    def execute(self, dataset: MultimodalDataset) -> MultimodalDataset:
        def strip(row):
            omissions = []
            result = reasoning_strip(row, omissions=omissions)
            return self._append_omissions(result, omissions)

        return dataset.map(strip)
