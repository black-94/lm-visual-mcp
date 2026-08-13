# Vision MCP Server

> **English** | [**简体中文**](README.zh-CN.md)

A **Vision MCP Server** that gives text-only LLMs / coding agents visual
capabilities over the [Model Context Protocol](https://modelcontextprotocol.io).
It also ships a **Vision Proxy** — a transparent HTTP forwarder that lets a
text-only model's normal API client "see" images without any code change.

---

## Table of Contents

- [Why a Vision MCP?](#why-a-vision-mcp)
- [Architecture](#architecture)
- [Vision Proxy](#vision-proxy)
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Lifecycle commands](#lifecycle-commands)
- [MCP client configuration](#mcp-client-configuration)
- [Configuration](#configuration)
- [Tools](#tools)
- [Structured output](#structured-output)
- [Doctor](#doctor)
- [Provider detection](#provider-detection)
- [Security](#security)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## Why a Vision MCP?

Most coding agents / text-only LLMs cannot "see" screenshots, error traces, UI
mockups or diagrams. This server acts as their eyes:

```text
Text-only LLM
      │  MCP
      ▼
Vision MCP Server
   ├── Z.AI-compatible Vision Tools
   ├── Specialized Prompt Layer
   ├── Media / Workspace Layer
   ├── Structured JSON Layer
   └── Provider Router (AGY → Codex → Gemini → OpenCode)
```

```
Text-only LLM
      │  HTTP (base_url points at the proxy)
      ▼
Vision Proxy (openai/chat · openai/responses · anthropic)
      │  no image => byte-level passthrough; image => describe + rewrite to text
      ▼
Real OpenAI / Anthropic API
```

---

## Architecture

Two cooperating singletons serve the whole setup:

1. **Shared daemon** — a single global instance that owns the one `VisionSession`
   and every MCP vision tool. The default CLI entry is a *client* that forwards
   tool calls over loopback HTTP to this daemon. It serializes requests through a
   concurrency semaphore and reclaims itself after `idle_timeout_ms` of no
   traffic.

2. **Vision Proxy** — the transparent forwarder described above, serving the
   agent's text-model client. It is also a singleton.

Each is identified by a pidfile under `~/.cache/lm-visual-mcp/` plus the PID
listening on its port. See [Lifecycle commands](#lifecycle-commands).

---

## Vision Proxy

### How to point at it

Point the agent's `base_url` at the proxy and nothing else. The proxy forwards
to the real API URL that is decoded from the path.

```text
http://127.0.0.1:8787/proxy/<protocol-path>/<base64url(base API URL)>[<SDK suffix>]
```

- `<protocol-path>`: `openai/chat`, `openai/responses`, `anthropic` — **explicit**,
  never inferred from the URL or the request body.
- `<base64url>`: base64url of the **base API URL** (the host, optionally with a
  path prefix, e.g. `https://api.openai.com` or `https://.../api/plan`).
- No querystring — only base64url.
- **SDK-suffix rebasing**: the Anthropic SDK appends the endpoint path (e.g.
  `/v1/messages`) to `base_url`. The proxy matches the protocol path as a prefix,
  scans the remaining segments for the first one that decodes to a full http(s)
  URL (the base), and **appends the later segments back onto it** before
  forwarding — the final upstream path is base + SDK suffix (e.g.
  `https://.../api/plan/v1/messages`). A raw curl with no suffix forwards the
  base URL as-is.

```text
http://127.0.0.1:8787/proxy/openai/chat/<b64-encoded-full-api-url>
http://127.0.0.1:8787/proxy/openai/responses/<b64-encoded-full-api-url>
http://127.0.0.1:8787/proxy/anthropic/<b64-encoded-full-api-url>
```

### Core flow

1. **No image** → byte-level passthrough: only hop-by-hop headers are stripped;
   `Authorization` / `x-api-key` and the body pass through untouched.
2. **Image present** → parse + extract images → per-image **SHA-256 cache** lookup
   (hit = reuse; miss = one **batched** vision call) → rewrite the image parts into
   text → forward.
3. The response (including SSE) is always piped back untouched.

### Constraints

- Only **OpenAI + Anthropic** protocols; both OpenAI formats (Chat + Responses).
- No `system_prompt` / `user_prompt` extraction: one generic describe (a "sensor")
  only. Deeper digging is left to the text model calling the MCP vision tools itself.
- Multi-image submitted in one request, one batched describe; cache granularity =
  **per image** (SHA-256), vision-call granularity = **per request**.
- describe reuses the existing `image_analysis.SYSTEM_PROMPT` and the Provider
  Router — the same provider chain and fallback.
- The only new dependency is `aiohttp`.

### Why it exists

MCP tools must be called explicitly by the agent; the text model's own API client
cannot "see". The proxy turns vision into a base_url swap, giving ordinary LLM
calls vision for free.

---

## Features

- 8 Z.AI-compatible vision tools + 2 aliases.
- Provider-neutral tools: **no** `provider`/`model`/`api_key`/`workdir`/`timeout`
  in tool schemas — those are server config.
- Provider Router with configurable order and fallback policy.
- Unified structured JSON output (observations, texts, elements, bbox).
- Local paths and HTTP(S) URLs; `file://` rejected.
- Per-task isolated workspaces; automatic cleanup.
- CLI-native images (Codex `-i`, OpenCode `--file`) and AGY workspace staging
  with vision-capability detection.
- Gemini API via `google-genai`.
- `lm-visual-mcp doctor` environment inspection + `--probe` vision smoke test.
- **Transparent Vision Proxy** (OpenAI / Anthropic protocols, auto-describe + cache).
- **Lifecycle commands** `start` / `stop` / `restart`.
- No ACP / no transport abstraction in v1.

---

## Requirements

- Python **3.11+**
- macOS / Linux / Windows

---

## Installation

### Method A: pip (venv recommended)

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

### Method B: uv (faster)

```bash
uv venv --python 3.11 .venv
source .venv/bin/activate            # Windows: .venv\Scripts\Activate.ps1
uv pip install -e ".[dev]"           # dev = pytest, pytest-asyncio, Pillow (for doctor --probe)
```

### Verify

```bash
lm-visual-mcp --version      # binary on PATH
lm-visual-mcp doctor         # inspect the 4 providers: enabled / executable / model
lm-visual-mcp doctor --probe # optional: real AGY vision smoke test (needs Pillow)
```

> On Windows, if `lm-visual-mcp` isn't on PATH, use `python -m lm_visual_mcp`.

---

## Quick start

### 1) Prepare config

```bash
# Copy the example config and edit it to suit your machine.
cp config.example.yaml ~/.config/lm-visual-mcp/config.yaml

# Edit: enable at least one provider, set the Gemini API key (see below).
```

### 2) Run as an MCP stdio server

```bash
lm-visual-mcp --config ~/.config/lm-visual-mcp/config.yaml
# or
python -m lm_visual_mcp --config ~/.config/lm-visual-mcp/config.yaml
```

No matter how many Claude Code sessions open, only **one** shared daemon runs
(global single instance on `runtime.host:runtime.port`). Each session probes for
it, proxies to it if present, or auto-starts it, then reuses it. Requests beyond
`runtime.max_concurrency` queue inside the daemon. The daemon exits itself after
`runtime.idle_timeout_ms` of no traffic.

> **Auto-launch (singleton)** On MCP client start, both the daemon and the vision
> proxy singletons are auto-started (probe-then-launch). Vision works out of the box.

### 3) Point a text-model client at the proxy

Point the client's `base_url` at the proxy (works for OpenAI or Anthropic):

```text
http://127.0.0.1:8787/proxy/anthropic/<base64url(https://api.anthropic.com)>
```

For example, Claude Code can point `ANTHROPIC_BASE_URL` at it so its text model
"sees" images automatically. See [Vision Proxy](#vision-proxy) for the URL format
and constraints.

---

## Lifecycle commands

By default MCP auto-starts them, but you can manage the two singletons
independently:

```bash
lm-visual-mcp start    [--service daemon|proxy]   # probe-then-launch, idempotent
lm-visual-mcp stop     [--service daemon|proxy]   # SIGTERM, idempotent
lm-visual-mcp restart  [--service daemon|proxy]   # stop then start
```

- Without `--service`, both are managed (`stop`/`restart` stop proxy before daemon).
- `stop` prefers pidfile + process-cmdline check (avoids killing an unrelated
  process that reused the PID), falling back to the PID listening on the port.
- The pidfile is written only after a successful bind, and removed on `stop`.

---

## MCP client configuration

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

## Configuration

Configuration priority: **CLI argument > environment variable > config file >
built-in default**.

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
  workdir: null          # null => temporary dir per task
  timeout: 120
  max_concurrency: 2
  host: 127.0.0.1        # singleton daemon bind host
  port: 6506             # singleton daemon bind port
  idle_timeout_ms: 300000 # daemon idle-exit timeout

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

# Transparent vision proxy (lm-visual-mcp proxy)
proxy:
  host: 127.0.0.1
  port: 8787
```

### Provider order

The router tries providers in order and falls back on failure. Default:
`agy → codex → gemini → opencode`.

Not fallback-eligible by default: `invalid_input`, `invalid_model`, `config_error`.
The `fallback.on` list is the final authority.

### Provider model

Each provider's model is set in config and used automatically on fallback — there
is no cross-provider model namespace to manage. Set a model to `null` to let the
provider use its own default.

```yaml
providers:
  agy:      { model: gemini-xxx }
  codex:    { model: gpt-xxx }
  gemini:   { model: gemini-xxx }
  opencode: { model: google/gemini-xxx }
```

### Provider effort

Each provider's reasoning effort is configured with `effort`
(`low` | `medium` | `high` | `xhigh`, provider-dependent; `null` = provider default)
and passed through at runtime:

- **AGY** → `--effort`
- **Codex** → `-c model_reasoning_effort=<effort>`
- **Gemini** → `thinking_config` (thinking level)
- **OpenCode** → `--variant`

### Gemini API key

API keys are **never** tool arguments. Resolution order:

```text
LM_VISUAL_MCP_GEMINI_API_KEY
    > config.providers.gemini.api_key_env (the env var it names)
    > GEMINI_API_KEY
```

For compatibility a plain `api_key` may be placed in the config file; it is stored
as a `SecretStr`, never printed, never dumped, never returned in MCP responses, and
never included in exceptions. Prefer the environment variable.

### Environment variables

```text
LM_VISUAL_MCP_CONFIG                  config file path
LM_VISUAL_MCP_WORKDIR                runtime workdir
LM_VISUAL_MCP_TIMEOUT                runtime timeout (s)
LM_VISUAL_MCP_MAX_CONCURRENCY        max concurrency (requests beyond this queue)
LM_VISUAL_MCP_HOST                   daemon bind host (default 127.0.0.1)
LM_VISUAL_MCP_PORT                   daemon bind port (default 6506)
LM_VISUAL_MCP_IDLE_TIMEOUT_MS        daemon idle-exit timeout (default 300000)
LM_VISUAL_MCP_AGY_COMMAND / _MODEL / _EFFORT
LM_VISUAL_MCP_CODEX_COMMAND / _MODEL / _EFFORT
LM_VISUAL_MCP_GEMINI_MODEL / _API_KEY / _EFFORT
GEMINI_API_KEY                       gemini API key (fallback)
LM_VISUAL_MCP_OPENCODE_COMMAND / _MODEL / _EFFORT
LM_VISUAL_MCP_PROXY_HOST             proxy bind host (default 127.0.0.1)
LM_VISUAL_MCP_PROXY_PORT             proxy bind port (default 8787)
LM_VISUAL_MCP_LOG_LEVEL              ERROR | WARNING | INFO | DEBUG
```

### Workdir

With `runtime.workdir: null` (default), every task gets a brand-new temporary
directory cleaned up on completion. With a project workdir configured, task media
is staged under `<workdir>/.lm-visual-mcp/<uuid>/` and removed after. User files
are never modified or deleted.

### Media limits

Images: png/jpg/jpeg/webp/gif/bmp/tiff (default `max_image_mb: 20`). Videos:
mp4/mov/m4v (`max_video_mb: 8`). Remote downloads are bounded by timeout, size and
a redirect limit, and validated by MIME type.

---

## Tools

| Tool | Purpose |
|------|---------|
| `ui_to_artifact` | Convert a UI screenshot into `code` / `prompt` / `spec` / `description` |
| `extract_text_from_screenshot` | Verbatim OCR of code / terminal / config / docs |
| `diagnose_error_screenshot` | Diagnose error / stack trace / root cause / fix |
| `understand_technical_diagram` | Understand architecture / flowchart / UML / ER diagrams |
| `analyze_data_visualization` | Analyze charts: trends, anomalies, comparisons |
| `ui_diff_check` | Compare EXPECTED vs ACTUAL UI for visual regression |
| `analyze_image` | General visual analysis |
| `analyze_video` | Video analysis (mp4/mov/m4v) |

Aliases share the same implementations: `image_analysis` → `analyze_image`,
`video_analysis` → `analyze_video`.

---

## Structured output

Every provider's result is normalized into one schema and wrapped in a standard
envelope:

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

`bbox` is normalized to `0..1000` as `[x_min, y_min, x_max, y_max]`. When a value
can't be determined, providers do not guess — they omit it and add a warning.

---

## Doctor

```bash
lm-visual-mcp doctor
lm-visual-mcp doctor --probe   # also runs a real AGY vision smoke test (needs Pillow)
lm-visual-mcp --version
```

`doctor` never prints API key contents.

---

## Provider detection

- **AGY**: `agy -p "<prompt>" --output-format json`. Images are staged into the
  workspace media dir and referenced by bare filename. AGY ignores the shell cwd
  and always runs its tools in its own workspace, so the media dir is registered
  with `--add-dir` (repeatable); files in an added dir are readable natively. The
  server launches AGY in a sandbox (`--sandbox`). There is no separate vision
  probe — every image request is exactly one real AGY call, and vision capability
  is discovered from that call's result and cached.
- **Codex**: `codex exec -i <img> ... --output-schema ... -s read-only`. Images
  passed natively; read-only sandbox enforced.
- **Gemini**: `google-genai`, structured JSON, multi-image, configured model.
- **OpenCode**: `opencode run --format json`, images via `--file`, JSON event
  stream parsed for the final assistant result.

> **AGY non-determinism**: AGY reads images from the dir registered with
> `--add-dir`. As of AGY CLI 1.1.x headless mode is still non-deterministic — a run
> may intermittently reach for a tool permission it does not hold. When that happens
> the server detects it and transparently falls back to the next provider.
> `lm-visual-mcp doctor --probe` reports the capability without failing the server.

---

## Security

The server only `LOOK / READ / UNDERSTAND / COMPARE / ANALYZE` — it never
`EDIT / BUILD / EXECUTE / MODIFY`. Codex runs in a read-only sandbox; AGY and
OpenCode are never launched with dangerous auto-approval. API keys are redacted
from all logs and responses.

The proxy forwards API keys and bodies untouched (only hop-by-hop headers are
stripped); it holds no account state of its own.

---

## Development

```bash
python -m pytest
```

Tests cover config, router, workspace, media, all four providers (subprocess /
genai mocked), Z.AI tool-schema compatibility, the proxy adapters + cache, and an
MCP `tools/list` + `tools/call` smoke test.

---

## Troubleshooting

- **`agy` falls back to codex for images** — AGY reads images from the dir
  registered with `--add-dir` (the media dir is added automatically). Headless
  mode is non-deterministic and may intermittently auto-deny a tool permission.
  The media dir is readable natively, so no `read_file` or `command(ls)` grant
  is needed and `command(*)` must never be configured. When AGY still fails, the
  server falls back transparently. Run `lm-visual-mcp doctor --probe` to exercise
  AGY directly.
- **Nothing responds** — no provider is `enabled`. Enable providers in config.
- **Gemini not used** — an API key is required; see "Gemini API key".
- **Codex blocks on stdin** — the server always closes stdin for CLI providers.
- **stdout corruption** — all logs go to stderr; stdout is reserved for MCP.
- **Proxy not forwarding** — confirm the base_url uses
  `/proxy/<protocol-path>/<base64url(base API URL)>` and the singletons are up
  (`lm-visual-mcp start` / `lm-visual-mcp doctor`).

---

## License

MIT