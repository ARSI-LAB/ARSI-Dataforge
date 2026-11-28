import copy

from base.dataset import MultimodalDataset
from base.trajectories import TRAJECTORY_DEGENERATE_FLAGS_COLUMN
from .trajectory_cleaning_operator import TrajectoryCleaningOperation
from .trajectory_primitives import DEFAULT_REWARD_HACK_SIGNALS, degenerate_detection


class DegenerateTrajectoryDetectionOperation(TrajectoryCleaningOperation):
    """Label degenerate trajectories without dropping records or omitting evidence."""

    PARAMS_SCHEMA = {
        "category": "cleaning",
        "description": "Detect repeated actions, infinite loops, and reward hacking",
        "params": {
            "repeat_action_threshold": {
                "type": "integer", "required": False, "default": 5,
                "description": "Consecutive identical actions beyond this count are flagged",
            },
            "loop_action_threshold": {
                "type": "integer", "required": False, "default": 3,
                "description": "Repeated action/observation pairs beyond this count are flagged",
            },
            "reward_hack_signals": {
                "type": "array", "required": False,
                "default": list(DEFAULT_REWARD_HACK_SIGNALS),
                "description": "Case-insensitive reward-hacking signal keywords",
            },
        },
        "example": {
            "repeat_action_threshold": 5,
            "loop_action_threshold": 3,
            "reward_hack_signals": list(DEFAULT_REWARD_HACK_SIGNALS),
        },
    }

    def execute(self, dataset: MultimodalDataset) -> MultimodalDataset:
        repeat_action_threshold = self.params["repeat_action_threshold"]
        loop_action_threshold = self.params["loop_action_threshold"]
        reward_hack_signals = self.params["reward_hack_signals"]

        def detect(row):
            flags = degenerate_detection(
                row,
                repeat_action_threshold=repeat_action_threshold,
                loop_action_threshold=loop_action_threshold,
                reward_hack_signals=reward_hack_signals,
            )
            result = copy.deepcopy(row)
            result[TRAJECTORY_DEGENERATE_FLAGS_COLUMN] = flags
            return result

        return dataset.map(detect)
