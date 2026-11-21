# SWE-bench 官方 resolved 跨轮实验自验证报告

- 生成时间：2026-11-17T12:42:23Z
- 说明：本报告记录本次端到端实验的执行过程、判分结果与跨轮校验结论；不复制或归档 harness 原始文件。
- 验收结论：**PASS**
- release：`swe-bench-lite-dev-c446a167a6fe`，split：`development`，题量：6
- suite：`swe-bench-official-suite@1`，suite SHA-256：`f2e8c0cbb00b8fb01b9686f39af61665ee8da6fd0bf66f326a2ee5a5bd71e281`

## 1. 验收结论与概要

| 验收维度 | 结论 | 本次范围 |
|---|---|---|
| 功能验收 | PASS | 官方原生 SWE-bench 题集构建、两轮评测、对比决策与导出 |
| 扩展性验收 | PASS（本次范围） | 6 题、2 轮策略、单 attempt；验证参数化与导出链路，不覆盖大规模扩产 |
| 可追溯性验收 | PASS | 数据集、taskset、suite、冻结包、运行时与逐 attempt 绑定 |
| 异常处理验收 | PASS | 负例拒绝、篡改校验、infra/cancelled 分母排除 |
| 交付验收 | PASS | 使用示例、运行脚本、自验证报告、测试记录 |

### 1.1 验收范围与限制

- 覆盖范围：官方原生 SWE-bench 题集、`development` split、6 道题、baseline/evolved 两轮、每轮 1 次 attempt。
- 未覆盖范围：派生扩充题、GAP 自动补盲、regression/holdout 真实终评、大规模题量与多 repeats 压测。
- 判分统一使用 `swebench_resolved_0_1` 官方 resolved 0/1 协议。

## 2. 功能验收

### 2.1 实验配置与制品钉扎

| 项目 | 值 |
|---|---|
| release | `swe-bench-lite-dev-c446a167a6fe` |
| split | `development` |
| 实例数 | 6 |
| 实例 | `sqlfluff__sqlfluff-1625`, `sqlfluff__sqlfluff-1733`, `sqlfluff__sqlfluff-1517`, `sqlfluff__sqlfluff-1763`, `sqlfluff__sqlfluff-2419`, `marshmallow-code__marshmallow-1359` |
| 运行环境 | host=`wsl`；Docker=`29.1.3`；swebench=`2.1.7` |
| suite | `swe-bench-official-suite@1` |
| suite SHA-256 | `f2e8c0cbb00b8fb01b9686f39af61665ee8da6fd0bf66f326a2ee5a5bd71e281` |
| taskset | `swe-bench-official@1` |
| taskset 指纹 | `sha256:5594d4d836d08801a77a689c3cc477107b04c4f1bf87d100e0d25d954b31e0ed` |
| taskset SHA-256 | `5594d4d836d08801a77a689c3cc477107b04c4f1bf87d100e0d25d954b31e0ed` |
| 冻结包清单 | `package/manifest.json` |
| 冻结包摘要 | `sha256:52d348dae2ed23b87fbc0b3a4a2290081cd430d0da702e96f8c815f917d70b53` |
| runner | `swebench-v2-native-adapter-v1` |
| harness | `2.1.7` |
| 判分协议 | `swebench_resolved_0_1` |
| 数据集 SHA-256 | `c446a167a6feac6179be2db8c2f144a97d0e1a4fff8495149c2ad89adb9d22e1` |

### 2.2 执行过程

| 步骤 | 执行动作 | 结果 |
|---|---|---|
| 1. 数据选择 | 从 `swe-bench-lite-dev-c446a167a6fe` 选择 6 个实例 | `sqlfluff__sqlfluff-1625`, `sqlfluff__sqlfluff-1733`, `sqlfluff__sqlfluff-1517`, `sqlfluff__sqlfluff-1763`, `sqlfluff__sqlfluff-2419`, `marshmallow-code__marshmallow-1359` |
| 2. 构建题集 | 规范化、参考解审计、负例审计 | accepted=6/6，admission_rate=1.0 |
| 3. 冻结与发布 | freeze package、taskset 登记、suite 组合与审阅 | `swe-bench-official-suite@1` |
| 4. baseline 评测 | solver=`swe-official-baseline-v1`，attempts=6 | passed=0，macro/micro=0.0000/0.0000，总耗时=591.3s |
| 5. evolved 评测 | solver=`swe-official-evolved-v1`，attempts=6 | passed=6，macro/micro=1.0000/1.0000，总耗时=604.2s |
| 6. 跨轮校验 | suite 摘要、有序题单、运行时版本、篡改防护 | 7/7 checks PASS |
| 7. 对比决策 | 配对逐题结果与符号检验 | `promote`，p=0.03125 |
| 8. Evaluator 导出 | 生成外部消费格式并校验摘要 | degraded=[]，source_suite=`f2e8c0cbb00b8fb01b9686f39af61665ee8da6fd0bf66f326a2ee5a5bd71e281` |

### 2.3 功能结果

| 轮次 | macro_pass_rate | micro_pass_rate | graded | passed | infra_failed | cancelled | attempts | 总耗时(s) |
|---|---|---|---|---|---|---|---|---|
| baseline | 0.0000 | 0.0000 | 6 | 0 | 0 | 0 | 6 | 591.3 |
| evolved | 1.0000 | 1.0000 | 6 | 6 | 0 | 0 | 6 | 604.2 |

#### 逐题结果

| 实例 | baseline | evolved | 变化 | baseline 耗时(s) | evolved 耗时(s) | harness | protocol |
|---|---|---|---|---|---|---|---|
| `sqlfluff__sqlfluff-1625` | failed (0.0) | passed (1.0) | failed -> passed | 116.8 | 132.2 | `2.1.7` | `swebench_resolved_0_1` |
| `sqlfluff__sqlfluff-1733` | failed (0.0) | passed (1.0) | failed -> passed | 96.4 | 98.7 | `2.1.7` | `swebench_resolved_0_1` |
| `sqlfluff__sqlfluff-1517` | failed (0.0) | passed (1.0) | failed -> passed | 98.0 | 97.8 | `2.1.7` | `swebench_resolved_0_1` |
| `sqlfluff__sqlfluff-1763` | failed (0.0) | passed (1.0) | failed -> passed | 106.4 | 104.8 | `2.1.7` | `swebench_resolved_0_1` |
| `sqlfluff__sqlfluff-2419` | failed (0.0) | passed (1.0) | failed -> passed | 103.8 | 100.5 | `2.1.7` | `swebench_resolved_0_1` |
| `marshmallow-code__marshmallow-1359` | failed (0.0) | passed (1.0) | failed -> passed | 69.9 | 70.2 | `2.1.7` | `swebench_resolved_0_1` |

#### 跨轮可比性校验

| 校验 | 结果 |
|---|---|
| suite SHA-256 一致 | PASS |
| 有序题单一致 | PASS |
| 执行策略一致 | PASS |
| verifier/harness/protocol 一致 | PASS |
| infra_failed / cancelled 为 0 | PASS |
| `infra_failed_excluded_from_denominator` | PASS |
| `ordered_task_list_identical` | PASS |
| `runtime_versions_recorded` | PASS |
| `same_suite_version` | PASS |
| `tampered_harness_version_rejected` | PASS |
| `tampered_order_rejected` | PASS |
| `tampered_suite_sha_rejected` | PASS |

#### 对比决策

| 指标 | baseline | evolved | delta |
|---|---|---|---|
| macro_pass_rate | 0.0 | 1.0 | 1.0 |
| micro_pass_rate | 0.0 | 1.0 | 1.0 |

- 决策：**promote**
- 原因：6 improved / 0 regressed, sign-test p=0.031250 <= alpha=0.05
- 配对样本：6，改善 6，回退 0，持平 0
- 符号检验：p=0.03125，alpha=0.05，min_sample_size=2

### 2.4 阶段耗时

| 阶段 | 数量 | 总耗时(s) | 平均耗时(s) |
|---|---|---|---|
| 参考解审计 | 6 | 1068.9 | 178.2 |
| 负例审计 | 6 | 1029.4 | 171.6 |
| baseline 评测 | 6 | 591.3 | 98.5 |
| evolved 评测 | 6 | 604.2 | 100.7 |
| 审计合计 | 12 | 2098.3 | 174.9 |
| 评测合计 | 12 | 1195.4 | 99.6 |

## 3. 扩展性验收

| 验收点 | 当前实现 | 本次验证 | 结论 |
|---|---|---|---|
| 实例选择参数化 | `--instances` / `--max-instances` | 选择 6 个指定实例 | PASS |
| 多题 suite 组合 | taskset 汇总为同 split 的 suite | 6 道题参与两轮评测 | PASS |
| 多轮策略替换 | baseline/evolved 独立 solver，同一 suite 重跑 | 两个 solver 版本 | PASS |
| Evaluator 导出 | 导出能力矩阵与摘要校验 | `degraded=[]` | PASS |
| 多样本/重复评测 | suite 支持 repeats/seed 字段 | 本次 `repeats=1`，未做多 repeats 压测 | PARTIAL |
| 派生题/大规模扩产 | 派生扩充与 GAP 补盲属于后续能力 | 未纳入本链路 | NOT COVERED |

结论：本次验证了题量、策略轮次和导出链路的可扩展接口；大规模扩产、多 repeats 与派生题不在本次验收范围。

## 4. 可追溯性验收

| 对象 | 可追溯信息 | 本次值 | 结论 |
|---|---|---|---|
| 数据集 | 内容 SHA-256 | `c446a167a6feac6179be2db8c2f144a97d0e1a4fff8495149c2ad89adb9d22e1` | PASS |
| 来源 | release + split | `swe-bench-lite-dev-c446a167a6fe` / `development` | PASS |
| taskset | 编号、版本、指纹、SHA-256 | `swe-bench-official@1` / `sha256:5594d4d836d08801a77a689c3cc477107b04c4f1bf87d100e0d25d954b31e0ed` / `5594d4d836d08801a77a689c3cc477107b04c4f1bf87d100e0d25d954b31e0ed` | PASS |
| suite | 编号、版本、SHA-256 | `swe-bench-official-suite@1` / `f2e8c0cbb00b8fb01b9686f39af61665ee8da6fd0bf66f326a2ee5a5bd71e281` | PASS |
| 冻结包 | 内容摘要 | `sha256:52d348dae2ed23b87fbc0b3a4a2290081cd430d0da702e96f8c815f917d70b53` | PASS |
| 运行环境 | host / Docker / swebench | `wsl` / `29.1.3` / `2.1.7` | PASS |
| 逐 attempt | seed、solver 版本、solver SHA-256、harness、protocol | 12 条 attempt 均有记录，见附录 7.3 | PASS |
| 运行记录 | acceptance、eval record、comparison、suite review、export | 见附录 7.6 | PASS |

## 5. 异常处理验收

| 异常/防护场景 | 机制 | 本次结果 | 结论 |
|---|---|---|---|
| 负例补丁通过 | 负例必须 resolved=0 | 6/6 被拒绝 | PASS |
| 有序题单被篡改 | 对比前校验有序题单 | `tampered_order_rejected=true` | PASS |
| suite 摘要被篡改 | 对比前校验 suite SHA | `tampered_suite_sha_rejected=true` | PASS |
| harness 版本被篡改 | 对比前校验运行时版本 | `tampered_harness_version_rejected=true` | PASS |
| 基础设施失败污染分母 | infra_failed / cancelled 不计入分母 | 本次 infra=0，cancelled=0；校验项通过 | PASS |
| 导出能力缺失 | 能力矩阵与 degraded 检查 | `degraded=[]` | PASS |

说明：本次真实验收未触发 Docker 超时或容器异常；异常处理结论同时来自运行结果的 0 异常和比较器的篡改/分母校验项。

## 6. 交付验收

### 6.1 使用示例

| 交付物 | 路径 | 用途 |
|---|---|---|
| 示例入口 | `examples/swe_bench_official/README.md` | 前置条件、快速开始与结果解读 |
| 使用指南 | `examples/swe_bench_official/docs/SWE-bench官方resolved跨轮实验_使用指南.md` | 完整构建、评测、对比与排障流程 |
| 一键脚本 | `examples/swe_bench_official/ops/swe_bench_e2e.sh` | 执行构建、两轮评测、对比与导出 |
| RSI 适配器 | `examples/swe_bench_official/rsi/README.md`、`rsi/solver.py` | 候选补丁接入协议 |
| 自验证报告 | `examples/swe_bench_official/docs/SWE-bench官方resolved跨轮实验_自验证报告.md` | 本次验收结果、过程与明细 |

### 6.2 验收和测试报告

| 项目 | 命令/依据 | 结果 | 结论 |
|---|---|---|---|
| 端到端实验 | `scripts/swe_official_e2e.py` | 6/6 题从 failed 变为 passed；macro/micro 0.0 -> 1.0；infra=0 | PASS |
| 跨轮一致性 | `compare_evaluations` + acceptance checks | suite、有序题单、运行时版本一致；3 项篡改校验通过 | PASS |
| 核心回归 | `pytest tests/benchmarks/...` | 36 passed（本次交付前回归） | PASS |
| 架构边界 | `lint-imports` | 4 kept，0 broken | PASS |
| 报告生成 | `python -m py_compile scripts/swe_self_report.py` 并生成本报告 | 通过 | PASS |

## 7. 附录：详细实验记录

### 7.1 审阅门禁

| 审阅项 | 结果 |
|---|---|
| scope | `official_native` |
| reviewer | `swe-official-e2e` |
| 许可已检查 | PASS |
| 原生正例/负例已验证 | PASS |
| 私有边界已验证 | PASS |
| 环境重置已验证 | PASS |

### 7.2 构建期审计明细

| 实例 | operation | 参考解 | 参考 F2P/P2P | 参考耗时(s) | 负例 | 负例 F2P/P2P | 负例耗时(s) |
|---|---|---|---|---|---|---|---|
| `sqlfluff__sqlfluff-1625` | `issue_fix` | passed (1.0) | 1.0/1.0 | 240.5 | rejected (0.0) | 0.0/1.0 | 158.2 |
| `sqlfluff__sqlfluff-1733` | `issue_fix` | passed (1.0) | 1.0/1.0 | 191.0 | rejected (0.0) | 0.0/1.0 | 123.4 |
| `sqlfluff__sqlfluff-1517` | `issue_fix` | passed (1.0) | 1.0/1.0 | 179.5 | rejected (0.0) | 0.0/1.0 | 168.6 |
| `sqlfluff__sqlfluff-1763` | `issue_fix` | passed (1.0) | 1.0/1.0 | 176.9 | rejected (0.0) | 0.0/1.0 | 227.4 |
| `sqlfluff__sqlfluff-2419` | `issue_fix` | passed (1.0) | 1.0/1.0 | 187.8 | rejected (0.0) | 0.0/1.0 | 194.0 |
| `marshmallow-code__marshmallow-1359` | `issue_fix` | passed (1.0) | 1.0/1.0 | 93.2 | rejected (0.0) | 0.0/1.0 | 157.7 |

### 7.3 Attempt 明细

| 轮次 | 实例 | attempt | status | score | 耗时(s) | seed | solver | solver SHA-256 | harness | protocol |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | `sqlfluff__sqlfluff-1625` | 0 | completed | 0.0 | 116.8 | 7242359816239054504 | `swe-official-baseline-v1` | `9c4c2c00a397` | `2.1.7` | `swebench_resolved_0_1` |
| baseline | `sqlfluff__sqlfluff-1733` | 0 | completed | 0.0 | 96.4 | 8989149349925043035 | `swe-official-baseline-v1` | `9c4c2c00a397` | `2.1.7` | `swebench_resolved_0_1` |
| baseline | `sqlfluff__sqlfluff-1517` | 0 | completed | 0.0 | 98.0 | 6425002703691019893 | `swe-official-baseline-v1` | `9c4c2c00a397` | `2.1.7` | `swebench_resolved_0_1` |
| baseline | `sqlfluff__sqlfluff-1763` | 0 | completed | 0.0 | 106.4 | 6223929813255323670 | `swe-official-baseline-v1` | `9c4c2c00a397` | `2.1.7` | `swebench_resolved_0_1` |
| baseline | `sqlfluff__sqlfluff-2419` | 0 | completed | 0.0 | 103.8 | 8261492558864389722 | `swe-official-baseline-v1` | `9c4c2c00a397` | `2.1.7` | `swebench_resolved_0_1` |
| baseline | `marshmallow-code__marshmallow-1359` | 0 | completed | 0.0 | 69.9 | 4157561155485865024 | `swe-official-baseline-v1` | `9c4c2c00a397` | `2.1.7` | `swebench_resolved_0_1` |
| evolved | `sqlfluff__sqlfluff-1625` | 0 | completed | 1.0 | 132.2 | 7242359816239054504 | `swe-official-evolved-v1` | `fa4b6254b66d` | `2.1.7` | `swebench_resolved_0_1` |
| evolved | `sqlfluff__sqlfluff-1733` | 0 | completed | 1.0 | 98.7 | 8989149349925043035 | `swe-official-evolved-v1` | `fa4b6254b66d` | `2.1.7` | `swebench_resolved_0_1` |
| evolved | `sqlfluff__sqlfluff-1517` | 0 | completed | 1.0 | 97.8 | 6425002703691019893 | `swe-official-evolved-v1` | `fa4b6254b66d` | `2.1.7` | `swebench_resolved_0_1` |
| evolved | `sqlfluff__sqlfluff-1763` | 0 | completed | 1.0 | 104.8 | 6223929813255323670 | `swe-official-evolved-v1` | `fa4b6254b66d` | `2.1.7` | `swebench_resolved_0_1` |
| evolved | `sqlfluff__sqlfluff-2419` | 0 | completed | 1.0 | 100.5 | 8261492558864389722 | `swe-official-evolved-v1` | `fa4b6254b66d` | `2.1.7` | `swebench_resolved_0_1` |
| evolved | `marshmallow-code__marshmallow-1359` | 0 | completed | 1.0 | 70.2 | 4157561155485865024 | `swe-official-evolved-v1` | `fa4b6254b66d` | `2.1.7` | `swebench_resolved_0_1` |

### 7.4 运行时版本

| 轮次 | verifier | harness | protocol |
|---|---|---|---|
| baseline | swebench-v2-native-adapter-v1 | 2.1.7 | swebench_resolved_0_1 |
| evolved | swebench-v2-native-adapter-v1 | 2.1.7 | swebench_resolved_0_1 |

### 7.5 Evaluator 导出能力

| 项目 | 值 |
|---|---|
| 输出目录 | `evaluator/`（相对运行工作目录） |
| capabilities version | `0.1.0` |
| source_suite_sha256 | `f2e8c0cbb00b8fb01b9686f39af61665ee8da6fd0bf66f326a2ee5a5bd71e281` |
| source_digest | `sha256:52d348dae2ed23b87fbc0b3a4a2290081cd430d0da702e96f8c815f917d70b53` |
| attempts | 1 |
| aggregation | micro |
| degraded | [] |
| unsupported | ['repeated attempts', 'solver seed', 'family weights', 'macro aggregation'] |

### 7.6 运行记录

- 运行工作目录保留 acceptance-summary.json、两轮 eval record、comparison、suite-review、Evaluator 导出与冻结包 manifest。
- 本报告记录执行过程、逐 attempt 结果与关键摘要，不复制 harness 逐题原始文件。

| 记录 | 相对路径 |
|---|---|
| 运行记录 | `acceptance-summary.json` |
| 运行记录 | `suite-review.json` |
| 运行记录 | `experiment/rounds/baseline/eval-record.json` |
| 运行记录 | `experiment/rounds/evolved/eval-record.json` |
| 运行记录 | `experiment/comparison.json` |
| 运行记录 | `experiment/summary.json` |
| 运行记录 | `evaluator/capabilities.json` |
| 运行记录 | `package/manifest.json` |

### 7.7 复现前置条件

| 项目 | 要求 | 本次运行 |
|---|---|---|
| 主机 | Linux 或 WSL2 Ubuntu，Docker daemon 可访问 | `wsl` |
| Docker | `docker version` 可读取服务端版本 | `29.1.3` |
| DataForge | 本仓库 checkout + Linux venv，Python >= 3.10 | `.venv/bin/python` |
| SWE-bench harness | 独立 venv，钉扎 `swebench==2.1.7` | `2.1.7` |
| 数据集 | SWE-bench 格式 `.jsonl` / `.json`，必需字段齐全 | 23 行本地子集，SHA-256 见 2.1 |
| 目录 | `SWE_BENCH_WORKDIR` 持久化；`SWE_BENCH_WORK` 在仓库外且为空 | `$HOME/swe-official/...` |
| 网络/磁盘 | 首次运行拉取 `sweb.base` / `sweb.env` Docker 镜像 | 首次显著变慢，建议预留数 GB 空间 |

### 7.8 一次性环境准备

```bash
# 1. DataForge 仓库与 Linux venv
git clone https://github.com/evolving-foundry/ember-dataforge.git
cd AscendAgent-DataDesigner
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e .
.venv/bin/python -c "import ray, data_designer; print('dataforge ok')"

# Ubuntu 若提示 ensurepip 缺失，先安装 python3-venv
# sudo apt-get update && sudo apt-get install -y python3-venv

# 2. 官方 SWE-bench harness：独立 venv，版本必须钉扎
python3 -m venv ~/.venvs/swebench217
~/.venvs/swebench217/bin/pip install -U pip
~/.venvs/swebench217/bin/pip install swebench==2.1.7
~/.venvs/swebench217/bin/python -c "import importlib.metadata as m; print(m.version('swebench'))"

# 3. Docker 守护进程
docker version --format '{{.Server.Version}}'
```

> Windows 宿主机请在 WSL2 内执行全部命令，仓库路径使用 `/mnt/d/...` 形式。
> Docker 未启动时，构建期审计会以 `official SWE-bench harness failed` 快速失败。

### 7.9 数据集准备与校验

- 仓库不附带数据集；请准备一份 SWE-bench 格式文件，每行至少包含 `instance_id`、`repo`、`base_commit`、`problem_statement`、`patch`、`test_patch`、`FAIL_TO_PASS`、`PASS_TO_PASS`、`created_at`、`version`。
- 本次验收使用 SWE-bench Lite dev 的 23 行本地导出，文件 SHA-256 为 `c446a167a6feac6179be2db8c2f144a97d0e1a4fff8495149c2ad89adb9d22e1`。

若手头没有数据，可从官方 split 直接导出：

```bash
# 若 harness venv 缺少 datasets：~/.venvs/swebench217/bin/pip install datasets
~/.venvs/swebench217/bin/python - <<'PY'
from datasets import load_dataset
import json, pathlib

out = pathlib.Path.home() / "swe-official" / "data" / "lite-dev.jsonl"
out.parent.mkdir(parents=True, exist_ok=True)
rows = load_dataset("princeton-nlp/SWE-bench_Lite", split="dev")
with out.open("w", encoding="utf-8") as f:
    for row in rows:
        f.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
print(out, len(rows))  # 期望 23
PY
```

> 官方导出与本次文件的实例集合相同，但 JSON 字段顺序/转义可能不同，因此文件 SHA-256、release 名与 taskset 指纹可能不同；这不影响构建、评测、对比流程与判据。

校验文件摘要与行数：

```bash
export SWE_BENCH_DATASET=$HOME/swe-official/data/lite-dev.jsonl
sha256sum "$SWE_BENCH_DATASET"
# 使用本次文件时应为 c446a167...d22e1
wc -l "$SWE_BENCH_DATASET"
# 期望 23
```

### 7.10 执行实验

```bash
export DATA_FORGE_ROOT=$HOME/code/AscendAgent-DataDesigner
export SWE_BENCH_PYTHON=$HOME/.venvs/swebench217/bin/python
export SWE_BENCH_WORKDIR=$HOME/swe-official/work
mkdir -p "$SWE_BENCH_WORKDIR"
export SWE_BENCH_DATASET=$HOME/swe-official/data/lite-dev.jsonl
export SWE_BENCH_WORK=$HOME/swe-official/e2e-replay   # 必须为空目录且在仓库外
export SWE_BENCH_INSTANCES=sqlfluff__sqlfluff-1625,sqlfluff__sqlfluff-1733,sqlfluff__sqlfluff-1517,sqlfluff__sqlfluff-1763,sqlfluff__sqlfluff-2419,marshmallow-code__marshmallow-1359
bash "$DATA_FORGE_ROOT/examples/swe_bench_official/ops/swe_bench_e2e.sh"
```

等价的手工入口：

```bash
cd "$DATA_FORGE_ROOT"
export RAY_ADDRESS=local
mkdir -p "$SWE_BENCH_WORKDIR"
.venv/bin/python scripts/swe_official_e2e.py \
  --dataset "$SWE_BENCH_DATASET" \
  --work "$SWE_BENCH_WORK" \
  --instances "$SWE_BENCH_INSTANCES" \
  --timeout 1800 \
  --audit-concurrency 1
```

- 冷环境镜像必须串行构建；镜像预热后可改用 `--audit-concurrency 2`。
- 本次 6 题、每轮 1 attempt 的实测耗时：构建期审计约 35 分钟，两轮评测约 20 分钟；首次拉取镜像会更久。
- `evolved` 轮使用脚本从 gold 派生到 `rsi-candidates/` 的补丁作为正控制；真实 RSI 候选接入见 `examples/swe_bench_official/rsi/README.md`。

### 7.11 结果校验与报告重生成

```bash
cd "$SWE_BENCH_WORK"
"$DATA_FORGE_ROOT/.venv/bin/dataforge" benchmark show \
  --catalog "$SWE_BENCH_WORK/catalog" \
  --suite swe-bench-official-suite@1 --approved-only
"$DATA_FORGE_ROOT/.venv/bin/dataforge" benchmark verify \
  --catalog "$SWE_BENCH_WORK/catalog" \
  --suite swe-bench-official-suite@1
"$DATA_FORGE_ROOT/.venv/bin/python" "$DATA_FORGE_ROOT/scripts/swe_self_report.py" \
  --work "$SWE_BENCH_WORK" \
  --output "$SWE_BENCH_WORK/self-verification-report.md"
```

本次数据集的期望结果：

| 校验点 | 期望值 |
|---|---|
| baseline | `macro_pass_rate=0.0`，`micro_pass_rate=0.0`，passed=0 |
| evolved | `macro_pass_rate=1.0`，`micro_pass_rate=1.0`，passed=6 |
| 逐题变化 | 6/6 `failed -> passed` |
| 决策 | `promote`，sign-test `p=0.03125` |
| infra / cancelled | `0 / 0`，且不计入分母 |
| 跨轮检查 | 7/7 为 `true` |
| Evaluator 导出 | `degraded=[]` |

> `benchmark show/verify` 的审阅证据字段会显示运行目录的绝对路径；本报告正文统一使用相对路径。
