# Claude Code Auto classifier 兼容性记录

> 状态：已实现并通过本地真实请求验证  
> 调查日期：2026-08-13  
> 项目版本：0.1.0

本文独立记录 Claude Code Auto Mode classifier 经过 `lm-visual-mcp` Vision Proxy 时的
协议特征、故障原因、兼容策略、配置和风险边界。原始 MCP 与 proxy 需求文档仍保留在
[`mcp_plan.md`](mcp_plan.md) 和 [`proxy_plan.md`](proxy_plan.md)，本次调查不改写那两份文档。

## 1. 结论

Claude Code 的 Auto classifier 请求遵守 `ANTHROPIC_BASE_URL`，并通过普通 Anthropic
Messages endpoint `/v1/messages` 进入代理。`classifier temporarily unavailable` 不等于
classifier 绕过代理或无法访问 Anthropic；HTTP 错误、字段不兼容、超时和响应无法解析都可能
被 Claude Code 汇总成这个提示。

本次真实请求失败的直接原因是 Anthropic-compatible gateway 没有正确实现请求中的
`stop_sequences: ["</block>"]`：它返回了完整闭合标签，把 `stop_reason` 写成 `end_turn`，
并可能在 verdict 前返回 thinking block。Claude Code 没有把这个 HTTP 200 响应识别为有效
classifier verdict。

Proxy 现在执行两项相互独立的兼容处理：

1. 请求侧可配置地写入 `"thinking": {"type": "disabled"}`，默认开启。
2. 已确认的二元第一阶段响应始终恢复为 Anthropic stop-sequence framing，不受 thinking
   配置影响。

## 2. 实际捕获的请求

测试客户端为 Claude Code 2.1.228（VS Code 环境）。请求经过：

```text
Claude Code
  → ANTHROPIC_BASE_URL
  → /proxy/anthropic/<base64url(upstream-base)>/v1/messages
  → <decoded-upstream-base>/v1/messages
```

捕获到的第一阶段请求具有以下特征：

- endpoint：Anthropic `/v1/messages`，没有 classifier 专用 URL；
- `model`：到达本代理前已经被映射为 `ark-code-latest`；
- `max_tokens`：本次为 `2112`；
- `stream`：未设置；
- `tools`：未设置；
- `stop_sequences`：包含 `</block>`；
- `system`：数组形式，含固定 security-monitor marker；
- `messages`：包含待审查的 transcript/action；
- prompt 很长，本次 system text 总计约 110 KB；
- 本次请求包含 3 个 `cache_control` block。

精简后的协议形态：

```json
{
  "model": "ark-code-latest",
  "max_tokens": 2112,
  "system": [
    {
      "type": "text",
      "text": "You are a security monitor for autonomous AI coding agents. ..."
    }
  ],
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "<transcript>...action...</transcript>"
        }
      ]
    }
  ],
  "stop_sequences": ["</block>"]
}
```

`model`、`max_tokens`、prompt 长度和 cache-control 数量都可能随 Claude Code、gateway 或
模型映射变化，因此实现没有把它们作为 classifier 指纹。

## 3. 识别逻辑

识别是确定性的协议指纹匹配，不调用另一个模型。

### 3.1 Classifier 家族

请求必须同时满足：

1. 走 `/proxy/anthropic/...` 协议路径；
2. JSON 顶层是 object；
3. `tools` 缺失、为 `null` 或空数组；
4. `system` 文本中包含完整 marker：

```text
You are a security monitor for autonomous AI coding agents.
```

匹配 classifier 家族后，`proxy.classifier.disable_thinking` 才可能改写请求。普通 Anthropic、
OpenAI Chat、OpenAI Responses 请求不会受到该配置影响。

### 3.2 已确认的二元第一阶段

在 classifier 家族条件之外，请求的 `stop_sequences` 还必须包含 `</block>`。只有这个阶段
进入当前的 yes/no 响应规范化流程。

分层识别的原因是 classifier 可能不止一个阶段。请求侧关闭 thinking 可以应用于整个已识别
家族，但不能把一个可能携带 `severity`、`category`、`reason` 的其他阶段响应擅自压缩为
二元结果。

实现见 [`src/lm_visual_mcp/proxy/classifier.py`](src/lm_visual_mcp/proxy/classifier.py)。

## 4. 请求改写

配置：

```yaml
proxy:
  classifier:
    disable_thinking: true
```

环境变量：

```text
LM_VISUAL_MCP_PROXY_CLASSIFIER_DISABLE_THINKING=true|false
```

默认值为 `true`。识别到 classifier 家族请求时，Proxy 写入：

```json
"thinking": {"type": "disabled"}
```

该配置只控制请求改写：

- `true`：新增或覆盖 classifier 请求的 `thinking` 字段；
- `false`：classifier 请求 body 保持原始字节；
- 两种配置下，第一阶段响应规范化都保持启用；
- 非 classifier 请求不会因为该配置而修改 `thinking`。

如果上游不接受 `thinking.type=disabled`，应将该配置设为 `false`，而不是关闭响应规范化。

## 5. 响应问题与规范化

### 5.1 失败时捕获的上游响应

上游返回 HTTP 200，形态为：

```json
{
  "type": "message",
  "content": [
    {"type": "thinking", "thinking": "..."},
    {"type": "text", "text": "<block>no</block>"}
  ],
  "stop_reason": "end_turn",
  "stop_sequence": null
}
```

这与请求声明的 `stop_sequences: ["</block>"]` 不一致。正确命中 Anthropic stop sequence
时，匹配到的停止文本不应出现在生成内容中，而且响应应报告命中的停止序列。

### 5.2 Proxy 输出

对于已识别的第一阶段、HTTP 200、JSON Anthropic message，Proxy 从所有 text block 中查找
唯一、明确的 `<block>yes` 或 `<block>no` verdict，然后输出：

```json
{
  "type": "message",
  "content": [
    {"type": "text", "text": "<block>no"}
  ],
  "stop_reason": "stop_sequence",
  "stop_sequence": "</block>"
}
```

`id`、`model`、`role`、`usage` 等其他顶层字段保留。响应 body 被改写后，与原始字节绑定的
`Content-Encoding`、`Content-MD5` 和 `ETag` 会移除，body framing 由 HTTP 库重新计算。

以下情况不改写响应：

- 非第一阶段 classifier；
- 非 HTTP 200；
- Content-Type 不是 JSON；
- 不是 Anthropic message object；
- 没有可识别 verdict；
- 同一响应同时出现 `yes` 和 `no`。

Proxy 不会凭空生成安全决定，也不会在模糊结果下默认 allow。

## 6. yes/no 的含义

本次捕获的第一阶段输出是二元 XML verdict：

- `<block>no</block>`：第一阶段没有发现需要进入阻止判断的候选，通常直接放行；
- `<block>yes</block>`：发现可能需要阻止的候选，但不应简单理解成最终拒绝。

捕获到的 policy 明确说明，第一阶段不应用完整的用户意图和 ALLOW 例外，这些由后续阶段
处理。因此整个 classifier 流程并不等同于单次 yes/no 调用。Claude Code 安装内容中还能
观察到 `xml_s1`、`xml_s2`、`stage2`、`severity`、`category`、`reason` 等相关标识。

本次测试没有捕获到真实第二阶段 wire payload，所以当前实现只对已验证的第一阶段格式做
响应规范化，不假设第二阶段的精确请求或响应结构。

## 7. 误报、漏报与后果

### 7.1 误报

普通请求必须同时复用完整 security-monitor marker、没有 tools，并走 Anthropic proxy，才会
被识别成 classifier 家族。自然流量中发生这种碰撞的可能性很低，但当前没有生产统计样本，
不能给出可信的百分比。

可能的误报来源：

- 用户刻意复制 classifier system prompt 做测试；
- 另一个安全产品复用完全相同的 prompt；
- 调用方恶意构造协议指纹。

家族级误报的后果是默认关闭该请求的 thinking。要进一步触发响应规范化，还必须包含
`</block>` stop sequence，并收到唯一的 `<block>yes/no`。最坏情况下，刻意使用同一 XML
协议的非 classifier 响应会被截成二元 text block。关闭 `disable_thinking` 可以消除请求侧
影响，但不会也不应关闭第一阶段响应规范化。

### 7.2 漏报

如果 Claude Code 将来修改 marker、引入 tools 或改变第一阶段 stop protocol，请求可能不再
被识别。漏报时 Proxy 透明转发，不会改写安全判断；但不兼容的 gateway 可能再次导致 Auto
Mode 报 classifier unavailable。

相比给出没有数据支撑的误报率，后续更适合增加不含请求正文的计数指标：

```text
classifier_family_detected
classifier_stage1_detected
classifier_thinking_rewritten
classifier_response_normalized
classifier_response_ambiguous
```

## 8. 验证结果

真实验证分两轮关键对照：

1. 未关闭 thinking：上游返回 thinking + `<block>no</block>` 和 `end_turn`；经过响应规范化后，
   Claude Code Auto Mode 成功继续执行。
2. 默认关闭 thinking：发往上游的请求含 `thinking: disabled`；上游只返回 text，但仍使用
   完整 `</block>` 和 `end_turn`，证明响应规范化仍然必要；Claude Code 同样成功继续执行。

自动测试覆盖：

- classifier 家族和第一阶段分层识别；
- 普通请求不被识别；
- thinking 默认改写与幂等；
- 关闭请求改写后，原始请求字节保持不变；
- yes/no、已有截断结果、无 verdict、冲突 verdict；
- HTTP 端到端转发和响应 header 处理；
- YAML 默认值和环境变量覆盖。

全量测试基线：

```text
118 passed, 1 warning
```

warning 来自 MCP SDK 依赖的 Pydantic forward reference，与 classifier 修改无关。详细抓包
日志和临时验证文件在确认结果后已删除，正式代码不记录 classifier prompt、transcript、API
key 或完整响应 body。

## 9. 相关文件

- [`src/lm_visual_mcp/proxy/classifier.py`](src/lm_visual_mcp/proxy/classifier.py)：识别、请求改写、响应规范化。
- [`src/lm_visual_mcp/proxy/server.py`](src/lm_visual_mcp/proxy/server.py)：Anthropic 请求接入与转发。
- [`src/lm_visual_mcp/config.py`](src/lm_visual_mcp/config.py)：配置模型和环境变量解析。
- [`config.example.yaml`](config.example.yaml)：配置示例。
- [`tests/test_proxy.py`](tests/test_proxy.py)：识别、规范化与端到端测试。
- [`tests/test_config.py`](tests/test_config.py)：配置测试。

