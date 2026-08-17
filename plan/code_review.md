# 全仓库代码审查记录

> 审查日期：2026-08-13  
> 当前代码基线：`351db3b`（`add auto classifier support`）  
> 测试基线：`118 passed, 1 warning`

本文保存 `lm-visual-mcp` 全仓库静态审查、测试核对和本地运行验证获得的结论。它不是对
[`mcp_plan.md`](mcp_plan.md) 或 [`proxy_plan.md`](proxy_plan.md) 的替代：那两份文件保留
原始需求和历史内容；本文记录当前实现状态、已发现风险和后续整改顺序。

Claude Code Auto classifier 的专项抓包与修复单独记录在
[`classifier_compatibility.md`](classifier_compatibility.md)。

## 1. 审查范围与方法

覆盖范围：

- MCP tool 注册、schema 和 prompt 路由；
- provider registry、probe、fallback 和结构化输出；
- 本地/远程媒体解析、任务工作区和清理；
- MCP stdio client、共享 daemon 和 singleton 生命周期；
- OpenAI Chat、OpenAI Responses、Anthropic Vision Proxy；
- 配置合并、环境变量、凭证和日志；
- macOS/Linux/Windows 相关进程管理代码；
- 单元、集成、socket 和 MCP smoke tests；
- Claude Code Auto classifier 的真实代理请求。

采用的方法：

1. 逐模块阅读生产代码和对应测试；
2. 对配置、tool schema、fallback、媒体、proxy、daemon 生命周期做调用链核对；
3. 在允许绑定 loopback socket 的环境运行全量测试；
4. 启动正式 proxy，检查 `/health` 和真实 Claude Code 请求；
5. 对安全结论按“受信任单用户 loopback 部署”和“错误暴露到其他网络”两种模型评估。

本次不是第三方渗透测试，也没有生产负载、长期缓存或 Windows 真机测试数据。文中未给出
没有统计依据的故障率或攻击概率。

## 2. 总体结论

项目主路径已经形成可运行闭环：MCP tool → 共享 daemon → provider router → 统一 JSON，
Vision Proxy 也覆盖三种显式协议。代码结构清晰，测试对核心接口有较好覆盖，适合作为受信任
单用户机器上的本地开发工具。

当前不宜直接作为局域网或公网服务部署。主要原因不是 provider 调用本身，而是网络信任边界
仍靠文档约定，没有由代码强制：daemon 无认证，proxy 接受路径指定的任意上游并转发调用方
凭证，远程媒体抓取也没有私网/metadata 地址限制。若监听地址被改为非 loopback，这些问题会
组合成 SSRF、凭证转发和未授权工具调用风险。

审查未发现 `shell=True`、把 API key 暴露到 MCP tool schema、修改用户源媒体文件等问题。
CLI provider 均通过 argv 数组启动，任务工作区会在结束后清理，结构化结果也有防御性归一化。

## 3. 已确认的良好实现

### 3.1 MCP 接口与策略隔离

- 注册了 8 个主工具和 2 个兼容别名；tool name 有同步检查。
- tool schema 只暴露视觉业务参数，不暴露 provider、model、API key、workdir、timeout 或
  fallback 策略。
- 每类任务通过独立 system prompt 引导；provider 选择完全在服务端完成。
- 对应测试位于 [`tests/test_mcp_smoke.py`](tests/test_mcp_smoke.py) 和
  [`tests/test_zai_tool_schemas.py`](tests/test_zai_tool_schemas.py)。

### 3.2 Provider 调用与输出

- 默认 provider 顺序为 AGY → Codex → Gemini → OpenCode，可由服务端配置调整。
- CLI provider 使用 `asyncio.create_subprocess_exec` 和 argv 数组，stdin 关闭并有执行超时，
  没有 shell 拼接用户输入。
- Gemini key 支持环境变量间接引用，配置中的兼容明文 key 使用 `SecretStr` 承载。
- provider 输出统一归一化为 `VisionResult`，异常字段会降级为 warning，未知顶层字段保留在
  `details`。

### 3.3 工作区与并发

- 每个 MCP 任务创建独立 workspace；远程文件和 schema 暂存在任务目录。
- `finally` 中清理任务目录，用户源文件只读取和复制，不会被删除。
- 多个 MCP stdio client 复用一个 daemon 和 `VisionSession`，全局并发由同一 semaphore 控制。
- daemon/proxy 都采用 probe-then-launch，并通过端口竞争实现单实例。

### 3.4 Proxy 协议边界

- protocol path 显式区分 OpenAI Chat、OpenAI Responses 和 Anthropic，不根据请求体猜协议。
- 没有图片时通常保留原始 body 字节；图片存在时只替换对应 image part。
- hop-by-hop header 会过滤，Authorization 和 API key header 有意透传给上游。
- classifier thinking 改写和第一阶段响应规范化已经独立配置并通过真实请求验证。

## 4. 风险与缺陷清单

严重度以项目被错误暴露或作为通用 gateway 使用时的影响评估。在 README 所要求的受信任
loopback 部署中，部分安全项的可利用性会明显降低，但仍建议由代码强制边界。

### H-1：网络信任边界只靠配置约定，没有强制保护

**证据**

- `RuntimeConfig.host` 和 `ProxyConfig.host` 接受任意字符串，没有 loopback 校验或显式
  `allow_remote_bind`。
- daemon `/tool` 没有认证、请求签名或来源令牌。
- proxy 从 URL path 解码任意 `http://`/`https://` 上游地址，并将 Authorization、x-api-key
  等调用方 header 原样转发。
- MCP 媒体下载和 proxy 远程图片下载使用通用 URL opener，没有阻止 loopback、RFC1918、
  link-local、云 metadata 地址，也没有在重定向后重新验证目标 IP。

**后果**

若 daemon/proxy 监听到非受信任接口，攻击者可能未授权调用本地视觉 provider、让服务访问
内网资源，或诱导 proxy 把调用方凭证发往攻击者指定的 host。任意上游转发、凭证透传和无
allowlist 是一个组合风险。

**建议**

1. 默认只允许 loopback；非 loopback bind 必须显式 opt-in。
2. daemon 增加随机本地 token，或拒绝一切非 loopback 监听。
3. proxy 增加上游 host allowlist，并默认拒绝 HTTP 明文目标。
4. 每次 DNS 解析和重定向后校验目标 IP，默认阻止私网、loopback、link-local 和 metadata。

### H-2：媒体和请求大小限制不统一

**证据**

- MCP 本地文件和远程下载会检查扩展名/MIME、流式下载上限和最终 kind 上限。
- proxy 的 data URL 和 Anthropic raw base64 路径直接整体解码并写文件，没有调用
  `MediaService._validate_size` 或 MIME allowlist；未知 media type 默认写成 `.png`。
- base64 解码没有使用严格校验模式，部分非规范输入可能被宽松接受。
- aiohttp `web.Application()` 没有显式 `client_max_size`，实际默认限制可能和配置声明的
  20 MB 图片能力不一致。
- daemon 按调用方 `Content-Length` 直接读取 `/tool` body，没有独立 request-body 上限。

**后果**

同一张图片从 URL、本地路径、data URL 或 Anthropic base64 进入时会得到不同安全与正确性
结果。可能出现超预期内存/磁盘使用、伪造 MIME、合法大图被 HTTP 层提前拒绝，或 daemon
线程长时间读取大请求。

**建议**

- 所有媒体入口统一走 MIME、magic bytes、解码后大小和图片数量校验。
- 为 proxy/daemon 明确设置 request body 上限，并与 `max_image_mb`、最大图片数协调。
- base64 使用严格校验，解码前先按编码长度做快速拒绝。

### M-1：Provider probe 与 fallback policy 语义不一致

**证据**

`ProviderRouter.route` 在 `probe()` 返回 unavailable 时，只检查 `fallback.enabled`，继续下一个
provider；只有 `analyze()` 抛出 `ProviderUnavailableError` 时才检查 `fallback.on`。

**后果**

配置声称只对某些 reason fallback，但同一个 reason 如果在 probe 阶段发现，会绕过
`fallback.on`。策略结果取决于 provider 在哪个阶段报告失败。

**建议**

把 probe 和 analyze 的失败都转换为统一 failure decision，使用同一个 policy 函数判断是否
继续。为 probe 的 non-fallback reason 增加测试。

### M-2：视频工具已公开，但没有可靠的端到端实现

**证据**

- `analyze_video` 和 `video_analysis` 出现在 MCP tool list 和文档中。
- Codex、Gemini、OpenCode 当前明确拒绝 `VisionRequest.videos`。
- AGY 没有稳定、经过验证的视频附件通路。

**后果**

客户端会把已注册 tool 视为可用能力，但正常配置下大概率得到所有 provider 失败，形成接口
承诺与实际能力不一致。

**建议**

在 v1 发布前二选一：实现至少一个真实端到端视频 provider 并测试；或隐藏视频工具，仅在
显式实验配置下注册。

### M-3：配置缺少数值范围和网络安全验证

**证据**

`timeout`、`max_concurrency`、port、媒体大小、download timeout 等主要是普通 `float/int`
字段，没有正数或端口范围约束。`max_concurrency: 0` 会创建永不放行的 semaphore。

**后果**

错误配置可能导致永久排队、立即超时、无效端口、负数限制或意外非 loopback 暴露，且问题
只在运行时出现。

**建议**

使用 Pydantic `Field(gt=...)`/validator：concurrency 至少为 1，port 为 1..65535，timeout 和
媒体限制为正数，并验证 host 安全策略。

### M-4：daemon 初始化失败可能永久等待

**证据**

`ToolServer._loop_main` 只有在 `session_factory` 成功返回后才执行 `_ready.set()`。如果 import、
provider registry 或 session 构造抛异常，`session` property 中的 `_ready.wait()` 永远不会
结束；与此同时 `/health` 仍可返回 `ok: true`。

**后果**

进程看起来健康，但 `/tool` handler 会挂到外层 execution timeout，且真正初始化异常在线程
中不容易被调用方识别。

**建议**

增加 `starting/ready/failed/stopping` 状态；在 `finally` 设置 event 并保存初始化异常；
`/health` 和 `/tool` 返回明确的 failed 状态。

### M-5：Proxy fail-open 会产生部分改写或原图透传

**证据**

adapter 对单个无法解析的图片执行 `continue`。如果所有图片都失败，请求原样转发；如果同一
请求部分成功，成功图片会替换为文本，失败图片仍保留在改写后的请求中。

**后果**

调用方无法确定上游收到的是纯文本、原图还是混合内容。对不支持图片的上游会产生不可预测
错误；对依赖 proxy 去除敏感图片的场景则可能造成意外原图转发。

**建议**

增加 `image_error_policy: reject | passthrough`，默认 reject；一个请求应原子地全部改写或全部
拒绝，不做隐式部分改写。

### M-6：Proxy query string 被丢弃

**证据**

目标拼接只使用 `request.path`，没有把 `request.query_string` 合并到 upstream URL。

**后果**

依赖 query 参数的 Azure/OpenAI-compatible endpoint、版本参数或签名 URL 无法正确转发。

**建议**

在明确的编码规则下转发原 query string，并增加覆盖重复 key、空值和 percent encoding 的
集成测试。

### M-7：缓存键、并发 miss 和失败缓存策略不足

**证据**

- `VisionCache` 只以图片字节 SHA-256 为键，没有 descriptor prompt/version、provider/model
  policy 或配置版本。
- 缓存只有最大条数和 FIFO，没有 TTL。
- `aget` 与 describe 之间没有 single-flight，并发相同 miss 会重复产生付费视觉请求。
- 空描述和结果数量不足时补出的空字符串也会被缓存。
- proxy 媒体文件保留到整个 proxy shutdown，缓存本身实际只需要描述文本。

**后果**

修改 prompt/provider 后可能继续返回旧描述；并发浪费调用；一次瞬时失败可能长期污染缓存；
长期运行会保留不必要的临时媒体文件。

**建议**

缓存键加入 descriptor version 和 provider/model policy；增加 TTL+LRU、single-flight；不缓存
空值/失败；请求结束后删除不再需要的媒体文件。

### M-8：描述结果数量不匹配时静默填空

**证据**

`describe()` 会把 provider 返回数组 pad/truncate 到输入图片数；缺失项用空字符串填充。随后
proxy 把对应图片替换为只有 `[Image N]` 的空描述。

**后果**

上游文本模型会把“图片成功感知但内容为空”和“provider 输出损坏”混为一谈，降低正确性且
掩盖 provider contract 问题。

**建议**

数量不匹配应作为明确错误进入 image error policy，不应成功缓存或静默替换。

### M-9：生命周期管理的 Windows 与退出语义不完整

**证据**

- `_is_our_process` 使用 `ps`，端口 PID fallback 使用 `lsof`。
- `_kill` 发送 SIGTERM 后立即返回 `stopped`，没有等待或确认退出。
- pidfile 主要由显式 stop 清理；daemon/proxy 正常或异常退出路径没有统一清理。
- 后台 proxy 的 stdout/stderr 指向 DEVNULL，缺少持久诊断日志；daemon 日志级别固定为 INFO。

**后果**

Windows stop/restart 可能找不到目标；服务尚未退出就报告成功；陈旧 pidfile 和静默后台错误
增加运维困难。

**建议**

抽象跨平台进程查询与终止；stop 等待退出并报告超时；所有退出路径清理 pidfile；为 proxy
提供与 daemon 一致的可配置文件日志。

### M-10：日志“按值脱敏”尚未实际生效

**证据**

CLI 安装了 redaction filter，但内部 `_secrets` 始终为空，没有把 effective API key 或其他
token 注册进去。`SecretStr` 只保护配置对象的字符串表示，不会自动清洗 provider stderr、
异常文本或第三方日志。

**后果**

目前代码大多避免主动记录 key，但不能声称“所有日志严格脱敏”。未来新增 debug 日志或第三方
异常时可能泄漏 secret。

**建议**

在配置解析后向 filter 注册所有 effective secrets，或采用结构化日志并禁止原始 headers、
provider stderr、请求 body。增加 canary secret 测试覆盖 stderr、daemon file log 和异常链。

### L-1：Tool 和 daemon payload 校验不够严格

**证据**

- `ui_to_artifact.output_type` 是普通 string；非法值会落到通用 prompt，而不是 schema 拒绝。
- daemon `/tool` 没有 tool allowlist、字段类型、图片数量或 prompt 长度验证。
- video_sources 多于一个时只取第一个。

**后果**

错误调用可能静默改变语义，内部 HTTP 接口比 MCP schema 更宽松，也不利于诊断。

**建议**

有限字段使用 `Literal`/enum；daemon 为内部 payload 建 Pydantic model，并验证 tool allowlist、
媒体数量和字符串长度。

### L-2：bbox 只验证形状，没有验证坐标契约

**证据**

Pydantic 类型保证 bbox 是四个整数，但没有强制 `0..1000`，也没有检查 min/max 顺序。provider
JSON schema 对 bbox 甚至没有限制数组长度。

**后果**

调用方可能收到负坐标、超范围坐标或反向矩形，和 prompt 声明的统一坐标系不一致。

**建议**

增加长度、范围、`x_min <= x_max`、`y_min <= y_max` 校验；非法 bbox 移除并追加 warning。

### L-3：Provider CLI 兼容版本和健康探测未固定

**证据**

CLI command/model 可配置，但没有最低版本检查。多数 probe 只确认 executable 或 key 存在，
不代表附件参数、JSON schema 或登录状态与当前代码兼容。

**后果**

CLI 升级后参数变化可能只在真实请求中暴露，出现难以定位的 fallback。

**建议**

记录最低已验证版本；`doctor` 输出版本和能力探测结果；将真实付费 probe 保持为显式 opt-in。

### L-4：错误响应与观测指标不足

**证据**

daemon 会把 `str(exc)` 返回客户端，可能包含本地路径或 provider 细节；proxy 对未知异常只返回
通用 500。当前没有稳定的 request id、reject reason、cache hit/miss、provider latency、
upstream latency 等结构化指标。

**后果**

一个方向可能暴露过多内部信息，另一个方向又缺少定位线索。问题发生后依赖临时详细日志，
不适合长期运维。

**建议**

使用安全错误码和 server-side request id；增加不含 prompt、图片、凭证或完整 body 的结构化
指标。

## 5. 已解决的专项问题

### Claude Code Auto classifier

已确认 classifier 请求会遵守 `ANTHROPIC_BASE_URL`。失败根因是上游 gateway 返回的 HTTP 200
响应不符合 Anthropic stop-sequence framing，而不是 classifier 绕过代理。

当前实现：

- 通过 security-monitor marker + no tools 识别 classifier 家族；
- `proxy.classifier.disable_thinking` 默认只改写 classifier 请求；
- 带 `</block>` stop sequence 的第一阶段响应始终规范化；
- 无明确 verdict 或 yes/no 冲突时不猜测；
- 普通请求不受 thinking 配置影响。

详见 [`classifier_compatibility.md`](classifier_compatibility.md)。

## 6. 整改优先级

### P0：发布或扩大部署范围前

1. 强制 loopback，或实现认证与显式 remote-bind opt-in。
2. 为 proxy 上游和远程媒体增加 host/IP/redirect 策略，阻断 SSRF 和任意凭证转发。
3. 统一本地、URL、data URL、raw base64 的 MIME、magic bytes、数量和大小限制。
4. 给 daemon/proxy 设置明确 request body 上限。
5. 让日志脱敏声明与实际实现一致，并增加 secret canary 测试。

### P1：正确性与可运维性

1. 统一 probe/analyze fallback policy。
2. 决定视频能力是实现还是隐藏。
3. 增加配置数值/端口/host validation。
4. 修复 daemon 初始化状态机和 `/health` 语义。
5. proxy 图片错误改为显式、原子的 error policy。
6. 转发 query string，并补 endpoint 兼容测试。
7. 重做缓存 key、TTL/LRU、single-flight 和失败策略。
8. 完善跨平台 stop/restart、pidfile 和后台日志。

### P2：接口质量与诊断

1. 收紧 tool/daemon payload schema 和 bbox validation。
2. 固定 provider CLI 已验证版本，扩展 `doctor`。
3. 增加安全 request id、结构化指标和 `doctor --json`。
4. 增加 Linux/macOS/Windows CI；socket 测试必须运行在允许 loopback bind 的 job。

## 7. 建议验收标准

### 安全边界

- 默认配置不能监听非 loopback；危险 opt-in 有独立测试。
- 内网、loopback、link-local、metadata 和重定向到这些地址的媒体请求均被拒绝。
- 不在 allowlist 的 upstream 不会收到 Authorization/API key header。
- data URL/base64 超限在完整解码或写盘前拒绝。
- canary secret 不出现在 stderr、daemon/proxy 日志、doctor 和错误响应。

### 正确性

- probe 与 analyze 对相同 reason 作出相同 fallback 决策。
- 图片请求要么全部转换、要么按配置整体拒绝/整体透传，不出现隐式部分改写。
- query string 的 key、值和编码端到端保持。
- descriptor prompt/provider policy 改变后不会命中旧缓存。
- provider 返回图片描述数量不匹配时产生明确错误。

### 生命周期

- daemon 初始化失败时 `/health` 报 failed，`/tool` 快速返回，不永久等待。
- `stop` 只在确认进程退出后报告 stopped。
- 正常退出、异常退出、端口竞争后 pidfile 状态正确。
- Windows 不依赖 `ps`、`lsof` 或 POSIX signal 假设。

### 测试

- 当前 118 个测试保持通过。
- 新增安全边界、配置非法值、并发 cache miss、daemon init failure、query forwarding、
  classifier false-positive guard 测试。
- 保留真实 provider/Claude Code smoke test，但必须显式触发，不作为默认付费测试。

## 8. 证据索引

- MCP tools：[`src/lm_visual_mcp/server.py`](src/lm_visual_mcp/server.py)
- 共享执行与工作区：[`src/lm_visual_mcp/tools/__init__.py`](src/lm_visual_mcp/tools/__init__.py)
- Provider router：[`src/lm_visual_mcp/router.py`](src/lm_visual_mcp/router.py)
- 配置：[`src/lm_visual_mcp/config.py`](src/lm_visual_mcp/config.py)
- 媒体：[`src/lm_visual_mcp/services/media.py`](src/lm_visual_mcp/services/media.py)
- daemon：[`src/lm_visual_mcp/services/control.py`](src/lm_visual_mcp/services/control.py)
- 生命周期：[`src/lm_visual_mcp/services/lifecycle.py`](src/lm_visual_mcp/services/lifecycle.py)
- Proxy server：[`src/lm_visual_mcp/proxy/server.py`](src/lm_visual_mcp/proxy/server.py)
- Proxy media：[`src/lm_visual_mcp/proxy/media.py`](src/lm_visual_mcp/proxy/media.py)
- Proxy cache：[`src/lm_visual_mcp/proxy/cache.py`](src/lm_visual_mcp/proxy/cache.py)
- 描述阶段：[`src/lm_visual_mcp/proxy/describe.py`](src/lm_visual_mcp/proxy/describe.py)
- 结果 schema：[`src/lm_visual_mcp/schema.py`](src/lm_visual_mcp/schema.py)
- Classifier：[`src/lm_visual_mcp/proxy/classifier.py`](src/lm_visual_mcp/proxy/classifier.py)

