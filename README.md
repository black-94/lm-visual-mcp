# Vision MCP Server

> **English** | [简体中文](README.zh-CN.md)

`lm-visual-mcp` gives text-only LLMs and coding agents visual input through the
[Model Context Protocol](https://modelcontextprotocol.io). It is built from four
modules — `mcp`, `server`, `providers`, and `vision` — and also ships a transparent HTTP
proxy that rewrites image blocks in OpenAI or Anthropic requests into text descriptions
before forwarding them upstream, plus Claude Code Auto classifier interoperability.

This version (**v0.2.0**) supports **image recognition only**. Video input is no longer
declared or accepted anywhere in the stack.

## Modules

| Module | Responsibility |
| --- | --- |
| `mcp` | Thin stdio MCP entry. Every tool call is forwarded to the shared server — no embedded vision service, so rate limiting is always centralized. |
| `server` | The shared singleton process: `POST /vision/analyze` + the hook proxy (`/proxy/<proto>/<base64url>...`). Which hooks are active is pure configuration (`hooks.image.enabled`, `hooks.classifier.enabled`). |
| `providers` | Provider implementations behind a type registry with per-provider rate limiting (rpm / concurrency). Each provider implements one or both behavior groups: **IMAGE** (`probe_image` / `analyze_image`) and **CLASSIFIER** (`rewrite_classifier_request` / `rewrite_classifier_response`). |
| `vision` | Image-recognition orchestration: concurrency gate + the provider router that walks the configured `image_chain`. Prompts and the two behavior groups' shared types live here/next to providers. |

### Hooks

A hook's basic interface is `process(ctx) -> HookResult`: it may rewrite the request and
let it **continue** down the pipeline, or **intercept** it by returning a response that
goes straight back to the client. Hooks may also implement `process_response` to rewrite
the upstream response (used by the classifier hook).

- **Image hook** — detects image-bearing requests, describes each image once
  (SHA-256 cache) through the image chain, and replaces the image block with text.
  Every rewritten block records the image's **absolute local path**
  (`[Image N: /abs/path.png]`), and staged files persist, so the text model can reference
  or re-submit the image later.
- **Classifier hook** — detects Claude Code Auto classifier requests and delegates to the
  classifier chain. Only API providers that implement classifier handling
  (`rewrite_classifier_request` / `rewrite_classifier_response`) rewrite these; local CLI
  providers (agy, codex) pass them through byte-for-byte untouched.

Both hooks accept a `models` allowlist — empty = apply to all models, non-empty = only
the listed models run through the router; everything else passes through untouched.

### Providers, dual chains & fallback

The **top-level `providers:`** section defines provider *instances* (the single source of
truth, referenced by `name`). `vision` then references those names in **two independent
execution chains**:

- `image_chain` — image analysis fallback order (**first success wins**).
- `classifier_chain` — classifier handling order (**first provider that reports a changed
  rewrite wins**; if none implements classifier handling, requests pass through untouched).

The router never hardcodes providers — `type` is resolved through a registry, so adding
one is a class + one registry line + config.

Rate limiting lives **inside each provider** (`rate_limit: {rpm, concurrency}` per
entry, both optional). When a limit is hit the provider raises `rate_limited` and the
router immediately downgrades to the next provider in its chain.

- `agy` — AGY CLI (`-p` + `--add-dir` + sandbox), unsupported-vision verdict caching.
  IMAGE only; no classifier handling.
- `codex` — `codex exec` with `--output-schema`, read-only sandbox. IMAGE only.
- `gemini` — google-genai API (`api_key_env: GEMINI_API_KEY`). IMAGE + classifier
  (honors `disable_thinking`).
- `opencode` — direct OpenAI-compatible API; `mode: go` (default,
  `https://opencode.ai/zen/go/v1`) or `mode: zen`, `base_url` overrides mode.
  IMAGE + classifier; **no local CLI required**.
- `volcengine` — Volcano Ark; `mode: agent` (Anthropic Messages `/v1/messages` over
  `api/plan`), `mode: coding` (`api/coding`), or `mode: api` (OpenAI chat-completions
  `api/v3`). IMAGE + classifier.

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
    │                          └── router walks image_chain: provider₁ → provider₂ → …
    │                                (each with its own rpm/concurrency limiter;
    │                                 limit hit → fall back to the next)
    └── hook proxy  /proxy/<proto>/<base64url>[/suffix]
           ├── image hook      → image chain (description rewrite, model-allowlist)
           ├── classifier hook → classifier chain (API-provider rewrite, model-allowlist /
           │                      byte-level passthrough when no provider handles it)
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
to configure the listen address, hooks, providers and the two chains. Root nodes are
`server` / `hooks` / `providers` / `vision` / `media` / `logging`. There is no `mcp:`
section in the file — the start-server decision belongs to the agent's MCP config, not
to the YAML.

Example (top-level `providers` defines instances; `vision` declares the chains):

```yaml
server:
  host: 127.0.0.1
  port: 8787

hooks:
  image:      { enabled: true, models: [] }   # models empty = all models
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
  image_chain: [agy, gemini, opencode]      # first success wins
  classifier_chain: [gemini]                # only API providers belong here
```

Local CLI providers (agy, codex) have no classifier handling; put only API providers
(gemini / opencode / volcengine) on the `classifier_chain`.

## Tools

`ui_to_artifact`, `extract_text_from_screenshot`, `diagnose_error_screenshot`,
`understand_technical_diagram`, `analyze_data_visualization`, `ui_diff_check`,
`analyze_image` (+ `image_analysis` alias). Provider, model, credentials, fallback
policy, timeout are server configuration and never appear in tool schemas.
