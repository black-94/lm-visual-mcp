# 开发任务：实现完整可运行的 Python Vision MCP Server

请直接在当前工作目录开发一个完整、可安装、可测试、可实际运行的 Python Vision MCP Server。

不要只输出设计方案或代码片段。必须实际创建项目、实现功能、运行测试，并修复到可用状态。

---

# 一、项目目标

开发一个用于给 **纯文本 LLM / Coding Agent 外挂视觉能力** 的 Vision MCP Server。

整体架构：

```text
Text-only LLM
      │
      │ MCP
      ▼
Vision MCP Server
      │
      ├── Z.AI-compatible Vision Tools
      ├── Specialized Prompt Layer
      ├── Media / Workspace Layer
      ├── Structured JSON Layer
      │
      └── Provider Router
              │
              ├── 1. AGY CLI
              ├── 2. Codex CLI
              ├── 3. Gemini API
              └── 4. OpenCode CLI
```

核心要求：

1. 使用 Python 实现。
2. 完整兼容 Z.AI `@z_ai/mcp-server` 当前 Vision MCP 工具接口。
3. MCP Tool 本身不感知底层 Provider。
4. Provider fallback 属于 Server 内部策略。
5. Provider、模型、API Key、fallback 顺序等全部通过 Server 配置管理。
6. 不允许让 LLM 在普通 MCP Tool 调用里指定 API Key、Provider、模型等基础设施参数。
7. 未来如果需要 ACP，新增 `XxxAcpProvider`即可。
9. 所有 Provider 最终输出统一结构化 JSON。
11. 支持本地路径和 HTTP/HTTPS URL。
12. 未指定工作目录时默认使用独立系统临时目录。

---

# 二、核心设计原则

## 2.1 Tool 是业务接口

例如：

```text
analyze_image
diagnose_error_screenshot
extract_text_from_screenshot
ui_diff_check
```

Tool 只描述：

> 要分析什么视觉内容。

Tool 不应该暴露：

```text
provider
model
provider_models
api_key
workdir
timeout
fallback
```

这些属于 Server 配置。

---

## 2.2 Provider Router 是 Server Policy

LLM 不应该决定：

```text
这次用 AGY
这次用 Codex
这次花 Gemini API
```

Router 根据配置自动决定。

默认：

```text
AGY
 ↓ unavailable
Codex
 ↓ unavailable
Gemini API
 ↓ unavailable
OpenCode
```

---

# 三、技术栈

使用：

```text
Python 3.11+
官方 Python MCP SDK
Pydantic
asyncio
pathlib
tempfile
shutil
logging
google-genai
pytest
pytest-asyncio
```

尽量减少依赖。

必须支持：

```text
macOS
Linux
Windows
```

Subprocess：

- 禁止 `shell=True`
- 使用参数数组
- 正确处理 Windows/macOS/Linux 路径
- 不拼接未经转义的用户输入

---

# 四、项目结构

推荐：

```text
vision-mcp/
├── pyproject.toml
├── README.md
├── LICENSE
├── .gitignore
├── config.example.yaml
│
├── src/
│   └── vision_mcp/
│       ├── __init__.py
│       ├── __main__.py
│       │
│       ├── server.py
│       ├── config.py
│       ├── models.py
│       ├── schema.py
│       ├── errors.py
│       ├── router.py
│       │
│       ├── providers/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── agy.py
│       │   ├── codex.py
│       │   ├── gemini.py
│       │   └── opencode.py
│       │
│       ├── services/
│       │   ├── workspace.py
│       │   ├── media.py
│       │   ├── subprocess_runner.py
│       │   └── json_output.py
│       │
│       ├── tools/
│       │   ├── ui_to_artifact.py
│       │   ├── extract_text.py
│       │   ├── diagnose_error.py
│       │   ├── technical_diagram.py
│       │   ├── data_visualization.py
│       │   ├── ui_diff.py
│       │   ├── analyze_image.py
│       │   └── analyze_video.py
│       │
│       └── prompts/
│           ├── ui_to_code.py
│           ├── ui_to_prompt.py
│           ├── ui_to_spec.py
│           ├── ui_description.py
│           ├── text_extraction.py
│           ├── error_diagnosis.py
│           ├── technical_diagram.py
│           ├── data_visualization.py
│           ├── ui_diff.py
│           ├── image_analysis.py
│           └── video_analysis.py
│
└── tests/
    ├── test_config.py
    ├── test_router.py
    ├── test_workspace.py
    ├── test_media.py
    ├── test_agy_provider.py
    ├── test_codex_provider.py
    ├── test_gemini_provider.py
    ├── test_opencode_provider.py
    ├── test_zai_tool_schemas.py
    └── test_mcp_smoke.py
```

不要为了架构形式增加没有实际用途的层。

---

# 五、Server 配置系统

必须实现独立配置文件，例如：

```text
vision-mcp.yaml
```

推荐完整结构：

```yaml
version: 1

providers:
  order:
    - agy
    - codex
    - gemini
    - opencode

  agy:
    enabled: true
    command: agy
    model: null

  codex:
    enabled: true
    command: codex
    model: null

  gemini:
    enabled: true
    model: null
    api_key_env: GEMINI_API_KEY

  opencode:
    enabled: true
    command: opencode
    model: null

runtime:
  workdir: null
  timeout: 120
  max_concurrency: 2

fallback:
  enabled: true

  on:
    - command_not_found
    - not_authenticated
    - permission denied
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
```

---

# 六、配置优先级

统一：

```text
CLI argument
    >
Environment variable
    >
Config file
    >
Built-in default
```

CLI 第一版只需要：

```text
vision-mcp --config <path>
vision-mcp --log-level DEBUG
vision-mcp doctor
vision-mcp --version
```

不要为所有 Provider 参数都增加 CLI flag。

---

# 七、环境变量

至少支持：

```text
VISION_MCP_CONFIG

VISION_MCP_WORKDIR
VISION_MCP_TIMEOUT
VISION_MCP_MAX_CONCURRENCY

VISION_MCP_AGY_COMMAND
VISION_MCP_AGY_MODEL

VISION_MCP_CODEX_COMMAND
VISION_MCP_CODEX_MODEL

VISION_MCP_GEMINI_MODEL
VISION_MCP_GEMINI_API_KEY

GEMINI_API_KEY

VISION_MCP_OPENCODE_COMMAND
VISION_MCP_OPENCODE_MODEL

VISION_MCP_LOG_LEVEL
```

---

# 八、Secret / API Key 设计

API Key 绝对不能作为普通 MCP Tool 参数。

Gemini Key 获取顺序：

```text
VISION_MCP_GEMINI_API_KEY
    >
config.providers.gemini.api_key_env 指向的环境变量
    >
GEMINI_API_KEY
```

允许配置文件包含：

```yaml
gemini:
  api_key_env: GEMINI_API_KEY
```

不要推荐：

```yaml
api_key: actual-secret
```

如为了兼容确实支持配置文件明文 key：

- 使用 `SecretStr`
- 永远不打印
- 永远不 dump
- 永远不进入 MCP response
- 永远不出现在异常信息
- README 明确推荐环境变量

---

# 九、Provider 配置

Provider 的以下属性全部属于 Server 配置：

```text
enabled
command
model
API key reference
provider order
timeout
workdir
```

例如：

```yaml
providers:
  order:
    - agy
    - codex
    - gemini
    - opencode

  agy:
    enabled: true
    command: /usr/local/bin/agy
    model: gemini-xxx

  codex:
    enabled: true
    command: codex
    model: gpt-xxx

  gemini:
    enabled: false
    model: gemini-xxx
    api_key_env: GEMINI_API_KEY

  opencode:
    enabled: true
    command: opencode
    model: google/gemini-xxx
```

---

# 十、Provider Router

定义：

```python
DEFAULT_PROVIDER_ORDER = (
    "agy",
    "codex",
    "gemini",
    "opencode",
)
```

Router：

```python
for provider_name in configured_order:
    provider = providers[provider_name]

    if not provider.enabled:
        continue

    status = await provider.probe(...)

    if not status.available:
        record_fallback(...)
        continue

    try:
        return await provider.analyze(request)
    except FallbackEligibleError:
        record_fallback(...)
        continue
```

---

# 十一、Provider 状态

定义类似：

```python
class ProviderFailureReason(StrEnum):
    COMMAND_NOT_FOUND = "command_not_found"
    NOT_AUTHENTICATED = "not_authenticated"
    API_KEY_MISSING = "api_key_missing"
    QUOTA_EXHAUSTED = "quota_exhausted"
    UNSUPPORTED_MEDIA = "unsupported_media"
    TIMEOUT = "timeout"
    TEMPORARY_FAILURE = "temporary_failure"

    INVALID_INPUT = "invalid_input"
    INVALID_MODEL = "invalid_model"
    CONFIG_ERROR = "config_error"
```

默认允许 fallback：

```text
COMMAND_NOT_FOUND
NOT_AUTHENTICATED
API_KEY_MISSING
QUOTA_EXHAUSTED
UNSUPPORTED_MEDIA
TIMEOUT
TEMPORARY_FAILURE
```

默认禁止 fallback：

```text
INVALID_INPUT
INVALID_MODEL
CONFIG_ERROR
```

fallback 条件以：

```yaml
fallback:
  on:
```

配置为最终准则。

---

# 十二、Provider 接口

定义真正 provider-neutral 的模型。

例如：

```python
@dataclass
class VisionRequest:
    system_prompt: str
    user_prompt: str

    images: list["ImageInput"] = field(default_factory=list)
    videos: list["VideoInput"] = field(default_factory=list)

    output_schema: dict | None = None
    workdir: Path | None = None
    timeout: float | None = None


@dataclass
class ImageInput:
    source: str
    local_path: Path | None = None
    url: str | None = None
    mime_type: str | None = None


@dataclass
class VideoInput:
    source: str
    local_path: Path | None = None
    url: str | None = None
    mime_type: str | None = None
```

Provider：

```python
class VisionProvider(Protocol):

    name: str

    async def probe(
        self,
        request: VisionRequest | None = None,
    ) -> ProviderStatus:
        ...

    async def analyze(
        self,
        request: VisionRequest,
    ) -> ProviderResult:
        ...
```

不要使用 OpenAI：

```text
role
content
image_url
```

等厂商绑定协议作为内部统一协议。

---

# 十三、模型选择

模型完全由 Provider 配置决定。

例如：

```yaml
agy:
  model: gemini-A

codex:
  model: gpt-B

gemini:
  model: gemini-C

opencode:
  model: google/gemini-D
```

fallback 时自动使用当前 Provider 自己的 model。

不要设计：

```text
tool.model
tool.provider_models
```

这种参数。

这样可以避免不同 Provider 的模型命名空间冲突。

---

# 十四、Workspace

默认：

```yaml
runtime:
  workdir: null
```

含义：

> 每次视觉任务创建独立临时工作目录。

例如：

```text
/tmp/vision-mcp-f320a1/
├── input/
│   ├── image-0.png
│   └── image-1.png
├── schema.json
└── output/
```

任务完成自动 cleanup。

如果：

```yaml
runtime:
  workdir: /some/project
```

则本地 Agent cwd 使用该目录。

MCP 自己生成的媒体临时目录应放：

```text
<workdir>/.vision-mcp/<uuid>/
```

并在任务结束清理。

永远不能删除用户原文件。

---

# 十五、媒体处理

支持：

```text
本地路径
HTTP URL
HTTPS URL
```

不要允许任意：

```text
file://
```

绕过路径验证。

远程媒体：

- timeout
- max response size
- MIME validation
- redirect limit
- 清晰错误处理

图片至少支持：

```text
png
jpg
jpeg
webp
gif
bmp
tiff
```

视频至少：

```text
mp4
mov
m4v
```

Z.AI 视频兼容默认限制：

```text
8 MB
```

图片限制配置：

```yaml
media:
  max_image_mb: 20
```

---

# 十六、Provider 1：AGY

最高优先级。

检测：

```python
shutil.which(config.command)
```

默认：

```text
agy
```

使用 headless：

```bash
agy -p "<prompt>" \
    --output-format json
```

如果需要 structured JSON：

```text
--json-schema
```

按照当前 AGY 实际 CLI 参数格式正确实现。

模型：

```text
--model <configured-model>
```

如果配置 model 为 null：

> 不传 model 参数，让 AGY 使用自身默认模型。

## AGY 图片输入

不要假设 AGY headless 存在 `--image`。

优先实现：

```text
图片复制/链接进入 workspace
       ↓
prompt 明确提供相对路径
       ↓
要求 AGY 实际读取图片
```

例如：

```text
Inspect the supplied image file:

./input/image-0.png

You MUST actually inspect the image.
Do not infer its contents from its filename.

...
```

实现 capability detection。

如果 AGY 当前版本无法通过 headless 读取图片：

```text
UNSUPPORTED_MEDIA
```

然后自动 fallback 到 Codex。

解析 AGY：

优先：

```text
structured_output
```

其次：

```text
response
```

usage 如果可获得则记录。

---

# 十七、Provider 2：Codex

默认：

```text
codex
```

使用：

```bash
codex exec
```

图片：

```text
-i
--image
```

多图片逐个传递。

模型：

```text
-m <configured model>
```

工作目录：

```text
-C <workdir>
```

临时目录不是 Git repo 时：

```text
--skip-git-repo-check
```

使用：

```text
--output-schema
```

要求结构化结果。

Codex 只执行视觉分析。

必须尽可能使用 read-only sandbox。

不要使用：

```text
--yolo
dangerous bypass
```

---

# 十八、Provider 3：Gemini API

使用官方：

```text
google-genai
```

不要自己手写 HTTP API，除非 SDK 无法满足需求。

available 条件：

```text
enabled = true
AND
API key exists
```

支持：

- image
- multi-image
- structured JSON
- JSON Schema
- configured model

API Key 不得泄露。

---

# 十九、Provider 4：OpenCode

默认 command：

```text
opencode
```

使用：

```bash
opencode run
```

图片：

```text
--file
```

多图片重复。

模型：

```text
--model <configured model>
```

工作目录：

```text
--dir <workdir>
```

使用：

```text
--format json
```

解析 JSON event stream。

提取最终 assistant result。

如输出 JSON 不严格：

允许有限的一次：

```text
extract / repair
```

禁止无限 retry。


---

# 二十、完整兼容 Z.AI Vision MCP Tool 接口

必须实现以下 8 个主要 Tool。

Tool Schema 不应该添加：

```text
provider
model
api_key
workdir
timeout
```

必须保持 Z.AI 业务接口清晰。

---

## Tool 1：`ui_to_artifact`

参数：

```json
{
  "image_source": "string",
  "output_type": "code | prompt | spec | description",
  "prompt": "string"
}
```

三个字段 required。

根据：

```text
code
prompt
spec
description
```

选择不同 specialized prompt。

---

## Tool 2：`extract_text_from_screenshot`

参数：

```json
{
  "image_source": "string",
  "prompt": "string",
  "programming_language": "string | optional"
}
```

required：

```text
image_source
prompt
```

用途：

- OCR
- source code
- terminal
- config
- documentation
- general text

尽可能逐字提取。

不得无依据纠正原图内容。

---

## Tool 3：`diagnose_error_screenshot`

参数：

```json
{
  "image_source": "string",
  "prompt": "string",
  "context": "string | optional"
}
```

required：

```text
image_source
prompt
```

分析：

- error
- stack trace
- file
- line
- root cause
- suggested fix
- uncertainty

---

## Tool 4：`understand_technical_diagram`

参数：

```json
{
  "image_source": "string",
  "prompt": "string",
  "diagram_type": "string | optional"
}
```

例如：

```text
architecture
flowchart
uml
er diagram
sequence diagram
system diagram
```

---

## Tool 5：`analyze_data_visualization`

参数：

```json
{
  "image_source": "string",
  "prompt": "string",
  "analysis_focus": "string | optional"
}
```

例如：

```text
trends
anomalies
comparisons
performance
distribution
```

---

## Tool 6：`ui_diff_check`

参数：

```json
{
  "expected_image_source": "string",
  "actual_image_source": "string",
  "prompt": "string"
}
```

全部 required。

必须保证：

```text
Image 1 = EXPECTED / REFERENCE
Image 2 = ACTUAL / CURRENT
```

不得交换。

比较：

- missing elements
- layout
- typography
- spacing
- alignment
- size
- color
- styling
- visual regression

---

## Tool 7：`analyze_image`

参数：

```json
{
  "image_source": "string",
  "prompt": "string"
}
```

通用视觉分析。

为了兼容文档命名，再注册 alias：

```text
image_analysis
```

两者调用同一实现。

---

## Tool 8：`analyze_video`

参数：

```json
{
  "video_source": "string",
  "prompt": "string"
}
```

alias：

```text
video_analysis
```

Provider 不支持 video 时：

```text
UNSUPPORTED_MEDIA
```

然后 fallback。

---

# 二十一、Prompt Layer

不要所有工具共用一个 generic prompt。

为每个工具建立 specialized system prompt。

参考 Z.AI Vision MCP 的行为与公开实现/复刻项目，包括但不限于：

```text
@z_ai/mcp-server
vlm-mcp-server
其它公开兼容实现
```

重点保留其：

```text
tool taxonomy
specialized prompts
output expectations
```

但不要机械复制内部 HTTP Provider 架构。

Prompt 结构建议：

```text
Tool-specific system prompt
        +
User-supplied prompt
        +
Media mapping
        +
JSON output rules
```

Provider 不需要知道当前是哪个 MCP Tool。

Provider 只接收最终 VisionRequest。

---

# 二十二、统一结构化输出

所有视觉 Provider 最终统一为类似：

```json
{
  "summary": "Short visual summary",

  "answer": "Direct answer to the requested task",

  "observations": [
    {
      "type": "text|object|ui|error|diagram|data|other",
      "text": "observation",
      "confidence": 0.95
    }
  ],

  "texts": [
    {
      "text": "visible text",
      "bbox": [100, 100, 900, 200],
      "confidence": 0.98
    }
  ],

  "elements": [
    {
      "label": "Build button",
      "type": "ui_element",
      "bbox": [700, 20, 820, 70],
      "confidence": 0.93
    }
  ],

  "warnings": []
}
```

bbox：

```text
normalized 0..1000
```

格式：

```text
[x_min, y_min, x_max, y_max]
```

无法确定时：

- 不猜
- 留空
- 降低 confidence
- warnings 说明

专项 Tool 可以增加：

```text
details
```

字段。

---

# 二十三、MCP 最终 Response Envelope

返回：

```json
{
  "provider": "agy",
  "model": "configured-model",

  "result": {
    "summary": "...",
    "answer": "...",
    "observations": [],
    "texts": [],
    "elements": [],
    "warnings": []
  },

  "meta": {
    "duration_ms": 4812,

    "fallbacks": [],

    "usage": {
      "input_tokens": null,
      "output_tokens": null,
      "thinking_tokens": null,
      "cache_read_tokens": null,
      "total_tokens": null
    }
  }
}
```

fallback 示例：

```json
{
  "provider": "codex",

  "meta": {
    "fallbacks": [
      {
        "provider": "agy",
        "reason": "command_not_found",
        "message": "agy executable not found"
      }
    ]
  }
}
```

不得返回：

```text
API key
token
cookie
credential
完整敏感 stderr
```

---

# 二十四、为什么 Provider 信息可以出现在 Response

Tool 不允许 LLM 控制 Provider。

但 Response 可以提供：

```text
provider
model
fallbacks
usage
```

用于：

- debugging
- quota tracking
- operational visibility

这是只读 metadata。

---

# 二十五、MCP Server 启动

必须支持：

```bash
python -m vision_mcp
```

以及 console script：

```bash
vision-mcp
```

默认 stdio。

例如：

```json
{
  "mcpServers": {
    "vision": {
      "command": "vision-mcp",
      "args": [
        "--config",
        "/Users/me/.config/vision-mcp/config.yaml"
      ],
      "env": {
        "GEMINI_API_KEY": "..."
      }
    }
  }
}
```

stdout 专用于 MCP protocol。

---

# 二十六、日志

所有日志必须进入：

```text
stderr
```

严禁普通：

```python
print(...)
```

污染 MCP stdio stdout。

使用：

```python
logging.StreamHandler(sys.stderr)
```

支持：

```text
ERROR
WARNING
INFO
DEBUG
```

API key 必须 redact。

Prompt DEBUG logging：

- 默认不要记录完整 prompt
- 可记录长度/摘要
- 不记录 secret

---

# 二十七、Doctor

实现：

```bash
vision-mcp doctor
```

示例：

```text
Vision MCP

Configuration:
  /Users/me/.config/vision-mcp/config.yaml

Provider order:
  agy -> codex -> gemini -> opencode

AGY
  enabled: yes
  executable: /usr/local/bin/agy
  model: default
  vision capability: unknown

Codex
  enabled: yes
  executable: /usr/local/bin/codex
  model: default

Gemini
  enabled: yes
  API key: configured
  model: gemini-xxx

OpenCode
  enabled: yes
  executable: not found

Runtime
  workdir: temporary
  timeout: 120
```

绝不能显示 API Key 内容。

---

# 二十八、AGY Vision Smoke Test

如果本机有：

```text
agy
```

doctor 可提供更深的：

```bash
vision-mcp doctor --probe
```

生成测试图片：

```text
白底
黑字：

VISION_TEST_7391
```

然后实际让 AgyProvider：

```text
Read the exact text shown in the supplied image.
```

预期：

```json
{
  "answer": "VISION_TEST_7391"
}
```

如果成功：

```text
vision capability: available
```

失败：

```text
vision capability: unsupported
```

不能因此导致 MCP Server 启动失败。

---

# 二十九、测试

必须完整测试。

## Config

测试：

```text
defaults
yaml loading
environment override
CLI override
invalid provider
duplicate provider
disabled provider
API secret redaction
```

## Router

测试：

```text
AGY success
AGY missing → Codex
AGY failure → Codex
Codex missing → Gemini
Gemini key missing → OpenCode
disabled provider ignored
configured order respected
fallback disabled
non-fallback error stops
all unavailable → clear error
```

## CLI Provider

全部 mock subprocess。

检查：

```text
command
args
cwd
model
media
schema
timeout
return code
stderr
JSON parse
malformed output
quota error
auth error
```

## Gemini Provider

mock google-genai。

检查：

```text
API key resolution
model
image
multiple images
schema
structured response
errors
```

## Z.AI Compatibility

必须建立：

```text
test_zai_tool_schemas.py
```

检查：

```text
ui_to_artifact
extract_text_from_screenshot
diagnose_error_screenshot
understand_technical_diagram
analyze_data_visualization
ui_diff_check
analyze_image
analyze_video
```

以及：

```text
image_analysis
video_analysis
```

特别确认：

> Tool Schema 不包含 provider/model/api_key/workdir/timeout 等 Server 配置字段。

---

# 三十、MCP Smoke Test

必须实际验证：

```text
tools/list
```

能够列出所有工具。

至少：

```text
ui_to_artifact
extract_text_from_screenshot
diagnose_error_screenshot
understand_technical_diagram
analyze_data_visualization
ui_diff_check
analyze_image
analyze_video
image_analysis
video_analysis
```

通过 mock Provider 完成至少一次：

```text
tools/call
```

端到端测试。

---

# 三十一、安全

Vision MCP 的职责：

```text
LOOK
READ
UNDERSTAND
COMPARE
ANALYZE
```

不是：

```text
EDIT
BUILD
EXECUTE
MODIFY
```

本地 Agent system prompt 明确加入：

```text
You are acting only as a visual analysis provider.

Do not modify files.
Do not edit the workspace.
Do not execute unrelated commands.
Do not perform coding tasks.

Only inspect the supplied visual media and return
the requested structured result.
```

Codex 尽可能 read-only。

AGY/OpenCode 不开启危险自动授权。

---

# 三十二、README

README 至少覆盖：

1. 项目是什么
2. 为什么纯文本模型需要 Vision MCP
3. 安装
4. Python requirements
5. 启动
6. MCP Client 配置
7. config 文件
8. Provider order
9. enabled
10. Provider model
11. API key
12. environment variables
13. fallback
14. workdir
15. timeout
16. media limits
17. 8 个 Z.AI Tools
18. aliases
19. JSON output
20. doctor
21. provider detection
22. AGY limitation
23. tests
24. security
25. troubleshooting

---

# 三十三、第一版不要实现的功能

明确不实现：

```text
ACP
profile system
让模型选择 Provider
让模型选择模型
Tool API Key
Tool workdir
Tool timeout
GUI
Web 管理界面
复杂持久化数据库
```

避免过度设计。

---

# 三十四、未来扩展必须容易

Provider 抽象需要保证未来可以简单增加：

```text
AcpProvider
ClaudeProvider
KimiProvider
QwenProvider
LocalVlmProvider
```

但不要现在实现。

一个新 Provider 理论上只需要：

```python
class NewProvider(VisionProvider):

    async def probe(...):
        ...

    async def analyze(...):
        ...
```

然后注册到 Provider registry。

---

# 三十五、开发执行顺序

请直接执行：

1. 检查当前工作目录。
2. 检查是否已有项目代码。
3. 创建 Python package。
4. 实现 config。
5. 实现 models / errors。
6. 实现 workspace/media service。
7. 实现 Provider interface。
8. 实现 AgyProvider。
9. 实现 CodexProvider。
10. 实现 GeminiProvider。
11. 实现 OpenCodeProvider。
12. 实现 Router。
13. 实现统一 JSON schema。
14. 实现 specialized prompts。
15. 实现 Z.AI 8 个 Tools。
16. 实现 aliases。
17. 实现 MCP stdio server。
18. 实现 doctor。
19. 写单元测试。
20. 写 MCP smoke test。
21. 执行 pytest。
22. 修复所有测试。
23. 执行 tools/list。
24. 如本机 Provider 可用，进行真实 smoke test。
25. 编写 README。
26. 最终重新运行完整测试。

不要在中间只汇报计划然后停止。

非关键细节自行作合理工程决策。

---

# 三十六、完成标准

以下必须成功：

```bash
pip install -e .
```

```bash
pytest
```

```bash
vision-mcp --version
```

```bash
vision-mcp doctor
```

```bash
python -m vision_mcp
```

MCP：

```text
tools/list
```

必须正常。

能够发现完整 Z.AI-compatible tools。

mock Provider 下：

```text
tools/call
```

必须正常完成。

---

# 三十七、最终汇报

全部完成后，再给出：

1. 项目结构
2. MCP Tools
3. Z.AI compatibility 情况
4. 配置结构
5. Provider 顺序
6. fallback 行为
7. Provider model 配置
8. Gemini API Key 配置
9. workdir 行为
10. doctor 结果
11. tests 结果
12. tools/list 结果
13. 当前机器检测到的 Provider
14. 真实 smoke test 结果
15. AGY 图片输入是否真正可用
16. 尚存限制
17. 安装命令
18. 启动命令
19. MCP Client 配置示例

最重要的要求：

> 不要只生成设计文档。请实际实现完整项目、运行测试并修复到可运行状态。