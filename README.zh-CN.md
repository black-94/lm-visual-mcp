# Vision MCP Server

> [English](README.md) | **简体中文**

`lm-visual-mcp` 通过 [Model Context Protocol](https://modelcontextprotocol.io)
为纯文本 LLM / 编程 Agent 提供视觉输入。项目由三个模块组成 —— `vision`、`server`、
`mcp` —— 并内置透明 HTTP 代理：在转发上游前，把 OpenAI / Anthropic 请求中的图片块
改写为文字描述；同时提供 Claude Code Auto classifier 兼容处理。

本版本（**v0.2.0**）**仅支持图片识别**，整个链路不再声明、也不再接受视频输入。

## 模块划分

| 模块 | 职责 |
| --- | --- |
| `mcp` | 薄 stdio MCP 入口。所有工具调用都转发到共享 server —— 不内嵌 vision 服务，限流因此始终集中可控。 |
| `server` | 共享单例进程：`POST /vision/analyze` + hook 代理（`/proxy/<proto>/<base64url>...`）。启用哪些 hook 完全由配置决定（`server.image_hook.enabled`、`server.classifier_hook.enabled`）。 |
| `vision` | 图片识别能力：类型注册表背后的 provider 链，每个 provider 自带限流（rpm / 并发），按序链式降级。 |

### Hook

hook 的基本接口是 `process(ctx) -> HookResult`：可以改写请求并让请求**继续往下传递**，
也可以**中断**——直接返回响应给客户端。hook 还可实现 `process_response` 改写上游响应
（classifier hook 使用）。

- **图片 hook** —— 识别携带图片的请求，经 vision 链对每张图做一次描述（SHA-256
  缓存），把图片块替换为文本。每个改写块都记录该图片的**绝对本地路径**
  （`[Image N: /abs/path.png]`），且落盘文件持久保留，文本模型之后可以直接引用或
  重新提交该图片。
- **classifier hook** —— 对 Claude Code Auto classifier 请求禁用 thinking，并对
  stage-1 verdict 响应恢复 stop-sequence 语义。

### Vision provider 与降级

provider 以 `{name, type, ...}` 列表配置，列表顺序即降级顺序。router 不耦合任何
具体 provider —— `type` 经注册表解析，新增 provider 只需实现类 + 注册一行 + 配置引用。

限流配置在**每个 provider 内部**（`rate_limit: {rpm, concurrency}`，两者均可选）。
达到限流时 provider 抛出 `rate_limited`，router 立即降级到链中的下一个 provider。

- `agy` —— AGY CLI（`-p` + `--add-dir` + sandbox），"不支持视觉" 结论带 TTL 缓存。
- `codex` —— `codex exec` + `--output-schema`，只读沙箱。
- `gemini` —— google-genai API（`api_key_env: GEMINI_API_KEY`）。
- `opencode` —— 直连 OpenAI 兼容 API（默认 `https://opencode.ai/zen/v1`，
  `api_key_env: OPENCODE_API_KEY`）；**不依赖本地 CLI**。

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
    │                      └── 链：provider₁ -> provider₂ -> …
    │                           （各自带 rpm/并发限流；达到即降级下一个）
    └── hook 代理  /proxy/<proto>/<base64url>[/suffix]
           ├── hooks：图片改写 / classifier 兼容（各自可开关）
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
`~/.config/lm-visual-mcp/`）配置 provider、限流、hook 与监听地址。配置文件中没有
`mcp:` 段 —— 是否拉起 server 由 agent 的 MCP 配置决定，不属于 YAML。

## 工具

`ui_to_artifact`、`extract_text_from_screenshot`、`diagnose_error_screenshot`、
`understand_technical_diagram`、`analyze_data_visualization`、`ui_diff_check`、
`analyze_image`（+ `image_analysis` 别名）。provider、模型、凭据、降级策略、超时
均为 server 配置，绝不出现在工具 schema 中。
