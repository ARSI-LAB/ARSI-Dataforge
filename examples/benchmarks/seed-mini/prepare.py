#!/usr/bin/env python3
"""Materialize a runnable seed-mini workspace (custom seeds + official case)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EXAMPLES))

from _kit import materialize  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", default=str(Path(__file__).resolve().parent / "run"))
    args = parser.parse_args()
    summary = materialize(Path(__file__).resolve().parent, args.work)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
