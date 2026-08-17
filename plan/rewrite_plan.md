# 项目重写：mcp / server / vision 三模块架构

参考现有代码（`src/lm_visual_mcp/`），重写整个项目。Python 3.11+，依赖不变（mcp、pydantic、PyYAML、google-genai、aiohttp）。

## 目标结构

```
src/lm_visual_mcp/
  __init__.py / __main__.py
  cli.py                 # 入口：默认 MCP stdio、server、doctor、start/stop/restart
  config.py              # 统一配置（version 2），两段式：server / vision（无 mcp 段）
  errors.py              # 精简错误层级
  media.py               # MediaService（仅图片）+ Workspace
  mcp/
    __init__.py
    server.py            # FastMCP 工具注册（仅图片工具，删 video 工具/别名）
    client.py            # 薄客户端：探测/拉起单例 server，工具调用经 HTTP 转发到 server 的 vision service
  server/
    __init__.py
    app.py               # aiohttp 应用：/health、/vision/analyze、/proxy/<proto>/<b64> 转发、单例绑定、run()
    hooks.py             # Hook 协议（process / process_response）+ HookContext/HookResult + 管线
    image_hook.py        # 图片请求 hook：提取→describe→改写；改写文本带图片绝对路径
    classifier_hook.py   # classifier hook：禁 thinking + 响应 verdict 规整
    protocols/           # anthropic / openai_chat / openai_responses 适配器（自 proxy/ 迁移）
  vision/
    __init__.py
    types.py             # ImageInput、ImageRequest、ProviderResult/Status/Usage（仅图片）
    router.py            # 链式降级路由器，只面向 VisionProvider 接口
    providers/
      __init__.py        # PROVIDER_TYPES 注册表，按配置 type 构建；限流器在各 provider 内部
      base.py            # VisionProvider Protocol + RateLimitedMixin/基类（含 rpm+并发限流）
      ratelimit.py       # RateLimiter：rpm 滑动窗口 + 并发信号量，非阻塞 try_acquire
      agy.py / codex.py / opencode_api.py / gemini.py
    prompts/             # 仅图片相关 prompt（删 video_analysis）
    service.py           # VisionService：并发闸 + 构建请求 + 调 router + 封装 envelope
```

**删除**：`services/`、`proxy/`、`tools/`、旧顶层 `server.py`、`router.py`、`models.py`、`schema.py`、`providers/opencode.py`（CLI 版）。

## 关键设计

### 1. 配置与 mcp 参数（反馈 a）

- 配置文件无 `mcp:` 段。mcp 层只有"是否拉起 server"一个开关，由 **agent 的 MCP 配置**（stdio 启动参数）传入：
  - `lm-visual-mcp --start-server`（默认）/ `lm-visual-mcp --no-start-server`
  - 对应 env：`LM_VISUAL_MCP_START_SERVER=0/1`
- host/port 只在 `server:` 段配置，mcp 客户端从同一配置文件读取连接地址。

```yaml
version: 2
server:
  host: 127.0.0.1
  port: 8787
  image_hook:
    enabled: true
  classifier_hook:
    enabled: true
    disable_thinking: true
vision:
  timeout: 120
  max_concurrency: 2
  fallback:
    enabled: true
    on: [command_not_found, not_authenticated, permission_denied, api_key_missing,
         quota_exhausted, rate_limited, unsupported_media, timeout, temporary_failure]
  providers:                 # 列表顺序 = 降级链顺序；name+type 解耦
    - name: agy
      type: agy
      enabled: true
      command: agy
      model: gemini-3.6-flash
      effort: high
      vision_cache_ttl: 300
      rate_limit: {rpm: 30, concurrency: 2}
    - name: gemini
      type: gemini
      api_key_env: GEMINI_API_KEY
      rate_limit: {rpm: 60}
    - name: opencode
      type: opencode
      api_key_env: OPENCODE_API_KEY
      base_url: https://opencode.ai/zen/v1   # OpenAI 兼容端点（默认值可按实际调整）
      model: ...
      rate_limit: {rpm: 30}
media: {max_image_mb: 20, download_timeout: 30, max_download_mb: 32}
logging: {level: INFO}
```

### 2. 单例 server 是唯一的 vision 入口（反馈 c）

server 进程（aiohttp）同时承载：
- `GET /health`
- `POST /vision/analyze`：暴露 vision.VisionService（router + 全局限流都在这里）
- `ALL /proxy/<proto>/<b64url>[/suffix]`：hook 代理转发

mcp 模块是**薄客户端**，绝不内嵌 VisionService：
- 启动时按 `--start-server` 决定是否探测+拉起单例 server（沿用 probe→bind→detached spawn、pidfile、端口冲突静默退出模式）；
- 所有工具调用转发到 `POST /vision/analyze`；
- `--no-start-server` 且探测不到 server 时，工具调用返回带提示的错误（提示手动 `lm-visual-mcp server`），保证限流始终集中可控。
- 单例生命周期沿用 `server` 子命令 + `start/stop/restart`。

### 3. Hook 接口（server 模块）

```python
@dataclass
class HookContext:
    method: str
    url: str            # 解码后的上游 URL
    headers: dict       # 可变副本
    body: bytes         # 可变

@dataclass
class HookResult:
    action: Literal["continue", "intercept"]
    body: bytes | None = None                        # continue 时改写后的请求体
    response: tuple[int, dict, bytes] | None = None  # intercept 时直接回客户端

class Hook(Protocol):
    name: str
    async def process(self, ctx: HookContext) -> HookResult: ...
    async def process_response(self, ctx, status, headers, body)
        -> tuple[int, dict, bytes] | None: ...  # 默认不改；classifier 用于响应规整
```

管线按配置顺序执行启用的 hook；`intercept` 中断并回响应，`continue`（可带改写 body）传给下一个 hook，最后转发上游。转发/SSE 透传/hop-by-hop 头/rotating log 照搬现 `proxy/server.py`。

**image_hook（反馈 e）**：图片块改写为文本时，描述文本头部记录该图片落盘后的**绝对路径**，例如：

```
[image file: /Users/x/.cache/lm-visual-mcp/proxy-media/3f2a….png]
<图片描述…>
```

staged 文件按内容 hash 命名持久保留（不再随请求/关服清理），后续文本模型可直接引用该路径再调用 MCP 工具。

### 4. vision 模块

- 仅图片：`ImageRequest{system_prompt, user_prompt, images, output_schema, workdir, timeout}`。
- **Router 不耦合具体 provider**：只依赖 Protocol + 注册表（`PROVIDER_TYPES: {agy, codex, gemini, opencode}`）。新增 provider = 实现类 + 注册一行 + 配置引用。
- **限流在 provider 内部（反馈 b）**：每个 provider 的配置自带 `rate_limit: {rpm, concurrency}`，限流器随 provider 实例构造；`analyze()` 入口 `try_acquire()`，非阻塞——拿不到即抛 `ProviderUnavailableError(RATE_LIMITED)`（新增 reason，默认可降级），router 据此链式降级到下一个 provider。rpm（滑动窗口）与并发（信号量）任一达到即降级，不排队。
- 降级链沿用现 `ProviderRouter.route` 的 probe→analyze→reason 判定→fallback 记录逻辑。

### 5. opencode provider 重写为直连 API（反馈 d）

删除本地 CLI 调用，改为 OpenAI 兼容 HTTP provider（`opencode_api.py`，aiohttp）：
- 配置：`base_url`（默认 opencode zen 端点）、`api_key_env: OPENCODE_API_KEY`、`model`、`rate_limit`；
- `POST {base_url}/chat/completions`，图片以 `image_url` data URL 传入，system/user 消息 + JSON 输出要求；
- 错误分类：401/403→`NOT_AUTHENTICATED`/`PERMISSION_DENIED`，429→`QUOTA_EXHAUSTED`，5xx/超时→`TEMPORARY_FAILURE`；
- 不依赖本地 opencode CLI 安装。

### 6. MCP 工具

仅保留图片工具：ui_to_artifact / extract_text_from_screenshot / diagnose_error_screenshot / understand_technical_diagram / analyze_data_visualization / ui_diff_check / analyze_image（+ image_analysis 别名）。删除 analyze_video / video_analysis。

### 7. 测试与文档

- 重写/迁移 `tests/`：config、router 链式降级 + provider 限流降级（fake provider + fake clock）、hook 管线（intercept/continue/classifier 规整/图片路径记录）、opencode_api（aiohttp mock）、gemini/agy/codex、cli。
- 更新 `README.md` / `README.zh-CN.md` / `config.example.yaml`。

## 实施顺序

1. vision/（types、ratelimit、providers 迁移+opencode 重写、prompts 精简、router、service）
2. server/（hooks 协议与管线、image/classifier hook、protocols 迁移、app 含 /vision/analyze）
3. mcp/（client、server 工具）+ cli.py + config.py + media.py，删除旧文件
4. tests 重写迁移，pytest 全绿
5. README / config.example.yaml 更新
