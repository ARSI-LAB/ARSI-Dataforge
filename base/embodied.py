"""Embodied episode contracts shared by Curator and DataForge.

This module is contract-only: it defines the JSON-shaped episode vocabulary and
its structural validation. It does not generate samples, call world models, load
robot datasets, or write media assets.

Media (frames, point clouds, large action arrays) is referenced through
``AssetRef`` content-addressed handles rather than embedded inline. Payloads are
plain JSON so they can travel through ``RecordEnvelope`` and ``canonical_json``;
asset bytes are expected to live in a package-level store laid out as
``resources/<sha256-hex>/<filename>`` and are verified by their ``sha256``
digest. The URI prefix is a packaging convention rather than a contract
constraint: ``AssetRef.uri`` accepts any non-blank string.

Kept in ``base`` (not ``data_designer``) because ``curator`` must not import
``data_designer``; this mirrors the trajectory contract split between
``base/trajectories.py`` and ``data_designer/contracts/trajectories.py``.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Dict, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .provenance import canonical_json, content_digest

EMBODIED_EPISODE_SCHEMA = "embodied-episode-v1"
EMBODIED_SCHEMA_VERSION = EMBODIED_EPISODE_SCHEMA

# Documented conventions, not closed enums: add new strings without changing
# the contract version. Validation only requires a non-empty value.
KNOWN_CONTROL_MODES = frozenset(
    {"joint_position", "joint_velocity", "joint_torque", "ee_delta_pose", "ee_absolute_pose"}
)
KNOWN_ACTION_SPACES = frozenset(
    {"joint_position", "joint_velocity", "joint_torque", "ee_delta_pose", "ee_absolute_pose", "binary_gripper"}
)


def _assert_finite(values: Tuple[float, ...], field_name: str) -> None:
    for index, value in enumerate(values):
        if not math.isfinite(value):
            raise ValueError(f"{field_name}[{index}] must be finite")


def _assert_non_blank(value: str, field_name: str) -> str:
    if not value.strip() or "\x00" in value:
        raise ValueError(f"{field_name} must be non-empty and contain no NUL")
    return value


class AssetRef(BaseModel):
    """Content-addressed reference to one media asset.

    ``uri`` is package-relative in published datasets; ``digest`` pins the bytes
    so a package can detect drift. Nothing in this contract reads the asset.
    """

    model_config = ConfigDict(extra="forbid")

    uri: str = Field(min_length=1)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    media_type: str = Field(min_length=1)
    shape: Tuple[Annotated[int, Field(strict=True)], ...] = ()
    dtype: str = ""
    fps: Optional[float] = Field(default=None, strict=True)

    @field_validator("uri")
    @classmethod
    def _uri_non_blank(cls, value: str) -> str:
        return _assert_non_blank(value, "asset uri")

    @field_validator("media_type")
    @classmethod
    def _media_type_non_blank(cls, value: str) -> str:
        return _assert_non_blank(value, "asset media_type")

    @field_validator("shape")
    @classmethod
    def _shape(cls, value: Tuple[int, ...]) -> Tuple[int, ...]:
        if any(item < 0 for item in value):
            raise ValueError("shape entries must be non-negative")
        return value

    @field_validator("fps")
    @classmethod
    def _fps(cls, value: Optional[float]) -> Optional[float]:
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("fps must be positive and finite")
        return value

    def fingerprint(self) -> str:
        return content_digest(self.model_dump(mode="json"))


class CameraModel(BaseModel):
    """Pinhole camera calibration for one observation stream."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    intrinsics: Tuple[Annotated[float, Field(strict=True)], ...]
    extrinsics: Tuple[Annotated[float, Field(strict=True)], ...]
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _assert_non_blank(value, "camera name")

    @field_validator("intrinsics")
    @classmethod
    def _intrinsics(cls, value: Tuple[float, ...]) -> Tuple[float, ...]:
        if len(value) != 4:
            raise ValueError("intrinsics must be [fx, fy, cx, cy]")
        _assert_finite(value, "intrinsics")
        return value

    @field_validator("extrinsics")
    @classmethod
    def _extrinsics(cls, value: Tuple[float, ...]) -> Tuple[float, ...]:
        if len(value) != 16:
            raise ValueError("extrinsics must contain 16 values (row-major 4x4)")
        _assert_finite(value, "extrinsics")
        return value


class RobotSpec(BaseModel):
    """Embodiment and control interface for one episode."""

    model_config = ConfigDict(extra="forbid")

    embodiment: str = Field(min_length=1)
    dof: int = Field(gt=0, strict=True)
    control_mode: str = Field(min_length=1)
    control_freq_hz: float = Field(strict=True)
    gripper: Optional[str] = None

    @field_validator("embodiment")
    @classmethod
    def _embodiment_non_blank(cls, value: str) -> str:
        return _assert_non_blank(value, "robot embodiment")

    @field_validator("control_mode")
    @classmethod
    def _control_mode_non_blank(cls, value: str) -> str:
        return _assert_non_blank(value, "robot control_mode")

    @field_validator("control_freq_hz")
    @classmethod
    def _frequency(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("control_freq_hz must be positive and finite")
        return value

    @field_validator("gripper")
    @classmethod
    def _gripper(cls, value: Optional[str]) -> Optional[str]:
        if value is not None:
            _assert_non_blank(value, "gripper")
        return value


class Observation(BaseModel):
    """One timestamped observation sample."""

    model_config = ConfigDict(extra="forbid")

    timestamp_ns: int = Field(ge=0, strict=True)
    images: Dict[str, AssetRef] = Field(default_factory=dict)
    point_cloud: Optional[AssetRef] = None
    proprio: Tuple[Annotated[float, Field(strict=True)], ...] = ()
    language: str = ""

    @field_validator("images")
    @classmethod
    def _image_keys(cls, value: Dict[str, AssetRef]) -> Dict[str, AssetRef]:
        for key in value:
            _assert_non_blank(key, "image key")
        return value

    @field_validator("proprio")
    @classmethod
    def _proprio(cls, value: Tuple[float, ...]) -> Tuple[float, ...]:
        _assert_finite(value, "proprio")
        return value


class ActionChunk(BaseModel):
    """One timestamped action sample or action chunk."""

    model_config = ConfigDict(extra="forbid")

    timestamp_ns: int = Field(ge=0, strict=True)
    values: Tuple[Annotated[float, Field(strict=True)], ...] = ()
    action_space: str = ""

    @field_validator("values")
    @classmethod
    def _values(cls, value: Tuple[float, ...]) -> Tuple[float, ...]:
        _assert_finite(value, "action values")
        return value


class EpisodeLabel(BaseModel):
    """Task instruction and outcome signals for one episode."""

    model_config = ConfigDict(extra="forbid")

    instruction: str = ""
    subgoal: str = ""
    success: Optional[bool] = Field(default=None, strict=True)
    reward: Optional[float] = Field(default=None, strict=True)
    failure_reason: str = ""

    @field_validator("reward")
    @classmethod
    def _reward(cls, value: Optional[float]) -> Optional[float]:
        if value is not None and not math.isfinite(value):
            raise ValueError("reward must be finite")
        return value


class EmbodiedEpisode(BaseModel):
    """One normalized embodied episode (schema ``embodied-episode-v1``).

    Construction validates structure only. Use ``validate_temporal_order`` for
    the optional chronological invariant, and ``fingerprint`` for the
    content-addressed identity used by provenance and package digests.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["embodied-episode-v1"] = EMBODIED_EPISODE_SCHEMA
    episode_id: str = Field(min_length=1)
    robot: RobotSpec
    cameras: Tuple[CameraModel, ...] = ()
    observations: Tuple[Observation, ...] = ()
    actions: Tuple[ActionChunk, ...] = ()
    label: EpisodeLabel = Field(default_factory=EpisodeLabel)
    source: Dict[str, Any] = Field(default_factory=dict)
    generation: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("episode_id")
    @classmethod
    def _episode_id(cls, value: str) -> str:
        return _assert_non_blank(value, "episode_id")

    @model_validator(mode="after")
    def _metadata_is_json_safe(self) -> "EmbodiedEpisode":
        try:
            canonical_json(self.source)
            canonical_json(self.generation)
        except (TypeError, ValueError) as error:
            raise ValueError(f"source/generation must be JSON-serializable: {error}") from error
        return self

    def to_payload(self) -> Dict[str, Any]:
        """Return the JSON-compatible payload suitable for ``RecordEnvelope``."""
        return self.model_dump(mode="json")

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "EmbodiedEpisode":
        return cls.model_validate(payload)

    def fingerprint(self) -> str:
        return content_digest(self.to_payload())

    def asset_refs(self) -> Tuple[AssetRef, ...]:
        """Collect every referenced media asset in observation order."""
        refs = []
        for observation in self.observations:
            refs.extend(observation.images.values())
            if observation.point_cloud is not None:
                refs.append(observation.point_cloud)
        return tuple(refs)

    def validate_temporal_order(self) -> None:
        """Raise when observation or action timestamps move backwards."""
        for name, timestamps in (
            ("observations", [item.timestamp_ns for item in self.observations]),
            ("actions", [item.timestamp_ns for item in self.actions]),
        ):
            if any(later < earlier for earlier, later in zip(timestamps, timestamps[1:])):
                raise ValueError(f"{name} timestamps must be non-decreasing")


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
