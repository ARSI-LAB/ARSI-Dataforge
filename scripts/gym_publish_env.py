"""Compatibility wrapper for the Gym-native environment publisher.

The implementation lives in
``data_designer.integrations.gym.env_publish``. This script keeps the legacy
stdin contract used by existing automation:

``{"storePath": "...", "refId": "...", "version": 1, "name": "..."}``
or
``{"compileResultPath": "...", "name": "..."}``.

Releases are verified with ``ArtifactCatalog`` and
``load_release_metadata`` before any bytes are copied into Gym. No runner
daemon is started.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from data_designer.integrations.gym.env_publish import (  # noqa: E402
    GymPublishError,
    publish,
)


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Publish a Gym-native DataForge environment package"
    )
    parser.add_argument("--gym-root", required=True)
    parser.add_argument("--register", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    try:
        payload = json.load(sys.stdin)
    except ValueError as error:
        print("stdin is not valid JSON: {}".format(error), file=sys.stderr)
        return 2
    try:
        result = publish(
            payload,
            Path(args.gym_root).expanduser().resolve(),
            register=args.register,
        )
    except (GymPublishError, OSError, ValueError) as error:
        print("[gym-publish-env] rejected: {}".format(error), file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
