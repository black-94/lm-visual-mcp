# Vision MCP Server

> [English](README.md) | **简体中文**

`lm-visual-mcp` 通过 [Model Context Protocol](https://modelcontextprotocol.io)
为纯文本 LLM / 编程 Agent 提供视觉输入。项目由四个模块组成 —— `mcp`、`server`、
`providers`、`vision` —— 并内置透明 HTTP 代理：在转发上游前，把 OpenAI / Anthropic
请求中的图片块改写为文字描述；同时提供 Claude Code Auto classifier 兼容处理。

本版本（**v0.2.0**）**仅支持图片识别**，整个链路不再声明、也不再接受视频输入。

## 模块划分

| 模块 | 职责 |
| --- | --- |
| `mcp` | 薄 stdio MCP 入口。所有工具调用都转发到共享 server —— 不内嵌 vision 服务，限流因此始终集中可控。 |
| `server` | 共享单例进程：`POST /vision/analyze` + hook 代理（`/proxy/<proto>/<base64url>...`）。启用哪些 hook 完全由配置决定（`hooks.image.enabled`、`hooks.classifier.enabled`）。 |
| `providers` | provider 实现，背后是类型注册表，每个 provider 自带限流（rpm / 并发）。每个 provider 实现一个或两个行为组：**IMAGE**（`probe_image` / `analyze_image`）和 **CLASSIFIER**（`rewrite_classifier_request` / `rewrite_classifier_response`）。 |
| `vision` | 图片识别编排：并发闸 + 驱动 `image_chain` 的 provider router。prompt 与两个行为组的共享类型在这里 / providers 旁。 |

### Hook

hook 的基本接口是 `process(ctx) -> HookResult`：可以改写请求并让请求**继续往下传递**，
也可以**中断**——直接返回响应给客户端。hook 还可实现 `process_response` 改写上游响应
（classifier hook 使用）。

- **图片 hook** —— 识别携带图片的请求，经 image 链对每张图做一次描述（SHA-256
  缓存），把图片块替换为文本。每个改写块都记录该图片的**绝对本地路径**
  （`[Image N: /abs/path.png]`），且落盘文件持久保留，文本模型之后可以直接引用或
  重新提交该图片。
- **classifier hook** —— 识别 Claude Code Auto classifier 请求并委托给 classifier
  链。只有实现了 classifier 处理（`rewrite_classifier_request` /
  `rewrite_classifier_response`）的 API 型 provider 才会改写；本地 CLI provider
  （agy、codex）原样透传、不做字节级改动。

两个 hook 都支持 `models` 白名单 —— 空 = 应用于所有 model；非空 = 仅列表内 model 走
router，其余全部原样透传。

### Provider、双链与降级

**顶层 `providers:`** 定义 provider *实例*（唯一出处，按 `name` 引用）。`vision` 在
**两条相互独立的执行链**里引用这些 name：

- `image_chain` —— 图片分析降级顺序（**第一个成功即回**）。
- `classifier_chain` —— classifier 处理顺序（**第一个返回 changed 改写的生效**；若
  无任何 provider 实现 classifier 处理，请求原样透传）。

router 不耦合任何具体 provider —— `type` 经注册表解析，新增 provider 只需实现类 +
注册一行 + 配置引用。

限流配置在**每个 provider 内部**（`rate_limit: {rpm, concurrency}`，两者均可选）。
达到限流时 provider 抛出 `rate_limited`，router 立即降级到链中的下一个 provider。

- `agy` —— AGY CLI（`-p` + `--add-dir` + sandbox），"不支持视觉" 结论带 TTL 缓存。
  仅 IMAGE；无 classifier 处理。
- `codex` —— `codex exec` + `--output-schema`，只读沙箱。仅 IMAGE。
- `gemini` —— google-genai API（`api_key_env: GEMINI_API_KEY`）。IMAGE + classifier
  （读取 `disable_thinking`）。
- `opencode` —— 直连 OpenAI 兼容 API；`mode: go`（默认，
  `https://opencode.ai/zen/go/v1`）或 `mode: zen`，`base_url` 可覆盖 mode。IMAGE +
  classifier；**不依赖本地 CLI**。
- `volcengine` —— 火山方舟；`mode: agent`（Anthropic Messages `/v1/messages`，走
  `api/plan`）、`mode: coding`（`api/coding`）或 `mode: api`（OpenAI
  chat-completions，`api/v3`）。IMAGE + classifier。

## 架构

```text
MCP client 进程（agent 配置：--start-server / --no-start-server）
    │ stdio
    ▼
mcp 模块（薄客户端）
    │ loopback HTTP  POST /vision/analyze
    ▼
server 模块（共享单例）
    ├── vision 端点 ──► vision 模块
    │                      ├── 并发闸
    │                      └── router 走 image_chain：provider₁ -> provider₂ -> …
    │                           （各自带 rpm/并发限流；达到即降级下一个）
    └── hook 代理  /proxy/<proto>/<base64url>[/suffix]
           ├── image hook      → image 链（描述改写，model 白名单）
           ├── classifier hook → classifier 链（API provider 改写，model 白名单 /
           │                     无 provider 处理时字节级透传）
           └── 无 hook 命中时字节级透传
```

## 快速开始

```bash
# agent 的 MCP 配置（stdio）—— 默认会拉起共享 server（若不存在）：
lm-visual-mcp

# 不由 MCP 进程拉起 server（使用已运行的实例）：
lm-visual-mcp --no-start-server      # 环境变量：LM_VISUAL_MCP_START_SERVER=0

lm-visual-mcp start | stop | restart  # 管理 server 单例
lm-visual-mcp server                  # 前台运行 server
lm-visual-mcp doctor                  # 检查配置与 provider
```

把 `config.example.yaml` 复制为 `lm-visual-mcp.yaml`（或放到
`~/.config/lm-visual-mcp/`）配置监听地址、hook、provider 与两条链。根节点为
`server` / `hooks` / `providers` / `vision` / `media` / `logging`。配置文件中没有
`mcp:` 段 —— 是否拉起 server 由 agent 的 MCP 配置决定，不属于 YAML。

示例（顶层 `providers` 定义实例；`vision` 声明两条链）：

```yaml
server:
  host: 127.0.0.1
  port: 8787

hooks:
  image:      { enabled: true, models: [] }   # models 空 = 所有 model
  classifier: { enabled: true, models: [] }

providers:
  - name: agy
    type: agy
    command: agy
    model: gemini-3.6-flash
    effort: high
    rate_limit: { rpm: 30, concurrency: 2 }
  - name: gemini
    type: gemini
    api_key_env: GEMINI_API_KEY
    disable_thinking: true
  - name: opencode
    type: opencode
    mode: go                              # go | zen
    api_key_env: OPENCODE_API_KEY
  - name: volcengine
    type: volcengine
    mode: agent                           # agent | coding | api
    api_key_env: VOLCENGINE_API_KEY

vision:
  timeout: 120
  max_concurrency: 2
  image_chain: [agy, gemini, opencode]      # 第一个成功即回
  classifier_chain: [gemini]                # 只放 API 型 provider
```

本地 CLI provider（agy、codex）没有 classifier 处理；`classifier_chain` 只应放
API 型 provider（gemini / opencode / volcengine）。

## 工具

`ui_to_artifact`、`extract_text_from_screenshot`、`diagnose_error_screenshot`、
`understand_technical_diagram`、`analyze_data_visualization`、`ui_diff_check`、
`analyze_image`（+ `image_analysis` 别名）。provider、模型、凭据、降级策略、超时
均为 server 配置，绝不出现在工具 schema 中。
