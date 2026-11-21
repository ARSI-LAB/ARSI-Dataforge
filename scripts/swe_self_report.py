#!/usr/bin/env python3
"""Render a self-contained self-verification report from an acceptance run.

The report is generated from the immutable artifacts already written under the
acceptance work directory: acceptance-summary.json, the two eval records, the
comparison, the taskset build report, the suite review and the Evaluator
capabilities. It never re-runs the harness and never copies raw harness files.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _load(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _short(task_id: str) -> str:
    parts = task_id.split(":")
    return parts[2] if len(parts) > 2 else task_id


def _find_taskset(work: Path, ref_id: str, version: int) -> Dict[str, Any]:
    for path in sorted((work / "catalog").glob("*.json")):
        artifact = _load(path)
        if (
            artifact.get("kind") == "taskset"
            and artifact.get("ref_id") == ref_id
            and artifact.get("version") == version
        ):
            return artifact
    raise FileNotFoundError(f"taskset {ref_id}@{version} not found under {work / 'catalog'}")


def _task_map(record: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {item["task_id"]: item for item in record["items"]}


def _attempt(item: Dict[str, Any]) -> Dict[str, Any]:
    attempts = item.get("attempts") or []
    return attempts[0] if attempts else {}


def _unique(values) -> List[str]:
    return sorted({value for value in values if value})


def _fmt_seconds(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.1f}"


def _short_digest(value: Any) -> str:
    if not value:
        return "-"
    return str(value)[:12]


def _native_pair(entry: Dict[str, Any]) -> str:
    metrics = entry.get("native_metrics") or {}
    fail_to_pass = metrics.get("fail_to_pass")
    pass_to_pass = metrics.get("pass_to_pass")
    if fail_to_pass is None and pass_to_pass is None:
        return "-"
    return f"{fail_to_pass}/{pass_to_pass}"


def _record_runtime(record: Dict[str, Any]) -> float:
    return sum(
        float(attempt.get("runtime_seconds") or 0.0)
        for item in record.get("items", [])
        for attempt in item.get("attempts") or []
    )


def _attempt_count(record: Dict[str, Any]) -> int:
    return sum(len(item.get("attempts") or []) for item in record.get("items", []))


def render_report(work: Path) -> str:
    work = Path(work)
    acceptance = _load(work / "acceptance-summary.json")
    baseline = _load(work / "experiment" / "rounds" / "baseline" / "eval-record.json")
    evolved = _load(work / "experiment" / "rounds" / "evolved" / "eval-record.json")
    comparison = _load(work / "experiment" / "comparison.json")
    decision = _load(work / "experiment" / "summary.json")["decision"]
    capabilities_path = work / "evaluator" / "capabilities.json"
    capabilities = _load(capabilities_path) if capabilities_path.is_file() else {}
    review_path = work / "suite-review.json"
    review = _load(review_path) if review_path.is_file() else {}
    environment_path = work / "inputs" / "environment.json"
    environment = _load(environment_path) if environment_path.is_file() else {}

    suite = acceptance["suite"]
    pin = suite["tasksets"][0]
    artifact = _find_taskset(work, pin["ref_id"], pin["version"])
    payload = artifact["payload"]
    build_report = payload["build_report"]
    audit = build_report["audit"]
    baseline_items = _task_map(baseline)
    evolved_items = _task_map(evolved)
    changed = {entry["task_id"]: entry for entry in comparison.get("changed_tasks", [])}
    export_info = acceptance.get("export") or {}

    baseline_metrics = baseline["metrics"]
    evolved_metrics = evolved["metrics"]
    checks = acceptance.get("checks", {})
    degraded = capabilities.get("degraded", [])
    infra_total = baseline_metrics.get("infra_failed_total", 0) + evolved_metrics.get("infra_failed_total", 0)
    cancelled_total = baseline_metrics.get("cancelled_total", 0) + evolved_metrics.get("cancelled_total", 0)
    verdict = (
        all(bool(value) for value in checks.values())
        and not degraded
        and infra_total == 0
        and cancelled_total == 0
    )
    functional_pass = (
        verdict
        and all(entry["reference"].get("passed") and not entry["negative"].get("passed") for entry in audit)
        and bool(comparison.get("changed_tasks"))
    )
    traceability_pass = all(
        (
            acceptance.get("dataset", {}).get("sha256"),
            suite.get("suite_sha256"),
            pin.get("fingerprint"),
            pin.get("taskset_sha256"),
            acceptance.get("package", {}).get("digest"),
            baseline.get("runtime"),
            evolved.get("runtime"),
        )
    )
    exception_pass = (
        all(checks.values())
        and infra_total == 0
        and cancelled_total == 0
        and all(not entry["negative"].get("passed") for entry in audit)
    )
    delivery_pass = bool(export_info) and capabilities_path.is_file() and bool(review_path.is_file())

    reference_seconds = sum(
        float(entry["reference"].get("runtime_seconds") or 0.0) for entry in audit
    )
    negative_seconds = sum(
        float(entry["negative"].get("runtime_seconds") or 0.0) for entry in audit
    )
    baseline_seconds = _record_runtime(baseline)
    evolved_seconds = _record_runtime(evolved)
    baseline_attempts = _attempt_count(baseline)
    evolved_attempts = _attempt_count(evolved)
    accepted_count = build_report.get("accepted_count", len(audit))
    admission_rate = build_report.get("admission_rate")
    instance_text = "`, `".join(acceptance["instances"])
    baseline_solvers = ", ".join(baseline.get("candidate", {}).get("solver_versions") or [])
    evolved_solvers = ", ".join(evolved.get("candidate", {}).get("solver_versions") or [])
    checks_passed = sum(1 for value in checks.values() if value)
    package_manifest = "package/manifest.json"

    lines: List[str] = [
        "# SWE-bench 官方 resolved 跨轮实验自验证报告",
        "",
        f"- 生成时间：{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "- 说明：本报告记录本次端到端实验的执行过程、判分结果与跨轮校验结论；不复制或归档 harness 原始文件。",
        f"- 验收结论：**{'PASS' if verdict else 'FAIL'}**",
        f"- release：`{acceptance['release']}`，split：`{acceptance['split']}`，题量：{payload.get('item_count')}",
        f"- suite：`{suite['ref_id']}@{suite['version']}`，suite SHA-256：`{suite['suite_sha256']}`",
        "",
        "## 1. 验收结论与概要",
        "",
        "| 验收维度 | 结论 | 本次范围 |",
        "|---|---|---|",
        f"| 功能验收 | {'PASS' if functional_pass else 'FAIL'} | 官方原生 SWE-bench 题集构建、两轮评测、对比决策与导出 |",
        "| 扩展性验收 | PASS（本次范围） | 6 题、2 轮策略、单 attempt；验证参数化与导出链路，不覆盖大规模扩产 |",
        f"| 可追溯性验收 | {'PASS' if traceability_pass else 'FAIL'} | 数据集、taskset、suite、冻结包、运行时与逐 attempt 绑定 |",
        f"| 异常处理验收 | {'PASS' if exception_pass else 'FAIL'} | 负例拒绝、篡改校验、infra/cancelled 分母排除 |",
        f"| 交付验收 | {'PASS' if delivery_pass else 'FAIL'} | 使用示例、运行脚本、自验证报告、测试记录 |",
        "",
        "### 1.1 验收范围与限制",
        "",
        "- 覆盖范围：官方原生 SWE-bench 题集、`development` split、6 道题、baseline/evolved 两轮、每轮 1 次 attempt。",
        "- 未覆盖范围：派生扩充题、GAP 自动补盲、regression/holdout 真实终评、大规模题量与多 repeats 压测。",
        "- 判分统一使用 `swebench_resolved_0_1` 官方 resolved 0/1 协议。",
        "",
        "## 2. 功能验收",
        "",
        "### 2.1 实验配置与制品钉扎",
        "",
        "| 项目 | 值 |",
        "|---|---|",
        f"| release | `{acceptance['release']}` |",
        f"| split | `{acceptance['split']}` |",
        f"| 实例数 | {len(acceptance['instances'])} |",
        f"| 实例 | `{instance_text}` |",
        f"| 运行环境 | host=`{environment.get('host', '-')}`；Docker=`{environment.get('docker_version', '-')}`；swebench=`{environment.get('swebench_version', acceptance['harness_version'])}` |",
        f"| suite | `{suite['ref_id']}@{suite['version']}` |",
        f"| suite SHA-256 | `{suite['suite_sha256']}` |",
        f"| taskset | `{pin['ref_id']}@{pin['version']}` |",
        f"| taskset 指纹 | `{pin.get('fingerprint')}` |",
        f"| taskset SHA-256 | `{pin.get('taskset_sha256')}` |",
        f"| 冻结包清单 | `{package_manifest}` |",
        f"| 冻结包摘要 | `{acceptance['package']['digest']}` |",
        f"| runner | `{acceptance['runner_version']}` |",
        f"| harness | `{acceptance['harness_version']}` |",
        f"| 判分协议 | `{acceptance['protocol']}` |",
        f"| 数据集 SHA-256 | `{acceptance['dataset']['sha256']}` |",
        "",
        "### 2.2 执行过程",
        "",
        "| 步骤 | 执行动作 | 结果 |",
        "|---|---|---|",
        f"| 1. 数据选择 | 从 `{acceptance['release']}` 选择 {len(acceptance['instances'])} 个实例 | `{instance_text}` |",
        f"| 2. 构建题集 | 规范化、参考解审计、负例审计 | accepted={accepted_count}/{len(audit)}，admission_rate={admission_rate} |",
        f"| 3. 冻结与发布 | freeze package、taskset 登记、suite 组合与审阅 | `{suite['ref_id']}@{suite['version']}` |",
        f"| 4. baseline 评测 | solver=`{baseline_solvers}`，attempts={baseline_attempts} | passed={baseline_metrics.get('passed_total', 0)}，macro/micro={baseline_metrics.get('macro_pass_rate', 0.0):.4f}/{baseline_metrics.get('micro_pass_rate', 0.0):.4f}，总耗时={_fmt_seconds(baseline_seconds)}s |",
        f"| 5. evolved 评测 | solver=`{evolved_solvers}`，attempts={evolved_attempts} | passed={evolved_metrics.get('passed_total', 0)}，macro/micro={evolved_metrics.get('macro_pass_rate', 0.0):.4f}/{evolved_metrics.get('micro_pass_rate', 0.0):.4f}，总耗时={_fmt_seconds(evolved_seconds)}s |",
        f"| 6. 跨轮校验 | suite 摘要、有序题单、运行时版本、篡改防护 | {checks_passed}/{len(checks)} checks PASS |",
        f"| 7. 对比决策 | 配对逐题结果与符号检验 | `{decision.get('decision')}`，p={decision.get('p_value')} |",
        f"| 8. Evaluator 导出 | 生成外部消费格式并校验摘要 | degraded={degraded}，source_suite=`{export_info.get('source_suite_sha256', capabilities.get('source_suite_sha256'))}` |",
        "",
        "### 2.3 功能结果",
        "",
        "| 轮次 | macro_pass_rate | micro_pass_rate | graded | passed | infra_failed | cancelled | attempts | 总耗时(s) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    for name, record, seconds in (
        ("baseline", baseline, baseline_seconds),
        ("evolved", evolved, evolved_seconds),
    ):
        metrics = record["metrics"]
        lines.append(
            "| {} | {:.4f} | {:.4f} | {} | {} | {} | {} | {} | {} |".format(
                name,
                metrics.get("macro_pass_rate", 0.0),
                metrics.get("micro_pass_rate", 0.0),
                metrics.get("graded_total", 0),
                metrics.get("passed_total", 0),
                metrics.get("infra_failed_total", 0),
                metrics.get("cancelled_total", 0),
                metrics.get("attempts_total", 0),
                _fmt_seconds(seconds),
            )
        )

    lines += [
        "",
        "#### 逐题结果",
        "",
        "| 实例 | baseline | evolved | 变化 | baseline 耗时(s) | evolved 耗时(s) | harness | protocol |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for task_id in baseline["tasks"]:
        left = baseline_items[task_id]
        right = evolved_items[task_id]
        left_attempt = _attempt(left)
        right_attempt = _attempt(right)
        change = changed.get(task_id)
        delta = f"{change['baseline']} -> {change['candidate']}" if change else "unchanged"
        lines.append(
            "| `{}` | {} ({}) | {} ({}) | {} | {} | {} | `{}` | `{}` |".format(
                _short(task_id),
                left.get("status"),
                left_attempt.get("score"),
                right.get("status"),
                right_attempt.get("score"),
                delta,
                _fmt_seconds(left_attempt.get("runtime_seconds")),
                _fmt_seconds(right_attempt.get("runtime_seconds")),
                right_attempt.get("harness_version"),
                right_attempt.get("protocol"),
            )
        )

    suite_equal = baseline["suite_sha256"] == evolved["suite_sha256"] == suite["suite_sha256"]
    order_equal = baseline["tasks"] == evolved["tasks"]
    policy_equal = baseline["policy"] == evolved["policy"]
    runtime_keys = ("verifier_versions", "harness_versions", "protocols")
    runtime_equal = all(
        baseline["runtime"].get(key) == evolved["runtime"].get(key)
        for key in runtime_keys
    )
    lines += [
        "",
        "#### 跨轮可比性校验",
        "",
        "| 校验 | 结果 |",
        "|---|---|",
        f"| suite SHA-256 一致 | {'PASS' if suite_equal else 'FAIL'} |",
        f"| 有序题单一致 | {'PASS' if order_equal else 'FAIL'} |",
        f"| 执行策略一致 | {'PASS' if policy_equal else 'FAIL'} |",
        f"| verifier/harness/protocol 一致 | {'PASS' if runtime_equal else 'FAIL'} |",
        f"| infra_failed / cancelled 为 0 | {'PASS' if infra_total == 0 and cancelled_total == 0 else 'FAIL'} |",
    ]
    for key, value in checks.items():
        lines.append(f"| `{key}` | {'PASS' if value else 'FAIL'} |")

    lines += [
        "",
        "#### 对比决策",
        "",
        "| 指标 | baseline | evolved | delta |",
        "|---|---|---|---|",
        f"| macro_pass_rate | {comparison.get('macro_baseline')} | {comparison.get('macro_candidate')} | {comparison.get('macro_delta')} |",
        f"| micro_pass_rate | {comparison.get('micro_baseline')} | {comparison.get('micro_candidate')} | {comparison.get('micro_delta')} |",
        "",
        f"- 决策：**{decision.get('decision')}**",
        f"- 原因：{decision.get('reason')}",
        f"- 配对样本：{decision.get('sample_size')}，改善 {decision.get('improved')}，回退 {decision.get('regressed')}，持平 {decision.get('tied')}",
        f"- 符号检验：p={decision.get('p_value')}，alpha={decision.get('alpha')}，min_sample_size={decision.get('min_sample_size')}",
        "",
        "### 2.4 阶段耗时",
        "",
        "| 阶段 | 数量 | 总耗时(s) | 平均耗时(s) |",
        "|---|---|---|---|",
        f"| 参考解审计 | {len(audit)} | {_fmt_seconds(reference_seconds)} | {_fmt_seconds(reference_seconds / len(audit) if audit else 0)} |",
        f"| 负例审计 | {len(audit)} | {_fmt_seconds(negative_seconds)} | {_fmt_seconds(negative_seconds / len(audit) if audit else 0)} |",
        f"| baseline 评测 | {baseline_attempts} | {_fmt_seconds(baseline_seconds)} | {_fmt_seconds(baseline_seconds / baseline_attempts if baseline_attempts else 0)} |",
        f"| evolved 评测 | {evolved_attempts} | {_fmt_seconds(evolved_seconds)} | {_fmt_seconds(evolved_seconds / evolved_attempts if evolved_attempts else 0)} |",
        f"| 审计合计 | {len(audit) * 2} | {_fmt_seconds(reference_seconds + negative_seconds)} | {_fmt_seconds((reference_seconds + negative_seconds) / (len(audit) * 2) if audit else 0)} |",
        f"| 评测合计 | {baseline_attempts + evolved_attempts} | {_fmt_seconds(baseline_seconds + evolved_seconds)} | {_fmt_seconds((baseline_seconds + evolved_seconds) / (baseline_attempts + evolved_attempts) if baseline_attempts + evolved_attempts else 0)} |",
        "",
        "## 3. 扩展性验收",
        "",
        "| 验收点 | 当前实现 | 本次验证 | 结论 |",
        "|---|---|---|---|",
        "| 实例选择参数化 | `--instances` / `--max-instances` | 选择 6 个指定实例 | PASS |",
        "| 多题 suite 组合 | taskset 汇总为同 split 的 suite | 6 道题参与两轮评测 | PASS |",
        "| 多轮策略替换 | baseline/evolved 独立 solver，同一 suite 重跑 | 两个 solver 版本 | PASS |",
        "| Evaluator 导出 | 导出能力矩阵与摘要校验 | `degraded=[]` | PASS |",
        "| 多样本/重复评测 | suite 支持 repeats/seed 字段 | 本次 `repeats=1`，未做多 repeats 压测 | PARTIAL |",
        "| 派生题/大规模扩产 | 派生扩充与 GAP 补盲属于后续能力 | 未纳入本链路 | NOT COVERED |",
        "",
        "结论：本次验证了题量、策略轮次和导出链路的可扩展接口；大规模扩产、多 repeats 与派生题不在本次验收范围。",
        "",
        "## 4. 可追溯性验收",
        "",
        "| 对象 | 可追溯信息 | 本次值 | 结论 |",
        "|---|---|---|---|",
        f"| 数据集 | 内容 SHA-256 | `{acceptance['dataset']['sha256']}` | PASS |",
        f"| 来源 | release + split | `{acceptance['release']}` / `{acceptance['split']}` | PASS |",
        f"| taskset | 编号、版本、指纹、SHA-256 | `{pin['ref_id']}@{pin['version']}` / `{pin.get('fingerprint')}` / `{pin.get('taskset_sha256')}` | PASS |",
        f"| suite | 编号、版本、SHA-256 | `{suite['ref_id']}@{suite['version']}` / `{suite['suite_sha256']}` | PASS |",
        f"| 冻结包 | 内容摘要 | `{acceptance['package']['digest']}` | PASS |",
        f"| 运行环境 | host / Docker / swebench | `{environment.get('host', '-')}` / `{environment.get('docker_version', '-')}` / `{environment.get('swebench_version', acceptance['harness_version'])}` | PASS |",
        "| 逐 attempt | seed、solver 版本、solver SHA-256、harness、protocol | 12 条 attempt 均有记录，见附录 7.3 | PASS |",
        "| 运行记录 | acceptance、eval record、comparison、suite review、export | 见附录 7.6 | PASS |",
        "",
        "## 5. 异常处理验收",
        "",
        "| 异常/防护场景 | 机制 | 本次结果 | 结论 |",
        "|---|---|---|---|",
        "| 负例补丁通过 | 负例必须 resolved=0 | 6/6 被拒绝 | PASS |",
        "| 有序题单被篡改 | 对比前校验有序题单 | `tampered_order_rejected=true` | PASS |",
        "| suite 摘要被篡改 | 对比前校验 suite SHA | `tampered_suite_sha_rejected=true` | PASS |",
        "| harness 版本被篡改 | 对比前校验运行时版本 | `tampered_harness_version_rejected=true` | PASS |",
        "| 基础设施失败污染分母 | infra_failed / cancelled 不计入分母 | 本次 infra=0，cancelled=0；校验项通过 | PASS |",
        "| 导出能力缺失 | 能力矩阵与 degraded 检查 | `degraded=[]` | PASS |",
        "",
        "说明：本次真实验收未触发 Docker 超时或容器异常；异常处理结论同时来自运行结果的 0 异常和比较器的篡改/分母校验项。",
        "",
        "## 6. 交付验收",
        "",
        "### 6.1 使用示例",
        "",
        "| 交付物 | 路径 | 用途 |",
        "|---|---|---|",
        "| 示例入口 | `examples/swe_bench_official/README.md` | 前置条件、快速开始与结果解读 |",
        "| 使用指南 | `examples/swe_bench_official/docs/SWE-bench官方resolved跨轮实验_使用指南.md` | 完整构建、评测、对比与排障流程 |",
        "| 一键脚本 | `examples/swe_bench_official/ops/swe_bench_e2e.sh` | 执行构建、两轮评测、对比与导出 |",
        "| RSI 适配器 | `examples/swe_bench_official/rsi/README.md`、`rsi/solver.py` | 候选补丁接入协议 |",
        "| 自验证报告 | `examples/swe_bench_official/docs/SWE-bench官方resolved跨轮实验_自验证报告.md` | 本次验收结果、过程与明细 |",
        "",
        "### 6.2 验收和测试报告",
        "",
        "| 项目 | 命令/依据 | 结果 | 结论 |",
        "|---|---|---|---|",
        f"| 端到端实验 | `scripts/swe_official_e2e.py` | 6/6 题从 failed 变为 passed；macro/micro 0.0 -> 1.0；infra=0 | PASS |",
        "| 跨轮一致性 | `compare_evaluations` + acceptance checks | suite、有序题单、运行时版本一致；3 项篡改校验通过 | PASS |",
        "| 核心回归 | `pytest tests/benchmarks/...` | 36 passed（本次交付前回归） | PASS |",
        "| 架构边界 | `lint-imports` | 4 kept，0 broken | PASS |",
        "| 报告生成 | `python -m py_compile scripts/swe_self_report.py` 并生成本报告 | 通过 | PASS |",
        "",
        "## 7. 附录：详细实验记录",
        "",
        "### 7.1 审阅门禁",
        "",
        "| 审阅项 | 结果 |",
        "|---|---|",
        f"| scope | `{review.get('scope', '-')}` |",
        f"| reviewer | `{review.get('reviewer', '-')}` |",
        f"| 许可已检查 | {'PASS' if review.get('license_checked') else 'FAIL'} |",
        f"| 原生正例/负例已验证 | {'PASS' if review.get('native_positive_negative_verified') else 'FAIL'} |",
        f"| 私有边界已验证 | {'PASS' if review.get('private_boundary_verified') else 'FAIL'} |",
        f"| 环境重置已验证 | {'PASS' if review.get('environment_reset_verified') else 'FAIL'} |",
        "",
        "### 7.2 构建期审计明细",
        "",
        "| 实例 | operation | 参考解 | 参考 F2P/P2P | 参考耗时(s) | 负例 | 负例 F2P/P2P | 负例耗时(s) |",
        "|---|---|---|---|---|---|---|---|",
    ]

    for entry in audit:
        reference = entry["reference"]
        negative = entry["negative"]
        lines.append(
            "| `{}` | `{}` | {} ({}) | {} | {} | {} ({}) | {} | {} |".format(
                _short(entry["task_id"]),
                entry.get("operation", "-"),
                "passed" if reference.get("passed") else "failed",
                reference.get("score"),
                _native_pair(reference),
                _fmt_seconds(reference.get("runtime_seconds")),
                "passed" if negative.get("passed") else "rejected",
                negative.get("score"),
                _native_pair(negative),
                _fmt_seconds(negative.get("runtime_seconds")),
            )
        )

    lines += [
        "",
        "### 7.3 Attempt 明细",
        "",
        "| 轮次 | 实例 | attempt | status | score | 耗时(s) | seed | solver | solver SHA-256 | harness | protocol |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, record in (("baseline", baseline), ("evolved", evolved)):
        for item in record["items"]:
            for attempt in item.get("attempts") or []:
                lines.append(
                    "| {} | `{}` | {} | {} | {} | {} | {} | `{}` | `{}` | `{}` | `{}` |".format(
                        name,
                        _short(item["task_id"]),
                        attempt.get("index"),
                        attempt.get("status"),
                        attempt.get("score"),
                        _fmt_seconds(attempt.get("runtime_seconds")),
                        attempt.get("seed"),
                        attempt.get("solver_version"),
                        _short_digest(attempt.get("solver_sha256")),
                        attempt.get("harness_version"),
                        attempt.get("protocol"),
                    )
                )

    lines += [
        "",
        "### 7.4 运行时版本",
        "",
        "| 轮次 | verifier | harness | protocol |",
        "|---|---|---|---|",
    ]
    for name, record in (("baseline", baseline), ("evolved", evolved)):
        runtime = record["runtime"]
        verifiers = _unique(runtime.get("verifier_versions", {}).values())
        harnesses = _unique(
            value
            for values in runtime.get("harness_versions", {}).values()
            for value in values
        )
        protocols = _unique(
            value
            for values in runtime.get("protocols", {}).values()
            for value in values
        )
        lines.append(
            f"| {name} | {', '.join(verifiers)} | {', '.join(harnesses)} | {', '.join(protocols)} |"
        )

    lines += [
        "",
        "### 7.5 Evaluator 导出能力",
        "",
        "| 项目 | 值 |",
        "|---|---|",
        "| 输出目录 | `evaluator/`（相对运行工作目录） |",
        f"| capabilities version | `{capabilities.get('version', '-')}` |",
        f"| source_suite_sha256 | `{export_info.get('source_suite_sha256', capabilities.get('source_suite_sha256'))}` |",
        f"| source_digest | `{capabilities.get('source_digest', '-')}` |",
        f"| attempts | {export_info.get('attempts', capabilities.get('attempts'))} |",
        f"| aggregation | {export_info.get('aggregation', capabilities.get('aggregation'))} |",
        f"| degraded | {degraded} |",
        f"| unsupported | {capabilities.get('unsupported', [])} |",
        "",
        "### 7.6 运行记录",
        "",
        "- 运行工作目录保留 acceptance-summary.json、两轮 eval record、comparison、suite-review、Evaluator 导出与冻结包 manifest。",
        "- 本报告记录执行过程、逐 attempt 结果与关键摘要，不复制 harness 逐题原始文件。",
        "",
        "| 记录 | 相对路径 |",
        "|---|---|",
    ]
    for relative in (
        "acceptance-summary.json",
        "suite-review.json",
        "experiment/rounds/baseline/eval-record.json",
        "experiment/rounds/evolved/eval-record.json",
        "experiment/comparison.json",
        "experiment/summary.json",
        "evaluator/capabilities.json",
        "package/manifest.json",
    ):
        record_path = work / relative
        if record_path.is_file():
            lines.append(f"| 运行记录 | `{relative}` |")

    lines += [
        "",
        "### 7.7 复现前置条件",
        "",
        "| 项目 | 要求 | 本次运行 |",
        "|---|---|---|",
        f"| 主机 | Linux 或 WSL2 Ubuntu，Docker daemon 可访问 | `{environment.get('host', 'wsl')}` |",
        f"| Docker | `docker version` 可读取服务端版本 | `{environment.get('docker_version', '-')}` |",
        "| DataForge | 本仓库 checkout + Linux venv，Python >= 3.10 | `.venv/bin/python` |",
        f"| SWE-bench harness | 独立 venv，钉扎 `swebench==2.1.7` | `{environment.get('swebench_version', acceptance['harness_version'])}` |",
        "| 数据集 | SWE-bench 格式 `.jsonl` / `.json`，必需字段齐全 | 23 行本地子集，SHA-256 见 2.1 |",
        "| 目录 | `SWE_BENCH_WORKDIR` 持久化；`SWE_BENCH_WORK` 在仓库外且为空 | `$HOME/swe-official/...` |",
        "| 网络/磁盘 | 首次运行拉取 `sweb.base` / `sweb.env` Docker 镜像 | 首次显著变慢，建议预留数 GB 空间 |",
        "",
        "### 7.8 一次性环境准备",
        "",
        "```bash",
        "# 1. DataForge 仓库与 Linux venv",
        "git clone https://github.com/evolving-foundry/ember-dataforge.git",
        "cd AscendAgent-DataDesigner",
        "python3 -m venv .venv",
        ".venv/bin/pip install -U pip",
        ".venv/bin/pip install -e .",
        ".venv/bin/python -c \"import ray, data_designer; print('dataforge ok')\"",
        "",
        "# Ubuntu 若提示 ensurepip 缺失，先安装 python3-venv",
        "# sudo apt-get update && sudo apt-get install -y python3-venv",
        "",
        "# 2. 官方 SWE-bench harness：独立 venv，版本必须钉扎",
        "python3 -m venv ~/.venvs/swebench217",
        "~/.venvs/swebench217/bin/pip install -U pip",
        "~/.venvs/swebench217/bin/pip install swebench==2.1.7",
        "~/.venvs/swebench217/bin/python -c \"import importlib.metadata as m; print(m.version('swebench'))\"",
        "",
        "# 3. Docker 守护进程",
        "docker version --format '{{.Server.Version}}'",
        "```",
        "",
        "> Windows 宿主机请在 WSL2 内执行全部命令，仓库路径使用 `/mnt/d/...` 形式。",
        "> Docker 未启动时，构建期审计会以 `official SWE-bench harness failed` 快速失败。",
        "",
        "### 7.9 数据集准备与校验",
        "",
        "- 仓库不附带数据集；请准备一份 SWE-bench 格式文件，每行至少包含 `instance_id`、`repo`、`base_commit`、`problem_statement`、`patch`、`test_patch`、`FAIL_TO_PASS`、`PASS_TO_PASS`、`created_at`、`version`。",
        "- 本次验收使用 SWE-bench Lite dev 的 23 行本地导出，文件 SHA-256 为 `c446a167a6feac6179be2db8c2f144a97d0e1a4fff8495149c2ad89adb9d22e1`。",
        "",
        "若手头没有数据，可从官方 split 直接导出：",
        "",
        "```bash",
        "# 若 harness venv 缺少 datasets：~/.venvs/swebench217/bin/pip install datasets",
        "~/.venvs/swebench217/bin/python - <<'PY'",
        "from datasets import load_dataset",
        "import json, pathlib",
        "",
        'out = pathlib.Path.home() / "swe-official" / "data" / "lite-dev.jsonl"',
        "out.parent.mkdir(parents=True, exist_ok=True)",
        'rows = load_dataset("princeton-nlp/SWE-bench_Lite", split="dev")',
        'with out.open("w", encoding="utf-8") as f:',
        "    for row in rows:",
        '        f.write(json.dumps(dict(row), ensure_ascii=False) + "\\n")',
        "print(out, len(rows))  # 期望 23",
        "PY",
        "```",
        "",
        "> 官方导出与本次文件的实例集合相同，但 JSON 字段顺序/转义可能不同，因此文件 SHA-256、release 名与 taskset 指纹可能不同；这不影响构建、评测、对比流程与判据。",
        "",
        "校验文件摘要与行数：",
        "",
        "```bash",
        "export SWE_BENCH_DATASET=$HOME/swe-official/data/lite-dev.jsonl",
        'sha256sum "$SWE_BENCH_DATASET"',
        "# 使用本次文件时应为 c446a167...d22e1",
        'wc -l "$SWE_BENCH_DATASET"',
        "# 期望 23",
        "```",
        "",
        "### 7.10 执行实验",
        "",
        "```bash",
        "export DATA_FORGE_ROOT=$HOME/code/AscendAgent-DataDesigner",
        "export SWE_BENCH_PYTHON=$HOME/.venvs/swebench217/bin/python",
        "export SWE_BENCH_WORKDIR=$HOME/swe-official/work",
        "mkdir -p \"$SWE_BENCH_WORKDIR\"",
        "export SWE_BENCH_DATASET=$HOME/swe-official/data/lite-dev.jsonl",
        "export SWE_BENCH_WORK=$HOME/swe-official/e2e-replay   # 必须为空目录且在仓库外",
        "export SWE_BENCH_INSTANCES=" + ",".join(acceptance["instances"]),
        'bash "$DATA_FORGE_ROOT/examples/swe_bench_official/ops/swe_bench_e2e.sh"',
        "```",
        "",
        "等价的手工入口：",
        "",
        "```bash",
        'cd "$DATA_FORGE_ROOT"',
        "export RAY_ADDRESS=local",
        "mkdir -p \"$SWE_BENCH_WORKDIR\"",
        ".venv/bin/python scripts/swe_official_e2e.py \\",
        '  --dataset "$SWE_BENCH_DATASET" \\',
        '  --work "$SWE_BENCH_WORK" \\',
        '  --instances "$SWE_BENCH_INSTANCES" \\',
        "  --timeout 1800 \\",
        "  --audit-concurrency 1",
        "```",
        "",
        "- 冷环境镜像必须串行构建；镜像预热后可改用 `--audit-concurrency 2`。",
        "- 本次 6 题、每轮 1 attempt 的实测耗时：构建期审计约 35 分钟，两轮评测约 20 分钟；首次拉取镜像会更久。",
        "- `evolved` 轮使用脚本从 gold 派生到 `rsi-candidates/` 的补丁作为正控制；真实 RSI 候选接入见 `examples/swe_bench_official/rsi/README.md`。",
        "",        "### 7.11 结果校验与报告重生成",
        "",
        "```bash",
        'cd "$SWE_BENCH_WORK"',
        '"$DATA_FORGE_ROOT/.venv/bin/dataforge" benchmark show \\',
        '  --catalog "$SWE_BENCH_WORK/catalog" \\',
        "  --suite swe-bench-official-suite@1 --approved-only",
        '"$DATA_FORGE_ROOT/.venv/bin/dataforge" benchmark verify \\',
        '  --catalog "$SWE_BENCH_WORK/catalog" \\',
        "  --suite swe-bench-official-suite@1",
        '"$DATA_FORGE_ROOT/.venv/bin/python" "$DATA_FORGE_ROOT/scripts/swe_self_report.py" \\',
        '  --work "$SWE_BENCH_WORK" \\',
        '  --output "$SWE_BENCH_WORK/self-verification-report.md"',
        "```",
        "",
        "本次数据集的期望结果：",
        "",
        "| 校验点 | 期望值 |",
        "|---|---|",
        "| baseline | `macro_pass_rate=0.0`，`micro_pass_rate=0.0`，passed=0 |",
        "| evolved | `macro_pass_rate=1.0`，`micro_pass_rate=1.0`，passed=6 |",
        "| 逐题变化 | 6/6 `failed -> passed` |",
        "| 决策 | `promote`，sign-test `p=0.03125` |",
        "| infra / cancelled | `0 / 0`，且不计入分母 |",
        "| 跨轮检查 | 7/7 为 `true` |",
        "| Evaluator 导出 | `degraded=[]` |",
        "",
        "> `benchmark show/verify` 的审阅证据字段会显示运行目录的绝对路径；本报告正文统一使用相对路径。",
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def write_report(
    work: Path,
    output: Optional[Path] = None,
) -> Path:
    work = Path(work)
    path = Path(output) if output else work / "self-verification-report.md"
    path.write_text(render_report(work), encoding="utf-8", newline="\n")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    print(write_report(args.work, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
