# Vision MCP Server

> **English** | [简体中文](README.zh-CN.md)

`lm-visual-mcp` gives text-only LLMs and coding agents visual input through the
[Model Context Protocol](https://modelcontextprotocol.io). It is built from three
modules — `vision`, `server`, and `mcp` — and also ships a transparent HTTP proxy that
rewrites image blocks in OpenAI or Anthropic requests into text descriptions before
forwarding them upstream, plus Claude Code Auto classifier interoperability.

This version (**v0.2.0**) supports **image recognition only**. Video input is no longer
declared or accepted anywhere in the stack.

## Modules

| Module | Responsibility |
| --- | --- |
| `mcp` | Thin stdio MCP entry. Every tool call is forwarded to the shared server — no embedded vision service, so rate limiting is always centralized. |
| `server` | The shared singleton process: `POST /vision/analyze` + the hook proxy (`/proxy/<proto>/<base64url>...`). Which hooks are active is pure configuration (`server.image_hook.enabled`, `server.classifier_hook.enabled`). |
| `vision` | Image-recognition capability: a provider chain behind a type registry with per-provider rate limiting (rpm / concurrency) and ordered fallback. |

### Hooks

A hook's basic interface is `process(ctx) -> HookResult`: it may rewrite the request and
let it **continue** down the pipeline, or **intercept** it by returning a response that
goes straight back to the client. Hooks may also implement `process_response` to rewrite
the upstream response (used by the classifier hook).

- **Image hook** — detects image-bearing requests, describes each image once
  (SHA-256 cache) through the vision chain, and replaces the image block with text.
  Every rewritten block records the image's **absolute local path**
  (`[Image N: /abs/path.png]`), and staged files persist, so the text model can reference
  or re-submit the image later.
- **Classifier hook** — disables thinking on Claude Code Auto classifier requests and
  restores stop-sequence framing on stage-1 verdict responses.

### Vision providers & fallback

Providers are configured as a list of `{name, type, ...}` entries; list order is the
fallback order. The router never hardcodes providers — `type` is resolved through a
registry, so adding one is a class + one registry line + config.

Rate limiting lives **inside each provider** (`rate_limit: {rpm, concurrency}` per
entry, both optional). When a limit is hit the provider raises `rate_limited` and the
router immediately downgrades to the next provider in the chain.

- `agy` — AGY CLI (`-p` + `--add-dir` + sandbox), unsupported-vision verdict caching.
- `codex` — `codex exec` with `--output-schema`, read-only sandbox.
- `gemini` — google-genai API (`api_key_env: GEMINI_API_KEY`).
- `opencode` — direct OpenAI-compatible API (default `https://opencode.ai/zen/v1`,
  `api_key_env: OPENCODE_API_KEY`); **no local CLI required**.

## Architecture

```text
MCP client process (agent config: --start-server / --no-start-server)
    │ stdio
    ▼
mcp module (thin client)
    │ loopback HTTP  POST /vision/analyze
    ▼
server module (shared singleton)
    ├── vision endpoint ──► vision module
    │                          ├── concurrency gate
    │                          └── chain: provider₁ → provider₂ → …
    │                                (each with its own rpm/concurrency limiter;
    │                                 limit hit → fall back to the next)
    └── hook proxy  /proxy/<proto>/<base64url>[/suffix]
           ├── hooks: image rewrite / classifier compat (each toggleable)
           └── byte-level passthrough when no hook applies
```

## Quick start

```bash
# agent MCP config (stdio) — starts the shared server if absent:
lm-visual-mcp

# …or never start the server from the MCP process (use an already-running one):
lm-visual-mcp --no-start-server      # env: LM_VISUAL_MCP_START_SERVER=0

lm-visual-mcp start | stop | restart  # manage the server singleton
lm-visual-mcp server                  # run the server in the foreground
lm-visual-mcp doctor                  # inspect configuration and providers
```

Copy `config.example.yaml` to `lm-visual-mcp.yaml` (or `~/.config/lm-visual-mcp/`)
to configure providers, rate limits, hooks and the listen address. There is no `mcp:`
section in the file — the start-server decision belongs to the agent's MCP config, not
to the YAML.

## Tools

`ui_to_artifact`, `extract_text_from_screenshot`, `diagnose_error_screenshot`,
`understand_technical_diagram`, `analyze_data_visualization`, `ui_diff_check`,
`analyze_image` (+ `image_analysis` alias). Provider, model, credentials, fallback
policy, timeout are server configuration and never appear in tool schemas.
