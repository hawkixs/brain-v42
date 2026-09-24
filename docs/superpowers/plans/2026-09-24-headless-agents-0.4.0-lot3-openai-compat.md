# headless-agents 0.4.0 — Lot 3 (`openai-compat`) Implementation Plan

**Goal:** Ship spec section 3.2: an `openai-compat` provider, text-only chat completions over HTTP, with the presets `openrouter`, `mistral` and `nvidia`, reachable through the registry facade.

**Architecture:** One provider class, `providers.openai_compat.OpenAICompatProvider`, instantiated per registry name (a preset or the generic one). Each call runs `python -I -m headless_agents.providers._openai_worker` as a killable child in its own session: the parent writes one JSON envelope (endpoint, key, request body, socket timeout) on its stdin and reads one JSON outcome on its stdout. Standard library only (`urllib`); no CLI rail and no Dream code path is touched.

**Spec:** `docs/specs/2026-09-23-headless-agents-0.4.0-design.md` 3.2 (approved by the merge of PR #187; Brain ticket `8ebebf41`). Lots 1 (#190) and 2 (#192) are merged.

## Decisions where the spec is silent

| # | Question | Decision |
|---|---|---|
| 1 | Where the generic provider takes its endpoint | `RunSpec.extra["base_url"]` and `RunSpec.extra["key_env"]` (the variable NAME) — the runtime side of lot 4's `--base-url` / `--key-env`. A preset given either is a usage error (`2`), as the CLI will be: the runtime never silently ignores a caller's endpoint. |
| 2 | A `base_url` the provider accepts | `http` or `https`, a host, no credentials (a key in the URL would be written wherever the URL is), no query, no fragment. Plain `http` stays allowed: a LAN inference server is a legitimate `openai-compat` target. |
| 3 | A missing key | `1`, named by its variable, nothing sent. A configuration error, like 401/403 — it stops the chain. `probe()` reports the same fact at zero quota, so a caller can pre-filter a chain. |
| 4 | Where the context bundle goes | A `system` message carrying `ContextBundle.preamble()`, the prompt as the `user` message (spec 3.3: "openai-compat as a system message"). |
| 5 | Unknown keys in `RunSpec.extra` | A usage error (`2`): the spec limits request options to `response_format`, `temperature`, `max_tokens`; a typo or a `stream` must not be silently dropped. |
| 6 | Whose timeout wins | The parent's. The worker's socket timeout is the effective timeout plus 5 s, so a deadline is always the parent's `124`, never a category the worker made up a moment before. |
| 7 | Connection failures | Every `URLError` that is not a timeout (DNS, refused, reset, TLS) is `unreachable` → `3`: nothing was sent that a tool could have acted on, and an HTTP run has no tool anyway, so the chain may advance. |
| 8 | A `200` without `choices[0].message` | `1` (malformed), not an empty answer. An empty `content` is exit `0` with `text=None`, as on the CLI rails. |
| 9 | What is logged | `events.jsonl`: one line `{ok, category, status}`. `stderr.log`: `request failed: <category> (HTTP <status>)`. `report.log`: the answer. Never a response body on failure, never the key. |
| 10 | Tokens | `input`/`output` from `usage`; `cached` from `prompt_tokens_details.cached_tokens`; `fresh = input - cached` only when both are reported; `thinking` from `completion_tokens_details.reasoning_tokens`. Absent is `None`, never `0`. |
| 11 | Cost | Only the `openrouter` preset sends `usage: {include: true}`; `cost_usd` is read from `usage.cost` whenever a response carries it, `None` otherwise. |
| 12 | The worker's own defence | It refuses any URL that is not `http(s)` before `urlopen` (which would read `file:`, `data:`, `ftp:`), independently of the parent's validation. |

## Tasks (TDD: each test seen failing first)

1. `tests/unit/headless_agents/test_provider_openai_compat.py` against a real loopback HTTP server and the real worker process: success (text, tokens, model reported, `report.log`, `result.json`); request shape (bearer, system + user messages, options, no `stream`/`tools`); 429/500/503 → `3`, 401/403/400/404 → `1`, refused connection → `3`, malformed → `1`; own deadline and a passed `deadline` → `124` within bound; the key in no file even when the error body echoes it; the key neither in argv nor in the child environment; missing key → `1` with no request; `mcp`/`workspace` → `ValueError`; invalid generic configuration and empty model → `2`; presets refuse `base_url`/`key_env`; preset table; `usage.include` for OpenRouter only; cost parsing; the worker refuses non-http URLs.
2. `providers/_openai_worker.py`, then `providers/openai_compat.py`.
3. Registry: `PROVIDER_NAMES` becomes the spec's eight names (the lot-1 test pinning four is updated with it — the lot-1 plan says lot 3 extends it); `HTTP_PROVIDER_NAMES`; `probe(name, environ=None)` checks the key variable; `max_prompt_bytes` is `None` for HTTP.
4. `tests/live/headless_agents/test_openai_compat_live.py` (marked `live`, `HA_LIVE=1`, skipped per preset without its key): an answer with measured usage; a refused key → `1` and never written.
5. README section, CHANGELOG entry. Gates: `ruff check .`, `ruff format --check .`, `mypy src/ packages/headless-agents/src/`, `bandit -ll`, `pytest tests/unit`.

## Live measurement (2026-09-24, before the PR)

- Refused key → exit `1`, key absent from every file: **passed on all three real endpoints** (OpenRouter, Mistral, NVIDIA).
- Own deadline → `124` at 60 s: measured on NVIDIA.
- Successful answer: **not measured**. No `OPENROUTER_API_KEY` or `MISTRAL_API_KEY` on the machine; the available NVIDIA key timed out at 90 s on two models even through a bare `urllib` call outside this package (and 404 on a third), so the endpoint, not the provider, was unresponsive. To be replayed by the operator with the three keys before the 0.4.0 tag.
