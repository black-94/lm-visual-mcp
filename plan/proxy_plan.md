# Vision Proxy — 薄代理层

> 透明 API 转发器 + 图片预处理器。无图片时几乎等于 nginx；有图片时才介入。
> 复用现有 `lm_visual_mcp` 的 vision provider 栈，只新增一个 HTTP 入口 + 三个协议适配器。

## 0. 关键决定

- **只支持三种协议**：OpenAI Chat Completions、OpenAI Responses、Anthropic Messages。
- **describe 复用现有感知工具**：`prompts/image_analysis.SYSTEM_PROMPT`（Z.AI `analyze_image`）。
- **返回方向完全不碰**：含 SSE 一律原样 pipe。

## 1. URL 结构

```
/proxy/<协议路径>/<base64url(基础API URL)>[<SDK追加后缀>]
```

- 协议路径显式给定，决定用哪个改写适配器（不靠 URL 推断）。
- base64url 编码**基础 API URL**（域名，可含路径前缀），SDK 追加的 endpoint 路径
  由代理拼回去。

```text
origin:    https://api.deepseek.com                （基础，SDK 追加 /v1/chat/completions）
base64url: aHR0cHM6Ly9hcGkuZGVlcHNlZWsuY29t
最终转发:  https://api.deepseek.com/v1/chat/completions

base_url:  http://127.0.0.1:8787/proxy/openai/chat/aHR0cHM6...
                                  └─ 协议路径 ─┘
```

| 协议路径 | 解析目标 | 图片 part → 替换 |
|---|---|---|
| `/proxy/openai/chat` | `messages[].content[]` | `image_url` → `text` |
| `/proxy/openai/responses` | `input[].content[]` | `input_image` → `input_text` |
| `/proxy/anthropic` | `content[]` | `image` → `text` |

未知协议路径 → 404；base64url 解码失败 / 非 http(s) → 400。

**SDK 会把 endpoint 拼到 base_url 后面**：Claude Code 用 Anthropic SDK，`base_url` 配成
`/proxy/anthropic/<b64>` 后，请求实际到达 `/proxy/anthropic/<b64>/v1/messages`。解析器
协议路径按前缀匹配，然后在剩余段里找第一个能 base64url 解码成完整 http(s) URL 的段
作为**基础 URL**，并把其后段（如 `v1/messages`）**拼回解码出的 URL 上**再转发——最终
上游路径 = 基础 URL + SDK 追加后缀（如 `https://.../api/plan/v1/messages`）。raw curl
不带后缀时，只转发基础 URL。

## 2. 核心流程

```
handle(request):
    proto, b64    = split_path(request.path)      # /proxy/<proto>/<b64>
    adapter       = registry[proto]               # 显式
    target        = b64decode(b64)                # 完整 API URL
    body          = read_body(request)
    if not adapter.has_image(body):               # 字节扫描，不 parse
        return forward_raw(request, target, body) # 完全透明
    doc, slots    = adapter.extract(body, media)  # 复用 MediaService
    if not slots: return forward_raw(...)         # 误报保护：仍原样转发
    descs         = describe_with_cache(slots)    # 见 §3
    for s, d in zip(slots, descs): s.apply(d)     # 改写文本
    return forward(request, target, json.dumps(doc))
```

无图片路径的代价只是读一遍 body 再写回；只剥离 hop-by-hop headers，API key /
model / 其余 header / body 字节原封不动。

## 3. describe：感知器 + 逐图缓存 + 多图一次提交

- **缓存粒度 = 每张图**（key = 单图字节 SHA-256），**vision 调用粒度 = 每次请求**。
- 一次请求 N 张图：逐图查缓存，未命中的图合并进**同一次** describe 请求。
- 命中直接复用，未命中的结果按 SHA-256 各自写入缓存。
- 缓存逐图、调用合并：5 张图里 2 张命中 → 只对 3 张跑一次 vision。

```
for img in slots:  descs[i]  = cache.get(sha256(img)) or MISS
missed = [i for i where MISS]
if missed:
    results = await describe(images=[slots[i].image for i in missed])
    for k, i in enumerate(missed):
        cache[sha256(slots[i].image)] = results[k]
        descs[i] = results[k]
```

- describe 复用 `image_analysis.SYSTEM_PROMPT` 作为 system，user 提示要求 per-image 数组，
  用轻量 schema 强制：

```json
{"type":"object","properties":{"images":{"type":"array","items":{"type":"string"}}},"required":["images"]}
```

- `images[i]` 按位置回填到对应 content part。
- 深挖交给文本模型自己：代理只做第一次通用描述，文本模型要更多细节时直接调用
  现有 `lm-visual-mcp` MCP 服务器（10 个任务感知 vision 工具）继续追问。

## 4. 复用现有模块（零改动）

| 模块 | 用途 |
|---|---|
| `providers/__init__.py:build_registry` | 构建 provider 注册表 |
| `router.py:ProviderRouter.route()` | 多 provider 探测 + 自动 fallback |
| `models.py:VisionRequest / ImageInput` | 传入 vision 的统一请求 |
| `services/media.py:MediaService` | data URL / http URL → 本地临时文件 + 校验 |
| `config.py:AppConfig` | provider / key / fallback / media 配置 |
| `prompts/image_analysis.py` | describe 的 system 提示 |
| `schema.py:normalize_result` | 防御性解析（含 per-image 数组保留） |

`normalize_result` 一处小改：成功路径也会把未知顶层键保留进 `details`（例如
describe 的 `images` 数组），与失败路径一致——这是改进，非破坏。

## 5. 新增文件（薄）

```
src/lm_visual_mcp/proxy/
├── __init__.py
├── cache.py          # SHA-256 → 描述，逐图缓存                ~30 行
├── detect.py         # 协议 → 适配器注册表 + Extracted/ImageSlot ~40 行
├── media.py          # data URL / http → ImageInput            ~40 行
├── openai_chat.py    # OpenAI Chat 适配器                      ~45 行
├── openai_responses.py # OpenAI Responses 适配器               ~40 行
├── anthropic.py      # Anthropic 适配器                        ~45 行
├── describe.py       # 组装 VisionRequest → router → 拆数组     ~50 行
└── server.py         # aiohttp 应用 + 透明转发 + describe 缓存  ~120 行
```

新增依赖：`aiohttp`。

## 6. 配置 & 入口

- `config.py` 加 `ProxyConfig`：`host=127.0.0.1`、`port=8787`，env 覆盖
  `LM_VISUAL_MCP_PROXY_HOST` / `LM_VISUAL_MCP_PROXY_PORT`。
- `cli.py` 加 `lm-visual-mcp proxy` 子命令，启动即 `build_registry(cfg)` + `ProviderRouter(cfg)`。
- **自动拉起（单例）**：MCP 客户端入口（`_serve`）probe-then-launch——先确保共享 daemon
  单例在跑，再确保 vision proxy 单例在跑（`GET /health` 探测；`lm-visual-mcp proxy`
  绑定失败即静默退出，端口由唯一获胜者持有）。代理启动失败只记日志，不阻塞 MCP 服务——
  MCP vision 工具走 daemon，proxy 供 agent 的文本模型客户端用。
- API key 仍由原客户端配置，代理无账号状态。

### 6.1 生命周期命令（`start` / `stop` / `restart`）

默认 MCP 自动拉起，但也可单独管理两个单例：

    lm-visual-mcp start    [--service daemon|proxy]   # probe-then-launch，幂等
    lm-visual-mcp stop     [--service daemon|proxy]   # SIGTERM，幂等
    lm-visual-mcp restart  [--service daemon|proxy]   # stop 后 start

- 无 `--service` 时同时管理 daemon + proxy（`stop`/`restart` 先停 proxy 再停 daemon）。
- `services/lifecycle.py`：`service_targets(cfg, name)` 返回 `(名称, 端口, pidfile)`；
  `start_service` 用 cfg 取真实 host/port 探测；`stop_service` 优先读 pidfile +
  `_is_our_process`（`ps` 校验 cmdline 防止误杀被复用 PID 的无关进程），兜底用
  `lsof -ti tcp:<port>` 找端口监听 PID。
- daemon/proxy 绑定成功后才写 pidfile（`~/.cache/lm-visual-mcp/`），`stop` 后清理。
- 启动期修复：daemon 的 `/health` 现在立刻应答——`serve()` 不再等重型 `VisionSession`
  导入（`_ready` 阻塞移到 `session` property，仅 `/tool` 等待），`_TOOL_NAMES` 抽到无依赖
  的 `tool_names.py`，冷启动探测不再超时。

