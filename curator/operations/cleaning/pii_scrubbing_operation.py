from base.dataset import MultimodalDataset
from .trajectory_cleaning_operator import TrajectoryCleaningOperation
from .trajectory_primitives import pii_scrubbing


class PIIScrubbingOperation(TrajectoryCleaningOperation):
    """Mask PII and secrets with explicit omission evidence."""

    PARAMS_SCHEMA = {
        "category": "cleaning",
        "description": "Mask PII and secrets in trajectory text",
        "params": {
            "extra_patterns": {
                "type": "array", "required": False, "default": [],
                "description": "Additional [name, regular-expression] rules",
            },
            "scrub_task": {
                "type": "boolean", "required": False, "default": True,
                "description": "Whether to also clean the task field",
            },
        },
        "example": {"extra_patterns": [], "scrub_task": True},
    }

    def execute(self, dataset: MultimodalDataset) -> MultimodalDataset:
        extra_patterns = self.params["extra_patterns"]
        scrub_task = self.params["scrub_task"]

        def scrub(row):
            omissions = []
            result = pii_scrubbing(
                row,
                extra_patterns=extra_patterns,
                scrub_task=scrub_task,
                omissions=omissions,
            )
            return self._append_omissions(result, omissions)

        return dataset.map(scrub)
