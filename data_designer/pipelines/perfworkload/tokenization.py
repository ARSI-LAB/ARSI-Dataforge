"""Load and verify a local Hugging Face tokenizer profile for workload generation."""

from __future__ import annotations

import hashlib
import json
import re
from importlib.metadata import version
from pathlib import Path


def text_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def local_path(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value or "://" in value or Path(value).is_absolute():
        raise ValueError("workload input paths must be local and relative to the input file")
    return root / value


def load_profile(profiles, root):
    """Load the pinned tokenizer and its configuration into a package asset."""
    if not isinstance(profiles, dict) or len(profiles) != 1:
        raise ValueError("workload generation requires exactly one tokenizer profile")
    profile_id, config = next(iter(profiles.items()))
    keys = {"tokenizer_id", "tokenizer_file", "tokenizer_config", "tokenizer_sha256",
            "chat_template_sha256"}
    if (
        not isinstance(config, dict) or config.keys() != keys
        or any(not isinstance(value, str) or not value for value in config.values())
    ):
        raise ValueError("invalid tokenizer profile configuration")
    try:
        tokenizer_json = local_path(root, config["tokenizer_file"]).read_bytes().decode("utf-8")
        tokenizer_config = json.loads(local_path(root, config["tokenizer_config"]).read_bytes())
    except FileNotFoundError as error:
        raise ValueError(f"tokenizer_unavailable: {error.filename}") from error
    template = tokenizer_config.get("chat_template") if isinstance(tokenizer_config, dict) else None
    if not isinstance(template, str) or not template:
        raise ValueError("tokenizer_config must contain a chat_template string")
    profile = {
        "schema_version": "agent-workload-tokenizer-v1",
        "profile_id": profile_id,
        "tokenizer_id": config["tokenizer_id"],
        "tokenizer_sha256": config["tokenizer_sha256"],
        "chat_template_sha256": config["chat_template_sha256"],
        "add_special_tokens": False,
        "normalization": "none",
        "tool_serialization": "canonical-json",
        "tokenizers_version": "0.22.2",
    }
    return {"profile": profile, "tokenizer_json": tokenizer_json, "chat_template": template}


def validate_profile(asset):
    """Check the embedded tokenizer profile and artifact digests."""
    if not isinstance(asset, dict) or asset.keys() != {"profile", "tokenizer_json", "chat_template"}:
        raise ValueError("invalid tokenizer asset")
    profile = asset["profile"]
    if (
        not isinstance(profile, dict)
        or not all(isinstance(profile.get(key), str) for key in (
            "profile_id", "tokenizer_id", "tokenizer_sha256", "chat_template_sha256"
        ))
        or not all(isinstance(asset[key], str) for key in ("tokenizer_json", "chat_template"))
        or profile.get("schema_version") != "agent-workload-tokenizer-v1"
        or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", profile["profile_id"])
        or not profile["tokenizer_id"].startswith("hf:")
        or profile.get("add_special_tokens") is not False
        or profile.get("normalization") != "none"
        or profile.get("tool_serialization") != "canonical-json"
        or profile.get("tokenizers_version") != "0.22.2"
    ):
        raise ValueError("invalid tokenizer profile")
    tokenizer_json = asset["tokenizer_json"]
    if text_digest(tokenizer_json) != profile["tokenizer_sha256"]:
        raise ValueError("tokenizer_digest_mismatch")
    if text_digest(asset["chat_template"]) != profile["chat_template_sha256"]:
        raise ValueError("chat_template_digest_mismatch")


def token_counter(asset):
    """Create a text token counter from the verified tokenizer asset."""
    validate_profile(asset)
    profile = asset["profile"]
    try:
        from tokenizers import Tokenizer
    except ImportError as error:
        raise ValueError("tokenizer_unavailable: install tokenizers==0.22.2") from error
    if version("tokenizers") != profile["tokenizers_version"]:
        raise ValueError("tokenizer_unavailable: expected tokenizers==0.22.2")
    try:
        tokenizer = Tokenizer.from_str(asset["tokenizer_json"])
    except Exception as error:
        raise ValueError("invalid local tokenizer JSON") from error
    tokenizer.no_truncation()
    tokenizer.no_padding()

    def count(text):
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    return count
