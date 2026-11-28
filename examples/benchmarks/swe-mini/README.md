# swe-mini: offline benchmark example

A one-task, SWE-shaped example that runs offline end to end: build a frozen
taskset, register a suite, evaluate two candidate rounds, compare them, and
export an Evaluator bundle. The task is an artifact comparison (`output/result.json`
must equal `{"total": 42}`), scored by `../verifier/text_verifier.py`.

Replace the verifier entry in `runtimes.json` (or the runtime block in
`source.json`) to plug in a real SWE or terminal harness; nothing else changes.

## Files

| File | Purpose |
| --- | --- |
| `source.json` | Family, release, license, one source case. Runtime paths are relative; `PENDING` SHA fields are filled by `prepare.py`. |
| `augment.json` | One derived variant with the trusted reference/negative audit roots. |
| `runtimes.json` | Runtime registry (launcher, verifier, environment) materialized with real digests. |
| `job.yaml` | Build job: catalog, output, trusted dir, quality threshold. |
| `candidates/reference/` | Candidate that passes (`{"total": 42}`). |
| `candidates/negative/` | Audit negative used at build time (`{"total": 0}`). |
| `candidates/baseline/` | Second evaluation round (`{"total": 0}`). |
| `prepare.py` | Writes `run/` with absolute paths and SHA-256 pins. |

## Run it

From the repository root (all commands are offline):

```bash
python examples/benchmarks/swe-mini/prepare.py
dataforge benchmark build --config examples/benchmarks/swe-mini/run/job.yaml
```

The build writes `run/out/manifest.json` (portable `dataforge-benchmark-v1`
package), the taskset in `run/catalog`, case manifests in `run/trusted/cases`,
and reports quality/audit counts.

Full acceptance chain (build → suite approval → two evaluation rounds →
comparison → Evaluator export):

```bash
python scripts/benchmark_acceptance_e2e.py --example swe-mini
```

Expected summary:

- first round (`candidates/reference`): `macro_pass_rate = 1.0`, `infra_failed_total = 0`
- second round (`candidates/baseline`): `macro_pass_rate = 0.0`
- comparison: `macro_delta = 1.0`, one changed task
- export: `capabilities.json` carries `source_suite_sha256`, `policy.aggregation = "micro"`,
  and an empty `degraded` list

## Acceptance checklist (RFC §4 test design)

| RFC §4 stage / assertion | Command | Expected |
| --- | --- | --- |
| ① filter & normalize | `python scripts/benchmark_acceptance_e2e.py --example swe-mini --work /tmp/swe-accept` | one source case and one derived variant enter the build |
| ② augment & freeze | same command | reference/negative audit passes; frozen package and digest are produced |
| ③ publish & register | same command | taskset and approved suite are registered by `ref@version` |
| ⑤ evaluation load | same command | ordered task list and pinned verifier are loaded from the suite |
| ⑥ multi-round | same command | baseline `0.0`, reference `1.0`, `infra_failed_total = 0` |
| ⑧ evidence & acceptance | same command | eval records, `comparison.json`, Evaluator export and summary are written |
| Assertion 1 | inspect `comparison.json` | both rounds share `suite_sha256`; order is identical |
| Assertion 2 | inspect eval records | verifier version is pinned; this mini uses the offline verifier, not official `resolved` |
| Assertion 3 | inspect `comparison.json` | per-task deltas, per-family rates, macro/micro and variance are present; drift raises |
| Assertion 4 | not covered here | this mini is development-only; holdout must not seed augmentation and is reserved for promotion review |

Official SWE-bench `resolved` 0/1 judging is covered by
`examples/swe_bench_official`; this mini covers the generic build/gate/freeze/
suite/cross-round protocol offline.

Artifacts land under a temporary work directory unless `--work` is given.