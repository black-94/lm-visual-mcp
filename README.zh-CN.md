# Vision MCP Server / 视觉 MCP 服务器

> [**English**](README.md) | **简体中文**

一个**视觉 MCP 服务器**，通过
[Model Context Protocol](https://modelcontextprotocol.io) 为纯文本 LLM / 编码 agent
提供视觉能力，并附带一个**视觉代理（Vision Proxy）**——一个透明 HTTP 转发器，
让纯文本模型的普通 API 客户端无需改代码即可“看见”图片。

---

## 目录

- [简介](#简介)
- [为什么需要视觉 MCP？](#为什么需要视觉-mcp)
- [架构](#架构)
- [视觉代理](#视觉代理)
- [特性](#特性)
- [环境要求](#环境要求)
- [安装](#安装)
- [快速上手](#快速上手)
- [生命周期命令](#生命周期命令)
- [MCP 客户端配置](#mcp-客户端配置)
- [配置](#配置)
- [工具](#工具)
- [结构化输出](#结构化输出)
- [环境诊断](#环境诊断)
- [提供商探测](#提供商探测)
- [安全](#安全)
- [开发](#开发)
- [故障排查](#故障排查)
- [许可](#许可)

---

## 简介

本项目是一个**视觉 MCP 服务器**：让只能读文本的 LLM / 编码 agent 具备“看”的能力。
它对外暴露一组与 Z.AI 兼容的视觉工具（`analyze_image`、`extract_text_from_screenshot`、
`ui_diff_check` 等），并把每个请求路由到一条可配置的视觉提供商链路
（AGY → Codex → Gemini API → OpenCode），支持自动降级（fallback）。

**提供商、模型、API Key、降级顺序都是服务器策略（server policy）——LLM 永远看不到、
也不能选择它们。**

除了 MCP 工具，本项目还实现了一个**透明的视觉代理（Vision Proxy）**：

- 只支持 **OpenAI**（`openai/chat`、`openai/responses`）与 **Anthropic** 两种协议；
- **无图片时**完全透明转发（透传 body，仅剥离逐跳头，API Key 与 body 原样透传）；
- **有图片时**先做一次通用的 describe（复用现有视觉提供商），再按**每张图片的
  SHA-256 缓存**命中，把请求里的图片部分改写为文本；
- 响应（含 SSE 流式）始终原样回传。

它的目标：让 Claude Code / 其他 agent 的**文本模型客户端**仍走原来的 base_url，
只是把 base_url 指到代理，就能自动获得图像感知能力，而无需在客户端里改任何代码。

---

## 为什么需要视觉 MCP？

大多数编码 agent / 纯文本 LLM 无法“看见”截图、报错堆栈、UI 原型或图表。这个服务器充当它们的眼睛：

```text
纯文本 LLM
      │  MCP
      ▼
视觉 MCP 服务器
   ├── Z.AI 兼容视觉工具
   ├── 专用提示词层
   ├── 媒体 / 工作区层
   ├── 结构化 JSON 层
   └── 提供商路由（AGY → Codex → Gemini → OpenCode）
```

```
纯文本 LLM
      │  HTTP（base_url 指向代理）
      ▼
视觉代理（openai/chat · openai/responses · anthropic）
      │  无图片 => 字节级透明转发；有图片 => describe + 改写为文本
      ▼
真实 OpenAI / Anthropic API
```

---

## 架构

整套系统由两个协作的单例进程提供服务：

1. **共享守护进程（Shared daemon）**——持有唯一 `VisionSession` 与所有 MCP 视觉工具。
   默认 CLI 入口是一个*客户端*，把工具调用通过回环 HTTP 转发给这个守护进程。它通过并发
   信号量串行化请求，并在 `idle_timeout_ms` 内无流量时自我回收。

2. **视觉代理（Vision Proxy）**——上文所述的透明转发器，服务 agent 的文本模型客户端，
   同样是单例。

每个进程都通过 `~/.cache/lm-visual-mcp/` 下的 pidfile 以及监听其端口的 PID 来标识。
见 [生命周期命令](#生命周期命令)。

---

## 视觉代理

### 使用方式

把 agent 的 `base_url` 指向代理，仅此而已。代理把请求转发到从路径中解码出的真实 API URL。

```text
http://127.0.0.1:8787/proxy/<协议路径>/<base64url(基础 API URL)>[<SDK 追加后缀>]
```

- `<协议路径>`：`openai/chat`、`openai/responses`、`anthropic` —— **显式声明**，
  绝不从 URL 或请求体推断。
- `<base64url>`：对**基础 API URL**（域名，可含路径前缀，如
  `https://api.openai.com` 或 `https://.../api/plan`）做 base64url 编码。
- 无 querystring，只用 base64url。
- **SDK 后缀拼接**：SDK（如 Anthropic SDK 用的 Claude Code）会在 base_url 后追加
  endpoint 路径（如 `/v1/messages`）。代理扫描剩余路径段，认第一个能解码成完整
  http(s) URL 的段作为基础 URL，并把其后段**拼回到解码出的 URL 上**再转发——
  最终请求路径 = 基础 URL + SDK 追加后缀。这样网关收到的才是完整路径
  （如 `https://.../api/plan/v1/messages`）。raw curl 不带后缀时，就只转发基础 URL。

```text
http://127.0.0.1:8787/proxy/openai/chat/<b64-encoded-full-api-url>
http://127.0.0.1:8787/proxy/openai/responses/<b64-encoded-full-api-url>
http://127.0.0.1:8787/proxy/anthropic/<b64-encoded-full-api-url>
```

### 核心流程

1. **无图片** → 字节级透明转发：仅剥离逐跳头（hop-by-hop headers），
   `Authorization` / `x-api-key` 与 body 原样透传。
2. **有图片** → 解析协议、抽取图片 → 每张图按 **SHA-256 缓存** 命中（命中则直接复用描述，
   未命中则合并成**一次批量视觉调用**）→ 把请求里的图片部分改写为文本 → 再转发。
3. 响应（含 SSE）**始终原样回传**，绝不改写。

### 约束

- 只支持 **OpenAI + Anthropic** 两种协议；支持两种 OpenAI 格式（Chat + Responses）。
- **不抽取** `system_prompt` / `user_prompt`：只做一次通用 describe（“传感器”），
  更深的细节留给文本模型自己调用 `lm-visual-mcp` 的 MCP 工具去挖掘。
- 多图一次提交，一次批量 describe；缓存粒度 = **每张图片**（SHA-256），
  视觉调用粒度 = **每次请求**。
- describe 复用现有 `image_analysis.SYSTEM_PROMPT` 与 Provider Router，走相同的
  提供商链路与自动降级。
- 新增加依赖仅 `aiohttp`。

### 为什么有它

MCP 工具在 agent 侧需要显式调用；而文本模型自己的 API 客户端（OpenAI/Anthropic）是
无法“看”图的。代理把“看图”变成 base_url 的替换，让普通 LLM 调用也具备视觉能力。

---

## 特性

- 8 个 Z.AI 兼容视觉工具 + 2 个别名。
- 提供商无关的工具：工具 schema **不含** `provider`/`model`/`api_key`/`workdir`/`timeout`——
  均为服务器配置。
- 可配置顺序与降级策略的 Provider Router。
- 统一结构化 JSON 输出（observations、texts、elements、bbox）。
- 支持本地路径与 HTTP(S) URL；拒绝 `file://`。
- 每任务隔离的工作区，自动清理。
- CLI 原生图片（Codex `-i`、OpenCode `--file`）与 AGY 工作区 staging + 视觉能力探测。
- Gemini API via `google-genai`。
- `lm-visual-mcp doctor` 环境检查 + `--probe` 视觉冒烟测试。
- **透明视觉代理**（OpenAI / Anthropic 协议，自动 describe + 缓存）。
- **生命周期命令** `start` / `stop` / `restart`。
- 无 ACP / 无非 transport 抽象（v1）。

---

## 环境要求

- Python **3.11+**
- macOS / Linux / Windows

---

## 安装

### 方式 A：pip（推荐用虚拟环境）

```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

### 方式 B：uv（更快）

```bash
uv venv --python 3.11 .venv
source .venv/bin/activate                # Windows: .venv\Scripts\Activate.ps1
uv pip install -e ".[dev]"               # dev = pytest, pytest-asyncio, Pillow（供 doctor --probe）
```

### 验证安装

```bash
lm-visual-mcp --version       # 确认可执行文件在 PATH 上
lm-visual-mcp doctor          # 检查 4 个提供商：enabled / executable / model
lm-visual-mcp doctor --probe  # 可选：跑一次真实 AGY 视觉冒烟测试（需 Pillow）
```

> Windows 下若 `lm-visual-mcp` 不在 PATH，用 `python -m lm_visual_mcp` 代替。

---

## 快速上手

### 1) 准备配置

```bash
# 复制示例配置并按需修改
cp config.example.yaml ~/.config/lm-visual-mcp/config.yaml

# 编辑：开启至少一个提供商，配置 Gemini 的 API Key（见下文）
```

### 2) 以 MCP stdio 服务器运行

```bash
lm-visual-mcp --config ~/.config/lm-visual-mcp/config.yaml
# 或
python -m lm_visual_mcp --config ~/.config/lm-visual-mcp/config.yaml
```

无论打开多少个 Claude Code 会话，都只会有**一个**共享守护进程（`runtime.host:runtime.port`
上的全局单例）。每个会话进程都会先探测它，已在运行就复用，否则自动拉起，然后代理到它。
超过 `runtime.max_concurrency` 的请求会在守护进程内排队。守护进程在
`runtime.idle_timeout_ms` 内无流量时自动退出。

> **自动拉起（单例）** MCP 客户端启动时会自动确保 daemon 与 vision proxy 两个单例都在跑
> （probe-then-launch）。视觉能力开箱即用，无需手动启动。

### 3) 把文本模型客户端指向代理

把客户端的 `base_url` 指向代理即可（适用于 OpenAI 或 Anthropic 协议）：

```text
http://127.0.0.1:8787/proxy/anthropic/<base64url(https://api.anthropic.com)>
```

例如 Claude Code 通过 `ANTHROPIC_BASE_URL` 指向它即可让文本模型自动“看图”。
URL 格式与约束见 [视觉代理](#视觉代理)。

---

## 生命周期命令

默认 MCP 自动拉起，但 daemon 与 proxy 也可单独管理：

```bash
lm-visual-mcp start    [--service daemon|proxy]   # probe-then-launch，幂等
lm-visual-mcp stop     [--service daemon|proxy]   # SIGTERM，幂等
lm-visual-mcp restart  [--service daemon|proxy]   # 先停后启
```

- 无 `--service` 时同时管理 daemon + proxy（`stop`/`restart` 先停 proxy 再停 daemon）。
- `stop` 优先读 pidfile + 进程 cmdline 校验（防止误杀被复用 PID 的无关进程），
  兜底用 `lsof -ti tcp:<port>` 找端口监听 PID。
- daemon / proxy 绑定成功后才写 pidfile（`~/.cache/lm-visual-mcp/`），`stop` 后清理。

---

## MCP 客户端配置

```json
{
  "mcpServers": {
    "vision": {
      "command": "lm-visual-mcp",
      "args": ["--config", "/Users/me/.config/lm-visual-mcp/config.yaml"],
      "env": { "GEMINI_API_KEY": "..." }
    }
  }
}
```

---

## 配置

配置优先级：**CLI 参数 > 环境变量 > 配置文件 > 内置默认值**。

```yaml
version: 1

providers:
  order: [agy, codex, gemini, opencode]
  agy:
    enabled: true
    command: agy
    model: gemini-3.6-flash
    effort: high
  codex:
    enabled: true
    command: codex
    model: gpt-5.6-luna
    effort: high
  gemini:
    enabled: true
    model: gemini-3.6-flash
    effort: high
    api_key_env: GEMINI_API_KEY
  opencode:
    enabled: true
    command: opencode
    model: null
    effort: null

runtime:
  workdir: null          # null => 每任务临时目录
  timeout: 120
  max_concurrency: 2
  host: 127.0.0.1        # 共享守护进程绑定 host
  port: 6506             # 共享守护进程绑定 port
  idle_timeout_ms: 300000 # daemon 空闲自动退出

fallback:
  enabled: true
  on:
    - command_not_found
    - not_authenticated
    - permission_denied
    - api_key_missing
    - quota_exhausted
    - unsupported_media
    - timeout
    - temporary_failure

media:
  max_image_mb: 20
  max_video_mb: 8
  download_timeout: 30
  max_download_mb: 32

logging:
  level: INFO

# 透明视觉代理（lm-visual-mcp proxy）
proxy:
  host: 127.0.0.1
  port: 8787
```

### 提供商顺序

路由按配置顺序尝试提供商，失败即降级。默认：`agy → codex → gemini → opencode`。

默认不可降级的错误：`invalid_input`、`invalid_model`、`config_error`。`fallback.on` 列表是最终依据。

### 提供商模型

每个提供商的模型都在配置里设置，降级时自动使用——无需管理跨提供商的模型命名空间。
把模型设为 `null` 则让提供商使用自己的默认。

```yaml
providers:
  agy:      { model: gemini-xxx }
  codex:    { model: gpt-xxx }
  gemini:   { model: gemini-xxx }
  opencode: { model: google/gemini-xxx }
```

### 思考强度

每个提供商的思考强度用 `effort`（`low` | `medium` | `high` | `xhigh`，随提供商而异；
`null` = 提供商默认）配置，并在运行时透传：

- **AGY** → `--effort`
- **Codex** → `-c model_reasoning_effort=<effort>`
- **Gemini** → `thinking_config`（思考级别）
- **OpenCode** → `--variant`

### Gemini API key

API Key **永远不是**工具参数。解析顺序：

```text
LM_VISUAL_MCP_GEMINI_API_KEY
    > config.providers.gemini.api_key_env（它命名的环境变量）
    > GEMINI_API_KEY
```

为兼容，配置文件中也可放明文 `api_key`；它以 `SecretStr` 存储，绝不打印、绝不导出、
绝不出现在 MCP 响应或异常中。推荐用环境变量。

### 环境变量

```text
LM_VISUAL_MCP_CONFIG                 配置文件路径
LM_VISUAL_MCP_WORKDIR                runtime workdir
LM_VISUAL_MCP_TIMEOUT                runtime timeout (s)
LM_VISUAL_MCP_MAX_CONCURRENCY        max concurrency（超出则排队）
LM_VISUAL_MCP_HOST                   daemon 绑定 host（默认 127.0.0.1）
LM_VISUAL_MCP_PORT                   daemon 绑定 port（默认 6506）
LM_VISUAL_MCP_IDLE_TIMEOUT_MS        daemon 空闲退出超时（默认 300000）
LM_VISUAL_MCP_AGY_COMMAND / _MODEL / _EFFORT
LM_VISUAL_MCP_CODEX_COMMAND / _MODEL / _EFFORT
LM_VISUAL_MCP_GEMINI_MODEL / _API_KEY / _EFFORT
GEMINI_API_KEY                       gemini API key（兜底）
LM_VISUAL_MCP_OPENCODE_COMMAND / _MODEL / _EFFORT
LM_VISUAL_MCP_PROXY_HOST             代理绑定 host（默认 127.0.0.1）
LM_VISUAL_MCP_PROXY_PORT             代理绑定 port（默认 8787）
LM_VISUAL_MCP_LOG_LEVEL              ERROR | WARNING | INFO | DEBUG
```

### 工作目录

`runtime.workdir: null`（默认）时，每任务使用全新临时目录，完成后清理。配置了项目工作目录时，
任务媒体暂存于 `<workdir>/.lm-visual-mcp/<uuid>/` 并在之后移除。用户的文件永不被修改或删除。

### 媒体限制

图片：png/jpg/jpeg/webp/gif/bmp/tiff（默认 `max_image_mb: 20`）。视频：mp4/mov/m4v
（`max_video_mb: 8`）。远程下载受超时、大小与重定向次数限制，并按 MIME 类型校验。

---

## 工具

| 工具 | 用途 |
|------|---------|
| `ui_to_artifact` | UI 截图转 `code` / `prompt` / `spec` / `description` |
| `extract_text_from_screenshot` | 逐字 OCR 代码 / 终端 / 配置 / 文档 |
| `diagnose_error_screenshot` | 诊断报错 / 堆栈 / 根因 / 修复建议 |
| `understand_technical_diagram` | 理解架构 / 流程图 / UML / ER 图 |
| `analyze_data_visualization` | 分析图表：趋势、异常、对比、分布 |
| `ui_diff_check` | 对比 EXPECTED vs ACTUAL UI 视觉回归 |
| `analyze_image` | 通用视觉分析 |
| `analyze_video` | 视频分析（mp4/mov/m4v） |

别名共享同一实现：`image_analysis` → `analyze_image`、`video_analysis` → `analyze_video`。

---

## 结构化输出

每个提供商的输出都被归一化到统一 schema，并包装成标准信封：

```json
{
  "provider": "codex",
  "model": "gpt-xxx",
  "result": {
    "summary": "Short visual summary",
    "answer": "Direct answer",
    "observations": [{ "type": "text", "text": "...", "confidence": 0.95 }],
    "texts": [{ "text": "visible text", "bbox": [100, 100, 900, 200], "confidence": 0.98 }],
    "elements": [{ "label": "Build button", "type": "ui_element", "bbox": [700, 20, 820, 70], "confidence": 0.93 }],
    "warnings": []
  },
  "meta": {
    "duration_ms": 4812,
    "fallbacks": [],
    "usage": { "input_tokens": null, "output_tokens": null }
  }
}
```

`bbox` 归一化为 `0..1000` 的 `[x_min, y_min, x_max, y_max]`。无法确定的值不猜测——省略并加警告。

---

## 环境诊断

```bash
lm-visual-mcp doctor
lm-visual-mcp doctor --probe   # 额外跑一次真实 AGY 视觉冒烟测试（需 Pillow）
lm-visual-mcp --version
```

`doctor` 绝不打印 API Key 内容。

---

## 提供商探测

- **AGY**: `agy -p "<prompt>" --output-format json`。图片暂存到工作区媒体目录，以裸文件名引用。
  AGY 忽略 shell 工作目录，总是在自己的工作区运行工具，因此媒体目录通过 `--add-dir`（可重复）
  注册；已加入目录中的文件可原生读取。服务器在沙箱（`--sandbox`）中启动 AGY。没有独立的视觉
  探测——每个图片请求恰好是一次真实 AGY 调用，视觉能力从该调用的结果中发现并缓存。
- **Codex**: `codex exec -i <img> ... --output-schema ... -s read-only`。原生传图；
  强制只读沙箱。
- **Gemini**: `google-genai`，结构化 JSON，多图，配置模型。
- **OpenCode**: `opencode run --format json`，图片经 `--file`，解析 JSON 事件流取最终助手结果。

> **AGY 非确定性**：AGY 从 `--add-dir` 注册的目录读取图片。截至 AGY CLI 1.1.x，
> headless 模式仍非确定——一次运行可能间歇性地索要它并不拥有的工具权限。发生时服务器会检测并
> 透明降级到下一个提供商。`lm-visual-mcp doctor --probe` 会报告能力而不终止服务器。

---

## 安全

服务器只 `LOOK / READ / UNDERSTAND / COMPARE / ANALYZE`——从不
`EDIT / BUILD / EXECUTE / MODIFY`。Codex 在只读沙箱中运行；AGY 与 OpenCode 从不带危险
的自动批准启动。API Key 从所有日志与响应中脱敏。

代理透传 API Key 与 body（仅剥离逐跳头）；自身不持有任何账号状态。

---

## 开发

```bash
python -m pytest
```

测试覆盖配置、路由、工作区、媒体、四个提供商（mock 子进程 / genai）、Z.AI 工具 schema
兼容性、代理适配器 + 缓存，以及 MCP `tools/list` + `tools/call` 冒烟测试。

---

## 故障排查

- **`agy` 对图片降级到 codex** — AGY 从 `--add-dir` 注册的目录读图。Headless 模式
  非确定，可能间歇性自动拒绝工具权限。媒体目录可原生读取，因此无需 `read_file` 或
  `command(ls)` 授权，且**绝不能**配置 `command(*)`。仍失败时服务器透明降级。
  用 `lm-visual-mcp doctor --probe` 直接测试 AGY。
- **无响应** — 没有启用任何提供商。请在配置中启用提供商。
- **Gemini 未使用** — 需要 API Key；见 “Gemini API key”。
- **Codex 阻塞等待 stdin** — 服务器始终为 CLI 提供商关闭 stdin。
- **stdout 损坏** — 所有日志走 stderr；stdout 专用于 MCP。
- **代理不转发** — 确认 base_url 指向 `/proxy/<协议路径>/<base64url(基础 API URL)>`，
  且 daemon 与 proxy 单例已启动（`lm-visual-mcp start` / `lm-visual-mcp doctor`）。

---

## 许可

MIT