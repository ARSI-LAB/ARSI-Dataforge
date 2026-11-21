# SWE-bench 官方 resolved 端到端示例

本示例演示一条**真实可判分、跨轮可比**的 SWE-bench 评测链路：

```text
SWE-bench 官方快照
  -> DataForge 钉扎导入 + 官方 harness 参考/负例审计
  -> 冻结 taskset + 批准 suite
  -> baseline 候选轮 + evolved 候选轮（同一 suite 版本）
  -> 官方 resolved 0/1 判分
  -> comparison.json / summary.json（逐题差值 + macro/micro）
  -> 可选 Evaluator 导出
```

本目录是 **DataForge benchmark 端到端示例**，不是 ARSI 可加载 pack；脚本、RSI 适配器与文档均位于 DataDesigner 仓内。

## 边界

- **DataForge（AscendAgent-DataDesigner）**负责：种子导入、门禁、冻结、套件、
  两轮编排、跨轮校验、对比报告与导出。
- **官方 SWE-bench harness**负责：在 Docker 环境里应用候选 `patch.diff`、
  执行 `FAIL_TO_PASS` / `PASS_TO_PASS`，给出官方 `resolved`。
- **RSI**负责：产出候选补丁。RSI 不读取 gold、不修改 suite、不参与判分。
- 本示例只覆盖**官方原生题**（`official_native`）的 resolved 模式。
  受控语义扩充（`cross_file` / 通用 mutation twins）、GAP 补盲不在本链路内；
  这些能力属于派生题链，不能与官方 resolved 分数混比。

## 目录

| 路径 | 作用 |
|---|---|
| [docs/SWE-bench官方resolved跨轮实验_使用指南.md](docs/SWE-bench官方resolved跨轮实验_使用指南.md) | 完整操作指南、产物说明、验收口径与排障 |
| [docs/SWE-bench官方resolved跨轮实验_自验证报告.md](docs/SWE-bench官方resolved跨轮实验_自验证报告.md) | 本次端到端结果、运行时版本、自验证清单与 PASS 结论 |
| [ops/swe_bench_e2e.sh](ops/swe_bench_e2e.sh) | 一条命令跑通构建、批准、两轮评测、对比与导出 |
| [rsi/README.md](rsi/README.md) | RSI 候选补丁接入 solver 协议 |
| [rsi/solver.py](rsi/solver.py) | 可直接复制的候选 solver 适配器 |
| [solvers/baseline.py](solvers/baseline.py) | 基线候选：写入无害负例补丁 |
| [solvers/evolved.py](solvers/evolved.py) | 进化候选：读取外部候选补丁 |
| [solvers/common.py](solvers/common.py) | `benchmark-solver-v1` 最小 I/O 协议 |

## 前置条件

1. WSL2 Ubuntu（或 Linux 主机）可运行 Docker。
2. SWE-bench 官方 harness 独立 venv，建议 `swebench==2.1.7`。
3. DataForge 仓库及其 Linux venv（`.venv/bin/python`）。
4. 一份 SWE-bench 格式的数据集（本地 `.json` 或 `.jsonl`），每行包含
   `instance_id`、`repo`、`base_commit`、`problem_statement`、`patch`、
   `test_patch`、`FAIL_TO_PASS`、`PASS_TO_PASS`、`created_at`、`version`。

## 快速开始

```bash
export DATA_FORGE_ROOT=/mnt/d/Project/nobtree/AscendAgent-DataDesigner
export SWE_BENCH_PYTHON=/home/<user>/.venvs/swebench217/bin/python
export SWE_BENCH_WORKDIR=/home/<user>/swe-official/work
export SWE_BENCH_DATASET=/home/<user>/swe-official/data/lite-dev.jsonl
export SWE_BENCH_WORK=/home/<user>/swe-official/e2e-run1
export SWE_BENCH_INSTANCES=sqlfluff__sqlfluff-1625,sqlfluff__sqlfluff-1733,sqlfluff__sqlfluff-1517,sqlfluff__sqlfluff-1763,sqlfluff__sqlfluff-2419,marshmallow-code__marshmallow-1359

bash examples/swe_bench_official/ops/swe_bench_e2e.sh
```

`SWE_BENCH_WORK` 必须是**仓外**的空目录；脚本会拒绝复用非空目录。
第一次运行会构建缺失的 Docker 环境镜像，耗时明显长于后续运行。
不同仓库/版本各有独立镜像；冷镜像构建必须串行，脚本默认
`SWE_BENCH_AUDIT_CONCURRENCY=1`，镜像预热后才建议提高到 2。

## 验收口径

脚本在结束时读取并断言：

- 两轮引用同一个 `suite_sha256`，且有序题单完全一致；
- 每轮判分协议均为 `swebench_resolved_0_1`；
- 每轮记录 `swebench` harness 版本（例如 `2.1.7`）与 runner 版本；
- `infra_failed_total == 0`、`cancelled_total == 0`，且不计入分数分母；
- baseline macro/micro 为 0，evolved 为 1，`macro_delta == micro_delta == 1.0`；
- 逐题变化为 `failed -> passed`；
- 篡改题单顺序、suite SHA 或 harness 版本时，比较器必须报错；
- 六题全部改进时符号检验 `p=0.03125`，决策为 `promote`。

2026-11-17 本机实测（5 道 sqlfluff + 1 道 marshmallow）：suite
`f2e8c0cb...`，两轮 macro/micro `0.0 -> 1.0`，`p=0.03125`，决策 `promote`，
导出 `degraded=[]`。完整记录见使用指南第 11 节。

## 与 `examples/benchmarks/swe-mini` 的区别

`examples/benchmarks/swe-mini` 是离线 `workbuddy` artifact-compare 夹具，
**不是**官方 SWE-bench。它适合回归构建/门禁/套件/对比协议，但不能作为
`resolved` 分数的来源。官方验收请使用本示例的
`scripts/swe_official_e2e.py` 链路。

## 结果样例

运行完成后查看：

```bash
jq '{suite, rounds, comparison, decision, checks}' "$SWE_BENCH_WORK/acceptance-summary.json"
jq '{macro_baseline, macro_candidate, macro_delta, changed_tasks, infra_summary}' "$SWE_BENCH_WORK/experiment/comparison.json"
```

cat "$SWE_BENCH_WORK/self-verification-report.md"
