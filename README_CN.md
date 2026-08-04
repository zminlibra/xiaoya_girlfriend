<div align="center">
  <div>&nbsp;</div>
  <img src="https://raw.githubusercontent.com/huggingface/speech-to-speech/main/logo.png" width="600"/>

# 语音到语音（Speech To Speech）：用开源模型构建语音 Agent

[![PyPI](https://img.shields.io/pypi/v/speech-to-speech)](https://pypi.org/project/speech-to-speech/)
[![Python](https://img.shields.io/pypi/pyversions/speech-to-speech)](https://pypi.org/project/speech-to-speech/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](./LICENSE)
[![GitHub Trending: #1 Repository of the Day](https://img.shields.io/badge/GitHub%20Trending-%231%20Repository%20of%20the%20Day-7B2CBF?logo=github&logoColor=white)](https://trendshift.io/repositories/20645)

</div>

> ## 🌸 小雅（本分支说明）
>
> 本仓库基于 HuggingFace 官方 [speech-to-speech](https://github.com/huggingface/speech-to-speech) 改造，
> 为本地 AI 语音女友 **"小雅"** 定制，在官方实时语音 Agent 管线（VAD→STT→LLM→TTS）之上增强了：
>
> - **Agent 工具链**：文件读写/复制/移动（全盘）、打开/关闭应用、网页抓取、内容搜索、Everything 全盘秒搜、只读命令、剪贴板、系统信息
> - **VoiceDesign 音色**：文字描述定制音色 + 自定义音色管理（命名/编辑/删除/持久化）
> - **向量记忆**：Qdrant 本地向量库 + bge 中文语义检索，替代固定条数注入
> - **多格式文档解析**：PDF / Word / Excel / PPT / 图片 OCR / 音频转写
> - **文件上传 + 美观文档生成（docgen）**
> - **Live2D 数字人桌宠前端**（Electron 透明桌宠）
>
> 上游：https://github.com/huggingface/speech-to-speech

这是一个低延迟、完全模块化的语音 Agent 管线：**VAD → STT → LLM → TTS**，通过 **OpenAI Realtime 兼容的 WebSocket API** 对外暴露。每个组件都可以替换。LLM 槽位支持 OpenAI 兼容协议，因此可以指向托管服务商、[HF Inference Providers](https://huggingface.co/inference-providers)，或你自有硬件上的 vLLM / llama.cpp 服务器，实现完全本地、完全开源的堆栈。

该管线已在生产环境中作为数千台 [Reachy Mini](https://huggingface.co/blog/reachy-mini) 机器人的对话后端运行。

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./docs/assets/endpoint-swap-dark.gif">
    <source media="(prefers-color-scheme: light)" srcset="./docs/assets/endpoint-swap-light.gif">
    <img src="./docs/assets/endpoint-swap-light.gif" alt="把 OpenAI Realtime 客户端端点从托管 OpenAI 切换到自托管 speech-to-speech 服务器" width="640">
  </picture>
</p>

## 快速开始（Quickstart）

```bash
pip install speech-to-speech
export OPENAI_API_KEY=...
speech-to-speech
```

这会在 `ws://localhost:8765/v1/realtime` 启动一个 OpenAI Realtime 兼容服务器，使用 Parakeet TDT 做本地语音识别、OpenAI 兼容 LLM、Qwen3-TTS 做本地语音输出。

从源码目录出发，在另一个终端跟它对话：

```bash
python scripts/listen_and_play_realtime.py --host 127.0.0.1 --port 8765
```

想完全在本地跑 LLM？用 llama.cpp 启动 Gemma 4：

```bash
llama-server -hf ggml-org/gemma-4-E4B-it-GGUF -np 2 -c 65536 -fa on --swa-full
```

然后把 OpenAI 兼容 LLM 后端指向它：

```bash
speech-to-speech \
    --model_name "ggml-org/gemma-4-E4B-it-GGUF" \
    --responses_api_base_url "http://127.0.0.1:8080/v1" \
    --responses_api_api_key ""
```

任何 OpenAI Realtime 兼容客户端都可以连接。协议细节见 [Realtime API](#realtime-api)，服务商与本地服务器选项见 [LLM 后端](#llm-backends)。

## 目录

* [工作原理](#工作原理)
* [安装](#安装)
* [支持的组件](#支持的组件)
* [运行模式](#运行模式)
* [Realtime API](#realtime-api)
* [LLM 后端](#llm-backends)
* [多语言支持](#多语言支持)
* [Pocket TTS](#pocket-tts)
* [CLI 参考](#cli-参考)
* [参与贡献](#参与贡献)
* [Star 历史](#star-历史)
* [引用](#引用)

## 工作原理

管线由四个组件级联组成，每个组件在独立线程中运行，通过队列连接：

1. **语音活动检测（VAD）**：[Silero VAD v5](https://github.com/snakers4/silero-vad) 检测语音边界与说话轮次。
2. **语音转文字（STT）**：转录用户的话轮，支持可选的实时部分转录。
3. **语言模型（LLM）**：生成回复，流式输出文本与工具调用。
4. **文字转语音（TTS）**：合成音频并流式返回给客户端。

每个阶段都有多种可互换的后端，通过 CLI 参数选择。代码设计易于修改，重点关注可通过 Transformers 和 Hugging Face Hub 获得的模型。

## 安装

要求 Python 3.10+。

```bash
pip install speech-to-speech
```

默认安装覆盖标准实时路径：

- Parakeet TDT 做语音识别
- OpenAI 兼容 API 做语言模型
- Qwen3-TTS 做语音输出（非 macOS 平台默认用 GGML 后端，Apple Silicon 用 `mlx-audio`）
- 本地音频与实时服务器模式

macOS 与非 macOS 的依赖通过 `pyproject.toml` 中的平台标记自动解析。

### Qwen3-TTS 的 CUDA 说明

在 Linux 上，Qwen3-TTS 的 GGML 后端来自 `faster-qwen3-tts[ggml]`。它在 PyPI 上的默认 `qwentts-cpp-python` wheel 面向 CUDA 12.8。如果你的机器没有该 wheel 所要求的 CUDA 12 运行时，请在安装 `speech-to-speech` 之前，从 Hugging Face wheelhouse 安装匹配的 wheel：

```bash
# CUDA 13.x
pip install "qwentts-cpp-python==0.3.1+cu130" \
  -f https://huggingface.co/datasets/andito/qwentts-cpp-python-wheels/tree/main/whl/cu130

# CUDA 12.4
pip install "qwentts-cpp-python==0.3.1+cu124" \
  -f https://huggingface.co/datasets/andito/qwentts-cpp-python-wheels/tree/main/whl/cu124

# 仅 CPU 兜底
pip install "qwentts-cpp-python==0.3.1+cpu" \
  -f https://huggingface.co/datasets/andito/qwentts-cpp-python-wheels/tree/main/whl/cpu

pip install speech-to-speech
```

如果想使用之前的 CUDA-graphs 实现而不是 GGML，传 `--qwen3_tts_backend torch`。

### 可选后端（Optional Backends）

额外后端通过 pip extras 安装：

```bash
pip install "speech-to-speech[kokoro]"          # 非 macOS 上的 Kokoro-82M TTS
pip install "speech-to-speech[pocket]"          # Pocket TTS
pip install "speech-to-speech[chattts]"         # ChatTTS
pip install "speech-to-speech[facebook-mms]"    # MMS TTS
pip install "speech-to-speech[faster-whisper]"  # Faster Whisper STT
pip install "speech-to-speech[whisper-mlx]"     # macOS 上的 Lightning Whisper MLX STT
pip install "speech-to-speech[paraformer]"      # 通过 FunASR 的 Paraformer STT
pip install "speech-to-speech[mlx-lm]"          # macOS 上支持视觉模型的 mlx-vlm
```

已废弃的实现（包括 MeloTTS）位于 [`archive/`](./archive)，不再接入 CLI。

**关于 DeepFilterNet 的说明：** DeepFilterNet 用于 VAD 中可选的音频增强，要求 `numpy<2`，与要求 `numpy>=2` 的 Pocket TTS 冲突。请只在你不使用 Pocket TTS 的环境中手动安装。

### 从源码安装

```bash
git clone https://github.com/huggingface/speech-to-speech.git
cd speech-to-speech
uv sync
```

这会以可编辑模式安装包，并提供 `speech-to-speech` CLI。

## 支持的组件

| 组件 | 后端 | 平台 | 安装方式 |
|---|---|---|---|
| VAD | [Silero VAD v5](https://github.com/snakers4/silero-vad) | 全部 | 内置 |
| STT | [Parakeet TDT](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)（默认） | CUDA / CPU（nano-parakeet）、Apple Silicon（MLX） | 内置 |
| STT | 通过 Transformers 的 [Whisper](https://huggingface.co/docs/transformers/en/model_doc/whisper) | CUDA / CPU | 内置 |
| STT | [Faster Whisper](https://github.com/SYSTRAN/faster-whisper) | CUDA / CPU | `faster-whisper` |
| STT | [Lightning Whisper MLX](https://github.com/mustafaaljadery/lightning-whisper-mlx) | Apple Silicon | `whisper-mlx` |
| STT | [MLX Audio Whisper](https://github.com/huggingface/mlx-audio) | Apple Silicon | macOS 内置 |
| STT | [Paraformer](https://github.com/modelscope/FunASR) | CUDA / CPU | `paraformer` |
| LLM | OpenAI 兼容 API（`responses-api`、`chat-completions`） | 托管服务或自托管服务器 | 内置 |
| LLM | [Transformers](https://huggingface.co/models?pipeline_tag=text-generation&sort=trending) | CUDA / CPU | 内置 |
| LLM | [mlx-lm](https://github.com/ml-explore/mlx-lm) | Apple Silicon | macOS 内置 |
| TTS | [Qwen3-TTS](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice)（默认） | Linux 上 GGML / CUDA、macOS 上 mlx-audio | 内置 |
| TTS | [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) | CUDA / CPU、Apple Silicon | 非 macOS 用 `kokoro`；macOS 内置 |
| TTS | [Pocket TTS](https://github.com/kyutai-labs/pocket-tts) | CPU / CUDA | `pocket` |
| TTS | [ChatTTS](https://github.com/2noise/ChatTTS) | CUDA / CPU | `chattts` |
| TTS | [MMS TTS](https://huggingface.co/docs/transformers/model_doc/mms) | CUDA / CPU | `facebook-mms` |

用 `--stt`、`--llm_backend` 和 `--tts` 选择实现。运行 `speech-to-speech -h` 查看具体取值和后端专属参数。

## 运行模式

| 模式 | 传输方式 | 适用场景 |
|---|---|---|
| `realtime`（默认） | 通过 WebSocket 或 WebRTC 的 OpenAI Realtime 协议 | 你在基于标准语音 API 构建应用或设备。 |
| `local` | 本机麦克风与扬声器 | 你想直接跟管线对话，无需客户端。 |
| `raw-websocket` | 通过 WebSocket 的原始 PCM | 你想要一个不使用 Realtime 协议的最小自定义客户端。 |
| `socket` | 通过 TCP 的原始 PCM | 模型跑在远程服务器，配合简单的麦克风/播放客户端。 |

### 实时服务器（Realtime Server）

```bash
export OPENAI_API_KEY=...
speech-to-speech
```

这等价于：

```bash
speech-to-speech \
    --thresh 0.6 \
    --stt parakeet-tdt \
    --llm_backend responses-api \
    --tts qwen3 \
    --qwen3_tts_model_name Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice \
    --qwen3_tts_speaker Aiden \
    --qwen3_tts_language auto \
    --qwen3_tts_backend ggml \
    --qwen3_tts_non_streaming_mode True \
    --qwen3_tts_mlx_quantization 6bit \
    --model_name gpt-5.4-mini \
    --chat_size 30 \
    --responses_api_stream \
    --enable_live_transcription \
    --mode realtime
```

默认模型是通过 OpenAI Responses API 的 `gpt-5.4-mini`。用 `--model_name` 覆盖，用 `--responses_api_base_url` 指定其他 OpenAI 兼容服务商或服务器。

### 本地 Mac

```bash
speech-to-speech --local_mac_optimal_settings
```

可选地指定某个 LLM：

```bash
speech-to-speech \
    --local_mac_optimal_settings \
    --model_name mlx-community/Qwen3-4B-Instruct-2507-bf16
```

该设置会：

- 加 `--device mps`，让所有模型使用 MPS。
- STT 用 Parakeet TDT。
- LLM 后端用 MLX LM。
- TTS 用 Qwen3-TTS，默认用 `mlx-audio` 的 `6bit` MLX 变体。
- 设置 `--mode local`。

`--tts pocket` 和 `--tts kokoro` 在 macOS 上也有效。

本地对比 MLX 量化变体：

```bash
python scripts/benchmark_tts.py \
    --handlers qwen3 \
    --iterations 3 \
    --qwen3_mlx_quantizations bf16 4bit 6bit 8bit
```

### 原始 WebSocket（Raw WebSocket）

1. 以原始 WebSocket 模式运行管线：

   ```bash
   speech-to-speech --mode raw-websocket --ws_host 0.0.0.0 --ws_port 8765
   ```

2. 从你的客户端连接到 `ws://<服务器IP>:8765`。发送 16 kHz、int16、单声道 PCM 原始音频字节，接收生成的音频字节。

### TCP Socket

TCP socket 模式刻意保持最小化。它流式传输原始 PCM 音频，但不提供完整 Realtime API 功能集（包括打断处理、实时转录事件、工具调用事件）。

1. 在服务器上运行管线：

   ```bash
   speech-to-speech --mode socket --recv_host 0.0.0.0 --send_host 0.0.0.0
   ```

2. 在本地运行客户端处理麦克风输入与播放：

   ```bash
   python scripts/listen_and_play.py --host <你的服务器IP>
   ```

### Docker

安装 [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)，然后：

```bash
docker compose up
```

compose 文件会启动一个带 Gemma 4 的 llama.cpp 服务器，启动 TCP socket 服务器，并暴露端口 `8080`、`12345`、`12346`。

## Realtime API

Realtime 模式支持通过 WebSocket 和 WebRTC 使用 OpenAI Realtime 协议，带实时转录和低延迟说话轮次。WebSocket 客户端连接到 `/v1/realtime`：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8765/v1",
    websocket_base_url="ws://localhost:8765/v1",
    api_key="not-needed",
)

with client.realtime.connect(model="local") as conn:
    conn.send(
        {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": "You are a helpful assistant.",
                "audio": {
                    "input": {
                        "turn_detection": {
                            "type": "server_vad",
                            "interrupt_response": True,
                        }
                    }
                },
            },
        }
    )

    for event in conn:
        print(event.type)
```

服务器实现了核心 Realtime 事件集：入站 `input_audio_buffer.append`、`session.update`、`conversation.item.create`、`response.create`、`response.cancel`；出站语音开始/结束、流式转录、音频增量、工具调用、`response.done`。完整的事件参考、架构与设计细节见 [Realtime Engine README](./src/speech_to_speech/api/openai_realtime/README.md)。

### LLM 代理（LLM Proxy）

使用 `--enable_llm_proxy` 时，实时服务器还会把其配置的远程 LLM 暴露为普通的 OpenAI 兼容端点，这样客户端可以运行侧任务（摘要、标题、后台 Agent），支持工具与流式，且与语音对话完全并发、不会被新语音打断：

* 运行 `--llm_backend chat-completions` 时提供 `POST /v1/chat/completions`
* 运行 `--llm_backend responses-api` 时提供 `POST /v1/responses`

服务器本身不做认证、不限流。只在可信网络上启用代理，或把服务器部署在负责访问控制的前端网关后面。s2s 端点的 compute 副本就是这样的网关：只对用 HF token 创建会话的客户端开放这些路径，用该 token 校验 API key，并按用户限流。把标准 OpenAI SDK 指向你连接的主机即可；本服务器忽略 API key（由前端网关决定它应该是什么）：

```python
from openai import OpenAI

llm = OpenAI(base_url="http://localhost:8765/v1", api_key="unused")
completion = llm.chat.completions.create(
    model="anything",  # 被忽略：服务器强制用自己配置的 --model_name
    messages=[{"role": "user", "content": "Summarize the conversation so far: ..."}],
)
```

请求是无状态的（每次发送完整消息列表），会代理到配置的上游，key 由服务器持有、绝不会到达客户端。`model` 字段总是被覆盖为服务器配置的 `--model_name`。代理默认关闭，需要远程后端（`chat-completions` 或 `responses-api`），否则返回 501 及原因。

## LLM 后端

LLM 是管线中计算最密集、延迟最高的组件。大模型的一次前向传播可能主导端到端响应时间，所以根据你的硬件和延迟预算选择正确的后端很重要。管线支持：

- **本地推理**：CUDA / CPU 上的 `transformers` 和 Apple Silicon 上的 `mlx-lm`。
- **自托管服务器**：`responses-api` 和 `chat-completions` 可以指向本地 [vLLM](https://github.com/vllm-project/vllm) 或 [llama.cpp](https://github.com/ggerganov/llama.cpp) 服务器。
- **服务商 API**：同一批后端也支持 OpenAI、[HF Inference Providers](https://huggingface.co/inference-providers)、[OpenRouter](https://openrouter.ai) 等 OpenAI 兼容服务商。

有两个 API 后端可用，共用同一套 `--responses_api_*` 连接参数：

- `--llm_backend responses-api`（默认）指向 `/v1/responses`。
- `--llm_backend chat-completions` 指向 `/v1/chat/completions`。

下面的示例把 Parakeet TDT（本地 STT）和 Qwen3-TTS（本地 TTS）与不同 LLM 后端组合。

### Responses API 后端

适用于任何实现 OpenAI Responses API 的服务商或服务器。把 `--responses_api_base_url` 指向端点，并按需设置 `--model_name`：

| 服务商 / 服务器 | `--responses_api_base_url` | `--responses_api_api_key` |
|---|---|---|
| OpenAI | 省略，用 OpenAI 默认 | `$OPENAI_API_KEY` |
| HF Inference Providers | `https://router.huggingface.co/v1` | `$HF_TOKEN` |
| OpenRouter | `https://openrouter.ai/api/v1` | `$OPENROUTER_API_KEY` |
| vLLM | `http://localhost:8000/v1` | 省略或任意字符串 |
| llama.cpp | `http://127.0.0.1:8080/v1` | 空字符串 |

```bash
# OpenAI
speech-to-speech \
    --mode local \
    --stt parakeet-tdt \
    --llm_backend responses-api \
    --tts qwen3 \
    --qwen3_tts_mlx_quantization 6bit \
    --model_name "gpt-4o-mini" \
    --responses_api_api_key "$OPENAI_API_KEY" \
    --responses_api_stream \
    --enable_live_transcription
```

```bash
# HF Inference Providers：通过 Together 的 Qwen3.5-9B
speech-to-speech \
    --mode local \
    --stt parakeet-tdt \
    --llm_backend responses-api \
    --tts qwen3 \
    --qwen3_tts_mlx_quantization 6bit \
    --model_name "Qwen/Qwen3.5-9B:together" \
    --responses_api_base_url "https://router.huggingface.co/v1" \
    --responses_api_api_key "$HF_TOKEN" \
    --responses_api_stream \
    --enable_live_transcription
```

```bash
# HF Inference Providers：通过 Groq 的 GPT-oss-20B
speech-to-speech \
    --stt parakeet-tdt \
    --llm_backend responses-api \
    --tts qwen3 \
    --qwen3_tts_mlx_quantization 6bit \
    --model_name "openai/gpt-oss-20b:groq" \
    --responses_api_base_url "https://router.huggingface.co/v1" \
    --responses_api_api_key "$HF_TOKEN" \
    --responses_api_stream \
    --enable_live_transcription
```

### Chat Completions 后端

与 `responses-api` 配置相同，复用同一套 `--responses_api_*` 连接参数，但访问 `/v1/chat/completions` 而不是 `/v1/responses`。在以下情况优先使用它：

- 服务商在 Responses 路径上忽略 `chat_template_kwargs.enable_thinking`，需要一个 `reasoning_effort` 旋钮来抑制推理，或
- 服务器的 Responses 流式工具调用路径不可靠，而 Chat Completions 的流式工具调用很稳定。某些 vLLM 构建有此问题；见 [#312](https://github.com/huggingface/speech-to-speech/issues/312)。

在 chat-template 标志无效的服务商上，加 `--responses_api_reasoning_effort none` 可禁用推理：

```bash
# 带工具调用的 vLLM 服务 Qwen 模型
speech-to-speech \
    --mode realtime \
    --stt parakeet-tdt \
    --llm_backend chat-completions \
    --tts qwen3 \
    --model_name "Qwen/Qwen3-4B-Instruct-2507" \
    --responses_api_base_url "http://localhost:8000/v1" \
    --responses_api_stream
```

```bash
# 通过 Cerebras 上的 HF router 服务 Gemma 4 31B，为低语音延迟禁用推理
speech-to-speech \
    --mode realtime \
    --stt parakeet-tdt \
    --llm_backend chat-completions \
    --tts qwen3 \
    --model_name "google/gemma-4-31B-it:cerebras" \
    --responses_api_base_url "https://router.huggingface.co/v1" \
    --responses_api_api_key "$HF_TOKEN" \
    --responses_api_reasoning_effort none \
    --responses_api_stream
```

### 完全本地

在单独的 llama.cpp 进程中运行 LLM，可获得摩擦最小的完全本地设置，如 [Reachy Mini 本地对话指南](https://huggingface.co/blog/local-reachy-mini-conversation) 所示：

```bash
# 终端 1：llama.cpp 服务 Gemma 4
llama-server -hf ggml-org/gemma-4-E4B-it-GGUF -np 2 -c 65536 -fa on --swa-full
```

```bash
# 终端 2：使用该本地 LLM 服务器的 speech-to-speech
speech-to-speech \
    --mode realtime \
    --stt parakeet-tdt \
    --llm_backend responses-api \
    --tts qwen3 \
    --model_name "ggml-org/gemma-4-E4B-it-GGUF" \
    --responses_api_base_url "http://127.0.0.1:8080/v1" \
    --responses_api_api_key "" \
    --responses_api_stream \
    --enable_live_transcription
```

当你希望通过运行服务器的机器直接对话时，可以用 `--mode local` 代替 `--mode realtime`。进程内本地后端仍可通过 Apple Silicon 上的 `--llm_backend mlx-lm` 或 CUDA / CPU 上的 `--llm_backend transformers` 使用。

## 多语言支持

语言覆盖取决于你选择的 STT 和 TTS 后端，而不是管线本身：

| 组件 | 后端 | 语言 |
|---|---|---|
| STT | Parakeet TDT（默认） | 25 种欧洲语言 |
| STT | Whisper / Whisper MLX / Faster Whisper | 广泛的多语言覆盖，取决于所选 Whisper 检查点 |
| STT | Paraformer | 取决于所选 FunASR 检查点；默认为中文 |
| TTS | Qwen3-TTS（默认） | 多语言，默认 `--qwen3_tts_language auto` |
| TTS | Kokoro | 多种语言/音色映射，取决于后端可用性 |
| TTS | ChatTTS | 英语和中文 |
| TTS | MMS TTS | 通过 MMS 检查点的广泛多语言覆盖 |

确保你搭配的 STT、LLM 和 TTS 都覆盖你的目标语言。两种用法：

- **单语言**：把 `--language` 设为目标语言代码。默认是 `en`。
- **语言切换**：设 `--language auto`。STT 检测每句语音的语言并转发给 LLM。可选加 `--enable_lang_prompt`，会追加一句"请用……回复我"的指令。默认为 `False`；大模型通常能根据上下文推断语言，但显式指令有助于小模型。

自动语言检测：

```bash
speech-to-speech \
    --stt parakeet-tdt \
    --language auto \
    --llm_backend mlx-lm \
    --model_name "mlx-community/Qwen3-4B-Instruct-2507-bf16"
```

单一非英语语言（此例为中文）：

```bash
speech-to-speech \
    --stt whisper-mlx \
    --stt_model_name large-v3 \
    --language zh \
    --llm_backend mlx-lm \
    --model_name mlx-community/Qwen3-4B-Instruct-2507-bf16
```

两条命令也都可以叠加在 `--local_mac_optimal_settings` 之上；显式 `--stt` 参数会覆盖它设置的默认值。

## Pocket TTS

来自 Kyutai Labs 的 Pocket TTS 提供带音色克隆的流式 TTS：

```bash
speech-to-speech \
    --tts pocket \
    --pocket_tts_voice jean \
    --pocket_tts_device cpu
```

可用音色预设：`alba`、`marius`、`javert`、`jean`、`fantine`、`cosette`、`eponine`、`azelma`。自定义音色文件和 Hugging Face 路径也支持。

## CLI 参考

所有 CLI 参数的参考见 [arguments classes](./src/speech_to_speech/arguments_classes) 和 `speech-to-speech -h`。

### 模块级参数

见 [ModuleArguments](./src/speech_to_speech/arguments_classes/module_arguments.py)。它允许设置：

- 公共 `--device`，如果所有部分应运行在相同设备上
- `--mode`：`realtime`（默认）、`local`、`socket` 或 `raw-websocket`
- STT 实现（`--stt`）
- LLM 后端（`--llm_backend`：`transformers`、`mlx-lm`、`responses-api` 或 `chat-completions`）
- TTS 实现（`--tts`）
- 日志级别
- 实时管线池大小（`--num_pipelines`）

### VAD 参数

见 [VADHandlerArguments](./src/speech_to_speech/arguments_classes/vad_arguments.py)。值得注意的选项：

- `--thresh`：触发语音活动检测的阈值。
- `--min_speech_ms`：被视为语音的检测活动最小时长。
- `--min_speech_continuation_ms`：持续语音的迟滞阈值，用于在重开窗口内延续软结束、未提交的话轮。默认推荐搭配为 `--min_speech_ms 384 --min_speech_continuation_ms 192`。
- `--min_silence_ms`：切分语音所需的最短静音长度。默认 64 毫秒。
- `--short_segment_merge_ms`：可选的合并窗口，用于拼接相邻的、每个都短于 `--min_speech_ms` 的 VAD 段。
- `--unanswered_reopen_ms`：一个软结束的推测话轮在收到任何助手输出之前保持可重开状态的合理性上限。

### STT、LLM、TTS 参数

每个 STT、LLM、TTS 实现都暴露了 `model_name`、`torch_dtype` 和 `device`。STT 和 TTS 参数使用处理器前缀，例如 `--stt_model_name` 或 `--qwen3_tts_device`。LLM 模型选择与聊天设置通过无前缀参数跨后端共享，例如 `--model_name` 和 `--chat_size`；后端专属参数用 `responses_api_` 前缀（`responses-api` 和 `chat-completions` 后端）或 `llm_` 前缀（本地后端）。

例如：

```bash
# 本地 transformers/mlx-lm 后端
--model_name google/gemma-2b-it

# OpenAI 兼容后端
--llm_backend responses-api --model_name deepseek-chat --responses_api_base_url https://api.deepseek.com
```

### 生成参数

其他生成参数可以用处理器前缀加 `_gen_` 设置，例如 `--stt_gen_max_new_tokens 128` 或 `--llm_gen_temperature 0.7`。尚未暴露的参数可以加到对应的 arguments class。

## 参与贡献

欢迎提 issue 和 PR。好的起点是[开放 issue](https://github.com/huggingface/speech-to-speech/issues)。较大的改动请先开 issue 讨论方案。

本地开发：

```bash
uv sync
pytest
ruff check
```

## Star 历史

[![Star History Chart](assets/star-history.svg)](https://github.com/huggingface/speech-to-speech/stargazers)

## 引用

如果你使用这条管线，也请引用你运行到的组件模型。默认是：

### Silero VAD

```bibtex
@misc{SileroVAD,
  author = {Silero Team},
  title = {Silero VAD: pre-trained enterprise-grade Voice Activity Detector (VAD), Number Detector and Language Classifier},
  year = {2021},
  publisher = {GitHub},
  journal = {GitHub repository},
  howpublished = {\url{https://github.com/snakers4/silero-vad}},
  email = {hello@silero.ai}
}
```

### Parakeet TDT

```bibtex
@misc{parakeet-tdt,
  author = {NVIDIA},
  title = {Parakeet TDT 0.6B v3},
  publisher = {Hugging Face},
  howpublished = {\url{https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3}}
}
```

### Qwen3-TTS

```bibtex
@misc{qwen3-tts,
  author = {Qwen Team},
  title = {Qwen3-TTS},
  publisher = {Hugging Face},
  howpublished = {\url{https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice}}
}
```
