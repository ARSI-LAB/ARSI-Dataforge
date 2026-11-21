# seed-mini: custom seeds plus an official case

A two-task example that shows the `benchmark-source-v1.seeds` adapter: one
customer-owned JSONL seed row is converted into an ordinary source case at
build time, merged with one official snapshot case, and recorded in a seed
ledger. The family is a `workbuddy` artifact comparison scored offline by
`../verifier/text_verifier.py`.

## Files

| File | Purpose |
| --- | --- |
| `seeds/customer.jsonl` | Customer-owned seed row (`agent-task-v1` fields plus a `customer` passthrough column). |
| `source.json` | Official case, `seeds` block, runtime template. Relative paths and `PENDING` digests are filled by `prepare.py`. |
| `runtimes.json` | Runtime registry keyed by original seed id; materialized with real digests. |
| `augment.json` | One derived variant per merged source case. |
| `job.yaml` | Build job. |
| `candidates/{reference,negative,baseline}/` | Same two-round fixtures as `swe-mini`. |
| `prepare.py` | Materializes `run/` with absolute pins and the seed-file digest. |

## Run it

From the repository root:

```bash
python examples/benchmarks/seed-mini/prepare.py
dataforge benchmark build --config examples/benchmarks/seed-mini/run/job.yaml
```

Expected build result:

- two tasks: `workbuddy:seed-mini:seed:customer-001:v1` (owned) and
  `workbuddy:seed-mini:official-001:v1` (official)
- `build_report.source_grade_counts = {"owned_native": 1, "official_native": 1}`
- the seed ledger is written next to the trusted case manifests and its digest
  is recorded in the taskset payload

Full acceptance chain:

```bash
python scripts/benchmark_acceptance_e2e.py --example seed-mini
```

## Acceptance checklist (RFC §4 test design)

| RFC §4 stage / assertion | Command | Expected |
| --- | --- | --- |
| ① seed intake & normalize | `python scripts/benchmark_acceptance_e2e.py --example seed-mini --work /tmp/seed-accept` | two tasks; `source_grade_counts = {"owned_native": 1, "official_native": 1}`; seed ledger records the owned row |
| ② augment & freeze | same command | audited variants, frozen package and content digest are produced |
| ③ publish & register | same command | taskset and approved suite are registered by `ref@version` |
| ⑤ evaluation load | same command | ordered task list and pinned verifier are loaded from the suite |
| ⑥ multi-round | same command | reference `1.0`, baseline `0.0`, no infra failures |
| ⑧ evidence & acceptance | same command | eval records, `comparison.json`, Evaluator export and summary are written |
| Assertion 1 | inspect `comparison.json` | both rounds share `suite_sha256`; order is identical |
| Assertion 2 | inspect eval records | verifier version is pinned; this mini uses the offline verifier, not official `resolved` |
| Assertion 3 | inspect `comparison.json` | per-task deltas, per-family rates, macro/micro and variance are present; drift raises |
| Assertion 4 | not covered here | this mini is development-only; holdout must not seed augmentation and is reserved for promotion review |

Both example workspaces are self-contained: after `prepare.py` the only host
requirement is the Python interpreter that materialized the launcher pin.