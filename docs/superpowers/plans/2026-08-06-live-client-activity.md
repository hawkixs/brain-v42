# Live client activity for the brain — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace red-monitor's "Codex activity" panel with a "Live workload" panel that shows one row per brain client session, merging the agents' OTLP telemetry with activity observed on the brain side.

**Architecture:** Two processes. The MCP server (`:8765`) observes its callers in `ProvenanceMiddleware` and pushes bounded observations over loopback to the metrics sidecar (`:9200`). The sidecar also receives OTLP from the CLIs (Codex and Claude Code, two decoders), hashes every identifier on reception with its process secret, and merges everything into a single bounded registry exposed by `/api/cockpit`. red-monitor re-proxies with no Go change; only the SolidJS panel moves.

**Tech Stack:** Python 3.12, aiohttp (sidecar), FastMCP 3.x (middleware), httpx (emitter), pytest / pytest-asyncio, SolidJS + Vitest (red-monitor).

**Spec:** `docs/superpowers/specs/2026-08-06-live-client-activity-design.md`
**Ticket brain:** `2dfbb83d-f6cf-4570-9b13-502acc8c776c`

## Global Constraints

- **TDD mandatory.** No implementation code without a test that fails first. Never modify a test to make code pass.
- **Green before commit**, in this exact order: `pytest tests/unit`, `ruff check src/ tests/`, `ruff format --check src/ tests/`, `mypy src/`. CI runs `ruff format --check` — `ruff check` alone is not enough.
- **Atomic commits, Conventional Commits**, messages in French like the rest of the repo. End every message with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- **Type hints everywhere**, `from __future__ import annotations` at the top of every module.
- **Never `print`**: `structlog` only.
- **`null`, never `0`, for any field without a data source.** Doctrine already written in `cockpit.py`: a cosmetic `0.0` is indistinguishable from a real measured zero.
- **Every sidecar HTTP receiver is loopback-only and bounded**, with the hardening of `/v1/logs`: non-loopback rejected with 403, oversize body with 413, saturation with 503, malformed with 400.
- **No raw identifier ever leaves in the payload.** Hashing happens on reception, sidecar-side, with the process secret.
- Do not cite learning `b77dba43`: refuted by `310a9953`.
- **Never call a `brain_session_*` tool** during this plan. The session cycle belongs to the user.

---

## File structure

**Created — `brain_v42`:**

| File | Responsibility |
|---------|----------------|
| `src/brain_v42/metrics/client_activity.py` | The merged registry: bounded state, join, `clients[]` projection |
| `src/brain_v42/metrics/claude_telemetry.py` | Claude Code OTLP decoder (schema distinct from Codex) |
| `src/brain_v42/metrics/client_observation.py` | Brain-side observation feed format: bounded decoding |
| `src/brain_v42/mcp/activity_reporter.py` | Loopback emitter on the MCP process side |
| `tests/fixtures/claude_otlp_logs.json` | Real capture produced by task 1, oracle for attribute names |

**Modified — `brain_v42`:**

| File | Change |
|---------|-----------|
| `src/brain_v42/provenance.py` | Session and call-depth ContextVars |
| `src/brain_v42/mcp/provenance_middleware.py` | Sets the session, re-entrance guard, triggers the emitter |
| `src/brain_v42/metrics/codex_telemetry.py` | Keeps its decoder; the registry moves out |
| `src/brain_v42/metrics/server.py` | `/v1/client-activity` route, registry wiring |
| `src/brain_v42/metrics/cockpit.py` | `clients[]` field |
| `src/brain_v42/config.py` | Emitter settings |

**Modified — `red-monitor`** (repo `~/hawkixs_infra/git_repo/ReD_v1/projects/red-monitor`):
`frontend/src/tabs/brain/BrainActivity.jsx`, `brainPresentation.js`, `BrainStatusBar.jsx`, `Brain.test.jsx`.

**Untouched, deliberately:** `internal/web/brain.go` (re-proxies raw bytes), `metrics/flusher.py` and `process_metrics` (30 s cadence, ruled out by the spec), `db/tables.py` (the registry is in memory).

---

### Task 1: Spike — prove the session join

**Plan gate.** Do not proceed without its written result. No implementation code here: this is a measurement.

**Files:**
- Create: `tests/fixtures/claude_otlp_logs.json`
- Create: `docs/upstream/2026-08-06-claude-otlp-session-join.md`

**Interfaces:**
- Consumes: nothing.
- Produces: the capture file, oracle of attribute names for task 5; the `JOINTURE POSSIBLE` or `JOINTURE IMPOSSIBLE` verdict that gates task 8.

- [ ] **Step 1: Add the session header to the local MCP configuration**

In `~/.claude.json`, next to `"X-Brain-Agent": "${PWD}"` (around line 3807), add:

```json
"X-Brain-Session": "${CLAUDE_CODE_SESSION_ID}"
```

- [ ] **Step 2: Open an INTERACTIVE Claude Code session and read the header received**

The 2026-08-06 measurement comes from an `sdk-cli` session with `CLAUDE_CODE_CHILD_SESSION=1`. An interactive session may export a different environment: that is precisely what is being checked.

In the interactive session, note the value of `CLAUDE_CODE_SESSION_ID`:

```bash
echo "$CLAUDE_CODE_SESSION_ID"
```

Then capture the header actually received by the MCP server. The simplest way without touching the code: a temporary log line in the middleware.

```bash
# In the MCP process, TEMPORARILY add to provenance_middleware.py,
# line 33, then restart the server:
#   import structlog; structlog.get_logger(__name__).warning(
#       "spike.headers", session=headers.get("x-brain-session"))
journalctl --user -u brain-v42-mcp -f | grep spike.headers
```

Make any brain tool call from the interactive session, read the value.

**Remove the temporary log line before proceeding.** It must leave nothing behind in the tree.

- [ ] **Step 3: Enable Claude Code OTLP telemetry and capture a real payload**

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
```

Port `4318`, not `9200`: capture the raw payload first with a throwaway receiver, without routing it through the production receiver.

```bash
python - <<'PY'
import json, pathlib
from http.server import BaseHTTPRequestHandler, HTTPServer

OUT = pathlib.Path("/tmp/claude_otlp_capture.json")

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        OUT.write_text(json.dumps(json.loads(body), indent=2))
        print("captured", len(body), "bytes ->", OUT)
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")

HTTPServer(("127.0.0.1", 4318), H).serve_forever()
PY
```

Start an interactive Claude Code session with these variables, send two or three prompts, stop the receiver.

- [ ] **Step 4: Answer the two questions and write the verdict**

Open `/tmp/claude_otlp_capture.json` and record:

1. The exact name of the attribute carrying the session identifier (expected `session.id`).
2. Its **value**: is it **identical** to `$CLAUDE_CODE_SESSION_ID` and to the `X-Brain-Session` header seen at Step 2?
3. The exact event names (expected `claude_code.user_prompt`, `claude_code.api_request`).
4. The exact counter attribute names (expected `input_tokens`, `output_tokens`, `cost_usd`, `model`).

Write `docs/upstream/2026-08-06-claude-otlp-session-join.md` with, explicitly: the `JOINTURE POSSIBLE` or `JOINTURE IMPOSSIBLE` verdict, the four findings above, and the Claude Code version measured.

- [ ] **Step 5: Reduce the capture to a fixture**

Copy the payload into `tests/fixtures/claude_otlp_logs.json`, **replacing every real UUID with** `12345678-1234-4abc-8def-1234567890ab` (the same constant as `tests/unit/test_codex_telemetry_endpoint.py:33`). No prompt data must remain: keep only the records whose attributes are the ones recorded.

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/claude_otlp_logs.json docs/upstream/2026-08-06-claude-otlp-session-join.md
git commit -m "$(cat <<'EOF'
test(metrics): capturer le schéma OTLP réel de Claude Code

Oracle des noms d'attributs pour le décodeur. Porte la réponse aux deux
questions du spike : expansion de l'en-tête en session interactive, et
égalité de session.id avec CLAUDE_CODE_SESSION_ID.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

**If the verdict is `JOINTURE IMPOSSIBLE`**: the plan continues as is. All Claude lines will become `unattributed` on the brain side plus OTLP-only lines, which the design accounts for. Only task 8 changes: its join test becomes a no-join test. Note it in the verdict document.

---

### Task 2: Session identity in the provenance layer

**Files:**
- Modify: `src/brain_v42/provenance.py`
- Test: `tests/unit/test_provenance.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `normalize_session(value: str | None) -> str | None`, `set_current_session(session: str | None) -> None`, `get_current_session() -> str | None`. Returns `None` when no valid session is declared.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_provenance.py`:

```python
class TestNormalizeSession:
    def test_canonical_uuid_passes_through(self) -> None:
        assert normalize_session("3d7a88d7-791b-45da-b8b9-75727e3c9eec") == (
            "3d7a88d7-791b-45da-b8b9-75727e3c9eec"
        )

    def test_unexpanded_template_is_rejected(self) -> None:
        assert normalize_session("${CLAUDE_CODE_SESSION_ID}") is None

    def test_non_uuid_is_rejected(self) -> None:
        assert normalize_session("brain-v42") is None

    def test_uppercase_uuid_is_rejected(self) -> None:
        # Single canonical form only, otherwise two clients writing the same
        # session under two different cases would produce two distinct rows.
        assert normalize_session("3D7A88D7-791B-45DA-B8B9-75727E3C9EEC") is None

    def test_blank_and_none_are_rejected(self) -> None:
        assert normalize_session("   ") is None
        assert normalize_session(None) is None

    def test_overlong_value_is_rejected(self) -> None:
        assert normalize_session("x" * 4096) is None


class TestCurrentSession:
    def test_default_is_none(self) -> None:
        assert get_current_session() is None

    def test_set_then_get(self) -> None:
        set_current_session("3d7a88d7-791b-45da-b8b9-75727e3c9eec")
        assert get_current_session() == "3d7a88d7-791b-45da-b8b9-75727e3c9eec"
        set_current_session(None)

    def test_isolated_between_tasks(self) -> None:
        async def scenario() -> tuple[str | None, str | None]:
            async def inner() -> str | None:
                set_current_session("11111111-1111-4111-8111-111111111111")
                return get_current_session()

            inside = await asyncio.create_task(inner())
            return inside, get_current_session()

        inside, outside = asyncio.run(scenario())
        assert inside == "11111111-1111-4111-8111-111111111111"
        assert outside is None
```

Add `normalize_session`, `set_current_session`, `get_current_session` to the import at the top of the file.

- [ ] **Step 2: Verify the failure**

Run: `pytest tests/unit/test_provenance.py -v`
Expected: FAIL — `ImportError: cannot import name 'normalize_session'`

- [ ] **Step 3: Implement the minimum**

In `src/brain_v42/provenance.py`, after `normalize_agent`:

```python
_MAX_SESSION_CHARS = 36

_current_session: ContextVar[str | None] = ContextVar(
    "brain_v42_current_session",
    default=None,
)


def normalize_session(value: str | None) -> str | None:
    """Réduire un ``X-Brain-Session`` brut en UUID canonique, ou ``None``.

    Seule la forme canonique minuscule est acceptée : la valeur sert de clé de
    jointure, et deux graphies de la même session produiraient deux lignes.
    Tout ce qui n'est pas un UUID — gabarit non expansé, libellé, valeur
    surdimensionnée — vaut ``None``, c'est-à-dire « pas de session déclarée ».
    """
    value = (value or "").strip()
    if not value or len(value) > _MAX_SESSION_CHARS:
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    return value if str(parsed) == value else None


def set_current_session(session: str | None) -> None:
    """Poser la session pour la durée du contexte courant."""
    _current_session.set(session)


def get_current_session() -> str | None:
    """Lire la session courante. ``None`` hors contexte ou sans déclaration."""
    return _current_session.get()
```

Add `import uuid` at the top of the module.

- [ ] **Step 4: Verify it passes**

Run: `pytest tests/unit/test_provenance.py -v`
Expected: PASS, including all pre-existing tests.

- [ ] **Step 5: Verify the full green**

```bash
pytest tests/unit -q && ruff check src/ tests/ && ruff format --check src/ tests/ && mypy src/
```

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/provenance.py tests/unit/test_provenance.py
git commit -m "$(cat <<'EOF'
feat(provenance): porter l'identité de session déclarée par le client

Seule la forme UUID canonique est retenue : la valeur sert de clé de
jointure, et deux graphies de la même session produiraient deux lignes.
Un gabarit non expansé vaut « pas de session ».

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Re-entrance guard

Without it, `brain_calls` counts double in the `compact` profile, which is the production profile. Measured: commit `58329a84`.

**Files:**
- Modify: `src/brain_v42/provenance.py`
- Modify: `src/brain_v42/mcp/provenance_middleware.py`
- Test: `tests/unit/test_provenance.py`, `tests/unit/test_provenance_middleware.py`

**Interfaces:**
- Consumes: task 2.
- Produces: `enter_call() -> Token[int]`, `exit_call(token: Token[int]) -> None`, `is_outermost_call() -> bool`. `is_outermost_call()` is `True` only between `enter_call()` and its `exit_call()` at the first nesting level.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_provenance.py`:

```python
class TestCallDepth:
    def test_outermost_call_is_reported_once(self) -> None:
        outer = enter_call()
        assert is_outermost_call() is True
        inner = enter_call()
        assert is_outermost_call() is False
        exit_call(inner)
        assert is_outermost_call() is True
        exit_call(outer)

    def test_depth_resets_after_exit(self) -> None:
        token = enter_call()
        exit_call(token)
        again = enter_call()
        assert is_outermost_call() is True
        exit_call(again)

    def test_outside_any_call_is_not_outermost(self) -> None:
        assert is_outermost_call() is False
```

Create `tests/unit/test_provenance_middleware.py`:

```python
"""Le middleware de provenance face à la ré-entrance du profil compact."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from brain_v42.mcp.provenance_middleware import ProvenanceMiddleware
from brain_v42.provenance import get_current_actor, get_current_session


class _Recorder:
    def __init__(self) -> None:
        self.outermost_flags: list[bool] = []


@pytest.mark.asyncio
async def test_compact_gateway_reports_one_outermost_call() -> None:
    """Un appel compact déclenche on_call_tool deux fois : passerelle puis
    tool interne. La garde ne doit en retenir qu'un.

    Ce test DOIT simuler l'imbrication réelle. Un test qui appelle le
    middleware deux fois à plat passerait au vert sans rien prouver.
    """
    middleware = ProvenanceMiddleware()

    async def inner_call_next(_context: Any) -> str:
        return "inner"

    async def outer_call_next(_context: Any) -> str:
        # The gateway tool re-enters the middleware chain.
        return await middleware.on_call_tool(object(), inner_call_next)

    with patch.object(ProvenanceMiddleware, "_report", autospec=True) as report:
        with patch(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            return_value={"x-brain-agent": "brain-v42"},
        ):
            result = await middleware.on_call_tool(object(), outer_call_next)

    assert result == "inner"
    assert report.call_count == 1


@pytest.mark.asyncio
async def test_actor_and_session_are_posted_from_headers() -> None:
    middleware = ProvenanceMiddleware()
    captured: dict[str, object] = {}

    async def call_next(_context: Any) -> None:
        captured["actor"] = get_current_actor()
        captured["session"] = get_current_session()

    with patch.object(ProvenanceMiddleware, "_report", autospec=True):
        with patch(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            return_value={
                "x-brain-agent": "/home/hawixs/git/red-lab",
                "x-brain-session": "3d7a88d7-791b-45da-b8b9-75727e3c9eec",
            },
        ):
            await middleware.on_call_tool(object(), call_next)

    assert captured["actor"] == "red-lab"
    assert captured["session"] == "3d7a88d7-791b-45da-b8b9-75727e3c9eec"


@pytest.mark.asyncio
async def test_absent_session_header_leaves_session_none() -> None:
    middleware = ProvenanceMiddleware()
    captured: dict[str, object] = {}

    async def call_next(_context: Any) -> None:
        captured["session"] = get_current_session()

    with patch.object(ProvenanceMiddleware, "_report", autospec=True):
        with patch(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            return_value={"x-brain-agent": "codex"},
        ):
            await middleware.on_call_tool(object(), call_next)

    assert captured["session"] is None
```

- [ ] **Step 2: Verify the failure**

Run: `pytest tests/unit/test_provenance.py tests/unit/test_provenance_middleware.py -v`
Expected: FAIL — `ImportError: cannot import name 'enter_call'`, then `AttributeError: _report`.

- [ ] **Step 3: Implement**

In `src/brain_v42/provenance.py`:

```python
_call_depth: ContextVar[int] = ContextVar("brain_v42_call_depth", default=0)


def enter_call() -> Token[int]:
    """Entrer d'un niveau dans la chaîne d'appels de tools."""
    return _call_depth.set(_call_depth.get() + 1)


def exit_call(token: Token[int]) -> None:
    """Ressortir du niveau ouvert par ``enter_call``."""
    _call_depth.reset(token)


def is_outermost_call() -> bool:
    """Vrai au seul premier niveau d'imbrication.

    En profil ``compact`` la passerelle ``brain_call_tool`` ré-entre dans la
    chaîne de middlewares (mesuré, commit 58329a84) : ``on_call_tool`` se
    déclenche deux fois par appel client. Compter à tous les niveaux
    gonflerait le compteur x2 en production et x1 en profil natif.
    """
    return _call_depth.get() == 1
```

Add `from contextvars import ContextVar, Token` at the top.

Rewrite `src/brain_v42/mcp/provenance_middleware.py`:

```python
class ProvenanceMiddleware(Middleware):
    """Pose l'acteur et la session déclarés, et signale l'appel une seule fois."""

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        headers = get_http_headers() or {}
        actor = normalize_agent(headers.get("x-brain-agent"))
        session = normalize_session(headers.get("x-brain-session"))
        set_current_actor(actor)
        set_current_session(session)

        token = enter_call()
        try:
            if is_outermost_call():
                self._report(actor, session)
            return await call_next(context)
        finally:
            exit_call(token)

    def _report(self, actor: str, session: str | None) -> None:
        """Signaler l'appel. Câblé à la tâche 7 ; sans effet jusque-là."""
```

Update the module docstring: re-entrance is no longer "harmless", it is **handled**.

- [ ] **Step 4: Verify it passes**

Run: `pytest tests/unit/test_provenance.py tests/unit/test_provenance_middleware.py -v`
Expected: PASS

- [ ] **Step 5: Verify the full green**

```bash
pytest tests/unit -q && ruff check src/ tests/ && ruff format --check src/ tests/ && mypy src/
```

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/provenance.py src/brain_v42/mcp/provenance_middleware.py tests/unit/test_provenance.py tests/unit/test_provenance_middleware.py
git commit -m "$(cat <<'EOF'
feat(provenance): ne compter qu'un événement par appel client

En profil compact, la passerelle ré-entre dans la chaîne de middlewares :
on_call_tool se déclenche deux fois par appel. Une garde de profondeur
retient le seul niveau extérieur, sinon le compteur vaudrait x2 en
production et x1 en profil natif — deux chiffres incomparables.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Move the registry without changing its behavior

Pure refactor. No existing test should be modified, and all must stay green: that is the proof that the move changes nothing.

**Files:**
- Create: `src/brain_v42/metrics/client_activity.py`
- Modify: `src/brain_v42/metrics/codex_telemetry.py`, `src/brain_v42/metrics/server.py`, `src/brain_v42/metrics/cockpit.py`
- Test: `tests/unit/test_client_activity.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ClientActivityRegistry` in `metrics/client_activity.py`, with `ingest_otlp_json(payload: bytes) -> None` and `snapshot() -> dict[str, object]` behaving identically to `CodexConversationRegistry`. `CodexConversationRegistry = ClientActivityRegistry` remains as an alias in `codex_telemetry.py`.

- [ ] **Step 1: Write the equivalence test**

Create `tests/unit/test_client_activity.py`:

```python
"""Le registre généralisé — équivalence avec l'ancien, puis fusion."""

from __future__ import annotations

from brain_v42.metrics.client_activity import ClientActivityRegistry
from brain_v42.metrics.codex_telemetry import CodexConversationRegistry


def test_codex_registry_is_the_generalized_registry() -> None:
    assert CodexConversationRegistry is ClientActivityRegistry


def test_empty_snapshot_keeps_the_legacy_shape() -> None:
    registry = ClientActivityRegistry(secret=b"\x01" * 32)
    snapshot = registry.snapshot()
    assert snapshot["active_convs"] == 0
    assert snapshot["ctx_tokens"] == 0
    assert snapshot["activeConvs"] == []
```

- [ ] **Step 2: Verify the failure**

Run: `pytest tests/unit/test_client_activity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'brain_v42.metrics.client_activity'`

- [ ] **Step 3: Move it**

Move the `CodexConversationRegistry` class and the `_Conversation` dataclasses from `codex_telemetry.py` to `client_activity.py`, **without touching the body of the methods**. Also move the constants used only by it: `MAX_ACTIVE_CONVERSATIONS`, `ACTIVITY_TTL_SECONDS`, `MAX_FINGERPRINTS`, `FINGERPRINT_TTL_SECONDS`.

`client_activity.py` imports the decoding from `codex_telemetry`:

```python
from brain_v42.metrics.codex_telemetry import _COMPLETION_EVENTS, _ProjectedRecord, _decode
```

Rename the class to `ClientActivityRegistry` and leave in `codex_telemetry.py`:

```python
from brain_v42.metrics.client_activity import ClientActivityRegistry

# Historical name. The registry is no longer Codex-specific since it
# merges the sources, but the old name is still imported by the tests.
CodexConversationRegistry = ClientActivityRegistry
```

Watch out for the circular import: `codex_telemetry` must import `client_activity` **only at the end of the module**, after `_decode` is defined. If `ruff` complains (`E402`), invert the dependency instead by moving the alias into `metrics/__init__.py`.

Update the imports in `server.py` and `cockpit.py` to point to `client_activity`.

- [ ] **Step 4: Verify that EVERYTHING passes, including the old tests**

```bash
pytest tests/unit/test_client_activity.py tests/unit/test_codex_telemetry_endpoint.py tests/unit/test_metrics_cockpit_collector.py -v
```
Expected: PASS everywhere. A single failure here means the move changed a behavior — fix it before moving on, do not adjust the test.

- [ ] **Step 5: Verify the full green**

```bash
pytest tests/unit -q && ruff check src/ tests/ && ruff format --check src/ tests/ && mypy src/
```

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/metrics/client_activity.py src/brain_v42/metrics/codex_telemetry.py src/brain_v42/metrics/server.py src/brain_v42/metrics/cockpit.py tests/unit/test_client_activity.py
git commit -m "$(cat <<'EOF'
refactor(metrics): sortir le registre d'activité de codex_telemetry

Déménagement à comportement constant, prouvé par les tests existants
laissés intacts. Le patron (TTL, cap, HMAC, dédup) est bon ; c'est son
nom qui était trop étroit avant d'accueillir d'autres sources.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Claude Code OTLP decoder

**Files:**
- Create: `src/brain_v42/metrics/claude_telemetry.py`
- Test: `tests/unit/test_claude_telemetry.py`

**Interfaces:**
- Consumes: the task 1 fixture; `codex_telemetry._load_json`, `_string_value`, `_token_value`, `_canonical_uuid`, `CodexTelemetryLimitError`, `CodexTelemetryMalformedError`.
- Produces: `decode_claude_logs(payload: bytes) -> tuple[ClaudeRecord, ...]` and the frozen dataclass `ClaudeRecord(session_id: str, event_name: str, model: str, input_tokens: int | None, cache_read_tokens: int | None, cache_creation_tokens: int | None, output_tokens: int | None, cost_usd: float | None, timestamp: int | None)`.

**Name oracle:** `tests/fixtures/claude_otlp_logs.json`, real capture produced by task 1. Verdict and full findings in `docs/upstream/2026-08-06-claude-otlp-session-join.md`.

**Three measured corrections, already applied to the code below — do not undo them:**

1. **`event.name` is NOT prefixed.** Measured: the attribute holds `user_prompt`,
   `api_request`, `assistant_response`. The `claude_code.` prefix is in
   `body.stringValue`, not in the attribute. A filter on `claude_code.*` via
   `event.name` would recognize nothing.
2. **`input_tokens` does not measure context.** Real finding: `input_tokens=10`
   while `cache_read_tokens=11776` and `cache_creation_tokens=6804`. The three
   counters must therefore be projected; task 8 will sum them.
3. **Every record carries personal data in the clear** — `user.email`,
   `user.id`, `user.account_uuid`, `organization.id` — including on hook and
   plugin events. Whitelist projection is the only thing keeping them out of
   a registry exposed over HTTP. It has its own test.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_claude_telemetry.py`:

```python
"""Décodage borné des logs OTLP de Claude Code."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain_v42.metrics.claude_telemetry import decode_claude_logs
from brain_v42.metrics.codex_telemetry import (
    CodexTelemetryLimitError,
    CodexTelemetryMalformedError,
)

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "claude_otlp_logs.json"
FAKE_UUID = "12345678-1234-4abc-8def-1234567890ab"


def _attribute(key: str, value: dict[str, object]) -> dict[str, object]:
    return {"key": key, "value": value}


def _envelope(records: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {"resourceLogs": [{"scopeLogs": [{"logRecords": records}]}]}
    ).encode()


def _record(
    *,
    event_name: str = "user_prompt",
    session_id: str = FAKE_UUID,
    extra: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    attributes = [
        _attribute("event.name", {"stringValue": event_name}),
        _attribute("session.id", {"stringValue": session_id}),
    ]
    attributes.extend(extra or [])
    return {"timeUnixNano": "1", "attributes": attributes}


class TestDecodeClaudeLogs:
    def test_user_prompt_yields_a_record(self) -> None:
        records = decode_claude_logs(_envelope([_record()]))
        assert len(records) == 1
        assert records[0].session_id == FAKE_UUID
        assert records[0].event_name == "user_prompt"

    def test_api_request_carries_tokens_and_cost(self) -> None:
        payload = _envelope([
            _record(
                event_name="api_request",
                extra=[
                    _attribute("model", {"stringValue": "claude-opus-5"}),
                    _attribute("input_tokens", {"intValue": 10}),
                    _attribute("cache_read_tokens", {"intValue": 11_776}),
                    _attribute("cache_creation_tokens", {"intValue": 6_804}),
                    _attribute("output_tokens", {"intValue": 340}),
                    _attribute("cost_usd", {"doubleValue": 0.0125}),
                ],
            )
        ])
        record = decode_claude_logs(payload)[0]
        assert record.model == "claude-opus-5"
        assert record.input_tokens == 10
        assert record.cache_read_tokens == 11_776
        assert record.cache_creation_tokens == 6_804
        assert record.output_tokens == 340
        assert record.cost_usd == pytest.approx(0.0125)

    def test_prefixed_event_name_is_not_recognized(self) -> None:
        """Le préfixe claude_code. est dans le CORPS, pas dans l'attribut.

        Mesuré le 2026-08-06 sur Claude Code 2.1.220. Ce test fige la
        correction : un décodeur qui filtrerait sur claude_code.user_prompt
        via event.name ne reconnaîtrait aucun enregistrement réel.
        """
        assert decode_claude_logs(_envelope([_record(event_name="claude_code.user_prompt")])) == ()

    def test_unknown_event_is_ignored(self) -> None:
        assert decode_claude_logs(_envelope([_record(event_name="tool_decision")])) == ()

    def test_personal_data_never_survives_projection(self) -> None:
        """Chaque enregistrement réel porte l'e-mail du compte en clair.

        Mesuré le 2026-08-06 : user.email, user.id, user.account_uuid et
        organization.id sont présents sur TOUS les enregistrements, y compris
        les événements de hook. La liste blanche est la seule chose qui les
        empêche d'entrer dans un registre exposé par HTTP.
        """
        payload = _envelope([
            _record(
                extra=[
                    _attribute("user.email", {"stringValue": "personne@exemple.test"}),
                    _attribute("user.id", {"stringValue": "e" * 64}),
                    _attribute("user.account_uuid", {"stringValue": FAKE_UUID}),
                    _attribute("organization.id", {"stringValue": FAKE_UUID}),
                    _attribute("prompt", {"stringValue": "secret de l'utilisateur"}),
                ],
            )
        ])
        record = decode_claude_logs(payload)[0]
        rendered = repr(record)
        assert "personne@exemple.test" not in rendered
        assert "secret de l'utilisateur" not in rendered
        assert not hasattr(record, "user_email")

    def test_non_uuid_session_is_malformed(self) -> None:
        with pytest.raises(CodexTelemetryMalformedError):
            decode_claude_logs(_envelope([_record(session_id="not-a-uuid")]))

    def test_missing_session_is_malformed(self) -> None:
        payload = _envelope([
            {"timeUnixNano": "1", "attributes": [
                _attribute("event.name", {"stringValue": "user_prompt"})
            ]}
        ])
        with pytest.raises(CodexTelemetryMalformedError):
            decode_claude_logs(payload)

    def test_absent_model_falls_back_to_unknown(self) -> None:
        assert decode_claude_logs(_envelope([_record()]))[0].model == "unknown"

    def test_negative_cost_is_dropped(self) -> None:
        payload = _envelope([
            _record(
                event_name="api_request",
                extra=[_attribute("cost_usd", {"doubleValue": -1.0})],
            )
        ])
        assert decode_claude_logs(payload)[0].cost_usd is None

    def test_oversized_payload_raises_limit(self) -> None:
        with pytest.raises(CodexTelemetryLimitError):
            decode_claude_logs(b"{" + b" " * 300_000 + b"}")


class TestRealCapture:
    def test_recorded_capture_decodes(self) -> None:
        """La capture réelle du spike doit décoder sans exception.

        Ce test est l'oracle : s'il échoue, ce sont les noms d'attributs du
        code qui sont faux, pas la capture.
        """
        records = decode_claude_logs(FIXTURE_PATH.read_bytes())
        assert records, "la capture ne contient aucun événement reconnu"
        assert all(record.session_id for record in records)
```

- [ ] **Step 2: Verify the failure**

Run: `pytest tests/unit/test_claude_telemetry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'brain_v42.metrics.claude_telemetry'`

- [ ] **Step 3: Implement**

Create `src/brain_v42/metrics/claude_telemetry.py`:

```python
"""Projection bornée des logs OTLP/HTTP JSON de Claude Code.

Schéma distinct de celui de Codex : l'identifiant est ``session.id`` et non
``conversation.id``, les noms d'événements sont nus (``user_prompt``,
``api_request`` — le préfixe ``claude_code.`` vit dans le corps, pas dans
l'attribut), et les compteurs d'entrée sont éclatés en trois : nouveaux tokens,
lecture de cache et création de cache. Les bornes sont partagées avec le
récepteur existant.

La projection par liste blanche est la partie qui compte : la source envoie
aussi ``user.email``, ``user.id`` et ``organization.id`` en clair sur chaque
enregistrement. Rien de tout cela ne doit atteindre le registre.
"""

from __future__ import annotations

from dataclasses import dataclass

from brain_v42.metrics.codex_telemetry import (
    _canonical_uuid,
    _load_json,
    _model_value,
    _only_any_value,
    _raise_malformed,
    _string_value,
    _timestamp_value,
    _token_value,
    CodexTelemetryLimitError,
    MAX_ATTRIBUTES_PER_RECORD,
    MAX_LOG_RECORDS,
)

# Strict whitelist. Measured on 2026-08-06: real records also carry
# user.email, user.id, user.account_uuid and organization.id in the clear.
# Anything not here is dropped before entering the registry.
_PROJECTED_KEYS = frozenset(
    {
        "session.id",
        "event.name",
        "model",
        "input_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "output_tokens",
        "cost_usd",
    }
)
# No prefix: `event.name` holds `user_prompt`, the body holds
# `claude_code.user_prompt`. Measured on Claude Code 2.1.220.
_KNOWN_EVENTS = frozenset({"user_prompt", "api_request"})
_MAX_COST_USD = 1_000_000.0


@dataclass(frozen=True, slots=True)
class ClaudeRecord:
    session_id: str
    event_name: str
    model: str
    input_tokens: int | None
    cache_read_tokens: int | None
    cache_creation_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    timestamp: int | None


def _cost_value(value: object) -> float | None:
    raw = _only_any_value(value, "doubleValue")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    cost = float(raw)
    if cost != cost or cost < 0.0 or cost > _MAX_COST_USD:  # NaN, negative, out of bounds
        return None
    return cost


def _attributes(record: dict[str, object]) -> dict[str, object]:
    raw = record.get("attributes", [])
    if not isinstance(raw, list):
        _raise_malformed()
    if len(raw) > MAX_ATTRIBUTES_PER_RECORD:
        raise CodexTelemetryLimitError
    projected: dict[str, object] = {}
    for item in raw:
        if not isinstance(item, dict):
            _raise_malformed()
        key, value = item.get("key"), item.get("value")
        if not isinstance(key, str) or not isinstance(value, dict):
            _raise_malformed()
        if key in _PROJECTED_KEYS:
            if key in projected:
                _raise_malformed()
            projected[key] = value
    return projected


def decode_claude_logs(payload: bytes) -> tuple[ClaudeRecord, ...]:
    """Valider une charge OTLP complète et en extraire la projection sûre."""
    root = _load_json(payload)
    resource_logs = root.get("resourceLogs")
    if not isinstance(resource_logs, list):
        _raise_malformed()

    decoded: list[ClaudeRecord] = []
    seen = 0
    for resource_log in resource_logs:
        if not isinstance(resource_log, dict):
            _raise_malformed()
        scope_logs = resource_log.get("scopeLogs", [])
        if not isinstance(scope_logs, list):
            _raise_malformed()
        for scope_log in scope_logs:
            if not isinstance(scope_log, dict):
                _raise_malformed()
            log_records = scope_log.get("logRecords", [])
            if not isinstance(log_records, list):
                _raise_malformed()
            seen += len(log_records)
            if seen > MAX_LOG_RECORDS:
                raise CodexTelemetryLimitError
            for record in log_records:
                if not isinstance(record, dict):
                    _raise_malformed()
                attributes = _attributes(record)
                name_value = attributes.get("event.name")
                if name_value is None:
                    continue
                event_name = _string_value(name_value)
                if event_name is None:
                    _raise_malformed()
                if event_name not in _KNOWN_EVENTS:
                    continue
                session_id = _canonical_uuid(attributes.get("session.id"))
                if session_id is None:
                    _raise_malformed()
                decoded.append(
                    ClaudeRecord(
                        session_id=session_id,
                        event_name=event_name,
                        model=_model_value(attributes.get("model")),
                        input_tokens=_token_value(attributes.get("input_tokens")),
                        cache_read_tokens=_token_value(attributes.get("cache_read_tokens")),
                        cache_creation_tokens=_token_value(
                            attributes.get("cache_creation_tokens")
                        ),
                        output_tokens=_token_value(attributes.get("output_tokens")),
                        cost_usd=_cost_value(attributes.get("cost_usd")),
                        timestamp=_timestamp_value(record.get("timeUnixNano")),
                    )
                )
    return tuple(decoded)
```

`_model_value` already returns `"unknown"` when the value is absent or non-conforming — which is what `test_absent_model_falls_back_to_unknown` wants.

- [ ] **Step 4: Verify it passes**

Run: `pytest tests/unit/test_claude_telemetry.py -v`
Expected: PASS. If `TestRealCapture` fails, compare the code's attribute names to those in the fixture and **fix the code**, never the fixture.

- [ ] **Step 5: Verify the full green**

```bash
pytest tests/unit -q && ruff check src/ tests/ && ruff format --check src/ tests/ && mypy src/
```

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/metrics/claude_telemetry.py tests/unit/test_claude_telemetry.py
git commit -m "$(cat <<'EOF'
feat(metrics): décoder les logs OTLP de Claude Code

Schéma distinct de Codex : session.id, noms d'événements nus, trois
compteurs d'entrée et un coût. Les bornes sont partagées avec le récepteur
existant. La capture réelle du spike sert d'oracle des noms d'attributs.

La liste blanche jette user.email, que la source envoie en clair sur
chaque enregistrement.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Brain-side observation feed format

**Files:**
- Create: `src/brain_v42/metrics/client_observation.py`
- Test: `tests/unit/test_client_observation.py`

**Interfaces:**
- Consumes: `codex_telemetry._load_json`, `_raise_malformed`, `CodexTelemetryLimitError`, `CodexTelemetryMalformedError`.
- Produces: `MAX_OBSERVATION_BYTES: int`, `MAX_OBSERVATIONS: int`, the frozen dataclass `ClientObservation(actor: str, session_id: str | None, calls: int)`, and `decode_observations(payload: bytes) -> tuple[ClientObservation, ...]`.

Feed format, deliberately tiny:

```json
{"observations": [{"actor": "brain-v42", "session": "12345678-…", "calls": 1}]}
```

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_client_observation.py`:

```python
"""Décodage borné des observations poussées par le processus MCP."""

from __future__ import annotations

import json

import pytest

from brain_v42.metrics.client_observation import (
    MAX_OBSERVATIONS,
    decode_observations,
)
from brain_v42.metrics.codex_telemetry import (
    CodexTelemetryLimitError,
    CodexTelemetryMalformedError,
)

FAKE_UUID = "12345678-1234-4abc-8def-1234567890ab"


def _payload(items: list[dict[str, object]]) -> bytes:
    return json.dumps({"observations": items}).encode()


class TestDecodeObservations:
    def test_full_observation(self) -> None:
        observation = decode_observations(
            _payload([{"actor": "brain-v42", "session": FAKE_UUID, "calls": 3}])
        )[0]
        assert observation.actor == "brain-v42"
        assert observation.session_id == FAKE_UUID
        assert observation.calls == 3

    def test_session_is_optional(self) -> None:
        observation = decode_observations(_payload([{"actor": "codex", "calls": 1}]))[0]
        assert observation.session_id is None

    def test_actor_is_normalized(self) -> None:
        observation = decode_observations(
            _payload([{"actor": "/home/hawixs/git/red-lab", "calls": 1}])
        )[0]
        assert observation.actor == "red-lab"

    def test_non_uuid_session_is_dropped_not_fatal(self) -> None:
        # An unreadable session degrades the row to "unattributed".
        # Rejecting the whole batch would punish the valid observations from the same submission.
        observation = decode_observations(
            _payload([{"actor": "codex", "session": "nope", "calls": 1}])
        )[0]
        assert observation.session_id is None

    def test_missing_root_key_is_malformed(self) -> None:
        with pytest.raises(CodexTelemetryMalformedError):
            decode_observations(json.dumps({"nope": []}).encode())

    def test_non_integer_calls_is_malformed(self) -> None:
        with pytest.raises(CodexTelemetryMalformedError):
            decode_observations(_payload([{"actor": "codex", "calls": "1"}]))

    def test_negative_calls_is_malformed(self) -> None:
        with pytest.raises(CodexTelemetryMalformedError):
            decode_observations(_payload([{"actor": "codex", "calls": -1}]))

    def test_too_many_observations_raises_limit(self) -> None:
        items = [{"actor": "a", "calls": 1}] * (MAX_OBSERVATIONS + 1)
        with pytest.raises(CodexTelemetryLimitError):
            decode_observations(_payload(items))
```

- [ ] **Step 2: Verify the failure**

Run: `pytest tests/unit/test_client_observation.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
"""Format de fil des observations d'activité poussées par le processus MCP.

Le middleware de provenance vit dans le serveur MCP (:8765), le registre dans
le sidecar métriques (:9200). Ce module décrit le peu qui traverse la socket
loopback entre les deux, avec les mêmes bornes que le récepteur OTLP.
"""

from __future__ import annotations

from dataclasses import dataclass

from brain_v42.metrics.codex_telemetry import (
    CodexTelemetryLimitError,
    _load_json,
    _raise_malformed,
)
from brain_v42.provenance import normalize_agent, normalize_session

MAX_OBSERVATION_BYTES = 16_384
MAX_OBSERVATIONS = 64
MAX_CALLS_PER_OBSERVATION = 1_000_000


@dataclass(frozen=True, slots=True)
class ClientObservation:
    actor: str
    session_id: str | None
    calls: int


def decode_observations(payload: bytes) -> tuple[ClientObservation, ...]:
    """Valider un lot complet d'observations avant toute application."""
    if len(payload) > MAX_OBSERVATION_BYTES:
        raise CodexTelemetryLimitError
    root = _load_json(payload)
    items = root.get("observations")
    if not isinstance(items, list):
        _raise_malformed()
    if len(items) > MAX_OBSERVATIONS:
        raise CodexTelemetryLimitError

    decoded: list[ClientObservation] = []
    for item in items:
        if not isinstance(item, dict):
            _raise_malformed()
        actor = item.get("actor")
        calls = item.get("calls")
        if not isinstance(actor, str):
            _raise_malformed()
        if not isinstance(calls, int) or isinstance(calls, bool):
            _raise_malformed()
        if calls < 0:
            _raise_malformed()
        if calls > MAX_CALLS_PER_OBSERVATION:
            raise CodexTelemetryLimitError
        session = item.get("session")
        decoded.append(
            ClientObservation(
                actor=normalize_agent(actor),
                session_id=normalize_session(session if isinstance(session, str) else None),
                calls=calls,
            )
        )
    return tuple(decoded)
```

`_load_json` already bounds the JSON depth and container count; `MAX_OBSERVATION_BYTES` is checked before it because it is much tighter than `MAX_REQUEST_BYTES`.

- [ ] **Step 4: Verify it passes**

Run: `pytest tests/unit/test_client_observation.py -v`
Expected: PASS

- [ ] **Step 5: Verify the full green**

```bash
pytest tests/unit -q && ruff check src/ tests/ && ruff format --check src/ tests/ && mypy src/
```

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/metrics/client_observation.py tests/unit/test_client_observation.py
git commit -m "$(cat <<'EOF'
feat(metrics): décrire le format de fil des observations brain-side

Le middleware et le registre vivent dans deux processus : ce module borne
le peu qui traverse la socket loopback. Une session illisible dégrade la
ligne en « non attribué » plutôt que de faire tomber le lot entier.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Emitter on the MCP process side

**Files:**
- Create: `src/brain_v42/mcp/activity_reporter.py`
- Modify: `src/brain_v42/mcp/provenance_middleware.py`, `src/brain_v42/config.py`
- Test: `tests/unit/test_activity_reporter.py`

**Interfaces:**
- Consumes: task 3 (`_report`), task 6 (feed format).
- Produces: `ActivityReporter(url: str, timeout: float = 1.0, max_in_flight: int = 8)` with `report(actor: str, session_id: str | None) -> None` and `async close() -> None`; `get_activity_reporter() -> ActivityReporter | None`.

**Constraint: emission must never slow down or break a tool call.** It runs as a background task, capped, and any error is swallowed after logging.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_activity_reporter.py`:

```python
"""L'émetteur d'activité ne doit ni bloquer ni casser un appel de tool."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from brain_v42.mcp.activity_reporter import ActivityReporter

FAKE_UUID = "12345678-1234-4abc-8def-1234567890ab"


@pytest.mark.asyncio
async def test_report_posts_the_observation() -> None:
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity")
    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock()
        reporter.report("brain-v42", FAKE_UUID)
        await reporter.drain()
        client.post.assert_awaited_once()
        sent = json.loads(client.post.await_args.kwargs["content"])
    assert sent == {
        "observations": [{"actor": "brain-v42", "session": FAKE_UUID, "calls": 1}]
    }
    await reporter.close()


@pytest.mark.asyncio
async def test_absent_session_is_omitted_from_the_wire() -> None:
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity")
    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock()
        reporter.report("codex", None)
        await reporter.drain()
        sent = json.loads(client.post.await_args.kwargs["content"])
    assert sent == {"observations": [{"actor": "codex", "calls": 1}]}
    await reporter.close()


@pytest.mark.asyncio
async def test_transport_failure_is_swallowed() -> None:
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity")
    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock(side_effect=OSError("sidecar down"))
        reporter.report("brain-v42", None)
        await reporter.drain()  # must not raise
    await reporter.close()


@pytest.mark.asyncio
async def test_saturation_drops_instead_of_blocking() -> None:
    reporter = ActivityReporter(
        url="http://127.0.0.1:9200/v1/client-activity", max_in_flight=1
    )
    release = asyncio.Event()

    async def slow_post(*_args: Any, **_kwargs: Any) -> None:
        await release.wait()

    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock(side_effect=slow_post)
        reporter.report("brain-v42", None)
        await asyncio.sleep(0)
        for _ in range(10):
            reporter.report("brain-v42", None)  # must return control immediately
        assert reporter.dropped == 10
        release.set()
        await reporter.drain()
    await reporter.close()
```

- [ ] **Step 2: Verify the failure**

Run: `pytest tests/unit/test_activity_reporter.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
"""Émetteur d'observations d'activité vers le sidecar métriques.

Feu-et-oubli borné : le registre vit dans un autre processus, et un sidecar
lent ou arrêté ne doit jamais ralentir ni casser un appel de tool. Sous
saturation, on préfère perdre une observation que faire attendre l'appelant —
la perte est comptée dans ``dropped``.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import structlog

from brain_v42.config import get_settings

logger = structlog.get_logger(__name__)

_reporter: ActivityReporter | None = None


class ActivityReporter:
    def __init__(
        self,
        url: str,
        timeout: float = 1.0,
        max_in_flight: int = 8,
    ) -> None:
        self._url = url
        self._client = httpx.AsyncClient(timeout=timeout)
        self._max_in_flight = max_in_flight
        self._pending: set[asyncio.Task[None]] = set()
        self.dropped = 0

    def report(self, actor: str, session_id: str | None) -> None:
        """Signaler un appel client. Ne bloque jamais, ne lève jamais."""
        if len(self._pending) >= self._max_in_flight:
            self.dropped += 1
            return
        observation: dict[str, object] = {"actor": actor, "calls": 1}
        if session_id is not None:
            observation["session"] = session_id
        body = json.dumps({"observations": [observation]})
        task = asyncio.create_task(self._post(body))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _post(self, body: str) -> None:
        try:
            await self._client.post(
                self._url,
                content=body,
                headers={"Content-Type": "application/json"},
            )
        except Exception:
            logger.debug("activity_reporter.post_failed", exc_info=True)

    async def drain(self) -> None:
        """Attendre les émissions en vol. Réservé aux tests et à l'arrêt."""
        while self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)

    async def close(self) -> None:
        await self.drain()
        await self._client.aclose()


def set_activity_reporter(reporter: ActivityReporter | None) -> None:
    """Poser un émetteur explicite. Réservé aux tests."""
    global _reporter
    _reporter = reporter


def get_activity_reporter() -> ActivityReporter | None:
    """Renvoyer l'émetteur, construit à la première utilisation.

    Construction paresseuse plutôt que câblage dans le cycle de vie du serveur :
    `mcp` est bâti au niveau module (voir le commentaire de `mcp/server.py:170`),
    et la première émission a lieu dans une boucle déjà tournante. Aucune
    fermeture n'est câblée — le client meurt avec le processus, et un
    `aclose()` à l'arrêt n'apporterait rien à un émetteur feu-et-oubli.
    """
    global _reporter
    if _reporter is None:
        settings = get_settings()
        if not settings.client_activity_reporting_enabled:
            return None
        _reporter = ActivityReporter(url=settings.client_activity_url)
    return _reporter
```

`report` must remain **synchronous**: the middleware calls it without `await`, so no latency is added to the tool call.

Wire `_report` into `provenance_middleware.py` — import `get_activity_reporter` from `brain_v42.mcp.activity_reporter`:

```python
    def _report(self, actor: str, session: str | None) -> None:
        reporter = get_activity_reporter()
        if reporter is not None:
            reporter.report(actor, session)
```

Add the settings in `src/brain_v42/config.py`, right after `metrics_host` (line 109):

```python
    client_activity_reporting_enabled: bool = True
    client_activity_url: str = "http://127.0.0.1:9200/v1/client-activity"
```

**No modification to `mcp/server.py`.** The emitter is born on the first emission; the middleware is already registered there at line 261.

The tests must reset the global state to zero so it does not leak from one test to another:

```python
@pytest.fixture(autouse=True)
def _reset_reporter() -> Any:
    from brain_v42.mcp import activity_reporter

    activity_reporter.set_activity_reporter(None)
    yield
    activity_reporter.set_activity_reporter(None)
```

- [ ] **Step 4: Verify it passes**

Run: `pytest tests/unit/test_activity_reporter.py tests/unit/test_provenance_middleware.py -v`
Expected: PASS

- [ ] **Step 5: Verify the full green**

```bash
pytest tests/unit -q && ruff check src/ tests/ && ruff format --check src/ tests/ && mypy src/
```

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/mcp/activity_reporter.py src/brain_v42/mcp/provenance_middleware.py src/brain_v42/config.py tests/unit/test_activity_reporter.py
git commit -m "$(cat <<'EOF'
feat(mcp): pousser l'activité observée vers le sidecar métriques

Feu-et-oubli borné : un sidecar lent ou arrêté ne doit jamais ralentir un
appel de tool. Sous saturation on perd l'observation plutôt que de faire
attendre l'appelant, et la perte est comptée.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Merge into the registry

The heart of the batch. This is where the join exists, or does not.

**Files:**
- Modify: `src/brain_v42/metrics/client_activity.py`
- Test: `tests/unit/test_client_activity.py`

**Interfaces:**
- Consumes: tasks 4, 5, 6.
- Produces: on `ClientActivityRegistry` — `ingest_claude_otlp_json(payload: bytes) -> None`, `record_observations(observations: tuple[ClientObservation, ...]) -> None`, and `snapshot()` which gains the `clients` key without losing `active_convs`, `ctx_tokens` or `activeConvs`.

**Row model:**

| `kind` | Internal key | `id` | Fields filled |
|--------|-------------|------|----------------|
| `session` | HMAC pseudonym of the UUID | `<agent>-<32hex>` | all those for which a source exists |
| `unattributed` | `actor:<actor>` | `unattributed:<actor>` | `actor`, `brain_calls`, `last_seen_s`; the rest `null` |

A `session` row may have only the OTLP side (`actor` and `brain_calls` at `null`) or only the brain side (`tokens`, `turns`, `cost`, `model`, `agent` at `null`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_client_activity.py`:

```python
import json

from brain_v42.metrics.client_observation import ClientObservation

FAKE_UUID = "12345678-1234-4abc-8def-1234567890ab"
SECRET = b"\x02" * 32


def _claude_payload(session_id: str = FAKE_UUID) -> bytes:
    return json.dumps({
        "resourceLogs": [{"scopeLogs": [{"logRecords": [
            {"timeUnixNano": "1", "attributes": [
                {"key": "event.name", "value": {"stringValue": "user_prompt"}},
                {"key": "session.id", "value": {"stringValue": session_id}},
            ]},
            {"timeUnixNano": "2", "attributes": [
                {"key": "event.name", "value": {"stringValue": "api_request"}},
                {"key": "session.id", "value": {"stringValue": session_id}},
                {"key": "model", "value": {"stringValue": "claude-opus-5"}},
                {"key": "input_tokens", "value": {"intValue": 10}},
                {"key": "cache_read_tokens", "value": {"intValue": 1000}},
                {"key": "cache_creation_tokens", "value": {"intValue": 190}},
                {"key": "cost_usd", "value": {"doubleValue": 0.05}},
            ]},
        ]}]}]
    }).encode()


def _rows(registry: ClientActivityRegistry) -> dict[str, dict[str, object]]:
    return {row["id"]: row for row in registry.snapshot()["clients"]}


class TestJoin:
    def test_same_session_from_both_sources_yields_one_row(self) -> None:
        """La jointure fonctionne — mais AUCUN client ne la déclenche aujourd'hui.

        Mesuré le 2026-08-06 (docs/upstream/2026-08-06-claude-otlp-session-join.md) :
        ni Claude Code ni Codex ne savent déclarer leur session dans un en-tête MCP.
        Ce test garde la mécanique vivante et testée pour le jour où l'un des deux
        le pourra. Il construit donc une observation que la production ne produit
        pas encore — c'est délibéré, ne pas le « corriger » en test d'absence.
        """
        registry = ClientActivityRegistry(secret=SECRET)
        registry.ingest_claude_otlp_json(_claude_payload())
        registry.record_observations(
            (ClientObservation(actor="brain-v42", session_id=FAKE_UUID, calls=4),)
        )
        rows = list(_rows(registry).values())
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == "session"
        assert row["agent"] == "claude"
        assert row["actor"] == "brain-v42"
        assert row["turns"] == 1
        assert row["tokens"] == 1200
        assert row["cost"] == 0.05
        assert row["brain_calls"] == 4

    def test_tokens_sum_the_three_input_counters(self) -> None:
        """input_tokens seul mentirait d'un facteur 1000.

        Relevé réel : input_tokens=10 pour un contexte de 18590 tokens, le reste
        étant dans cache_read_tokens et cache_creation_tokens.
        """
        registry = ClientActivityRegistry(secret=SECRET)
        registry.ingest_claude_otlp_json(_claude_payload())
        assert next(iter(_rows(registry).values()))["tokens"] == 10 + 1000 + 190

    def test_claude_today_yields_two_rows_not_one(self) -> None:
        """Le cas NOMINAL mesuré : Claude ne déclare pas sa session.

        Son en-tête arrive en gabarit non expansé, que normalize_session rejette.
        On obtient donc une ligne OTLP-only et une ligne résiduelle distinctes —
        exactement la situation de Codex. C'est le comportement attendu en
        production, pas une dégradation accidentelle.
        """
        registry = ClientActivityRegistry(secret=SECRET)
        registry.ingest_claude_otlp_json(_claude_payload())
        registry.record_observations(
            (ClientObservation(actor="brain-v42", session_id=None, calls=4),)
        )
        rows = _rows(registry)
        assert len(rows) == 2
        assert rows["unattributed:brain-v42"]["brain_calls"] == 4
        assert rows["unattributed:brain-v42"]["tokens"] is None

    def test_raw_uuid_never_appears_in_the_payload(self) -> None:
        registry = ClientActivityRegistry(secret=SECRET)
        registry.ingest_claude_otlp_json(_claude_payload())
        assert FAKE_UUID not in json.dumps(registry.snapshot())

    def test_otlp_only_row_has_no_brain_columns(self) -> None:
        registry = ClientActivityRegistry(secret=SECRET)
        registry.ingest_claude_otlp_json(_claude_payload())
        row = next(iter(_rows(registry).values()))
        assert row["actor"] is None
        assert row["brain_calls"] is None

    def test_brain_only_session_row_has_no_token_columns(self) -> None:
        registry = ClientActivityRegistry(secret=SECRET)
        registry.record_observations(
            (ClientObservation(actor="red-lab", session_id=FAKE_UUID, calls=2),)
        )
        row = next(iter(_rows(registry).values()))
        assert row["kind"] == "session"
        assert row["brain_calls"] == 2
        assert row["tokens"] is None
        assert row["cost"] is None
        assert row["agent"] is None


class TestUnattributed:
    def test_observation_without_session_yields_a_residual_row(self) -> None:
        registry = ClientActivityRegistry(secret=SECRET)
        registry.record_observations(
            (ClientObservation(actor="codex", session_id=None, calls=7),)
        )
        row = _rows(registry)["unattributed:codex"]
        assert row["kind"] == "unattributed"
        assert row["actor"] == "codex"
        assert row["brain_calls"] == 7
        assert row["tokens"] is None

    def test_residual_and_otlp_rows_coexist_for_codex(self) -> None:
        """Codex sort en N conversations PLUS une ligne non attribuée.

        Ce n'est pas un doublon : sa config MCP n'expose aucun identifiant de
        conversation, donc ses appels de tools ne sont attribuables à aucune
        de ses conversations. Le panneau montre ce trou.
        """
        registry = ClientActivityRegistry(secret=SECRET)
        registry.ingest_claude_otlp_json(_claude_payload())
        registry.record_observations(
            (ClientObservation(actor="codex", session_id=None, calls=3),)
        )
        assert len(_rows(registry)) == 2

    def test_calls_accumulate_across_observations(self) -> None:
        registry = ClientActivityRegistry(secret=SECRET)
        for _ in range(3):
            registry.record_observations(
                (ClientObservation(actor="codex", session_id=None, calls=1),)
            )
        assert _rows(registry)["unattributed:codex"]["brain_calls"] == 3


class TestBoundsAndLegacyShape:
    def test_stale_rows_expire(self) -> None:
        clock = iter([0.0, 0.0, 10_000.0, 10_000.0])
        registry = ClientActivityRegistry(secret=SECRET, clock=lambda: next(clock))
        registry.record_observations(
            (ClientObservation(actor="codex", session_id=None, calls=1),)
        )
        assert registry.snapshot()["clients"] == []

    def test_legacy_keys_survive(self) -> None:
        registry = ClientActivityRegistry(secret=SECRET)
        snapshot = registry.snapshot()
        assert "active_convs" in snapshot
        assert "ctx_tokens" in snapshot
        assert "activeConvs" in snapshot
```

- [ ] **Step 2: Verify the failure**

Run: `pytest tests/unit/test_client_activity.py -v`
Expected: FAIL — `AttributeError: 'ClientActivityRegistry' object has no attribute 'ingest_claude_otlp_json'`

- [ ] **Step 3: Implement**

In `client_activity.py`, add the required imports:

```python
from brain_v42.metrics.claude_telemetry import decode_claude_logs
from brain_v42.metrics.client_observation import ClientObservation
```

`client_observation.py` imports `provenance`, `claude_telemetry` imports
`codex_telemetry`: no cycle, `client_activity` sits at the end of the chain.

Add next to `_Conversation`:

```python
@dataclass(frozen=True, slots=True)
class _BrainActivity:
    actor: str
    calls: int
    last_seen: float
```

Add a second dictionary to the registry, `self._brain: dict[str, _BrainActivity]`, whose key is the HMAC pseudonym when a session is declared, and `f"actor:{actor}"` otherwise.

Add the three methods:

```python
    def ingest_claude_otlp_json(self, payload: bytes) -> None:
        """Appliquer un lot Claude Code après validation complète."""
        records = decode_claude_logs(payload)
        with self._lock:
            now = self._clock()
            started = self._wall_clock().strftime("%H:%M")
            conversations = self._prune_conversations(dict(self._conversations), now)
            receipt_order = self._receipt_order

            for record in records:
                pseudonym = self._pseudonym(record.session_id, agent="claude")
                current = conversations.get(pseudonym) or _Conversation(
                    pseudonym=pseudonym,
                    started=started,
                    turns=0,
                    tokens=0,
                    token_timestamp=None,
                    model="unknown",
                    last_seen=now,
                    receipt_order=receipt_order,
                    agent="claude",
                    cost=None,
                )
                turns = current.turns + (
                    1 if record.event_name == "user_prompt" else 0
                )
                # The real context is the sum of the three input counters.
                # input_tokens alone is 10 where the context is actually 18590
                # (measured on 2026-08-06): showing it alone would lie by a
                # factor of 1000. We keep the value of the LAST event, not a
                # running total: it is a context size, not a throughput.
                context = [
                    value
                    for value in (
                        record.input_tokens,
                        record.cache_read_tokens,
                        record.cache_creation_tokens,
                    )
                    if value is not None
                ]
                tokens, token_timestamp = current.tokens, current.token_timestamp
                if context and (
                    token_timestamp is None
                    or (record.timestamp or 0) > token_timestamp
                ):
                    tokens = sum(context)
                    token_timestamp = record.timestamp
                cost = current.cost
                if record.cost_usd is not None:
                    cost = (cost or 0.0) + record.cost_usd

                receipt_order += 1
                conversations[pseudonym] = replace(
                    current,
                    turns=turns,
                    tokens=tokens,
                    token_timestamp=token_timestamp,
                    model=record.model if record.model != "unknown" else current.model,
                    cost=cost,
                    last_seen=now,
                    receipt_order=receipt_order,
                )

            self._conversations = self._trim_conversations(conversations)
            self._receipt_order = receipt_order

    def record_observations(self, observations: tuple[ClientObservation, ...]) -> None:
        """Appliquer un lot d'observations brain-side."""
        with self._lock:
            now = self._clock()
            brain = {
                key: value
                for key, value in self._brain.items()
                if now - value.last_seen < ACTIVITY_TTL_SECONDS
            }
            for observation in observations:
                key = (
                    self._pseudonym(observation.session_id, agent="claude")
                    if observation.session_id is not None
                    else f"actor:{observation.actor}"
                )
                current = brain.get(key)
                brain[key] = _BrainActivity(
                    actor=observation.actor,
                    calls=(current.calls if current else 0) + observation.calls,
                    last_seen=now,
                )
            if len(brain) > MAX_ACTIVE_CONVERSATIONS:
                newest = sorted(brain.items(), key=lambda kv: kv[1].last_seen, reverse=True)
                brain = dict(newest[:MAX_ACTIVE_CONVERSATIONS])
            self._brain = brain
```

`_pseudonym` now takes the agent as a parameter to prefix the pseudonym (`codex-…` / `claude-…`), which keeps the current shape for Codex. The HMAC salt remains distinct per agent so that two agents cannot collide on the same UUID:

```python
    def _pseudonym(self, identifier: str, agent: str = "codex") -> str:
        digest = hmac.new(
            self._secret,
            f"{agent}-conversation-id\0".encode("ascii") + identifier.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return f"{agent}-{digest[:32]}"
```

**Caution**: this salt change would break `tests/unit/test_codex_telemetry_endpoint.py` if it pins a literal pseudonym. Check beforehand, and if that is the case, keep the historical Codex salt exactly — `b"codex-conversation-id\0"` — and introduce the new salt only for `claude`.

Finally, `snapshot()` builds `clients` by merging the two dictionaries:

```python
            rows: list[dict[str, object]] = []
            for item in ordered:  # OTLP conversations, already sorted
                brain = self._brain.get(item.pseudonym)
                rows.append({
                    "id": item.pseudonym,
                    "kind": "session",
                    "agent": item.agent,
                    "actor": brain.actor if brain else None,
                    "started": item.started,
                    "last_seen_s": int(now - item.last_seen),
                    "model": item.model if item.model != "unknown" else None,
                    "turns": item.turns,
                    "tokens": item.tokens,
                    "cost": item.cost,
                    "brain_calls": brain.calls if brain else None,
                })

            joined = {item.pseudonym for item in ordered}
            for key, brain in sorted(
                self._brain.items(), key=lambda kv: kv[1].last_seen, reverse=True
            ):
                if key in joined:
                    continue
                attributed = not key.startswith("actor:")
                rows.append({
                    "id": key if attributed else f"unattributed:{brain.actor}",
                    "kind": "session" if attributed else "unattributed",
                    "agent": None,
                    "actor": brain.actor,
                    "started": None,
                    "last_seen_s": int(now - brain.last_seen),
                    "model": None,
                    "turns": None,
                    "tokens": None,
                    "cost": None,
                    "brain_calls": brain.calls,
                })
```

Add `"clients": rows` to the returned dictionary, **without removing** `active_convs`, `ctx_tokens` or `activeConvs`.

Add to the existing `_Conversation` the fields `agent: str` (default `"codex"`) and `cost: float | None` (default `None`), and pass `agent="codex"` in `ingest_otlp_json`.

- [ ] **Step 4: Verify it passes**

```bash
pytest tests/unit/test_client_activity.py tests/unit/test_codex_telemetry_endpoint.py -v
```
Expected: PASS on both sides. The existing Codex tests remain the proof of no regression.

- [ ] **Step 5: Verify the full green**

```bash
pytest tests/unit -q && ruff check src/ tests/ && ruff format --check src/ tests/ && mypy src/
```

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/metrics/client_activity.py tests/unit/test_client_activity.py
git commit -m "$(cat <<'EOF'
feat(metrics): fusionner OTLP et activité brain en lignes de client

Jointure dans l'espace des pseudonymes : les deux côtés sont hachés avec
le secret de processus, l'UUID brut ne ressort jamais. Une activité sans
session déclarée donne une ligne résiduelle « non attribué » plutôt
qu'une corrélation inventée.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Sidecar receivers and cockpit exposure

**Files:**
- Modify: `src/brain_v42/metrics/server.py`, `src/brain_v42/metrics/cockpit.py`
- Test: `tests/unit/test_client_activity_endpoint.py`, `tests/unit/test_metrics_cockpit_collector.py`

**Interfaces:**
- Consumes: tasks 5, 6, 8.
- Produces: routes `POST /v1/client-activity` and `POST /v1/logs/claude` on the sidecar; `clients` key in the `/api/cockpit` payload.

Two distinct OTLP routes rather than a single receiver that guesses the schema: guessing would require probing the attributes of a payload not yet validated.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_client_activity_endpoint.py`. The harness follows that of
`tests/unit/test_codex_telemetry_endpoint.py`: the `aiohttp_client` fixture from
`pytest-aiohttp` for the nominal path, `make_mocked_request` with a fake transport
for peer rejections.

```python
"""Frontière HTTP des récepteurs d'activité, loopback-only et bornés."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from aiohttp.test_utils import make_mocked_request

from brain_v42.metrics.client_activity import ClientActivityRegistry
from brain_v42.metrics.client_observation import MAX_OBSERVATION_BYTES
from brain_v42.metrics.server import MetricsServer

FAKE_UUID = "12345678-1234-4abc-8def-1234567890ab"
JSON_HEADERS = {"Content-Type": "application/json"}


def _registry() -> ClientActivityRegistry:
    return ClientActivityRegistry(secret=b"\x03" * 32)


def _server(registry: ClientActivityRegistry) -> MetricsServer:
    return MetricsServer(
        MagicMock(),
        MagicMock(),
        host="127.0.0.1",
        codex_registry=registry,
    )


def _loopback_transport(host: str) -> Any:
    transport = MagicMock()
    transport.get_extra_info.return_value = (host, 54321)
    return transport


def _claude_payload(session_id: str = FAKE_UUID) -> bytes:
    return json.dumps({
        "resourceLogs": [{"scopeLogs": [{"logRecords": [
            {"timeUnixNano": "1", "attributes": [
                {"key": "event.name", "value": {"stringValue": "user_prompt"}},
                {"key": "session.id", "value": {"stringValue": session_id}},
            ]},
        ]}]}]
    }).encode()


async def test_valid_observation_is_accepted_and_applied(aiohttp_client: Any) -> None:
    registry = _registry()
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(
        "/v1/client-activity",
        data=json.dumps(
            {"observations": [{"actor": "brain-v42", "session": FAKE_UUID, "calls": 2}]}
        ).encode(),
        headers=JSON_HEADERS,
    )

    assert response.status == 200
    assert registry.snapshot()["clients"][0]["brain_calls"] == 2


async def test_non_loopback_peer_is_forbidden() -> None:
    server = _server(_registry())
    request = make_mocked_request(
        "POST",
        "/v1/client-activity",
        headers=JSON_HEADERS,
        transport=_loopback_transport("192.168.1.50"),
    )

    response = await server._handle_client_activity(request)

    assert response.status == 403


async def test_oversized_body_is_rejected(aiohttp_client: Any) -> None:
    client = await aiohttp_client(_server(_registry())._build_app())

    response = await client.post(
        "/v1/client-activity",
        data=b"x" * (MAX_OBSERVATION_BYTES + 1),
        headers=JSON_HEADERS,
    )

    assert response.status == 413


async def test_malformed_body_is_rejected(aiohttp_client: Any) -> None:
    client = await aiohttp_client(_server(_registry())._build_app())

    response = await client.post(
        "/v1/client-activity", data=b"not json", headers=JSON_HEADERS
    )

    assert response.status == 400


async def test_claude_logs_route_feeds_the_registry(aiohttp_client: Any) -> None:
    registry = _registry()
    client = await aiohttp_client(_server(registry)._build_app())

    response = await client.post(
        "/v1/logs/claude", data=_claude_payload(), headers=JSON_HEADERS
    )

    assert response.status == 200
    assert registry.snapshot()["clients"][0]["agent"] == "claude"


async def test_routes_are_absent_on_a_non_loopback_bind() -> None:
    app = MetricsServer(
        MagicMock(), MagicMock(), host="0.0.0.0", codex_registry=_registry()
    )._build_app()
    paths = {route.resource.canonical for route in app.router.routes()}
    assert "/v1/client-activity" not in paths
    assert "/v1/logs/claude" not in paths
```

`_loopback_transport` and `MAX_OBSERVATION_BYTES` must match what
`server.py` actually checks: reread `_has_loopback_tcp_peer`
(`server.py:55`) before writing the mock, the shape of `peername` depends on it.

Add to `tests/unit/test_metrics_cockpit_collector.py`:

```python
@pytest.mark.asyncio
async def test_cockpit_exposes_clients_without_dropping_legacy_keys() -> None:
    registry = ClientActivityRegistry(secret=b"\x04" * 32)
    registry.record_observations(
        (ClientObservation(actor="codex", session_id=None, calls=5),)
    )
    collector = CockpitCollector(
        collector=_stub_collector(),
        session_factory=_stub_session_factory(),
        codex_registry=registry,
    )
    payload = await collector.snapshot()
    assert payload["clients"][0]["brain_calls"] == 5
    assert "activeConvs" in payload
    assert payload["metrics"]["active_convs"] == 0
```

- [ ] **Step 2: Verify the failure**

Run: `pytest tests/unit/test_client_activity_endpoint.py tests/unit/test_metrics_cockpit_collector.py -v`
Expected: FAIL — 404 routes, then `KeyError: 'clients'`

- [ ] **Step 3: Implement**

In `server.py`, factor the existing hardening of `_handle_codex_logs` into a common helper, then register the routes under the same `_is_loopback_bind(self._host)` condition:

```python
        if _is_loopback_bind(self._host):
            app.router.add_post("/v1/logs", self._handle_codex_logs)
            app.router.add_post("/v1/logs/claude", self._handle_claude_logs)
            app.router.add_post("/v1/client-activity", self._handle_client_activity)
```

`_handle_claude_logs` mirrors `_handle_codex_logs` identically, calling `ingest_claude_otlp_json`.

`_handle_client_activity` applies the same loopback peer check and semaphore, reads a body bounded by `MAX_OBSERVATION_BYTES`, decodes with `decode_observations` and applies via `record_observations`. The same exceptions give the same statuses: `CodexTelemetryLimitError` → 413, `CodexTelemetryMalformedError` → 400.

In `cockpit.py`, add `"clients": codex_activity["clients"]` to the returned dictionary, and extend the no-registry fallback:

```python
        codex_activity = (
            self._codex_registry.snapshot()
            if self._codex_registry is not None
            else {"active_convs": 0, "ctx_tokens": 0, "activeConvs": [], "clients": []}
        )
```

- [ ] **Step 4: Verify it passes**

```bash
pytest tests/unit/test_client_activity_endpoint.py tests/unit/test_metrics_cockpit_collector.py tests/unit/test_codex_telemetry_endpoint.py tests/unit/test_cockpit_endpoint.py -v
```
Expected: PASS

- [ ] **Step 5: Verify the full green**

```bash
pytest tests/unit -q && ruff check src/ tests/ && ruff format --check src/ tests/ && mypy src/
```

- [ ] **Step 6: Commit**

```bash
git add src/brain_v42/metrics/server.py src/brain_v42/metrics/cockpit.py tests/unit/test_client_activity_endpoint.py tests/unit/test_metrics_cockpit_collector.py
git commit -m "$(cat <<'EOF'
feat(metrics): recevoir l'OTLP Claude et les observations, exposer clients[]

Deux routes distinctes plutôt qu'un récepteur qui devine le schéma :
deviner obligerait à sonder une charge non encore validée. Le contrat
reste additif, activeConvs est intact.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: "Live workload" panel in red-monitor

**Different repo.** `cd ~/hawkixs_infra/git_repo/ReD_v1/projects/red-monitor`. Read its `CLAUDE.md` before starting: conventions and test commands belong to it.

**Files:**
- Modify: `frontend/src/tabs/brain/BrainActivity.jsx`, `frontend/src/tabs/brain/brainPresentation.js`, `frontend/src/tabs/brain/BrainStatusBar.jsx`
- Test: `frontend/src/tabs/brain/Brain.test.jsx`

**Interfaces:**
- Consumes: the `clients[]` key of `/api/brain/live`, produced by task 9.
- Produces: nothing downstream.

**No Go work.** `internal/web/brain.go` re-proxies the raw bytes: the new fields arrive on their own.

- [ ] **Step 1: Write the failing tests**

In `frontend/src/tabs/brain/Brain.test.jsx`:

```jsx
const clientsPayload = {
  ...payload,
  clients: [
    {
      id: 'claude-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      kind: 'session',
      agent: 'claude',
      actor: 'brain-v42',
      started: '12:31',
      last_seen_s: 4,
      model: 'claude-opus-5',
      turns: 12,
      tokens: 128000,
      cost: 1.23,
      brain_calls: 37,
    },
    {
      id: 'unattributed:codex',
      kind: 'unattributed',
      agent: null,
      actor: 'codex',
      started: null,
      last_seen_s: 11,
      model: null,
      turns: null,
      tokens: null,
      cost: null,
      brain_calls: 9,
    },
  ],
};

test('renders one row per client', () => {
  render(() => <BrainActivity live={clientsPayload} />);
  expect(screen.getAllByRole('article')).toHaveLength(2);
});

test('shows an em dash for unmeasured columns, never a zero', () => {
  render(() => <BrainActivity live={clientsPayload} />);
  const residual = screen.getByTestId('client-unattributed:codex');
  expect(residual.textContent).toContain('—');
  expect(residual.textContent).not.toMatch(/\b0 tokens\b/);
});

test('labels the residual row so the gap is readable', () => {
  render(() => <BrainActivity live={clientsPayload} />);
  expect(screen.getByTestId('client-unattributed:codex').textContent)
    .toContain('non attribué');
});

test('carries the declared-not-proven caveat in the panel', () => {
  render(() => <BrainActivity live={clientsPayload} />);
  expect(screen.getByTestId('brain-clients').textContent)
    .toMatch(/déclaré par le client/i);
});

test('falls back to an empty state without clients', () => {
  render(() => <BrainActivity live={{ ...payload, clients: [] }} />);
  expect(screen.getByText(/no active clients/i)).toBeInTheDocument();
});
```

- [ ] **Step 2: Verify the failure**

Run: `cd frontend && npm test -- Brain.test.jsx`
Expected: FAIL — the panel still reads `live.activeConvs`.

- [ ] **Step 3: Implement**

In `BrainActivity.jsx`: replace `props.live.activeConvs` with `props.live.clients`, the title `Codex activity` with `Live workload`, the `data-testid` `brain-codex` with `brain-clients`, and add `data-testid={`client-${client.id}`}` on each `<article>`.

Each column goes through a formatter that renders `—` for `null` or `undefined`: `formatCompactNumber`, `formatCost` and `formatPercent` already do this (`brainPresentation.js:20-47`). Do not write `client.tokens || 0` — that would be exactly the cosmetic `0` the spec forbids.

A `kind === 'unattributed'` row carries a distinct class and the label "non attribué — la session n'est pas déclarée".

Add a permanent note below the list: "acteur et session déclarés par le client, non prouvés".

In `brainPresentation.js`, `shortPseudonym` returns `'anonymous'` and no longer `'codex-anonymous'`. In `BrainStatusBar.jsx:55`, `label="Codex"` becomes `label="Clients"` and the value counts `clients.length`.

- [ ] **Step 4: Verify it passes**

```bash
cd frontend && npm test
```
Expected: PASS, including the existing `Brain.test.jsx` tests.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/tabs/brain/
git commit -m "$(cat <<'EOF'
🎨 feat(frontend): généraliser le panneau Codex en Live workload

Une ligne par client, colonnes remplies par la source disponible et tiret
cadratin partout ailleurs. La ligne « non attribué » rend lisible le fait
que Codex ne dit pas quelle conversation appelle le brain.
EOF
)"
```

The red-monitor repo uses emoji commits (see `git log`) and does not have brain_v42's `Co-Authored-By` convention: follow local usage.

---

### Task 11: End-to-end verification and network boundary statement

**Files:**
- Modify: `CLAUDE.md` (brain_v42)

**Interfaces:**
- Consumes: all previous tasks.
- Produces: nothing.

**What this task cannot do alone.** Two of the three delivered paths are
*silent when they fail*: the brain emitter ships CLOSED (`client_activity_reporting_enabled=False`,
commit `e8951011`) and the OTLP receiver answers `200` even when it drops the entire batch. A
verification that just looks at the panel concludes "no traffic" just as easily
in front of a healthy but idle chain as in front of a badly wired one. Steps 3, 4 and 5
exist to remove that ambiguity; do not skip them.

- [ ] **Step 0: Verify that the units will actually run the delivered code**

The two systemd units run on the **production root**, not on a worktree:
`brain-metrics.service` has `WorkingDirectory=/home/hawixs/hawkixs_infra/git_repo/brain_v42` and
the venv's editable install (`_editable_impl_brain_v42.pth`) points to
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/src`. Restarting from a worktree therefore
changes nothing: the branch must first be merged into the root.

```bash
BRAIN_ROOT=/home/hawixs/hawkixs_infra/git_repo/brain_v42
$BRAIN_ROOT/.venv/bin/python -c "import brain_v42.metrics.server as m; print(m.__file__)"
grep -n "v1/logs/claude\|v1/client-activity" $BRAIN_ROOT/src/brain_v42/metrics/server.py
grep -n "client_activity_reporting_enabled" $BRAIN_ROOT/src/brain_v42/config.py
```
Expected: `__file__` is under `$BRAIN_ROOT/src/`, and all three `grep` find their line. If
one is missing, **stop**: the merge is not done and everything below would measure the old code.

- [ ] **Step 1: Restart both processes and probe the brain receiver**

The units are named **`brain-metrics.service`** and **`brain-mcp-http.service`**. Neither
`brain-v42-metrics` nor `brain-v42-mcp` exists: those names come back with `Unit … could not be found`,
**no process restarts**, and the following `curl` then queries the old code — a `404`
that would be read as a broken route. Check the names rather than copying them:

```bash
systemctl --user list-units --type=service --all --no-legend | grep -E 'brain-(metrics|mcp-http)\.service'
systemctl --user restart brain-metrics.service brain-mcp-http.service
systemctl --user is-active brain-metrics.service brain-mcp-http.service
```
Expected: `active` twice. (`brain-mcp-http.service` has two `ExecStartPre` preflight checks —
graph projector and MCP port; a restart failure shows up in
`journalctl --user -u brain-mcp-http.service -n 30`.)

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H 'Content-Type: application/json' \
  -d '{"observations":[{"actor":"probe","calls":1}]}' \
  http://127.0.0.1:9200/v1/client-activity
```
Expected: `200`. Reading grid for another status: `404` = the process is running code
prior to task 9, or its bind is not loopback (the three routes are registered only
if `METRICS_HOST` is loopback); `403` = the peer is not loopback; `415` = the
`Content-Type` header got lost; `413`/`400` = the body is out of bounds or not the expected shape.

- [ ] **Step 2: Verify that the probe row appears**

```bash
curl -s http://127.0.0.1:9200/api/cockpit \
  | /home/hawixs/hawkixs_infra/git_repo/brain_v42/.venv/bin/python -c "
import json, sys
payload = json.load(sys.stdin)
if 'clients' not in payload:
    sys.exit('PAS DE CLE clients — le sidecar tourne sur du code anterieur a la tache 9')
print(json.dumps(payload['clients'], indent=2, ensure_ascii=False))
"
```
Expected: a row `"id": "unattributed:probe"`, `"kind": "unattributed"`, `"brain_calls": 1`, and
`null` — never `0` — in `agent`, `started`, `model`, `turns`, `tokens`, `cost`. The registry's
retention is 600 s (`ACTIVITY_TTL_SECONDS`): read within the ten minutes following the probe.

A **missing** `clients` key is the measured symptom of an unmigrated sidecar (the payload from
before task 9 only exposes `activeConvs`): go back to Step 0 rather than concluding "no client".

At this point, only the **receiver** is proven. Nothing has yet proven that a client emits.

- [ ] **Step 3: Arm the brain emitter — and know how to disarm it**

Without this action, the MCP process emits **no** observation and the panel verification
(Step 7) is unreachable, whatever else happens. Two caches make arming inseparable from the
restart:

- `get_activity_reporter()` is a lazy singleton — once `_reporter` is built, it is
  **never** rebuilt and the killswitch is **never** reread;
- `get_settings()` is `@lru_cache(maxsize=1)` — the killswitch is therefore read at most once per
  process, even before any construction.

Arming without restarting `brain-mcp-http.service` therefore does nothing at all.

Arming via a systemd drop-in, on the model already present in `brain-metrics.service.d/transport.conf`.
A drop-in rather than the shared `.env`: it is reversible with a single `rm`, it only affects
the MCP unit (the sidecar reads the same `.env` via its `WorkingDirectory`), and a systemd
environment variable takes precedence over the dotenv in pydantic-settings.

```bash
mkdir -p ~/.config/systemd/user/brain-mcp-http.service.d
cat > ~/.config/systemd/user/brain-mcp-http.service.d/client-activity.conf <<'EOF'
[Service]
# Arming client activity reporting — operator action, Live workload panel rollout.
Environment=CLIENT_ACTIVITY_REPORTING_ENABLED=true
EOF
systemctl --user daemon-reload
systemctl --user restart brain-mcp-http.service
systemctl --user show brain-mcp-http.service -p Environment | grep CLIENT_ACTIVITY
```
Expected: the variable appears in `Environment=`, and the unit is `active`.

Do **not** set `CLIENT_ACTIVITY_URL`: the default is `http://127.0.0.1:9200/v1/client-activity`
and a `field_validator` already rejects any non-loopback target — a LAN value would fail the
startup, not leak the data.

**Disarming** (run as-is if the verification goes wrong):

```bash
rm ~/.config/systemd/user/brain-mcp-http.service.d/client-activity.conf
rmdir --ignore-fail-on-non-empty ~/.config/systemd/user/brain-mcp-http.service.d
systemctl --user daemon-reload
systemctl --user restart brain-mcp-http.service
systemctl --user show brain-mcp-http.service -p Environment | grep CLIENT_ACTIVITY || echo "désarmé"
```
Proof of disarming: after a brain tool call, no new `unattributed:` row
appears in `/api/cockpit`.

> **Arbitration for the operator to decide, not this plan**: the drop-in survives a reboot. Decide
> explicitly, at the end of the rollout, whether the emitter stays permanently armed (and then
> migrates to the shared `.env`, with an update to `CLAUDE.md`'s Configuration section) or is
> disarmed after the verification. Do not let this choice happen by oversight.

- [ ] **Step 4: Point the Claude exporter at the Claude route**

This is the costliest trap of the rollout, because it is **silent**. Measured on Claude Code
2.1.223: the bundled OTLP exporter resolves the URL via
`convertLegacyHttpOptions(config, "LOGS", "v1/logs", …)`, i.e. `url = <signal-specific> ?? <generic + "/v1/logs">`.

| Variable | Measured handling | Route reached |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:9200` | `v1/logs` **suffixed** | `/v1/logs` — **Codex** decoder |
| `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://127.0.0.1:9200/v1/logs/claude` | used **as-is** (`new URL(v).toString()`) | `/v1/logs/claude` — Claude decoder |

The signal-specific variable **wins** over the generic one.

Sent to `/v1/logs`, a Claude batch enters the Codex decoder: `event.name` is bare there
(`user_prompt`, `api_request`) whereas `_DIRECT_EVENTS` and `_COMPLETION_EVENTS` are all
prefixed with `codex.`. Every record is therefore dropped, **and the receiver answers `200 {}`**. A
misconfigured endpoint is rigorously indistinguishable from an absence of traffic.

The symmetric case is true and reads in the same code: a **Codex** batch arriving on
`/v1/logs/claude` runs into `_KNOWN_EVENTS = {"user_prompt", "api_request"}`;
`codex.user_prompt` is not among them, the record is dropped before `session.id` is even read,
and the receiver answers `200 {}`. Neither error reports itself.

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://127.0.0.1:9200/v1/logs/claude
unset OTEL_EXPORTER_OTLP_ENDPOINT
```

Without a trailing slash: `aiohttp` routes are exact and `new URL(...).toString()` preserves
the path verbatim (measured). Do not leave the generic one set "just in case": it would mask
the misconfiguration on any machine where the signal-specific one got lost.

Codex, for its part, keeps `/v1/logs` in `~/.codex/config.toml`
(`endpoint = "http://127.0.0.1:9200/v1/logs"`, cf. `docs/plans/2026-07-19-codex-otlp-cockpit-bridge-plan.md:392`).
Batches leave every `OTEL_LOGS_EXPORT_INTERVAL` ms, **5000 by default** (measured):
wait at least ten seconds after a prompt before concluding anything.

- [ ] **Step 5: Distinguish "no traffic" from "traffic dropped"**

Two halves, two different instruments. Neither one reads in the panel.

**a) Brain side (`/v1/client-activity`).** The emitter counts refusals separately
(`ActivityReporter.refused`, distinct from `dropped` which only counts local backpressure) and
logs the status alone. The counter lives in the MCP process's memory and is exposed by no
route: the observable trace is the log line. The MCP server's structlog `[debug]` lines
do show up in the journal without changing `LOG_LEVEL` (measured: `access_log.purged`,
`metrics_flusher.flushed` are visible there).

Make a brain tool call from a Claude session, then:

```bash
journalctl --user -u brain-mcp-http.service --since "-5 min" --no-pager \
  | grep -E 'activity_reporter\.(refused|post_failed|unavailable)'
```

| Observation | Diagnosis |
|---|---|
| a row `unattributed:<actor>` in `/api/cockpit` | accepted — nothing to do |
| no row + `activity_reporter.refused status=404` | emitted and **refused**: route absent (old process, or non-loopback bind) |
| no row + `activity_reporter.refused status=403/413/415/400` | emitted and **refused**: peer, bounds or format |
| no row + `activity_reporter.post_failed error=ConnectError` | sidecar stopped or wrong port |
| no row + `activity_reporter.unavailable` | unreadable settings on the MCP side |
| no row **and none of these lines** | nothing was ever emitted: killswitch still closed (Step 3 not applied or not restarted), or no brain tool called |

**b) Claude OTLP side (`/v1/logs/claude`).** Here there is **neither a counter nor a log**: the sidecar
answers `200 {}` whether it kept everything or dropped everything, and the metrics unit emits no access
line per request (measured: `journalctl --user -u brain-metrics.service | grep -c 'POST /v1/'` is `0`).
Silence is therefore not proof. The only honest discriminant is to first prove that
the exporter emits, with a throwaway receiver on another port — the same one as in task 1, Step 3:

```bash
# terminal A — throwaway receiver
/home/hawixs/hawkixs_infra/git_repo/brain_v42/.venv/bin/python - <<'PY'
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        print("captured", self.path, len(body), "bytes")
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
HTTPServer(("127.0.0.1", 4318), H).serve_forever()
PY

# terminal B — Claude session with the same configuration as in Step 4, but:
#   export OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://127.0.0.1:4318/v1/logs/claude
```

- nothing captured after a prompt and ten seconds → **no traffic**: the exporter is not armed
  in this session's environment (`CLAUDE_CODE_ENABLE_TELEMETRY` / `OTEL_LOGS_EXPORTER`);
- captured on `4318`, then no `"agent": "claude"` row in `/api/cockpit` once repointed
  at `9200` → **traffic dropped**: reread the path (Step 4), then the event and
  attribute names against the oracle `tests/fixtures/claude_otlp_logs.json`.

Stop the throwaway receiver before continuing.

- [ ] **Step 6: Verify the refusal from a non-loopback peer, on all three routes**

```bash
ssh arman@192.168.1.11 'for p in /v1/logs /v1/logs/claude /v1/client-activity; do \
  curl -s -m 5 -o /dev/null -w "$p %{http_code}\n" -X POST \
    -H "Content-Type: application/json" -d "{}" http://192.168.1.12:9200$p; done'
```

Expected: `000` for all three, with `curl` failing to connect (exit code 7) or
timing out (28). The sidecar is bound to `127.0.0.1`: the connection must not be established.

**Any** HTTP response — including a `404` — means the socket answered on the LAN:
**stop and report it**, the bind is not what the configuration claims. A `404` is
in fact the most likely admission: on a non-loopback bind, the three routes are not
registered at all. A LAN bind therefore does not expose these receivers — it silently
disables them, which breaks the brain half of the panel without saying so.

- [ ] **Step 7: Verify the panel**

Prerequisite: task 10 is merged and red-monitor restarted; Steps 3 and 4 are applied.

Open red-monitor, Brain tab. The spike verdict is **`JOINTURE IMPOSSIBLE`**
(`docs/upstream/2026-08-06-claude-otlp-session-join.md`): do not expect a single row per
session. The real expectation is:

- **one OTLP row per live Claude session** (`kind: session`, `agent: claude`), with `actor` and
  `brain_calls` at `—`: OTLP does not know which actor calls the brain;
- **one `unattributed:<actor>` row per ACTOR** on the brain side — not per session. `X-Brain-Agent`
  holds `${PWD}` reduced to the project basename, so several Claude sessions from the same project
  aggregate into a **single** row whose `brain_calls` is their sum;
- one row per live Codex conversation, plus `unattributed:codex` if Codex calls the brain;
- em dashes everywhere nothing is measured, and **no `0`** in a column without a source.

Cross-check at least one value against the source: the row read in the panel must match the
same row from `curl -s http://127.0.0.1:9200/api/cockpit` (Step 2). The panel reads
`/api/brain/live`, which re-proxies these bytes: a divergence is a rendering defect, not a measurement one.

- [ ] **Step 8: Declare the network boundary**

In `CLAUDE.md`, "Tracked network boundary" block, declare **both** faces of the change —
not just the receivers:

1. **Inputs.** The metrics sidecar registers three push receivers, and only if its bind is
   loopback: `/v1/logs` (Codex OTLP), `/v1/logs/claude` (Claude Code OTLP, **new**) and
   `/v1/client-activity` (brain-side observations, **new**). All bounded (body size, in-flight
   requests, `identity` encoding, a single `Content-Type: application/json`), loopback peer
   required, fail-closed, **with no application authentication**. A non-loopback bind does not
   expose them: it does not register them.
2. **Output, new.** The MCP process (`brain-mcp-http.service`) becomes a **local HTTP client** of
   the sidecar: a fire-and-forget POST to `CLIENT_ACTIVITY_URL` on every outermost tool call.
   This is a network output new to this process, constrained to loopback by a
   `field_validator` on `client_activity_url`, shipped **closed**
   (`CLIENT_ACTIVITY_REPORTING_ENABLED=false`); arming it is an operator action (Step 3).

- [ ] **Step 9: Commit**

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs(claude): déclarer les trois récepteurs et la sortie neuve du MCP

Le rollout ajoute deux récepteurs loopback au sidecar métriques, et fait
du processus MCP un client HTTP local de ce sidecar — une sortie réseau
qu'il n'avait pas. La frontière réseau du projet est suivie de près :
elle se déclare au moment du rollout, pas après.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Order and dependencies

```
1 (spike) ──┬─→ 5 (décodeur Claude) ──┐
            │                          ├─→ 8 (fusion) ─→ 9 (routes + cockpit) ─→ 10 (panneau) ─→ 11 (vérif)
2 (session) ┴─→ 3 (garde) ─→ 7 (émetteur) ─┤
                              6 (fil) ──────┘
            4 (déménagement) ─────────────────┘
```

Tasks 2, 4 and 6 depend on nothing and can be done in any order. Task 4 must precede task 8. Task 1 is a gate for tasks 5 and 8.

## What this plan does not do

- No persistence: the registry is in memory and loses everything on restart. An aggregated history is a separate effort.
- No removal of `activeConvs` from the payload: deferred until after the panel has been switched over and observed.
- No change to `process_metrics` or `flusher.py`.
- No link to `brain_sessions`: the table is not a source of liveness.
- No change to the sidecar's authentication.
