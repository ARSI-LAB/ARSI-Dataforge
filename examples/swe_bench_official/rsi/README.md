# RSI 候选接入

本目录把 RSI 产出的候选补丁接到 DataForge 的 solver 协议上。判分仍由冻结
suite 指到的官方 SWE-bench harness 完成，RSI 不接触 gold、trusted manifest
或判分器。

## Solver 协议

DataForge 对每道题、每次 attempt 启动一个独立进程：

```bash
python solver.py <request.json> <result.json>
```

请求字段：

| 字段 | 说明 |
|---|---|
| `schemaVersion` | 固定为 `benchmark-solver-v1` |
| `taskId` | 冻结题目的 task_id |
| `publicItem` | 公开题面与 `benchmark.source_task_id` 等元数据 |
| `workspace` | 本次 attempt 的干净工作目录 |
| `candidateRoot` | 候选交付目录；SWE 题写 `<candidateRoot>/patch.diff` |
| `attempt` | `{index, seed}`，必须回显到结果 |

结果字段：

```json
{
  "schema_version": "benchmark-solver-v1",
  "task_id": "swe:...:sqlfluff__sqlfluff-1625:official-resolved-v1",
  "attempt_index": 0,
  "seed": 123456,
  "solver_version": "rsi-candidate-v1"
}
```

`solver.py` 支持两种补丁来源：

```bash
# 单题
SWE_CANDIDATE_PATCH_FILE=/path/to/candidate.diff

# 多题（推荐）：<source_task_id>.diff
SWE_CANDIDATE_PATCH_DIR=/path/to/rsi-round-patches
```

`SWE_SOLVER_VERSION` 可覆盖结果中的版本字符串，便于把策略版本写入评测记录。

## RSI 侧最小闭环

1. RSI 读取上一轮 `eval-record.json` 与 `comparison.json`。
2. 为每道题产出候选补丁，落到 `SWE_CANDIDATE_PATCH_DIR/<source_task_id>.diff`。
3. 用同一个 suite 版本调用 `run_experiment`：

```python
from data_designer.pipelines.benchmarks.api import load_suite
from data_designer.pipelines.benchmarks.experiment import run_experiment

handle = load_suite(catalog, suite_ref, suite_version)
summary = run_experiment(
    handle.store,
    handle.suite,
    [
        {"id": "incumbent", "solver": "/path/to/baseline_solver.py"},
        {
            "id": "rsi-round-2",
            "solver": "/path/to/AscendAgent-DataDesigner/examples/swe_bench_official/rsi/solver.py",
            "solver_env": {
                "SWE_CANDIDATE_PATCH_DIR": "/path/to/rsi-round-2-patches",
                "SWE_SOLVER_VERSION": "rsi-policy-2026-11-17",
            },
        },
    ],
    "/path/to/experiment-output",
)
```

4. 系统强制校验 suite 指纹、有序题单、repeats/seed/policy，以及
   verifier/harness/protocol 版本；任一不一致即失败，不静默比较。

## 边界

- 候选 solver 只能看到 `publicItem`、`workspace`、`candidateRoot`。
- 不要把 gold patch、`reference_root`、`negative_root` 或 trusted manifest
  传给候选进程。
- 同一轮实验只能引用一个 suite 版本；不同 suite 或不同判分协议不得混比。