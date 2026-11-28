#!/usr/bin/env python3
"""Baseline candidate: a harmless patch that must not resolve the issue."""

from __future__ import annotations

import sys

from common import read_request, write_patch, write_result

NEGATIVE_PATCH = (
    "diff --git a/.swe-official-negative b/.swe-official-negative\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/.swe-official-negative\n"
    "@@ -0,0 +1 @@\n"
    "+baseline candidate produced no fix\n"
)


def main() -> int:
    request = read_request(sys.argv[1])
    write_patch(request, NEGATIVE_PATCH)
    write_result(sys.argv[2], request, "swe-official-baseline-v1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
