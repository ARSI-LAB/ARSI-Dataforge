"""Compatibility exports; import from :mod:`base.embodied` in new code."""

from base.embodied import (
    EMBODIED_EPISODE_SCHEMA,
    EMBODIED_SCHEMA_VERSION,
    KNOWN_ACTION_SPACES,
    KNOWN_CONTROL_MODES,
    ActionChunk,
    AssetRef,
    CameraModel,
    EmbodiedEpisode,
    EpisodeLabel,
    Observation,
    RobotSpec,
)

__all__ = [
    "EMBODIED_EPISODE_SCHEMA",
    "EMBODIED_SCHEMA_VERSION",
    "KNOWN_ACTION_SPACES",
    "KNOWN_CONTROL_MODES",
    "ActionChunk",
    "AssetRef",
    "CameraModel",
    "EmbodiedEpisode",
    "EpisodeLabel",
    "Observation",
    "RobotSpec",
]
