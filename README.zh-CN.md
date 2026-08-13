# Vision MCP Server / 视觉 MCP 服务器

> [English](README.md) | **简体中文**

`lm-visual-mcp` 通过 [Model Context Protocol](https://modelcontextprotocol.io) 为纯文本 LLM
和 Coding Agent 提供视觉输入。项目还带有一个可选的 HTTP Vision Proxy：它会先把
OpenAI 或 Anthropic 请求中的图片描述成文本，再把改写后的请求转发给上游模型 API；同时
兼容 Claude Code Auto classifier 经过 Anthropic-compatible gateway 的场景。

仓库当前版本为 **v0.1.0**。图片分析是稳定主路径；视频工具名为兼容性而保留，但当前没有
provider 具备可靠的端到端视频附件通路。详见[当前限制](#当前限制)。

## 能力概览

- 8 个任务型视觉工具和 2 个兼容别名。
- 服务端控制 provider 路由，默认 AGY → Codex → Gemini → OpenCode。
- 每任务隔离工作区和有界远程媒体下载。
- 多个 MCP 客户端复用一个本地 daemon。
- 无论使用哪个 provider，均返回统一 JSON 结构。
- 支持 OpenAI Chat、OpenAI Responses、Anthropic Messages 的透明视觉代理。
- 支持 Claude Code Auto classifier 的可配置 thinking 改写和第一阶段响应规范化。
- CLI 生命周期管理和环境诊断。

Provider、模型、凭证、fallback 策略、工作目录和超时均属于服务端配置，不会出现在 MCP
tool schema 中。

## 架构

```text
MCP 客户端进程
    │ stdio
    ▼
lm-visual-mcp client
    │ loopback HTTP
    ▼
共享 daemon（一个 VisionSession）
    ├── 选择任务 prompt
    ├── 工作区与媒体暂存
    ├── 全局并发限制
    └── ProviderRouter ── AGY → Codex → Gemini → OpenCode
```

```text
OpenAI / Anthropic SDK
    │ base_url 指向 proxy
    ▼
Vision Proxy
    ├── 无图片：原始 request body 透传
    └── 有图片：抽取 → 缓存/描述 → 替换为文本
    │
    ▼
上游模型 API（响应与 SSE 流式返回；classifier 第一阶段有兼容性规范化）
```

不带子命令运行 `lm-visual-mcp` 时，进程作为 MCP stdio 客户端：先探测共享 daemon 和
Vision Proxy，缺失时自动拉起，再把 MCP 调用转给 daemon。`runtime.max_concurrency` 对所有
已连接 MCP 客户端共同生效。daemon 在 `runtime.idle_timeout_ms` 无流量后退出；proxy 会持续
运行，直到被显式停止。

## 环境要求

- Python 3.11+
- 至少一个已配置的图片 provider：
  - 已安装并登录的 `agy`、`codex` 或 `opencode` CLI；或
  - 供 `google-genai` 使用的 Gemini API key。
- macOS、Linux 的覆盖最完整。Windows 可完成基础启动，但 `stop`/端口 PID 探测仍依赖
  POSIX 的 `ps` 和 `lsof`；见[当前限制](#当前限制)。

## 安装

从源码仓库安装：

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e .
```

开发和运行完整测试：

```bash
python -m pip install -e ".[dev]"
```

确认安装：

```bash
lm-visual-mcp --version
lm-visual-mcp doctor
```

如果 console script 不在 `PATH`，把示例中的 `lm-visual-mcp` 替换为
`python -m lm_visual_mcp`。

## 快速上手

### 1. 配置 provider

```bash
mkdir -p ~/.config/lm-visual-mcp
cp config.example.yaml ~/.config/lm-visual-mcp/config.yaml
```

编辑复制后的文件，禁用不用的 provider，并避免把凭证提交到仓库。使用 Gemini 时推荐设置
环境变量：

```bash
export GEMINI_API_KEY="..."
```

### 2. 添加到 MCP 客户端

```json
{
  "mcpServers": {
    "vision": {
      "command": "lm-visual-mcp",
      "args": [
        "--config",
        "/Users/me/.config/lm-visual-mcp/config.yaml"
      ],
      "env": {
        "GEMINI_API_KEY": "..."
      }
    }
  }
}
```

建议使用绝对配置路径。进程通过 stdout 传输 MCP，日志走 stderr。

### 3. 可选：让模型 API 流量经过 Vision Proxy

先把上游基础 API URL 编码成无 padding 的 base64url：

```bash
python -c "import base64; u=b'https://api.anthropic.com'; print(base64.urlsafe_b64encode(u).decode().rstrip('='))"
```

然后配置 SDK 的 base URL：

```text
http://127.0.0.1:8787/proxy/anthropic/aHR0cHM6Ly9hcGkuYW50aHJvcGljLmNvbQ
```

可用协议路径：

```text
/proxy/openai/chat/<base64url(基础 API URL)>
/proxy/openai/responses/<base64url(基础 API URL)>
/proxy/anthropic/<base64url(基础 API URL)>
```

支持 SDK 自动追加 endpoint。例如 Anthropic SDK 追加 `/v1/messages` 后，proxy 会把该后缀
拼到解码出的上游基础 URL 上。

当前行为需要特别注意：

- 无图片时 request body 字节通常保持不变；唯一例外是识别到 classifier 且
  `proxy.classifier.disable_thinking: true` 时会写入 `thinking: disabled`。
- OpenAI adapter 支持 data URL 和 HTTP(S) 图片 URL。
- Anthropic adapter 当前只支持 base64 图片 source。
- 图片解析失败时会 fail-open，转发原始请求。
- 发往 proxy endpoint 的 query string 当前不会转发。
- proxy 必须只运行在受信任的 loopback 接口；见[安全](#安全)。

### Claude Code Auto classifier 兼容

Auto classifier 与普通请求使用同一个 Anthropic `/v1/messages` endpoint。Proxy 不依赖
model 名或 token 数识别它，而是通过 security-monitor system marker 和无 tools 识别
classifier 家族；其中带 `</block>` stop sequence 的请求被识别为已确认的二元第一阶段。

识别后，`proxy.classifier.disable_thinking` 只控制**请求改写**。默认 `true`，向请求写入
`"thinking": {"type": "disabled"}`；如果上游模型拒绝 disabled thinking，可设置为
`false`。

第一阶段 classifier 的**响应规范化始终启用**，不受该参数影响。部分
Anthropic-compatible gateway 会忽略 `stop_sequences`，或把 thinking block 放在 text 前面。
Proxy 会从 text block 中提取唯一、明确的 `<block>yes</block>` / `<block>no</block>` verdict，
并恢复 Anthropic stop-sequence 语义：只返回一个内容为 `<block>yes` 或 `<block>no` 的 text
block，同时设置 `stop_reason: "stop_sequence"`、`stop_sequence: "</block>"`。没有 verdict
或同时出现冲突的 yes/no 时，proxy 不会猜测或改写。

第一阶段的 `no` 通常表示直接放行，`yes` 表示可能需要后续阶段，并不一定是最终拒绝。
完整的抓包结果、识别条件、误报/漏报边界和验证记录见
[`classifier_compatibility.md`](classifier_compatibility.md)。

## CLI

```text
lm-visual-mcp [--config PATH] [--log-level LEVEL]
lm-visual-mcp doctor [--probe]
lm-visual-mcp daemon
lm-visual-mcp proxy [--host HOST] [--port PORT]
lm-visual-mcp start   [--service daemon|proxy]
lm-visual-mcp stop    [--service daemon|proxy]
lm-visual-mcp restart [--service daemon|proxy]
lm-visual-mcp --version
```

公共选项可写在子命令之前或之后。不指定 `--service` 时，生命周期命令同时管理 daemon 和
proxy。`doctor --probe` 会执行一次真实 AGY 图片冒烟测试，需要 Pillow 和可用的 AGY CLI；
它不会对所有 provider 发起可能产生费用的真实调用。

Pidfile 和 daemon 日志位于 `~/.cache/lm-visual-mcp/`。

## 配置

配置优先级：

```text
CLI 参数 > 环境变量 > YAML 文件 > 内置默认值
```

未传 `--config` 时的搜索顺序：

1. `LM_VISUAL_MCP_CONFIG`
2. `./lm-visual-mcp.yaml`
3. `~/.config/lm-visual-mcp/config.yaml`
4. `~/.config/lm-visual-mcp/lm-visual-mcp.yaml`

所有当前字段见 [`config.example.yaml`](config.example.yaml)。最小示例：

```yaml
version: 1

providers:
  order: [codex, gemini, opencode]
  agy:
    enabled: false
    command: agy
  codex:
    enabled: true
    command: codex
    model: null
    effort: high
  gemini:
    enabled: true
    model: null
    effort: high
    api_key_env: GEMINI_API_KEY
  opencode:
    enabled: true
    command: opencode
    model: null
    effort: null

runtime:
  workdir: null
  timeout: 120
  max_concurrency: 2
  host: 127.0.0.1
  port: 6506
  idle_timeout_ms: 300000

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

proxy:
  host: 127.0.0.1
  port: 8787
  classifier:
    disable_thinking: true
```

### 环境变量

```text
LM_VISUAL_MCP_CONFIG
LM_VISUAL_MCP_WORKDIR
LM_VISUAL_MCP_TIMEOUT
LM_VISUAL_MCP_MAX_CONCURRENCY
LM_VISUAL_MCP_HOST
LM_VISUAL_MCP_PORT
LM_VISUAL_MCP_IDLE_TIMEOUT_MS

LM_VISUAL_MCP_AGY_COMMAND / _MODEL / _EFFORT
LM_VISUAL_MCP_CODEX_COMMAND / _MODEL / _EFFORT
LM_VISUAL_MCP_GEMINI_MODEL / _API_KEY / _EFFORT
LM_VISUAL_MCP_OPENCODE_COMMAND / _MODEL / _EFFORT

GEMINI_API_KEY
LM_VISUAL_MCP_PROXY_HOST
LM_VISUAL_MCP_PROXY_PORT
LM_VISUAL_MCP_PROXY_CLASSIFIER_DISABLE_THINKING
LM_VISUAL_MCP_LOG_LEVEL
```

Gemini 凭证解析顺序为：`LM_VISUAL_MCP_GEMINI_API_KEY`、兼容用的明文
`providers.gemini.api_key`、`api_key_env` 指向的环境变量、`GEMINI_API_KEY`。推荐使用
`api_key_env`；配置文件明文 key 只为兼容而保留。

### 工作区与媒体

`runtime.workdir: null` 时，每次 MCP 调用创建临时目录并在调用后删除。指定 workdir 时，
文件暂存到 `<workdir>/.lm-visual-mcp/<uuid>/`，任务结束后删除该任务目录。源文件只复制，
不会被修改。

MCP 图片类型：PNG、JPEG、WebP、GIF、BMP、TIFF。媒体层接受 MP4、MOV、M4V 视频扩展名，
但当前 provider 支持不完整。拒绝 `file://` URL；请使用本地路径或 HTTP(S) URL。

## MCP 工具

| 工具 | 必填输入 | 可选输入 | 用途 |
|---|---|---|---|
| `ui_to_artifact` | `image_source`, `output_type`, `prompt` | - | UI 转代码、prompt、spec 或描述 |
| `extract_text_from_screenshot` | `image_source`, `prompt` | `programming_language` | OCR/代码提取 |
| `diagnose_error_screenshot` | `image_source`, `prompt` | `context` | 报错与堆栈诊断 |
| `understand_technical_diagram` | `image_source`, `prompt` | `diagram_type` | 架构/UML/流程分析 |
| `analyze_data_visualization` | `image_source`, `prompt` | `analysis_focus` | 图表分析 |
| `ui_diff_check` | `expected_image_source`, `actual_image_source`, `prompt` | - | 视觉回归对比 |
| `analyze_image` | `image_source`, `prompt` | - | 通用图片分析 |
| `analyze_video` | `video_source`, `prompt` | - | 兼容/实验性视频入口 |

别名：`image_analysis` → `analyze_image`；`video_analysis` → `analyze_video`。

## 响应格式

```json
{
  "provider": "codex",
  "model": "configured-model",
  "result": {
    "summary": "简短摘要",
    "answer": "直接回答",
    "observations": [],
    "texts": [],
    "elements": [],
    "warnings": [],
    "details": {}
  },
  "meta": {
    "duration_ms": 4812.0,
    "fallbacks": [],
    "usage": {}
  }
}
```

为了可观测性，响应可以包含 `provider` 和 `model`，但调用方不能选择它们。bbox 约定采用
`0..1000` 范围的 `[x_min, y_min, x_max, y_max]`；当前尚未严格执行范围校验。

## Provider 说明

- **AGY**：把图片暂存到 added directory，以 `--sandbox` 调用 `agy`，并从真实请求中发现
  图片能力。Headless 图片访问可能不稳定，因此失败时会降级到下一个 provider。
- **Codex**：重复使用 `-i`，强制只读 sandbox，并通过 `--output-schema` 约束输出。
- **Gemini**：使用 `google-genai`、结构化 JSON 和多图片 part。
- **OpenCode**：重复使用 `--file`，解析 JSON event stream。

当前未固定 CLI 最低兼容版本；升级 provider CLI 后建议运行 `doctor`。

## 安全

安全部署模型是：受信任的单用户机器，两个 listener 都绑定 `127.0.0.1`。

- 不要把 daemon 6506 或 proxy 8787 端口暴露到局域网或公网。
- daemon `/tool` 没有认证。
- proxy 会把 Authorization/API key header 转发给路径中编码的上游，并能抓取 HTTP(S)
  图片 URL。当前没有上游 allowlist，也不会阻止私网图片目标；暴露 proxy 会产生 SSRF 和
  凭证转发风险。
- data URL 和 Anthropic base64 图片尚未获得与下载图片完全相同的大小校验。
- MCP 本地路径工具能读取调用方提交的路径，只应向受信任 agent 开放。
- 不要在 YAML 中放明文 API key。代码会避免主动记录 key，但尚未实现完整的按值日志脱敏。

原始 MCP 与 proxy 需求保留在 [`mcp_plan.md`](mcp_plan.md) 和
[`proxy_plan.md`](proxy_plan.md)。当前全仓库审查结论、风险分级和整改优先级见
[`code_review.md`](code_review.md)；Auto classifier 抓包与兼容细节见
[`classifier_compatibility.md`](classifier_compatibility.md)。

## 当前限制

- `analyze_video`/`video_analysis` 已注册，但 Codex、Gemini、OpenCode 会拒绝视频；AGY
  目前也拿不到可靠的暂存视频引用。应按“不支持视频”处理。
- Proxy endpoint 的 query string 会被丢弃。
- Proxy 图片解析失败会 fail-open，转发原始图片请求。
- Proxy 的 base64/data 图片需要统一 MIME 与大小校验。
- Proxy 上游和远程图片 URL 没有可配置 allowlist/私网策略。
- Windows 的 `stop`/`restart` 进程探测不完整。
- 数值配置尚未执行范围校验。
- MCP 冒烟测试可能出现依赖库的 Pydantic forward-reference warning，目前不导致测试失败。

## 开发

```bash
.venv/bin/python -m pytest -q
```

审查时的基线是在允许绑定本机临时回环端口的环境中 **118 tests passed**。在受限 sandbox
里，daemon/proxy socket 测试可能因 `PermissionError` 失败，但非网络测试仍可运行。

`mcp_plan.md` 和 `proxy_plan.md` 保留原始需求与历史内容。当前审查结果单独记录在
[`code_review.md`](code_review.md)，已完成的 Auto classifier 兼容修改、实测证据与风险
边界记录在 [`classifier_compatibility.md`](classifier_compatibility.md)。

## 故障排查

- **所有 provider 都失败**：运行 `lm-visual-mcp doctor`，禁用不可用 provider，并检查
  CLI 登录状态或 Gemini key。
- **AGY 意外 fallback**：运行 `lm-visual-mcp doctor --probe`；AGY headless 图片访问可能
  间歇性失败。
- **Codex 阻塞或不能写文件**：服务端会关闭 stdin，并且刻意使用只读 sandbox。
- **MCP stdout 被污染**：确认 wrapper/provider CLI 没有向 MCP 进程 stdout 打印；项目日志
  使用 stderr。
- **Proxy route 返回 400/404**：确认显式协议路径正确，并使用无 padding base64url，不要用
  可能包含 `/` 的标准 base64。
- **Proxy 无法访问上游**：确认解码出的 base URL 完整，且不依赖 query string。
- **Auto Mode 显示 classifier unavailable**：确认请求命中了 Anthropic proxy，检查上游是否
  拒绝长 system prompt、cache-control 或 thinking 字段；不要只依据 UI 文案判断为网络故障。
  若上游拒绝 disabled thinking，设置
  `LM_VISUAL_MCP_PROXY_CLASSIFIER_DISABLE_THINKING=false`，响应规范化仍会保留。

## 许可

MIT，见 [`LICENSE`](LICENSE)。
