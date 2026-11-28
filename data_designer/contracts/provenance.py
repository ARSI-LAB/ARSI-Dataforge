"""Compatibility wrappers; new code should import from :mod:`base.provenance`.

Imports are deferred so algorithm discovery does not load base's Ray-backed
package initializer.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    from base.provenance import canonical_json as implementation
    return implementation(value)


def content_digest(value: Any) -> str:
    from base.provenance import content_digest as implementation
    return implementation(value)


def file_digest(path: Path) -> str:
    from base.provenance import file_digest as implementation
    return implementation(path)


__all__ = ["canonical_json", "content_digest", "file_digest"]
