import math
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from base.embodied import (
    EMBODIED_EPISODE_SCHEMA,
    KNOWN_CONTROL_MODES,
    ActionChunk,
    AssetRef,
    CameraModel,
    EmbodiedEpisode,
    EpisodeLabel,
    Observation,
    RobotSpec,
)
from data_designer.contracts import embodied as compat
from data_designer.contracts.datasets import RecordEnvelope
from data_designer.contracts.provenance import canonical_json

DIGEST = "sha256:" + "a" * 64


def make_asset(name="frame.jpg", media_type="image/jpeg"):
    return AssetRef(
        uri=f"resources/{name}",
        digest=DIGEST,
        media_type=media_type,
        shape=(480, 640, 3),
        dtype="uint8",
    )


def make_episode(**overrides):
    base = dict(
        episode_id="ep-1",
        robot=RobotSpec(
            embodiment="franka_panda",
            dof=7,
            control_mode="joint_position",
            control_freq_hz=10.0,
        ),
        cameras=(
            CameraModel(
                name="wrist",
                intrinsics=(600.0, 600.0, 320.0, 240.0),
                extrinsics=tuple(float(i) for i in range(16)),
                width=640,
                height=480,
            ),
        ),
        observations=(
            Observation(
                timestamp_ns=0,
                images={"wrist": make_asset()},
                proprio=(0.0,) * 7,
                language="pick up the cube",
            ),
        ),
        actions=(ActionChunk(timestamp_ns=0, values=(0.0,) * 7, action_space="joint_position"),),
        label=EpisodeLabel(instruction="pick up the cube", success=True, reward=1.0),
        source={"dataset": "fixture", "split": "train"},
        generation={"model": "stub", "version": "0", "seed": 42},
    )
    base.update(overrides)
    return EmbodiedEpisode(**base)


def test_payload_roundtrip_is_json_and_stable():
    episode = make_episode()
    payload = episode.to_payload()

    canonical_json(payload)  # must be JSON-serializable with allow_nan=False
    assert isinstance(payload["observations"][0]["proprio"], list)

    restored = EmbodiedEpisode.from_payload(payload)
    assert restored == episode
    assert restored.fingerprint() == episode.fingerprint()
    assert episode.fingerprint().startswith("sha256:")


def test_fingerprint_tracks_content():
    first = make_episode()
    second = make_episode(episode_id="ep-2")
    same = make_episode()

    assert first.fingerprint() != second.fingerprint()
    assert first.fingerprint() == same.fingerprint()


def test_asset_digest_must_be_sha256():
    for bad in ("md5:abc", "sha256:short", "sha256:" + "g" * 64):
        with pytest.raises(ValidationError):
            AssetRef(uri="resources/a.bin", digest=bad, media_type="application/octet-stream")


def test_non_finite_numbers_are_rejected():
    with pytest.raises(ValidationError):
        make_episode(observations=(Observation(timestamp_ns=0, proprio=(0.0, math.nan)),))
    with pytest.raises(ValidationError):
        make_episode(actions=(ActionChunk(timestamp_ns=0, values=(math.inf,)),))
    with pytest.raises(ValidationError):
        make_episode(label=EpisodeLabel(reward=math.inf))
    with pytest.raises(ValidationError):
        AssetRef(uri="resources/a.bin", digest=DIGEST, media_type="application/octet-stream", fps=math.inf)


def test_structural_validation():
    with pytest.raises(ValidationError):
        make_episode(episode_id="   ")
    with pytest.raises(ValidationError):
        RobotSpec(embodiment="arm", dof=0, control_mode="joint_position", control_freq_hz=10.0)
    with pytest.raises(ValidationError):
        RobotSpec(embodiment="arm", dof=7, control_mode="joint_position", control_freq_hz=0.0)
    with pytest.raises(ValidationError):
        CameraModel(
            name="cam",
            intrinsics=(1.0, 2.0, 3.0),
            extrinsics=tuple(float(i) for i in range(16)),
            width=10,
            height=10,
        )
    with pytest.raises(ValidationError):
        CameraModel(
            name="cam",
            intrinsics=(1.0, 2.0, 3.0, 4.0),
            extrinsics=tuple(float(i) for i in range(15)),
            width=10,
            height=10,
        )
    with pytest.raises(ValidationError):
        CameraModel(
            name="cam",
            intrinsics=(1.0, 2.0, 3.0, 4.0),
            extrinsics=tuple(float(i) for i in range(16)),
            width=0,
            height=10,
        )


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        RobotSpec(
            embodiment="arm",
            dof=7,
            control_mode="joint_position",
            control_freq_hz=10.0,
            typo=1,
        )
    with pytest.raises(ValidationError):
        make_episode(unexpected="value")


def test_source_and_generation_must_be_json_safe():
    with pytest.raises(ValidationError):
        make_episode(source={"bad": math.nan})
    with pytest.raises(ValidationError):
        make_episode(generation={"bad": {1, 2}})


def test_temporal_order_is_opt_in():
    ordered = make_episode(
        observations=(Observation(timestamp_ns=0), Observation(timestamp_ns=10)),
        actions=(ActionChunk(timestamp_ns=0), ActionChunk(timestamp_ns=10)),
    )
    ordered.validate_temporal_order()

    backwards = make_episode(
        observations=(Observation(timestamp_ns=10), Observation(timestamp_ns=5))
    )
    with pytest.raises(ValueError, match="non-decreasing"):
        backwards.validate_temporal_order()


def test_asset_refs_collect_images_and_point_cloud():
    wrist = make_asset("wrist.jpg")
    cloud = make_asset("cloud.npy", "application/x-npy")
    episode = make_episode(
        observations=(
            Observation(timestamp_ns=0, images={"wrist": wrist}, point_cloud=cloud),
            Observation(timestamp_ns=1, images={"wrist": wrist}),
        )
    )

    assert episode.asset_refs() == (wrist, cloud, wrist)


def test_schema_version_is_pinned():
    assert make_episode().schema_version == EMBODIED_EPISODE_SCHEMA
    with pytest.raises(ValidationError):
        make_episode(schema_version="embodied-episode-v2")


def test_documented_conventions_are_not_closed_enums():
    assert "joint_position" in KNOWN_CONTROL_MODES
    # New control modes remain valid strings without a contract version bump.
    robot = RobotSpec(
        embodiment="custom-arm",
        dof=6,
        control_mode="site_velocity",
        control_freq_hz=20.0,
    )
    assert robot.control_mode == "site_velocity"


def test_compatibility_module_reexports_identity():
    assert compat.EmbodiedEpisode is EmbodiedEpisode
    assert compat.AssetRef is AssetRef
    assert compat.EMBODIED_EPISODE_SCHEMA == EMBODIED_EPISODE_SCHEMA


def test_contract_import_is_lightweight():
    root = Path(__file__).resolve().parents[2]
    probe = (
        "import sys; "
        "import base.embodied; "
        "import data_designer.contracts.embodied; "
        "heavy = [m for m in sys.modules "
        "if m == 'curator' or m.startswith('curator.') "
        "or m == 'torch' or m.startswith('torch.') "
        "or m == 'ray' or m.startswith('ray.')]; "
        "assert not heavy, heavy"
    )
    process = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(root),
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(root), "HOME": str(root)},
    )
    assert process.returncode == 0, process.stderr


def test_strict_types_reject_coercion():
    # §7.1: a JSON type mismatch is a ValidationError, not a silent coercion.
    for bad_int in ("7", 7.0, True):
        with pytest.raises(ValidationError):
            RobotSpec(
                embodiment="arm",
                dof=bad_int,
                control_mode="joint_position",
                control_freq_hz=10.0,
            )
    for bad_float in ("10", True):
        with pytest.raises(ValidationError):
            RobotSpec(
                embodiment="arm",
                dof=7,
                control_mode="joint_position",
                control_freq_hz=bad_float,
            )
    with pytest.raises(ValidationError):
        Observation(timestamp_ns=True)
    with pytest.raises(ValidationError):
        Observation(timestamp_ns=1.0)
    with pytest.raises(ValidationError):
        Observation(timestamp_ns=0, proprio=(True,))
    with pytest.raises(ValidationError):
        ActionChunk(timestamp_ns=0, values=(False,))
    with pytest.raises(ValidationError):
        CameraModel(
            name="cam",
            intrinsics=(1.0, 2.0, 3.0, 4.0),
            extrinsics=tuple(float(i) for i in range(16)),
            width=True,
            height=10,
        )
    with pytest.raises(ValidationError):
        CameraModel(
            name="cam",
            intrinsics=(True, 2.0, 3.0, 4.0),
            extrinsics=tuple(float(i) for i in range(16)),
            width=10,
            height=10,
        )
    with pytest.raises(ValidationError):
        AssetRef(
            uri="resources/a.bin",
            digest=DIGEST,
            media_type="application/octet-stream",
            shape=["480", "640"],
        )
    with pytest.raises(ValidationError):
        EpisodeLabel(success="false")
    with pytest.raises(ValidationError):
        EpisodeLabel(success=1)
    with pytest.raises(ValidationError):
        EpisodeLabel(reward="1.5")
    with pytest.raises(ValidationError):
        make_episode(episode_id=3)
    # JSON integers remain valid for float fields and lists for tuple containers.
    assert (
        RobotSpec(
            embodiment="arm", dof=7, control_mode="joint_position", control_freq_hz=10
        ).control_freq_hz
        == 10.0
    )
    assert EpisodeLabel(success=None, reward=1).reward == 1.0
    assert Observation(timestamp_ns=0, proprio=[0, 1, 2]).proprio == (0.0, 1.0, 2.0)
    assert ActionChunk(timestamp_ns=0, values=[1, 2]).values == (1.0, 2.0)


def test_asset_fingerprint_tracks_metadata():
    asset = make_asset()
    same = make_asset()
    other_uri = make_asset("other.jpg")
    other_shape = AssetRef(
        uri="resources/frame.jpg",
        digest=DIGEST,
        media_type="image/jpeg",
        shape=(480, 640),
        dtype="uint8",
    )
    other_digest = AssetRef(
        uri="resources/frame.jpg",
        digest="sha256:" + "b" * 64,
        media_type="image/jpeg",
        shape=(480, 640, 3),
        dtype="uint8",
    )
    other_media_type = make_asset(media_type="image/png")
    other_dtype = AssetRef(
        uri="resources/frame.jpg",
        digest=DIGEST,
        media_type="image/jpeg",
        shape=(480, 640, 3),
        dtype="float32",
    )
    other_fps = AssetRef(
        uri="resources/frame.jpg",
        digest=DIGEST,
        media_type="image/jpeg",
        shape=(480, 640, 3),
        dtype="uint8",
        fps=30.0,
    )

    assert asset.fingerprint() == same.fingerprint()
    assert asset.fingerprint() != other_uri.fingerprint()
    assert asset.fingerprint() != other_shape.fingerprint()
    assert asset.fingerprint() != other_digest.fingerprint()
    assert asset.fingerprint() != other_media_type.fingerprint()
    assert asset.fingerprint() != other_dtype.fingerprint()
    assert asset.fingerprint() != other_fps.fingerprint()
    assert asset.fingerprint().startswith("sha256:")


def test_blank_and_nul_identifiers_are_rejected():
    with pytest.raises(ValidationError):
        AssetRef(uri="   ", digest=DIGEST, media_type="image/jpeg")
    with pytest.raises(ValidationError):
        AssetRef(uri="resources/a\x00b.jpg", digest=DIGEST, media_type="image/jpeg")
    with pytest.raises(ValidationError):
        AssetRef(uri="resources/a.jpg", digest=DIGEST, media_type="  ")
    with pytest.raises(ValidationError):
        AssetRef(uri="resources/a.jpg", digest=DIGEST, media_type="image/\x00jpeg")
    with pytest.raises(ValidationError):
        make_episode(episode_id="ep\x001")
    with pytest.raises(ValidationError):
        make_episode(episode_id="  ")
    with pytest.raises(ValidationError):
        RobotSpec(embodiment=" ", dof=7, control_mode="joint_position", control_freq_hz=10.0)
    with pytest.raises(ValidationError):
        RobotSpec(embodiment="arm\x00", dof=7, control_mode="joint_position", control_freq_hz=10.0)
    with pytest.raises(ValidationError):
        RobotSpec(embodiment="arm", dof=7, control_mode=" ", control_freq_hz=10.0)
    with pytest.raises(ValidationError):
        RobotSpec(
            embodiment="arm",
            dof=7,
            control_mode="joint_position",
            control_freq_hz=10.0,
            gripper="grip\x00per",
        )
    with pytest.raises(ValidationError):
        CameraModel(
            name=" ",
            intrinsics=(1.0, 2.0, 3.0, 4.0),
            extrinsics=tuple(float(i) for i in range(16)),
            width=10,
            height=10,
        )
    with pytest.raises(ValidationError):
        CameraModel(
            name="ca\x00m",
            intrinsics=(1.0, 2.0, 3.0, 4.0),
            extrinsics=tuple(float(i) for i in range(16)),
            width=10,
            height=10,
        )
    with pytest.raises(ValidationError):
        Observation(timestamp_ns=0, images={"": make_asset()})


def test_record_envelope_integration():
    episode = make_episode()
    payload = episode.to_payload()

    envelope = RecordEnvelope(
        id=episode.episode_id,
        payload=payload,
        schema=EMBODIED_EPISODE_SCHEMA,
        metadata={"fingerprint": episode.fingerprint()},
    )

    # The payload pins its own version; the envelope must agree with it.
    assert payload["schema_version"] == EMBODIED_EPISODE_SCHEMA
    assert envelope.schema == payload["schema_version"]
    assert envelope.payload == payload
    assert envelope.metadata["fingerprint"] == episode.fingerprint()
    assert canonical_json(envelope.payload)
    # Envelope metadata is not part of the episode content identity.
    assert "metadata" not in envelope.payload
    assert episode.fingerprint() == make_episode().fingerprint()
    # The contract does not fill ``schema`` in: a producer that forgets it gets
    # the generic default, which disagrees with the payload version.
    default_envelope = RecordEnvelope(id=episode.episode_id, payload=payload)
    assert default_envelope.schema != payload["schema_version"]


def test_label_success_is_tri_state():
    unknown = make_episode(label=EpisodeLabel(success=None)).to_payload()
    failed = make_episode(label=EpisodeLabel(success=False)).to_payload()
    passed = make_episode(label=EpisodeLabel(success=True)).to_payload()

    assert unknown["label"]["success"] is None
    assert failed["label"]["success"] is False
    assert passed["label"]["success"] is True
    # The three states survive deserialization instead of collapsing.
    assert EmbodiedEpisode.from_payload(unknown).label.success is None
    assert EmbodiedEpisode.from_payload(failed).label.success is False
    assert EmbodiedEpisode.from_payload(passed).label.success is True
