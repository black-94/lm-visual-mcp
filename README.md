# Vision MCP Server

> **English** | [简体中文](README.zh-CN.md)

`lm-visual-mcp` gives text-only LLMs and coding agents visual input through the
[Model Context Protocol](https://modelcontextprotocol.io). It also includes an optional
HTTP Vision Proxy that converts image blocks in OpenAI or Anthropic requests into text
descriptions before forwarding them upstream. The proxy also handles Claude Code Auto
classifier interoperability with Anthropic-compatible gateways.

This repository is currently at **v0.1.0**. Image analysis is the supported core path;
video tool names are exposed for compatibility, but no provider currently has a reliable
end-to-end video attachment path. See [Current limitations](#current-limitations).

## What it provides

- 8 task-oriented vision tools plus 2 compatibility aliases.
- Server-controlled provider routing: AGY → Codex → Gemini → OpenCode by default.
- Per-task workspaces and bounded media downloads.
- A shared local lm-vision-server, reused by multiple MCP client processes.
- Unified JSON results regardless of provider.
- A transparent OpenAI Chat, OpenAI Responses, and Anthropic Messages proxy.
- Claude Code Auto classifier request and first-stage response compatibility.
- Lifecycle and environment diagnostics through the CLI.

Provider, model, credentials, fallback policy, workdir, and timeout are server
configuration. They are deliberately absent from MCP tool schemas.

## Architecture

```text
MCP client process
    │ stdio
    ▼
lm-visual-mcp client
    │ loopback HTTP
    ▼
lm-vision-server (one VisionSession)
    ├── prompt selection
    ├── workspace/media staging
    ├── concurrency limit
    └── ProviderRouter ── AGY → Codex → Gemini → OpenCode
```

```text
OpenAI / Anthropic SDK
    │ base_url points at lm-proxy
    ▼
lm-proxy
    ├── no image: forward the original request body unchanged
    └── image: extract → cache/describe → replace with text
    │
    ▼
upstream model API (responses/SSE stream back; classifier stage one is normalized)
```

Running `lm-visual-mcp` without a subcommand starts an MCP stdio client. It probes the
shared lm-vision-server and lm-proxy, starts missing services, then forwards MCP calls to
the server. `runtime.max_concurrency` limits work across all connected MCP clients.

Both singletons are **always-on** (常驻): once started they stay running until stopped.
At MCP startup the client probes each singleton's `/health` and launches it exactly once
if missing — there is **no runtime keep-alive or monitoring**. If lm-vision-server or
lm-proxy is unexpectedly killed, the MCP session's tool calls simply fail; restart the
Claude Code session (which re-probes and re-launches) or run `lm-visual-mcp start`.

## Requirements

- Python 3.11+
- At least one configured image-capable provider:
  - an installed and authenticated `agy`, `codex`, or `opencode` CLI; or
  - a Gemini API key for `google-genai`.
- macOS and Linux are the best-covered platforms. Basic startup works on Windows, but
  `stop`/port-PID discovery still relies on POSIX `ps` and `lsof`; see
  [Current limitations](#current-limitations).

## Installation

From a source checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e .
```

For development and the full test suite:

```bash
python -m pip install -e ".[dev]"
```

Verify the installation:

```bash
lm-visual-mcp --version
lm-visual-mcp doctor
```

If the console script is not on `PATH`, use `python -m lm_visual_mcp` in every example.

## Quick start

### 1. Configure providers

```bash
mkdir -p ~/.config/lm-visual-mcp
cp config.example.yaml ~/.config/lm-visual-mcp/config.yaml
```

Edit the copied file. Disable providers you do not use, and avoid committing credentials.
For Gemini, set an environment variable:

```bash
export GEMINI_API_KEY="..."
```

### 2. Add the MCP server to a client

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

Use an absolute config path. The process speaks MCP over stdout; logs go to stderr.

### 3. Optional: route model API traffic through the Vision Proxy

Encode the upstream base API URL as unpadded base64url:

```bash
python -c "import base64; u=b'https://api.anthropic.com'; print(base64.urlsafe_b64encode(u).decode().rstrip('='))"
```

Then configure the SDK base URL:

```text
http://127.0.0.1:8787/proxy/anthropic/aHR0cHM6Ly9hcGkuYW50aHJvcGljLmNvbQ
```

Available protocol paths are:

```text
/proxy/openai/chat/<base64url(base API URL)>
/proxy/openai/responses/<base64url(base API URL)>
/proxy/anthropic/<base64url(base API URL)>
```

SDK-appended endpoint paths are supported. For example, an Anthropic SDK may append
`/v1/messages`; the proxy rebases that suffix onto the decoded upstream base URL.

> **Tested upstream**: the proxy has so far only been exercised against **DeepSeek** as
> the upstream text backend. Other upstreams are expected to work but are not yet verified.

Important current behavior:

- **Byte-transparent passthrough**: the proxy is a transparent proxy — it disables aiohttp's
  automatic decompression and header rewriting (`auto_decompress=False`,
  `skip_auto_headers=("*",)`) and disables redirects, so an image-free request is forwarded
  upstream byte-for-byte with no decompress/recompress round-trip. The only exception is a
  detected classifier when `proxy.classifier.disable_thinking: true`, which inserts
  `thinking: disabled`.
- **Recursive image extraction**: images are rewritten wherever they appear in a request —
  not just top-level `messages[].content[]`. When Claude Code passes screenshots, `Read`
  output, or other tool results as image blocks nested inside `tool_result.content`, those
  are detected and rewritten too. This is what lets a text-only upstream accept tool-result
  images instead of rejecting them with a "Model only support text input" 400.
- OpenAI adapters accept data URLs and HTTP(S) image URLs; the Anthropic adapter accepts
  base64 image sources.
- Image parse failures fail open and forward the original request.
- **~100 MB request ceiling**: the proxy raises aiohttp's default request-size limit
  (`client_max_size`) from 1 MB to 100 MB so multi-image base64 payloads are not dropped by
  the framework; a request body larger than 100 MB is still rejected.
- Query strings sent to the proxy endpoint are currently not forwarded.
- The proxy must remain on a trusted loopback interface; see [Security](#security).

### Claude Code Auto classifier compatibility

Auto classifier calls use the same Anthropic `/v1/messages` endpoint as ordinary requests.
The proxy identifies them by their protocol contract rather than a model name or token count:
the security-monitor system marker and absence of tools identify the classifier family; a
`</block>` stop sequence identifies its known binary first stage.

For a detected classifier request, `proxy.classifier.disable_thinking` controls request
rewriting only. It defaults to `true` and inserts `"thinking": {"type": "disabled"}`. Set it
to `false` for an upstream model that rejects disabled thinking.

First-stage response normalization is always enabled and is independent of that option. Some
Anthropic-compatible gateways ignore `stop_sequences` or return a leading thinking block. The
proxy extracts one unambiguous `<block>yes</block>` or `<block>no</block>` text verdict and
restores Anthropic stop-sequence framing: a single text block with `stop_reason:
"stop_sequence"` and `stop_sequence: "</block>"`. A response with no verdict or conflicting
verdicts is never guessed or rewritten. The normalization path handles compressed
(gzip/deflate/brotli) bodies. In stage one, `no` normally allows the action while `yes`
indicates that a later stage may be needed — it is not necessarily a final denial.

The captured payload, exact detection rules, and false-positive/false-negative boundaries are
documented in [`classifier_compatibility.md`](classifier_compatibility.md).

## CLI

```text
lm-visual-mcp [--config PATH] [--log-level LEVEL]
lm-visual-mcp doctor [--probe]
lm-visual-mcp server
lm-visual-mcp proxy [--host HOST] [--port PORT]
lm-visual-mcp start   [--service server|proxy]
lm-visual-mcp stop    [--service server|proxy]
lm-visual-mcp restart [--service server|proxy]
lm-visual-mcp --version
```

`server` runs the shared lm-vision-server; `proxy` runs the lm-proxy. Common options may be
placed before or after a subcommand. Without `--service`, lifecycle commands manage both
services. `doctor --probe` performs a real AGY image smoke test and requires Pillow plus a
working AGY CLI; it does not probe every provider with a paid call.

Pidfiles and per-service disk logs (`lm-vision-server.log`, `proxy.log`) live under
`~/.cache/lm-visual-mcp/`.

## Configuration

Configuration priority is:

```text
CLI option > environment variable > YAML file > built-in default
```

Config search order when `--config` is omitted:

1. `LM_VISUAL_MCP_CONFIG`
2. `./lm-visual-mcp.yaml`
3. `~/.config/lm-visual-mcp/config.yaml`
4. `~/.config/lm-visual-mcp/lm-visual-mcp.yaml`

See [`config.example.yaml`](config.example.yaml) for every current field. A minimal example:

```yaml
version: 1

providers:
  order: [agy, codex, gemini, opencode]
  agy:
    enabled: true
    command: agy
  codex:
    enabled: true
    command: codex
    effort: high
  gemini:
    enabled: true
    effort: high
    api_key_env: GEMINI_API_KEY
  opencode:
    enabled: true
    command: opencode

runtime:
  workdir: null
  timeout: 120
  max_concurrency: 2
  host: 127.0.0.1
  port: 6506

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

### Environment variables

```text
LM_VISUAL_MCP_CONFIG
LM_VISUAL_MCP_WORKDIR
LM_VISUAL_MCP_TIMEOUT
LM_VISUAL_MCP_MAX_CONCURRENCY
LM_VISUAL_MCP_HOST
LM_VISUAL_MCP_PORT

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

Gemini credential resolution is: `LM_VISUAL_MCP_GEMINI_API_KEY`, then the compatibility
plain `providers.gemini.api_key`, then the environment variable named by `api_key_env`, then
`GEMINI_API_KEY`. Prefer `api_key_env`; a plain config key is accepted only for compatibility.

### Workspaces and media

With `runtime.workdir: null`, each MCP call gets a temporary directory that is deleted after
the call. With a configured workdir, files are staged under
`<workdir>/.lm-visual-mcp/<uuid>/` and that task directory is removed afterward. Source files
are copied, never modified.

MCP image types: PNG, JPEG, WebP, GIF, BMP, TIFF. Video file extensions accepted by the media
layer are MP4, MOV, and M4V, but provider support is currently incomplete. `file://` URLs are
rejected; use a local path or HTTP(S) URL.

## MCP tools

| Tool | Required input | Optional input | Purpose |
|---|---|---|---|
| `ui_to_artifact` | `image_source`, `output_type`, `prompt` | - | UI to code, prompt, spec, or description |
| `extract_text_from_screenshot` | `image_source`, `prompt` | `programming_language` | OCR/code extraction |
| `diagnose_error_screenshot` | `image_source`, `prompt` | `context` | Error and stack-trace diagnosis |
| `understand_technical_diagram` | `image_source`, `prompt` | `diagram_type` | Architecture/UML/flow analysis |
| `analyze_data_visualization` | `image_source`, `prompt` | `analysis_focus` | Chart and plot analysis |
| `ui_diff_check` | `expected_image_source`, `actual_image_source`, `prompt` | - | Visual regression comparison |
| `analyze_image` | `image_source`, `prompt` | - | General image analysis |
| `analyze_video` | `video_source`, `prompt` | - | Compatibility/experimental video entry |

Aliases: `image_analysis` → `analyze_image`; `video_analysis` → `analyze_video`.

## Response format

```json
{
  "provider": "codex",
  "model": "configured-model",
  "result": {
    "summary": "Short summary",
    "answer": "Direct answer",
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

`provider` and `model` may appear in the response for observability even though callers cannot
select them. bbox values are intended to use `[x_min, y_min, x_max, y_max]` on a `0..1000`
scale; strict range enforcement is not yet implemented.

## Provider notes

- **AGY**: stages images in an added directory, invokes `agy` with `--sandbox`, and discovers
  image capability from the real request. Headless image access can be non-deterministic, so
  failures may fall back to the next provider.
  The server invokes AGY headless (`-p`, non-interactive print mode), in which AGY **cannot
  prompt** for tool-permission confirmation and therefore **auto-denies** any tool action that
  would normally require one. AGY's own settings file (`~/.gemini/antigravity-cli/settings.json`)
  must set `"toolPermission": "proceed-in-sandbox"` and `"enableTerminalSandbox": true` so
  tool actions are auto-approved while still confined to the sandbox the server runs AGY in
  (`--sandbox`). Without these, AGY's headless run rejects the tool actions and the request
  falls back to the next provider.
- **Codex**: uses repeated `-i`, a read-only sandbox, and `--output-schema`.
- **Gemini**: uses `google-genai`, structured JSON, and multiple image parts.
- **OpenCode**: uses repeated `--file` and parses the JSON event stream.

CLI version compatibility is not currently pinned; run `doctor` after upgrading a provider CLI.

## Security

The safe deployment model is a trusted, single-user machine with both listeners bound to
`127.0.0.1`.

- Do not expose lm-vision-server port 6506 or lm-proxy port 8787 to a LAN or the public
  internet.
- The lm-vision-server `/tool` endpoint has no authentication.
- The proxy forwards Authorization/API-key headers to the upstream encoded in its path and can
  fetch HTTP(S) image URLs. An exposed proxy therefore creates SSRF and credential-forwarding
  risk; there is no configurable upstream allowlist or private-network policy yet.
- MCP local-path tools can read paths supplied by the caller; only trusted agents should have
  access to this server.
- Avoid plain API keys in YAML. The code avoids intentionally logging keys, but complete
  value-based log redaction is not implemented yet.

## Current limitations

- `analyze_video`/`video_analysis` are registered, but Codex, Gemini, and OpenCode reject video;
  AGY does not currently receive a dependable staged video reference. Treat video as unsupported.
- Proxy endpoint query strings are dropped.
- Proxy image parse errors fail open and forward the original image request.
- Proxy base64/data images need unified MIME and size validation.
- Proxy targets and remote image URLs have no configurable allowlist/private-network policy.
- Windows `stop`/`restart` process discovery is incomplete.
- Numeric configuration values are not yet range-validated.

## Development

```bash
.venv/bin/python -m pytest -q
```

The suite binds temporary loopback ports; in restricted sandboxes, socket-based
server/proxy tests can fail with `PermissionError` even though non-network tests pass.

## Troubleshooting

- **No provider succeeds**: run `lm-visual-mcp doctor`, disable unavailable providers, and check
  CLI authentication or the Gemini key.
- **AGY falls back unexpectedly**: run `lm-visual-mcp doctor --probe`; AGY headless image access
  may intermittently fail.
- **Codex blocks or cannot write**: the server closes stdin and intentionally uses a read-only
  sandbox.
- **MCP stdout is corrupted**: ensure wrappers and provider CLIs do not print into the MCP
  process stdout; project logs use stderr.
- **Proxy route returns 400/404**: verify the explicit protocol path and use unpadded base64url,
  not standard base64 containing `/`.
- **Proxy cannot reach upstream**: confirm the decoded base URL is complete and does not rely on
  a query string.
- **Auto Mode reports classifier unavailable**: verify that the request reached the Anthropic
  proxy and check whether the upstream rejected the long system prompt, cache-control, or thinking
  field; do not infer a network bypass from the UI message alone. If the upstream rejects disabled
  thinking, set `LM_VISUAL_MCP_PROXY_CLASSIFIER_DISABLE_THINKING=false`; response normalization
  remains enabled.

## License

MIT. See [`LICENSE`](LICENSE).