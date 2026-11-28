"""Trusted benchmark case and suite contracts for RSI.

Two SWE scoring routes are supported and must never be mixed in one
comparison:

* ``private.swe_harness`` pins one official SWE-bench row and scores through
  ``runners/swe.py``. The candidate only supplies ``patch.diff``; the harness
  reports the official ``resolved`` 0/1 result (protocol
  ``swebench_resolved_0_1``).
* ``private.verifier_spec`` with kind ``swe_offline_patch`` is the offline
  file-state re-expression (``runners/swe_offline.py``) for synthetic or
  derived tasks where no official instance exists.

The retired heavy-environment path is ``runners/osworld.py``; historical
byte-frozen artifacts remain valid only for validation. ``runners/swe.py`` is
active again only for the official-resolved mode.
"""

from .contracts import FAMILIES, BenchmarkError, canonical_sha256

__all__ = ["BenchmarkError", "FAMILIES", "canonical_sha256"]
