#!/usr/bin/env bash
# Run the official SWE-bench resolved cross-round acceptance chain.
#
# Required:
#   DATA_FORGE_ROOT      AscendAgent-DataDesigner checkout (Linux path)
#   SWE_BENCH_PYTHON     swebench venv python
#   SWE_BENCH_WORKDIR    harness work directory (persistent, outside DataForge)
#   SWE_BENCH_DATASET    SWE-bench-format .json/.jsonl
#   SWE_BENCH_WORK       empty output directory outside DataForge
#
# Optional:
#   SWE_BENCH_INSTANCES  comma-separated instance IDs
#   SWE_BENCH_MAX_INSTANCES  default 6 when INSTANCES is unset
#   SWE_BENCH_TIMEOUT    per-run timeout seconds, default 1800
#   SWE_BENCH_AUDIT_CONCURRENCY default 1 (cold env builds must not overlap)
#   SWE_BENCH_SKIP_EXPORT set to 1 to skip Evaluator export

set -euo pipefail

: "${DATA_FORGE_ROOT:?DATA_FORGE_ROOT is required}"
: "${SWE_BENCH_PYTHON:?SWE_BENCH_PYTHON is required}"
: "${SWE_BENCH_WORKDIR:?SWE_BENCH_WORKDIR is required}"
: "${SWE_BENCH_DATASET:?SWE_BENCH_DATASET is required}"
: "${SWE_BENCH_WORK:?SWE_BENCH_WORK is required}"

PYTHON="$DATA_FORGE_ROOT/.venv/bin/python"
ENTRY="$DATA_FORGE_ROOT/scripts/swe_official_e2e.py"

if [[ ! -x "$PYTHON" ]]; then
  echo "DataForge venv python not found: $PYTHON" >&2
  exit 2
fi
if [[ ! -f "$ENTRY" ]]; then
  echo "acceptance entry not found: $ENTRY (is DATA_FORGE_ROOT correct?)" >&2
  exit 2
fi
if [[ ! -f "$SWE_BENCH_DATASET" ]]; then
  echo "dataset not found: $SWE_BENCH_DATASET" >&2
  exit 2
fi

export RAY_ADDRESS="${RAY_ADDRESS:-local}"
export SWE_BENCH_PYTHON
export SWE_BENCH_WORKDIR

args=(
  --dataset "$SWE_BENCH_DATASET"
  --work "$SWE_BENCH_WORK"
  --timeout "${SWE_BENCH_TIMEOUT:-1800}"
  --audit-concurrency "${SWE_BENCH_AUDIT_CONCURRENCY:-1}"
)

if [[ -n "${SWE_BENCH_INSTANCES:-}" ]]; then
  args+=(--instances "$SWE_BENCH_INSTANCES")
else
  args+=(--max-instances "${SWE_BENCH_MAX_INSTANCES:-6}")
fi
if [[ "${SWE_BENCH_SKIP_EXPORT:-0}" == "1" ]]; then
  args+=(--skip-export)
fi

exec "$PYTHON" "$ENTRY" "${args[@]}"