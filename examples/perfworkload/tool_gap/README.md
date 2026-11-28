# Tool Gap 端到端实验

用 SWE-bench Verified 的真实任务生成环境包，观察 Agent Hint 在工具等待期间保留多少前缀缓存，以及模型续跑的首输出延迟（TTFT）。

```text
SWE-bench Verified → 生成并校验环境包 → Harness 发起模型请求
  → 执行工具并等待 → 回传工具结果 → 对比 Hint 关/开 → 中英文报告
```

## 前置条件

在 Linux 或 WSL 的 Bash 中，从 DataDesigner 仓库根目录执行。准备 `uv`、Python、可访问的 OpenAI 兼容流式推理服务，以及所选 harness 和对应的 Hint 插件。已有数据集可直接从第 2 步开始。

## 1. 生成环境包

准备含 SWE-bench Verified 数据的 Harbor Git 检出，默认快照为 `86723674f04e4209ac479d0fb75d9d9f44b4377e`。将 `dataset_dir` 设为一个尚不存在的输出目录。

```bash
harbor_repo='/path/to/harbor-datasets'
dataset_dir='/path/to/swebench-verified-tool-gap'
example="$PWD/examples/perfworkload/tool_gap"
tokenizer_dir="$example/swebench-verified/tokenizer"
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"

mkdir -p "$tokenizer_dir" "$(dirname "$dataset_dir")"
for file in tokenizer.json tokenizer_config.json; do
  curl -fL "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct/resolve/a09a354/$file" \
    -o "$tokenizer_dir/$file"
done
uv run --no-project --python 3.13 --with tokenizers==0.22.2 \
  python "$example/swebench-verified/prepare_batch.py" \
  --repo "$harbor_repo" --output "$dataset_dir"
```

快速验证可在生成命令末尾加 `--count 1`，默认生成 64 个包。脚本逐包校验，生成数据集索引 `index.json`。索引记录每个环境包的路径、用例 ID 和工具等待时长。

要构建约 40K 首次续跑输入的并发数据集，将输出目录改为新的目录，并在生成命令末尾加 `--count 120 --long-resume`。每个原始任务生成 12 秒、20 秒、45 秒各一条用例。若抽样包含 45 秒档，Hint TTL 需覆盖该等待及续跑排队时间。首轮保留原始任务长度，较长的输入来自工具结果。实际推理服务的上下文还要为生成内容留出空间。

## 2. 选择用例和推理服务

填写本机数据集路径、模型服务地址和部署时使用的模型名。服务需要认证时，先设置 `OPENAI_API_KEY`。以下示例跑一条 12 秒用例。要跑整个数据集，删掉 `--case "$case_id"`。

```bash
dataset_dir='/path/to/swebench-verified-tool-gap'
inference_url='http://inference.example:8000'
model='your-served-model-name'
case_id='astropy__astropy-12907-2-tools-12s-1r-1024t'
context_window=16384
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
runner="$PWD/examples/perfworkload/tool_gap/harness/run.py"
common=(
  --package "$dataset_dir" --case "$case_id"
  --endpoint "$inference_url/v1/chat/completions" --model "$model"
  --context-window "$context_window" --max-tokens 768
)

# 按引擎能力启用所需选项。
# common+=(--hint-transport ascend-body)
# common+=(--isolate-cache)
# common+=(--engine-metrics-url "$inference_url/metrics")
```

复现 12 秒缓存对照时，测试引擎需启用普通前缀缓存，并设为空闲 10 秒后过期。使用支持请求体 Hint 的 vLLM-Ascend 时启用 `ascend-body`。支持 `cache_salt` 时启用 `--isolate-cache`。用独占引擎采集 `/metrics` 可额外记录 Prefill 耗时、重算 token 和排队时间。

## 3. 运行 Harness

默认对每条用例先关 Hint 跑一次，再开 Hint 跑一次。只跑一种状态时可加 `--hint off` 或 `--hint on`。共用一个推理实例采集引擎指标时，串行执行各组测试。

### DSH

准备 Node、DSH CLI 和已编译且装好依赖的 Hint 插件。

```bash
dsh_bin='/path/to/dsh/lib/bin.js'
dsh_hint_plugin='/path/to/dsh-agent-hints/lib/types/index.js'

python3 "$runner" "${common[@]}" \
  --harness dsh --dsh-bin "$dsh_bin" --hint-plugin "$dsh_hint_plugin" --ttl 25
```

### LangChain

```bash
langchain_hint_dir='/path/to/langchain-agent-hints'

uv run --no-project --python 3.13 \
  --with 'langchain>=1.4,<2' --with langchain-openai \
  python "$runner" "${common[@]}" \
  --harness langchain --harness-dir "$langchain_hint_dir" --ttl 25
```

### Hermes

Hermes 实验需单独准备宿主和适配器。实验适配器包含 `tool_gap_launcher.py`、`short_context_cli.py` 和 `measure.py`。本例使用 Hermes v0.21.6，在宿主源码目录执行 `uv sync --locked --no-dev --no-default-groups` 创建 `.venv`，并把适配器的 `hint_source` 设为本机 Hint 插件路径。准备好后执行：

```bash
hermes_adapter_dir='/path/to/hermes-experiment-runtime'
hermes_output_dir='/path/to/new-hermes-experiment-output'

PYTHONPATH="$PYTHONPATH:$(dirname "$runner")" \
  python3 "$hermes_adapter_dir/tool_gap_launcher.py" "${common[@]}" \
  --output "$hermes_output_dir" --ttl 60 --harness langchain
```

当前仓库的测试入口直接支持 DSH 和 LangChain。上面的 `--harness langchain` 是实验适配器借用的入口，实际运行和报告记录的是 Hermes。

## 4. 查看报告

实验结束后打开 `report_zh.md` 或 `report_en.md`。数据集模式的总报告链接到各用例报告，里面有工具间隙、TTFT、TPOT、缓存命中量和原始日志。比较同一用例、同一轮的 Hint 开关结果。确认续跑表里有工具间隙、缓存和 TTFT 读数。若模型未按用例调用工具，查看该次运行的 `harness.log` 和 `stderr.log`。DSH 达到输出上限时可能显示退出码 1，是否完成续跑可结合报告中的响应状态和指标判断。

省略 `--output` 时，实验目录为 `./artifacts/{数据集名}_{harness名}_{编号}/`。可用 `--output` 指定新目录，或用 `--report-dir` 单独指定 Markdown 报告的存放根目录。

## 并发测试

在仓库根目录执行。使用第 2 步的 `dataset_dir`、`inference_url`、`model` 和 `runner`。将 `dataset_dir` 指向要压测的数据集根目录，并按部署填写上下文窗口和 Hint 保留时间。下面以 LangChain 为例，从数据集中随机选 100 个不同任务，最多同时向推理服务发送 25 个在途请求。

```bash
langchain_hint_dir='/path/to/langchain-agent-hints'
context_window=49152
hint_ttl=180
hint_transport='header'  # 使用请求体 Hint 的引擎填 ascend-body

batch_args=(
  --package "$dataset_dir"
  --harness langchain --harness-dir "$langchain_hint_dir"
  --endpoint "$inference_url/v1/chat/completions" --model "$model"
  --context-window "$context_window" --max-tokens 128
  --hint-transport "$hint_transport" --ttl "$hint_ttl" --concurrency 25
)
# 引擎支持 cache_salt 时，可追加 batch_args+=(--isolate-cache)。

uv run --no-project --python 3.13 \
  --with 'langchain>=1.4,<2' --with langchain-openai \
  python "$runner" "${batch_args[@]}" --sample-size 100 --sample-seed 42
```

脚本把抽到的任务保存为输出目录下的 `concurrent-set.json`，默认用同一份选单分别跑 Hint 关、开。复测时，将命令末尾的抽样参数替换为 `--concurrent-set /path/to/concurrent-set.json`。要用 DSH，沿用第 3 步的 DSH 启动方式和插件参数，保留并发与选单参数。

按需使用 `--gap-seconds` 筛选工具等待时间，用 `--target-resume-tokens` 和 `--resume-tolerance` 筛选首次续跑的估算长度。启用长度筛选时，运行环境需安装 `tokenizers==0.22.2` 和 `jinja2`。候选任务不足时，脚本会提示匹配数量。

并发测试使用独立的参数列表。工具等待期间不占推理名额。共享 Prometheus 计数无法归到单个并发请求，因此并发模式无需配置 `--engine-metrics-url`。

## 查看用例的输入长度估算

在仓库根目录运行下列命令，无需启动推理服务。省略 `--case` 会估算整个数据集。

```bash
uv run --no-project --python 3.13 --with tokenizers==0.22.2 --with jinja2 \
  python "$runner" --package "$dataset_dir" --case "$case_id" --estimate-only
```

脚本用聊天模板计算首轮和每次续跑的预计输入长度。默认使用包内 tokenizer，或第 1 步准备的 Qwen2.5 tokenizer，也可用 `--tokenizer-profile` 指定本地 profile。实测长度以引擎返回的 `prompt_tokens` 为准。
