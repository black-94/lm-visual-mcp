# Vision MCP Server

A **Vision MCP Server** that gives text-only LLMs / coding agents visual
capabilities over the [Model Context Protocol](https://modelcontextprotocol.io).

The server exposes Z.AI-compatible vision tools (`analyze_image`,
`extract_text_from_screenshot`, `ui_diff_check`, ...) and routes every request
through a configurable chain of visual providers (AGY, Codex, Gemini API,
OpenCode) with automatic fallback. Provider, model, API key and fallback order
are **server policy** — the LLM never sees or chooses them.

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
- No ACP / no transport abstraction in v1.

## Requirements

- Python **3.11+**
- macOS / Linux / Windows

## Installation

```bash
pip install -e .
```

Or with a virtualenv + uv:

```bash
uv venv --python 3.11 .venv
source .venv/bin/activate
uv pip install -e ".[dev]"   # dev = pytest, pytest-asyncio, Pillow (for doctor --probe)
```

## Quick start

First verify the install:

```bash
lm-visual-mcp --version    # confirm the binary is on PATH
lm-visual-mcp doctor       # inspect the 4 providers: enabled / executable / model
```

Then copy the example config and edit it to suit your machine:

```bash
# Copy the example config and edit it to suit your machine.
cp config.example.yaml ~/.config/lm-visual-mcp/config.yaml

# Run as an MCP stdio server
lm-visual-mcp --config ~/.config/lm-visual-mcp/config.yaml

# Or
python -m lm_visual_mcp --config ~/.config/lm-visual-mcp/config.yaml
```

### MCP client configuration

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
```

### Provider order

The router tries providers in the configured order and falls back on failure.
Default: `agy → codex → gemini → opencode`.

Not fallback-eligible by default: `invalid_input`, `invalid_model`,
`config_error`. The `fallback.on` list is the final authority.

### Provider model

Each provider's model is set in config and used automatically on fallback —
there is no cross-provider model namespace to manage.

```yaml
providers:
  agy:      { model: gemini-xxx }
  codex:    { model: gpt-xxx }
  gemini:   { model: gemini-xxx }
  opencode: { model: google/gemini-xxx }
```

Set a model to `null` to let the provider use its own default.

### Provider effort

Each provider's reasoning effort is configured with `effort`
(`low` | `medium` | `high` | `xhigh`, provider-dependent; `null` = provider
default). It is passed through to the backing CLI/API at runtime:

- **AGY** → `--effort` (AGY model names embed effort, so a bare base model like
  `gemini-3.6-flash` requires an explicit `--effort`; `gemini-3.6-flash` +
  `high` resolves to "Gemini 3.6 Flash (High)").
- **Codex** → `-c model_reasoning_effort=<effort>`.
- **Gemini** → `thinking_config` (thinking level).
- **OpenCode** → `--variant`.

```yaml
providers:
  agy:      { model: gemini-3.6-flash, effort: high }
  codex:    { model: gpt-5.6-luna,     effort: high }
  gemini:   { model: gemini-3.6-flash, effort: high }
  opencode: { model: null,             effort: null }
```

### Gemini API key

API keys are **never** tool arguments. Resolution order:

```text
LM_VISUAL_MCP_GEMINI_API_KEY
    > config.providers.gemini.api_key_env (the env var it names)
    > GEMINI_API_KEY
```

For compatibility a plain `api_key` may be placed in the config file; it is
stored as a `SecretStr`, never printed, never dumped, never returned in MCP
responses, and never included in exceptions. Prefer the environment variable.

### Environment variables

```text
LM_VISUAL_MCP_CONFIG                 config file path
LM_VISUAL_MCP_WORKDIR                runtime workdir
LM_VISUAL_MCP_TIMEOUT                runtime timeout (s)
LM_VISUAL_MCP_MAX_CONCURRENCY        max concurrency
LM_VISUAL_MCP_AGY_COMMAND            agy executable
LM_VISUAL_MCP_AGY_MODEL              agy model
LM_VISUAL_MCP_AGY_EFFORT             agy reasoning effort
LM_VISUAL_MCP_CODEX_COMMAND          codex executable
LM_VISUAL_MCP_CODEX_MODEL            codex model
LM_VISUAL_MCP_CODEX_EFFORT           codex reasoning effort
LM_VISUAL_MCP_GEMINI_MODEL           gemini model
LM_VISUAL_MCP_GEMINI_API_KEY         gemini API key
LM_VISUAL_MCP_GEMINI_EFFORT          gemini reasoning effort
GEMINI_API_KEY                       gemini API key (fallback)
LM_VISUAL_MCP_OPENCODE_COMMAND       opencode executable
LM_VISUAL_MCP_OPENCODE_MODEL         opencode model
LM_VISUAL_MCP_OPENCODE_EFFORT        opencode reasoning effort
LM_VISUAL_MCP_LOG_LEVEL              ERROR | WARNING | INFO | DEBUG
```

### Workdir

With `runtime.workdir: null` (default), every task gets a brand-new temporary
directory that is cleaned up on completion. With a project workdir configured,
task media is staged under `<workdir>/.lm-visual-mcp/<uuid>/` and removed after.
User files are never modified or deleted.

### Media limits

Images: png/jpg/jpeg/webp/gif/bmp/tiff (default `max_image_mb: 20`). Videos:
mp4/mov/m4v (Z.AI-compatible default `max_video_mb: 8`). Remote downloads are
bounded by timeout, size and a redirect limit, and validated by MIME type.

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

## Structured output

Every provider's result is normalized into one schema and wrapped in a
standard envelope:

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

`bbox` is normalized to `0..1000` as `[x_min, y_min, x_max, y_max]`. When a
value can't be determined, providers do not guess — they omit it and add a
warning.

## Doctor

```bash
lm-visual-mcp doctor
lm-visual-mcp doctor --probe   # also runs a real AGY vision smoke test (needs Pillow)
lm-visual-mcp --version
```

`doctor` never prints API key contents.

## Provider detection

- **AGY**: `agy -p "<prompt>" --output-format json`. Images are staged into the
  workspace media dir and referenced by bare filename. AGY ignores the shell cwd
  and always runs its tools in its own workspace, so the media dir is registered
  with `--add-dir` (repeatable); files in an added dir are readable natively —
  no `read_file` grant, no `command(ls)` grant, and never `command(*)`. The
  server launches AGY in a sandbox (`--sandbox`) so any command it runs is
  confined. There is no separate vision probe: every image request is exactly
  one real AGY call, and vision capability is discovered from that call's result
  and cached. If headless AGY produces no output (a tool permission it needs was
  auto-denied), the request raises `unsupported_media` and falls back, and the
  result is cached so later image requests fail fast instead of re-calling AGY.
- **Codex**: `codex exec -i <img> ... --output-schema ... -s read-only`. Images
  passed natively; read-only sandbox enforced.
- **Gemini**: `google-genai`, structured JSON, multi-image, configured model.
- **OpenCode**: `opencode run --format json`, images via `--file`, JSON event
  stream parsed for the final assistant result.

> **AGY non-determinism**: AGY reads images from the dir registered with
> `--add-dir`. As of AGY CLI 1.1.x, headless mode is still non-deterministic — a
> run may intermittently reach for a tool permission it does not hold. When that
> happens the server detects it and transparently falls back to the next
> provider. `lm-visual-mcp doctor --probe` reports the capability without failing
> the server.

## Security

The server only `LOOK / READ / UNDERSTAND / COMPARE / ANALYZE` — it never
`EDIT / BUILD / EXECUTE / MODIFY`. Codex runs in a read-only sandbox; AGY and
OpenCode are never launched with dangerous auto-approval. API keys are redacted
from all logs and responses.

## Development

```bash
python -m pytest
```

Tests cover config, router, workspace, media, all four providers (subprocess /
genai mocked), Z.AI tool-schema compatibility, and an MCP `tools/list` +
`tools/call` smoke test.

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

## License

MIT