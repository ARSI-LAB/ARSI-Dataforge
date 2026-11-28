"""Render the runner's JSON results as a human-readable Markdown report."""

import argparse
import json
from os.path import relpath
from pathlib import Path
from urllib.parse import quote


METRICS = (
    ("ttft_seconds", ("TTFT (ms)", "TTFT (ms)"), 1000),
    ("tpot_seconds", ("TPOT (ms/token)", "TPOT (ms/token)"), 1000),
    ("prefill_seconds", ("Prefill (ms)", "Prefill (ms)"), 1000),
    ("recomputed_tokens", ("重算 token", "Recomputed tokens"), 1),
    ("queue_seconds", ("排队 (ms)", "Queue (ms)"), 1000),
    ("cached_tokens", ("命中 token", "Cached tokens"), 1),
    ("tool_gap_seconds", ("工具间隙 (s)", "Tool gap (s)"), 1),
)


def value(number, scale=1, decimals=3):
    return "—" if number is None else f"{number * scale:,.{decimals}f}"


def metric(key, number, scale):
    return value(number, scale, 0 if key.endswith("_tokens") else 3)


def text(value):
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def table(lines, headers, rows):
    lines.extend(("| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"))
    lines.extend("| " + " | ".join(text(cell) for cell in row) + " |" for row in rows)
    lines.append("")


def status(report, tr):
    if report["harness_error"]:
        return report["harness_error"]
    if report["harness_exit_code"] == 0:
        return tr("正常退出", "Completed")
    reason = next((q.get("finish_reason") for q in reversed(report["requests"]) if q.get("finish_reason")), None)
    if reason == "length":
        return tr("达到输出上限（退出码 {}）", "Output limit reached (exit code {})").format(report["harness_exit_code"])
    return tr("退出码 {}", "Exit code {}").format(report["harness_exit_code"])


REPORT_FILENAMES = ("report_zh.md", "report_en.md")


def report_paths(output, report_dir=None):
    """Resolve the two language-specific report locations for an experiment."""
    directory = output if report_dir is None else report_dir / output.name
    return tuple(directory / filename for filename in REPORT_FILENAMES)


def render_markdown(output, destination, runs, summary, english):
    """Render one language from the run records and summary statistics."""
    def tr(chinese, english_text):
        return english_text if english else chinese

    # Markdown may be stored separately from the raw experiment artifacts.
    def relative_link(path):
        return quote(Path(relpath(path, destination.parent)).as_posix())

    example = runs[0][1]
    lines = [tr("# tool_gap 端到端实验报告", "# tool_gap End-to-end Experiment Report"), "", tr("## 实验配置", "## Configuration"), ""]
    config = [
        ("Harness", example["harness"]), (tr("模型", "Model"), example["model"]),
        (tr("用例", "Test case"), example["workload_id"]), (tr("声明工具轮数", "Expected tool rounds"), example["expected_tool_rounds"]),
        (tr("运行次数", "Runs"), len(runs)), (tr("Hint 传输", "Hint transport"), example["hint_transport"]),
        (tr("前缀缓存隔离", "Prefix cache isolation"), tr("开启", "Enabled") if example["cache_salt_enabled"] else tr("关闭", "Disabled")),
        (tr("引擎指标来源", "Engine metrics source"), example.get("engine_metrics_source") or tr("未启用", "Disabled")),
        (tr("用例摘要", "Workload digest"), example["workload_digest"]), (tr("环境摘要", "Environment digest"), example["env_digest"]),
    ]
    for field, label in (("time_scale", tr("等待缩放", "Wait time scale")), ("max_tokens", tr("生成上限 (tokens)", "Max output tokens"))):
        if field in example:
            config.append((label, example[field]))
    table(lines, [tr("配置", "Setting"), tr("值", "Value")], config)
    tools = example.get("declared_tools", [])
    if tools:
        lines += [tr("## 声明工具", "## Declared tools"), ""]
        table(lines, [tr("工具", "Tools"), tr("配置耗时 (s)", "Configured delay (s)"), tr("缩放后耗时 (s)", "Scaled delay (s)")], [
            (tool["name"], value(tool["latency_seconds"]),
             value(tool["latency_seconds"] * example.get("time_scale", 1)))
            for tool in tools
        ])

    lines += [tr("## 运行概况", "## Run overview"), ""]
    table(lines, [tr("运行", "Run"), "Hint / TTL (s)", tr("状态", "Status"), tr("模型请求", "Model requests"), tr("工具调用", "Tool calls"), tr("实测续跑轮数", "Observed continuation rounds"), tr("端到端 (s)", "End-to-end (s)")], [
        (name, f"{tr('开', 'On')} / {report['hint_ttl_seconds']}" if report["hint_enabled"] else tr("关", "Off"),
         status(report, tr), len(report["requests"]), len(report["tools"]), len(report["rounds"]),
         value(report["elapsed_seconds"]))
        for name, report in runs
    ])

    lines += [tr("## 同轮指标汇总", "## Metrics by round"), ""]
    if summary:
        rows = []
        for group in sorted(summary, key=lambda row: (row["round"], row["hint_enabled"])):
            cells = []
            for key, _, scale in METRICS:
                field = group["metrics"].get(key, {})
                formatted = metric(key, field.get("median"), scale)
                cells.append(f"{formatted} ({field['count']})" if field.get("count") else "—")
            rows.append((group["round"], tr("开", "On") if group["hint_enabled"] else tr("关", "Off"),
                         f"{group['parallel_samples']}/{group['runs']}", *cells))
        table(lines, [tr("轮次", "Round"), "Hint", tr("有效并行样本", "Valid parallel samples"), *(tr(*labels) for _, labels, _ in METRICS)], rows)
        lines += [tr("指标为中位数，括号内为该指标的样本数。有效并行样本要求预期工具均参与、存在共同重叠、回传数匹配且响应流完整。", "Values are medians, with the sample count for each metric in parentheses. A valid parallel sample includes all expected tools, overlapping execution, a matching number of returned results, and a complete response stream."), ""]
    else:
        lines += [tr("本次没有可汇总的续跑轮次。请查看运行状态与底部日志。", "No continuation rounds to summarize. See the run status and logs below."), ""]

    lines += [tr("## 逐轮工具执行", "## Tool execution by round"), ""]
    rows = [
        (name, index, ", ".join(row["tool_names"]), value(row["tool_gap_seconds"]),
         value(row["all_tools_overlap_seconds"]), tr("是", "Yes") if row["matched_tool_results"] else tr("否", "No"),
         tr("是", "Yes") if row["valid_stream"] else tr("否", "No"))
        for name, report in runs for index, row in enumerate(report["rounds"], 1)
    ]
    if rows:
        table(lines, [tr("运行", "Run"), tr("轮次", "Round"), tr("工具", "Tools"), tr("间隙 (s)", "Gap (s)"), tr("重叠 (s)", "Overlap (s)"), tr("回传匹配", "Results matched"), tr("响应完整", "Stream complete")], rows)
    else:
        lines += [tr("没有已关联到续跑请求的工具记录。", "No tool executions were linked to a continuation request."), ""]

    lines += [tr("## 逐请求实测", "## Per-request measurements"), ""]
    for name, report in runs:
        lines += [f"### {name}", ""]
        if report["requests"]:
            rounds = {row["request_index"]: i for i, row in enumerate(report["rounds"], 1)}
            table(lines, [tr("请求", "Request"), tr("阶段", "Phase"), "TTFT (ms)", "TPOT (ms/token)", "Prefill (ms)",
                          tr("重算 token", "Recomputed tokens"), tr("排队 (ms)", "Queue (ms)"), tr("Token 数（输入、输出、命中）", "Tokens (input, output, cached)"), tr("响应", "Response")], [
                (i + 1, tr("续跑 {}", "Continuation {}").format(rounds[i]) if i in rounds else (tr("初始", "Initial") if i == 0 else tr("其他", "Other")),
                 value(q.get("ttft_seconds"), 1000), value(q.get("tpot_seconds"), 1000),
                 value(q.get("prefill_seconds"), 1000), metric("recomputed_tokens", q.get("recomputed_tokens"), 1),
                 value(q.get("queue_seconds"), 1000),
                 " / ".join(metric(k, q.get(k), 1) for k in ("prompt_tokens", "completion_tokens", "cached_tokens")),
                 f"{q.get('http_status') or '—'} / {q.get('finish_reason') or '—'}"
                 + ("" if q.get("stream_done") else (" / " + tr("流未结束", "Incomplete stream"))))
                for i, q in enumerate(report["requests"])
            ])
            table(lines, [tr("请求", "Request"), tr("响应耗时 (s)", "Response time (s)"), tr("生成速率 (tokens/s)", "Output rate (tokens/s)"), tr("缓存命中率", "Cache hit ratio")], [
                (i + 1, value(q.get("elapsed_seconds")), value(q.get("output_tokens_per_second")),
                 "—" if q.get("cache_hit_ratio") is None else value(q["cache_hit_ratio"], 100, 1) + "%")
                for i, q in enumerate(report["requests"])
            ])
        else:
            lines += [tr("没有记录到推理请求。", "No inference requests were recorded."), ""]
        links = [f"[{label}]({relative_link(output / name / filename)})" for filename, label in (
            ("report.json", tr("结构化报告", "JSON report")), ("requests.jsonl", tr("请求原始记录", "Raw request records")),
            ("tools.jsonl", tr("工具原始记录", "Raw tool records")), ("harness.log", tr("Harness 日志", "Harness log")), ("stderr.log", tr("错误日志", "Error log")),
        ) if (output / name / filename).exists()]
        lines += [" · ".join(links), ""]

    lines += [
        tr("## 读数口径", "## How to read the metrics"), "",
        tr("- TTFT 从桥接程序发出请求计到首个生成片段。TPOT 用首末生成片段估算每个后续输出 token 的耗时。", "- TTFT measures the time from the bridge sending the request to the first generated chunk. TPOT estimates time per subsequent output token from the first and last generated chunks."),
        tr("- 工具间隙从本轮第一个工具开始等待到最后一个工具结束等待。端到端耗时包含 harness 与工具执行。", "- The tool gap runs from the start of the first tool wait to the end of the last. End-to-end time includes the harness and tool execution."),
        tr("- 「—」表示本次没有读数。引擎侧三项指标需要独占实例的 Prometheus 数据，缺失值不计入中位数。", "- A dash (—) means a reading was unavailable. Prefill time, recomputed tokens, and queue time rely on Prometheus metrics from a dedicated engine instance. Missing values are excluded from medians."),
        tr("- Hint 开、关的比较应按同一轮查看。一次运行的结果只能作为单次观察。", "- Compare Hint on and off within the same round. Each run provides one observation."),
        "", f"[{tr('机器可读汇总', 'Machine-readable summary')}]({relative_link(output / 'summary.json')})", "",
    ]
    return "\n".join(lines)


def write_markdown(output, summary, report_dir=None):
    """Read the saved run records and write both language versions."""
    output = output.resolve()
    runs = [(path.parent.name, json.loads(path.read_bytes()))
            for path in sorted(output.glob("*/report.json"))]
    if not runs:
        raise ValueError(f"No run reports found in {output}")

    destinations = tuple(path.resolve() for path in report_paths(output, report_dir))
    for destination, english in zip(destinations, (False, True), strict=True):
        content = render_markdown(output, destination, runs, summary, english)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    return destinations


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Existing experiment output directory")
    parser.add_argument("--report-dir", type=Path, help="Markdown report root directory")
    args = parser.parse_args()
    summary = json.loads((args.output / "summary.json").read_bytes())
    for destination in write_markdown(args.output, summary, args.report_dir):
        print(destination)
