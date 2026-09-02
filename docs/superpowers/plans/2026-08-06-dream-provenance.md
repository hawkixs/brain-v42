# Corpus Provenance — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Distinguish the dream's metabolism from human activity, so that PROMOTE's anti-rejudging cache, its maturity filter and the preflight gate stop invalidating themselves.

**Architecture:** A FastMCP middleware sets the caller's identity (`X-Brain-Agent`, already sent) into a `ContextVar` read when access is enqueued. Migration 041 adds `access_log.actor`, an `access_count_human` counter and a `content_updated_at` date fed by conditional triggers on value change. Three consumers switch to these signals.

**Tech Stack:** Python 3.12, FastMCP 3.4.2, SQLAlchemy 2.0 async, Alembic, PostgreSQL 16, pytest / pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-06-dream-provenance-design.md`

## Global Constraints

- **Strict TDD, non-negotiable** (CLAUDE.md): red test first, never implementation before a failing test. Never modify a test to make code pass.
- **DO NOT modify `public.update_updated_at()`.** Migration 039 pins it by SHA256 `83ca0f7a3230405dae8b4f4e692b4983869b58e4225b6e60bbf96db3f6ae9a59` and by length `96` bytes; its downgrade verifies it. Making this trigger conditional would break 039. Create a **separate** function.
- **No backfill.** `content_updated_at` stays `NULL` and `access_count_human` stays `0` on existing rows: "never measured".
- **Do not touch any dream killswitch or environment variable.** `BRAIN_DREAM_*` stays as is.
- **Do not touch** `instrument_embedding`, `InstrumentedEmbeddingService`, `InstrumentedReranker`, `InstrumentedGraphService`: these are not tools.
- Green before each commit: `pytest tests/unit`, `ruff check src/ tests/`, `ruff format --check src/ tests/`, `mypy src/`.
- Brain project key: always `brain-v42`.

## File Structure

| File | Responsibility |
|---|---|
| `src/brain_v42/provenance.py` | **Created.** Actor ContextVar, `normalize_agent()`, `is_human_actor()`. No MCP or DB dependency. |
| `src/brain_v42/mcp/provenance_middleware.py` | **Created.** `ProvenanceMiddleware` — sets the actor on `on_call_tool`. |
| `src/brain_v42/metrics/instrument.py` | **Modified.** `_normalize_agent` moves to `provenance.py`; `instrument_tool` reads the ContextVar. |
| `src/brain_v42/mcp/server.py` | **Modified.** Installs the middleware unconditionally. |
| `src/brain_v42/db/tables.py` | **Modified.** Columns `actor`, `access_count_human`, `content_updated_at`. |
| `alembic/versions/041_corpus_provenance.py` | **Created.** Columns + function + 5 triggers. |
| `src/brain_v42/services/access_logger.py` | **Modified.** `log_access` captures the actor at enqueue time. |
| `src/brain_v42/repositories/pg_access_log.py` | **Modified.** Aggregation with `count_human`. |
| `src/brain_v42/services/decay_flusher.py` | **Modified.** Writes `access_count_human`. |
| `scripts/dream/promote_prepare.py` | **Modified.** Cache + maturity. |
| `scripts/dream/dream_preflight.py` | **Modified.** Mutation signal. |

**Spec correction, to apply:** `decay_flusher._ENTITY_TABLES` contains **six** tables (`decisions, learnings, snippets, runbooks, adrs, indexed_plans`). `access_count_human` must therefore exist on all six, otherwise `_update_entities_batch` will fail on plans. `content_updated_at` stays on the **five** knowledge tables: `indexed_plans` is neither a promotion candidate nor part of the preflight signal.

---

### Task 1: Spike — measure what we don't yet know

**Measured results (2026-08-06):**

- **Q1 — YES.** `get_http_headers()` is reachable from `on_call_tool` in real HTTP
  (via `_serve_loopback` + `_mcp_client`). The `X-Brain-Agent` header sent is seen
  intact by the middleware. → Task 3 reads the header in the middleware (nominal
  path, no fail-closed variant to write).
- **Q2 — not empirically measured by this spike; assumed answer = gateway
  only, consistent with `tool_catalog.py`, to verify in Task 3.** The spike code
  in the plan (Step 3) registers `inner_tool` directly on a bare `FastMCP` and
  calls it by its real name via `client.call_tool("inner_tool", ...)` — without
  ever applying `apply_tool_catalog_profile(mcp, "compact")` or going through
  `brain_call_tool`. The name seen by the middleware (`['inner_tool']`) is thus
  trivially that of the only registered tool; this run proves nothing about the
  behavior of the `brain_call_tool` gateway in `compact` profile — there simply
  is no gateway in this setup. The "gateway only" answer remains the one
  already written in the plan (line 151, *"Expected answer"*) and by reading
  `tool_catalog.py` (`_RequestAwareBM25SearchTransform` only exposes the 7
  lifecycle tools + `brain_find_tool`/`brain_call_tool`), but is not confirmed
  by a direct measurement here. Practical consequence: this blocks nothing in
  this plan (the plan's note already says so); the optional follow-up "remove
  the monkey-patch" still needs to be verified with a setup that actually
  activates the `compact` catalog, not with this spike.

**Raw output:**

```
tests/unit/mcp/test_spike_middleware_context.py::test_spike_headers_and_granularity SPIKE headers_reachable(in-memory): True
SPIKE tool names seen by middleware: ['inner_tool']
PASSED
tests/unit/mcp/test_spike_middleware_context.py::test_spike_headers_over_real_http SPIKE Q1 headers_is_none: False
SPIKE Q1 agent seen     : dream-codex-scan
SPIKE Q2 tool names     : ['inner_tool']
PASSED
======================== 2 passed, 2 warnings in 2.27s =========================
```

**Divergence from the plan:** Step 2 expects `headers_reachable` to be `False`
in memory transport ("necessarily None"). The measurement gives `True`: in
in-memory transport, FastMCP 3.4.2's `get_http_headers()` does not return
`None` but an empty dict (`{} is not None` → `True`), so `headers_reachable`
is `True` even though no HTTP request took place. Without consequence: the
plan already states that only the HTTP measurement (Step 3) counts, and that
one confirms Q1 unambiguously (`headers_is_none: False`, `agent seen:
dream-codex-scan`).

**Q2 re-measurement (2026-08-06, real compact catalog) — the middleware sees
BOTH names.** Throwaway spike `tests/unit/mcp/test_spike_q2_compact_gateway.py`:
bare `FastMCP`
+ `inner_tool` + `apply_tool_catalog_profile(mcp, "compact")` + spy
middleware, call via `client.call_tool("brain_call_tool", {"name": "inner_tool", ...})`
in memory transport. Raw output:

```
SPIKE Q2 catalog exposé      : ['brain_call_tool', 'brain_find_tool']
SPIKE Q2 résultat passerelle : 2
SPIKE Q2 noms vus par on_call_tool : ['brain_call_tool', 'inner_tool']
```

The assumed answer ("gateway only") was **false**: `BaseSearchTransform`'s
gateway executes the internal call via
`await ctx.fastmcp.call_tool(name, arguments)` without disabling
`run_middleware` (default `True`, fastmcp 3.4.2), so the `on_call_tool` chain
is re-entered with the real tool name. The dispatch is server-side,
independent of the transport: the in-memory measurement suffices for Q2
(unlike Q1, which was about HTTP headers). Consequences:

- the optional follow-up "remove the monkey-patch" is **viable** — brain
  ticket `c352eaaa-3e3a-4e57-92c4-986b6d87512f`, corrective learning
  `310a9953` (refutes `b77dba43`);
- a metrics middleware will need to **ignore gateway names**
  (`brain_call_tool`, `brain_find_tool`) or double-count;
- `ProvenanceMiddleware` fires twice per compact call (gateway then internal
  tool) — harmless, it sets the same actor twice.

Two unverified hypotheses drive the rest of this plan. This spike measures them and **ships no production code**. Its result is written into the plan.

**Files:**
- Create (throwaway): `tests/unit/mcp/test_spike_middleware_context.py`

**Interfaces:**
- Consumes: nothing.
- Produces: two boolean answers, recorded at the end of the task.

- [ ] **Step 1: Write the spike**

```python
"""SPIKE JETABLE — à supprimer en fin de Task 1. Ne rien importer d'ici."""

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware

observed: dict[str, object] = {}


class _Spy(Middleware):
    async def on_call_tool(self, context, call_next):  # type: ignore[no-untyped-def]
        observed.setdefault("names", []).append(context.message.name)  # type: ignore[union-attr]
        observed["headers_reachable"] = get_http_headers() is not None
        return await call_next(context)


async def test_spike_headers_and_granularity() -> None:
    mcp = FastMCP("spike")

    @mcp.tool
    async def inner_tool(x: int) -> int:
        return x * 2

    mcp.add_middleware(_Spy())

    async with Client(mcp) as client:
        await client.call_tool("inner_tool", {"x": 3})

    print("SPIKE headers_reachable(in-memory):", observed.get("headers_reachable"))
    print("SPIKE tool names seen by middleware:", observed.get("names"))
```

- [ ] **Step 2: Run the spike**

Run: `uv run pytest tests/unit/mcp/test_spike_middleware_context.py -v -s`
Expected: the test PASSES and prints two `SPIKE …` lines. In memory transport `headers_reachable` will be `False` — that's normal, there is no HTTP request.

- [ ] **Step 3: Measure over real HTTP**

This is the measurement that counts: memory transport has no headers. `tests/unit/mcp/test_dream_capability_http.py` provides two reusable helpers — `_serve_loopback(app)` (asynccontextmanager, line 366, yields a `base_url`) and `_mcp_client(base_url, token, headers=...)`. Import them rather than rewriting them.

Add to the spike:

```python
from tests.unit.mcp.test_dream_capability_http import _mcp_client, _serve_loopback

http_observed: dict[str, object] = {}


class _HttpSpy(Middleware):
    async def on_call_tool(self, context, call_next):  # type: ignore[no-untyped-def]
        headers = get_http_headers()
        http_observed["headers_is_none"] = headers is None
        http_observed["agent"] = (headers or {}).get("x-brain-agent")
        http_observed.setdefault("names", []).append(context.message.name)  # type: ignore[union-attr]
        return await call_next(context)


async def test_spike_headers_over_real_http() -> None:
    mcp = FastMCP("spike-http")

    @mcp.tool
    async def inner_tool(x: int) -> int:
        return x * 2

    mcp.add_middleware(_HttpSpy())

    async with _serve_loopback(mcp.http_app()) as base_url:
        async with _mcp_client(
            base_url, None, headers={"X-Brain-Agent": "dream-codex-scan"}
        ) as client:
            await client.call_tool("inner_tool", {"x": 3})

    print("SPIKE Q1 headers_is_none:", http_observed.get("headers_is_none"))
    print("SPIKE Q1 agent seen     :", http_observed.get("agent"))
    print("SPIKE Q2 tool names     :", http_observed.get("names"))
```

If `_mcp_client`'s signature doesn't accept `token=None`, pass the token expected by the harness — the spike doesn't test authorization.

Run: `uv run pytest tests/unit/mcp/test_spike_middleware_context.py -v -s`
Expected: three `SPIKE …` lines. Q1 is answered by `agent seen == "dream-codex-scan"`.

- [ ] **Step 4: Record the two answers**

Write the answers at the top of this plan, under this task title:

- **Q1 — Is `get_http_headers()` reachable from `on_call_tool` over HTTP?**
  - **YES** → Task 3 reads the header in the middleware (nominal path).
  - **NO** → Task 3 reads nothing: the middleware calls `set_current_actor(normalize_agent(...))` from the value `instrument_tool` already reads. Adapt Task 3 Step 3 accordingly and note it here.
- **Q2 — Does the middleware see the internal tool's name, or only the gateway's?**
  - Expected answer: gateway only in `compact` profile. This answer **blocks nothing in this plan**: it only decides whether the optional follow-up "remove the monkey-patch" is viable. Record it and move on.

- [ ] **Step 5: Delete the spike, commit nothing but the recording**

```bash
rm tests/unit/mcp/test_spike_middleware_context.py
git add docs/superpowers/plans/2026-08-06-dream-provenance.md
git commit -m "docs(dream): consigner les mesures du spike middleware de provenance"
```

---

### Task 2: `provenance` module — ContextVar and classification

**Files:**
- Create: `src/brain_v42/provenance.py`
- Test: `tests/unit/test_provenance.py`

**Interfaces:**
- Consumes: nothing (leaf module, no MCP or DB dependency).
- Produces:
  - `normalize_agent(value: str | None) -> str`
  - `set_current_actor(actor: str) -> None`
  - `get_current_actor() -> str`
  - `is_human_actor(actor: str | None) -> bool`
  - `UNKNOWN_ACTOR: str` (equals `"unknown"`)

- [ ] **Step 1: Write the failing tests**

```python
"""Tests de la couche de provenance — classification et contexte d'acteur."""

from __future__ import annotations

import asyncio

from brain_v42.provenance import (
    UNKNOWN_ACTOR,
    get_current_actor,
    is_human_actor,
    normalize_agent,
    set_current_actor,
)


class TestNormalizeAgent:
    def test_absolute_path_reduces_to_basename(self) -> None:
        assert normalize_agent("/home/hawixs/git/red-lab") == "red-lab"

    def test_trailing_slash_is_stripped(self) -> None:
        assert normalize_agent("/home/hawixs/git/red-lab/") == "red-lab"

    def test_static_label_passes_through(self) -> None:
        assert normalize_agent("dream-codex-synth") == "dream-codex-synth"

    def test_blank_becomes_unknown(self) -> None:
        assert normalize_agent("   ") == UNKNOWN_ACTOR
        assert normalize_agent(None) == UNKNOWN_ACTOR

    def test_unexpanded_template_collapses(self) -> None:
        assert normalize_agent("${PWD}") == "_unexpanded"

    def test_bare_root_becomes_unknown(self) -> None:
        assert normalize_agent("/") == UNKNOWN_ACTOR


class TestIsHumanActor:
    def test_interactive_session_is_human(self) -> None:
        assert is_human_actor("red-lab") is True
        assert is_human_actor("brain_v42") is True

    def test_dream_phase_is_not_human(self) -> None:
        assert is_human_actor("dream-codex-synth") is False
        assert is_human_actor("dream-codex-reorg") is False

    def test_unknown_is_not_human(self) -> None:
        """Fail-closed : un appelant non identifié ne débloque aucune promotion."""
        assert is_human_actor(UNKNOWN_ACTOR) is False
        assert is_human_actor(None) is False
        assert is_human_actor("") is False

    def test_unexpanded_is_not_human(self) -> None:
        assert is_human_actor("_unexpanded") is False


class TestCurrentActor:
    def test_default_is_unknown(self) -> None:
        """Contexte neuf : un ContextVar non posé rend sa valeur par défaut.

        `Context()` est vide — ne PAS utiliser `get_current_actor()` nu ici, un
        test voisin ayant déjà posé une valeur dans le contexte courant.
        """
        from contextvars import Context

        assert Context().run(get_current_actor) == UNKNOWN_ACTOR

    def test_set_then_get(self) -> None:
        set_current_actor("red-lab")
        assert get_current_actor() == "red-lab"

    def test_blank_set_falls_back_to_unknown(self) -> None:
        set_current_actor("")
        assert get_current_actor() == UNKNOWN_ACTOR

    async def test_value_does_not_leak_across_tasks(self) -> None:
        """Chaque requête doit voir son propre acteur, pas celui d'une voisine."""
        seen: list[str] = []

        async def worker(actor: str) -> None:
            set_current_actor(actor)
            await asyncio.sleep(0)
            seen.append(get_current_actor())

        await asyncio.gather(worker("red-lab"), worker("dream-codex-scan"))
        assert sorted(seen) == ["dream-codex-scan", "red-lab"]
```

- [ ] **Step 2: Verify the failure**

Run: `uv run pytest tests/unit/test_provenance.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'brain_v42.provenance'`

- [ ] **Step 3: Write the minimal implementation**

```python
"""Provenance du corpus — qui a touché quelle entité.

Le header ``X-Brain-Agent`` est déclaré par le client, donc falsifiable : c'est
un signal d'hygiène, pas une frontière de sécurité — même posture que le
``client_key`` de session, « déclarée, pas prouvée ».

Module feuille volontairement : aucune dépendance MCP ni base de données, pour
qu'il soit importable depuis la couche transport comme depuis les services.
"""

from __future__ import annotations

import os
from contextvars import ContextVar

UNKNOWN_ACTOR = "unknown"
UNEXPANDED_ACTOR = "_unexpanded"

# System actor prefixes that self-declare. An actor absent from this list
# and not a sentinel is treated as human.
_SYSTEM_ACTOR_PREFIXES = ("dream-codex-",)
_NON_HUMAN = frozenset({UNKNOWN_ACTOR, UNEXPANDED_ACTOR, ""})

_current_actor: ContextVar[str] = ContextVar(
    "brain_v42_current_actor",
    default=UNKNOWN_ACTOR,
)


def normalize_agent(value: str | None) -> str:
    """Réduire un ``X-Brain-Agent`` brut en nom d'acteur propre.

    Les sessions Claude Code interactives envoient ``${PWD}``, que Claude Code
    expanse en chemin absolu du projet : on garde le basename. Les libellés de
    service statiques passent tels quels. Une session démon (pas de ``PWD``
    dans l'environnement) laisse le gabarit non expansé, qu'on effondre sur un
    seul seau plutôt que d'inventer un acteur par littéral.
    """
    value = (value or "").strip()
    if not value:
        return UNKNOWN_ACTOR
    if "${" in value:
        return UNEXPANDED_ACTOR
    if value.startswith("/"):
        return os.path.basename(value.rstrip("/")) or UNKNOWN_ACTOR
    return value


def set_current_actor(actor: str) -> None:
    """Poser l'acteur pour la durée du contexte courant."""
    _current_actor.set(actor or UNKNOWN_ACTOR)


def get_current_actor() -> str:
    """Lire l'acteur courant. ``unknown`` hors contexte de requête."""
    return _current_actor.get()


def is_human_actor(actor: str | None) -> bool:
    """Vrai si l'acteur est une session humaine.

    Fail-closed : un acteur inconnu ou non expansé n'est PAS humain, donc ne
    peut pas faire franchir à une entité le seuil de maturité de PROMOTE.
    """
    value = (actor or "").strip()
    if value in _NON_HUMAN:
        return False
    return not value.startswith(_SYSTEM_ACTOR_PREFIXES)
```

- [ ] **Step 4: Verify the pass**

Run: `uv run pytest tests/unit/test_provenance.py -v`
Expected: PASS, 14 tests.

- [ ] **Step 5: Green and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/
git add src/brain_v42/provenance.py tests/unit/test_provenance.py
git commit -m "feat(provenance): classifier l'acteur d'un appel et le porter en contexte"
```

---

### Task 3: Provenance middleware

**Files:**
- Create: `src/brain_v42/mcp/provenance_middleware.py`
- Modify: `src/brain_v42/mcp/server.py` (near `mcp = FastMCP(...)`, line 255)
- Modify: `src/brain_v42/metrics/instrument.py` (`_normalize_agent` and `instrument_tool`)
- Test: `tests/unit/mcp/test_provenance_middleware.py`

**Interfaces:**
- Consumes: `set_current_actor`, `normalize_agent`, `get_current_actor` (Task 2).
- Produces: `ProvenanceMiddleware` (class with no constructor argument).

**Scope note:** this middleware **does not carry the metrics**. `instrument_tool` and its monkey-patch stay in place — in `compact` profile they are the only ones to see the real tool name behind the `brain_call_tool` gateway (Task 1, Q2). Only the header read is shared.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests du middleware de provenance — pose de l'acteur sur on_call_tool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.mcp.provenance_middleware import ProvenanceMiddleware
from brain_v42.provenance import UNKNOWN_ACTOR, get_current_actor, set_current_actor


def _context(tool_name: str = "brain_get") -> MagicMock:
    context = MagicMock()
    context.message.name = tool_name
    return context


class TestProvenanceMiddleware:
    async def test_sets_actor_from_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda: {"x-brain-agent": "dream-codex-reorg"},
        )
        seen: list[str] = []

        async def call_next(_ctx: object) -> str:
            seen.append(get_current_actor())
            return "ok"

        result = await ProvenanceMiddleware().on_call_tool(_context(), call_next)

        assert result == "ok"
        assert seen == ["dream-codex-reorg"]

    async def test_missing_header_yields_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda: {},
        )
        set_current_actor("red-lab")
        seen: list[str] = []

        async def call_next(_ctx: object) -> str:
            seen.append(get_current_actor())
            return "ok"

        await ProvenanceMiddleware().on_call_tool(_context(), call_next)
        assert seen == [UNKNOWN_ACTOR]

    async def test_no_http_context_yields_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """En stdio, get_http_headers() retourne None — repli fail-closed."""
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda: None,
        )
        seen: list[str] = []

        async def call_next(_ctx: object) -> str:
            seen.append(get_current_actor())
            return "ok"

        await ProvenanceMiddleware().on_call_tool(_context(), call_next)
        assert seen == [UNKNOWN_ACTOR]

    async def test_actor_is_set_before_handler_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """L'acteur doit être posé AVANT call_next, pas après."""
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda: {"x-brain-agent": "/home/hawixs/git/red-lab"},
        )
        call_next = AsyncMock(return_value="ok")
        await ProvenanceMiddleware().on_call_tool(_context(), call_next)
        call_next.assert_awaited_once()

    async def test_exception_propagates_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda: {"x-brain-agent": "red-lab"},
        )

        async def call_next(_ctx: object) -> str:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await ProvenanceMiddleware().on_call_tool(_context(), call_next)
```

- [ ] **Step 2: Verify the failure**

Run: `uv run pytest tests/unit/mcp/test_provenance_middleware.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'brain_v42.mcp.provenance_middleware'`

- [ ] **Step 3: Write the middleware**

If Task 1 Q1 answered **NO**, replace the body of `on_call_tool` with the variant recorded in Task 1 Step 4 and adapt the Step 1 tests accordingly.

```python
"""Middleware de provenance — pose l'acteur courant pour tout appel de tool.

Installé INCONDITIONNELLEMENT. La provenance ne doit pas dépendre de
l'activation des métriques : `instrument_tool` n'est branché que si un
collecteur est fourni (`brain_tools.py`), et une provenance silencieusement
muette est pire que pas de provenance.

Ne porte pas les métriques : en profil `compact`, les appels transitent par la
passerelle `brain_call_tool` et seul l'enveloppement de la fonction du tool
voit le nom réel.
"""

from __future__ import annotations

from typing import Any

from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware

from brain_v42.provenance import normalize_agent, set_current_actor


class ProvenanceMiddleware(Middleware):
    """Pose l'acteur déclaré par ``X-Brain-Agent`` avant l'exécution du tool."""

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        headers = get_http_headers() or {}
        set_current_actor(normalize_agent(headers.get("x-brain-agent")))
        return await call_next(context)
```

- [ ] **Step 4: Verify the pass**

Run: `uv run pytest tests/unit/mcp/test_provenance_middleware.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Install the middleware unconditionally**

In `src/brain_v42/mcp/server.py`, right after `mcp = FastMCP("brain", mask_error_details=True)` (line 255):

```python
from brain_v42.mcp.provenance_middleware import ProvenanceMiddleware

mcp = FastMCP("brain", mask_error_details=True)
# Provenance: set here rather than in register_tools, to be independent of
# metrics activation and of tool registration order.
# `apply_tool_catalog_profile` and `maybe_apply_code_mode` return the SAME
# object, so this middleware survives both.
mcp.add_middleware(ProvenanceMiddleware())
```

- [ ] **Step 6: Make `instrument_tool` read the ContextVar**

A single point of header reading. In `src/brain_v42/metrics/instrument.py`:

1. Remove the `_normalize_agent` function (lines 24-46) and the import `from fastmcp.server.dependencies import get_http_headers`.
2. Add `from brain_v42.provenance import get_current_actor, normalize_agent`.
3. Add the compatibility alias `_normalize_agent = normalize_agent` — `tests/unit/test_metrics_instrument.py` imports it.
4. In `instrument_tool`, replace the line in the `finally` block:

```python
# before
agent = _normalize_agent((get_http_headers() or {}).get("x-brain-agent"))
# after
agent = get_current_actor()
```

- [ ] **Step 7: Verify no metrics regression**

Run: `uv run pytest tests/unit/test_metrics_instrument.py tests/unit/metrics/ -v`
Expected: PASS. The test `test_decorator_records_successful_call` expects `agent="unknown"` outside an HTTP context — the ContextVar's default value is `UNKNOWN_ACTOR`, so it passes unmodified.

Run: `uv run pytest tests/integration/metrics/test_agent_attribution.py -v`
Expected: PASS. This test sends real headers over HTTP; it validates end-to-end that the middleware does feed the ContextVar. **If it fails, do not modify the test** — it's the signal that the middleware isn't installed on the test server or that Q1 was NO.

- [ ] **Step 8: Green and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/
uv run pytest tests/unit -q
git add src/brain_v42/mcp/provenance_middleware.py src/brain_v42/mcp/server.py \
        src/brain_v42/metrics/instrument.py tests/unit/mcp/test_provenance_middleware.py
git commit -m "feat(provenance): poser l'acteur de l'appelant via un middleware FastMCP"
```

---

### Task 4: Migration 041 — columns, function, triggers

**Files:**
- Create: `alembic/versions/041_corpus_provenance.py`
- Modify: `src/brain_v42/db/tables.py`
- Test: `tests/integration/db/test_migration_041_provenance.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `access_log.actor`, `access_count_human` on 6 tables, `content_updated_at` on 5 tables, function `public.stamp_content_updated_at()`, 5 `trg_<table>_content_updated` triggers.

- [ ] **Step 1: Write the failing integration tests**

```python
"""Migration 041 — la date de contenu ne bouge que sur un changement de valeur."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.integration


class TestContentUpdatedAtTrigger:
    async def test_tag_change_does_not_stamp_content(self, db_session) -> None:
        """REORG normalise un tag : content_updated_at doit rester NULL."""
        lid = uuid.uuid4()
        await db_session.execute(
            sa.text(
                "INSERT INTO learnings (id, topic, insight, project_key, tags) "
                "VALUES (:id, 'sujet', 'corps', 'brain-v42', ARRAY['a'])"
            ),
            {"id": lid},
        )
        await db_session.execute(
            sa.text("UPDATE learnings SET tags = ARRAY['b'] WHERE id = :id"), {"id": lid}
        )
        value = (
            await db_session.execute(
                sa.text("SELECT content_updated_at FROM learnings WHERE id = :id"), {"id": lid}
            )
        ).scalar_one()
        assert value is None

    async def test_counter_write_does_not_stamp_content(self, db_session) -> None:
        """Le cas qui a produit la boucle de 23 nuits."""
        lid = uuid.uuid4()
        await db_session.execute(
            sa.text(
                "INSERT INTO learnings (id, topic, insight, project_key) "
                "VALUES (:id, 'sujet', 'corps', 'brain-v42')"
            ),
            {"id": lid},
        )
        await db_session.execute(
            sa.text("UPDATE learnings SET access_count = access_count + 1 WHERE id = :id"),
            {"id": lid},
        )
        value = (
            await db_session.execute(
                sa.text("SELECT content_updated_at FROM learnings WHERE id = :id"), {"id": lid}
            )
        ).scalar_one()
        assert value is None

    async def test_real_content_change_stamps(self, db_session) -> None:
        lid = uuid.uuid4()
        await db_session.execute(
            sa.text(
                "INSERT INTO learnings (id, topic, insight, project_key) "
                "VALUES (:id, 'sujet', 'corps', 'brain-v42')"
            ),
            {"id": lid},
        )
        await db_session.execute(
            sa.text("UPDATE learnings SET insight = 'corps révisé' WHERE id = :id"), {"id": lid}
        )
        value = (
            await db_session.execute(
                sa.text("SELECT content_updated_at FROM learnings WHERE id = :id"), {"id": lid}
            )
        ).scalar_one()
        assert value is not None

    async def test_rewriting_identical_content_does_not_stamp(self, db_session) -> None:
        """Sémantique de valeur : recopier le même texte ne rajeunit rien."""
        lid = uuid.uuid4()
        await db_session.execute(
            sa.text(
                "INSERT INTO learnings (id, topic, insight, project_key) "
                "VALUES (:id, 'sujet', 'corps', 'brain-v42')"
            ),
            {"id": lid},
        )
        await db_session.execute(
            sa.text("UPDATE learnings SET insight = 'corps' WHERE id = :id"), {"id": lid}
        )
        value = (
            await db_session.execute(
                sa.text("SELECT content_updated_at FROM learnings WHERE id = :id"), {"id": lid}
            )
        ).scalar_one()
        assert value is None


class TestMigrationShape:
    async def test_update_updated_at_is_untouched(self, db_session) -> None:
        """La 039 épingle cette fonction par SHA256 : la 041 ne doit pas y toucher."""
        digest = (
            await db_session.execute(
                sa.text(
                    "SELECT encode(sha256(convert_to(p.prosrc, 'UTF8')), 'hex') "
                    "FROM pg_proc p WHERE p.proname = 'update_updated_at'"
                )
            )
        ).scalar_one()
        assert digest == (
            "83ca0f7a3230405dae8b4f4e692b4983869b58e4225b6e60bbf96db3f6ae9a59"
        )

    async def test_access_log_has_actor(self, db_session) -> None:
        value = (
            await db_session.execute(
                sa.text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'access_log' AND column_name = 'actor'"
                )
            )
        ).scalar_one()
        assert "unknown" in value

    @pytest.mark.parametrize(
        "table",
        ["learnings", "decisions", "snippets", "runbooks", "adrs", "indexed_plans"],
    )
    async def test_access_count_human_exists(self, db_session, table: str) -> None:
        found = (
            await db_session.execute(
                sa.text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = 'access_count_human'"
                ),
                {"t": table},
            )
        ).scalar_one()
        assert found == 1
```

- [ ] **Step 2: Verify the failure**

Run: `uv run pytest tests/integration/db/test_migration_041_provenance.py -v`
Expected: FAIL — `UndefinedColumn: column "content_updated_at" does not exist`

Warning: run from a clean worktree or with a test `.env`: the trunk's `.env` is the PRODUCTION config and leaks into integration tests via pydantic-settings (learning `54fdfddc`).

- [ ] **Step 3: Write the migration**

```python
"""Distinguer le métabolisme du dream de l'activité humaine.

Revision ID: 041
Revises: 040

`updated_at` confond deux questions : qui a touché la ligne, et si c'est le
contenu qui a changé. Une écriture de compteur — `decay_flusher` incrémentant
`access_count` après une simple lecture — la rajeunit exactement comme une
réécriture humaine. Le cache anti-rejugement de PROMOTE compare un verdict à
cette date : il meurt donc à chaque lecture, et le même learning a été réévalué
23 nuits d'affilée pour le même verdict.

Trois colonnes, aucun backfill. `content_updated_at` NULL et
`access_count_human` 0 se lisent « jamais mesuré » et se réparent d'eux-mêmes
au premier vrai signal.

`content_updated_at` est écrite par TRIGGER, à l'inverse de `focus_updated_at`
(révision 040) qui l'est par code applicatif. La divergence est délibérée :
la clause WHEN ... IS DISTINCT FROM donne la sémantique de VALEUR que la 040
recherchait — recopier le même texte ne rajeunit rien — et le contenu des
entités a de nombreux écrivains (brain_learn, brain_update, REORG, merges de
CLEAN, scripts de backfill) là où le focus n'en a qu'un. Une invariante tenue
par convention sur N écrivains est oubliée par le N+1 ; c'est déjà arrivé ici
(voir repositories/pg_learning.py). Enfin `content_updated_at` PILOTE une
garde, quand `focus_updated_at` informe un humain : le niveau de garantie
exigé n'est pas le même.

La fonction `public.update_updated_at()` n'est PAS modifiée : la révision 039
l'épingle par SHA256 et par longueur, et la rendre conditionnelle rendrait 039
non-downgradable.
"""

import sqlalchemy as sa
from alembic import op

revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None

# Tables tracked by decay: all receive the human counter, because
# decay_flusher._ENTITY_TABLES updates them uniformly.
_COUNTER_TABLES = (
    "learnings",
    "decisions",
    "snippets",
    "runbooks",
    "adrs",
    "indexed_plans",
)

# Knowledge tables: content columns per table. `indexed_plans` is absent —
# neither a promotion candidate, nor part of the preflight signal.
_CONTENT_COLUMNS = {
    "learnings": ("topic", "insight"),
    "decisions": ("title", "description", "reasoning", "consequences"),
    "snippets": ("title", "code"),
    "runbooks": ("title", "description", "trigger", "steps"),
    "adrs": ("title", "context", "decision", "consequences"),
}

_CREATE_FUNCTION = """
CREATE FUNCTION public.stamp_content_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    NEW.content_updated_at := CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$function$;
"""


def upgrade() -> None:
    op.add_column(
        "access_log",
        sa.Column(
            "actor",
            sa.String(64),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
    )

    for table in _COUNTER_TABLES:
        op.add_column(
            table,
            sa.Column(
                "access_count_human",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )

    for table in _CONTENT_COLUMNS:
        op.add_column(
            table,
            sa.Column("content_updated_at", sa.DateTime(timezone=True), nullable=True),
        )

    op.execute(_CREATE_FUNCTION)

    for table, columns in _CONTENT_COLUMNS.items():
        column_list = ", ".join(columns)
        predicate = " OR ".join(
            f"OLD.{c} IS DISTINCT FROM NEW.{c}" for c in columns
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_content_updated
            BEFORE UPDATE OF {column_list} ON public.{table}
            FOR EACH ROW
            WHEN ({predicate})
            EXECUTE FUNCTION public.stamp_content_updated_at();
            """
        )


def downgrade() -> None:
    for table in _CONTENT_COLUMNS:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_content_updated ON public.{table};")
    op.execute("DROP FUNCTION IF EXISTS public.stamp_content_updated_at();")
    for table in _CONTENT_COLUMNS:
        op.drop_column(table, "content_updated_at")
    for table in _COUNTER_TABLES:
        op.drop_column(table, "access_count_human")
    op.drop_column("access_log", "actor")
```

- [ ] **Step 4: Declare the columns in `tables.py`**

In `src/brain_v42/db/tables.py`, add to the `access_log` definition (line 984):

```python
    Column("actor", String(64), nullable=False, server_default=sa.text("'unknown'")),
```

Then, on each of the tables `learnings`, `decisions`, `snippets`, `runbooks`, `adrs`, `indexed_plans`:

```python
    Column("access_count_human", sa.Integer, nullable=False, server_default=sa.text("0")),
```

And on the five knowledge tables only (not `indexed_plans`):

```python
    Column("content_updated_at", DateTime(timezone=True), nullable=True),
```

- [ ] **Step 5: Apply and verify**

```bash
uv run alembic upgrade head
docker exec brain_v42_postgres psql -U brain -d brain -Atc \
  "select version_num from alembic_version;"
```
Expected: `041`

Run: `uv run pytest tests/integration/db/test_migration_041_provenance.py -v`
Expected: PASS, 12 tests (4 on the trigger, 2 on shape, 6 parametrized on `access_count_human`).

- [ ] **Step 6: Verify the downgrade is clean**

```bash
uv run alembic downgrade 040
docker exec brain_v42_postgres psql -U brain -d brain -Atc \
  "select count(*) from pg_proc where proname='stamp_content_updated_at';"
uv run alembic upgrade head
```
Expected: `0` after the downgrade, and the upgrade goes back through without error.

- [ ] **Step 7: Green and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/
git add alembic/versions/041_corpus_provenance.py src/brain_v42/db/tables.py \
        tests/integration/db/test_migration_041_provenance.py
git commit -m "feat(db): dater le contenu et compter les lectures humaines (041)"
```

---

### Task 5: Capture the actor at enqueue time

**Files:**
- Modify: `src/brain_v42/services/access_logger.py`
- Test: `tests/unit/services/test_access_logger_actor.py`

**Interfaces:**
- Consumes: `get_current_actor` (Task 2), column `access_log.actor` (Task 4).
- Produces: every enqueued event dict now carries the `actor` key.

**The trap in this task:** `_flush_batch()` runs in a background task (`_run_loop`, every 5 s), **outside the request context**. Reading the ContextVar there would yield `unknown` for everyone. The actor must be read in `log_access()`, at enqueue time.

- [ ] **Step 1: Write the failing tests**

```python
"""L'acteur doit être capturé à la mise en file, pas au flush."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock
from uuid import uuid4

from brain_v42.provenance import UNKNOWN_ACTOR, set_current_actor
from brain_v42.services.access_logger import AccessLogger


class TestAccessLoggerActor:
    def test_event_carries_current_actor(self) -> None:
        logger = AccessLogger(session_factory=MagicMock())
        set_current_actor("dream-codex-reorg")
        logger.log_access("learning", uuid4(), "get_by_id")

        event = logger._queue.get_nowait()
        assert event["actor"] == "dream-codex-reorg"

    def test_actor_is_frozen_at_enqueue_not_at_flush(self) -> None:
        """Le flush tourne hors contexte de requête : l'acteur doit déjà être figé."""
        logger = AccessLogger(session_factory=MagicMock())

        set_current_actor("red-lab")
        logger.log_access("learning", uuid4(), "get_by_id")
        set_current_actor("dream-codex-synth")
        logger.log_access("learning", uuid4(), "get_by_id")

        first = logger._queue.get_nowait()
        second = logger._queue.get_nowait()
        assert first["actor"] == "red-lab"
        assert second["actor"] == "dream-codex-synth"

    def test_defaults_to_unknown_outside_request(self) -> None:
        logger = AccessLogger(session_factory=MagicMock())
        set_current_actor(UNKNOWN_ACTOR)
        logger.log_access("learning", uuid4(), "search_hit")

        event = logger._queue.get_nowait()
        assert event["actor"] == UNKNOWN_ACTOR
```

- [ ] **Step 2: Verify the failure**

Run: `uv run pytest tests/unit/services/test_access_logger_actor.py -v`
Expected: FAIL — `KeyError: 'actor'`

- [ ] **Step 3: Write the minimal implementation**

In `src/brain_v42/services/access_logger.py`, add the import:

```python
from brain_v42.provenance import get_current_actor
```

Then, in `log_access`, replace the enqueued dict:

```python
    def log_access(self, entity_type: str, entity_id: UUID, access_type: str) -> None:
        """Enqueue an access event. Logs warning and drops if queue is full.

        L'acteur est lu ICI, dans le contexte de la requête. `_flush_batch`
        tourne dans une tâche de fond où le ContextVar vaudrait `unknown`.
        """
        try:
            self._queue.put_nowait(
                {
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "access_type": access_type,
                    "actor": get_current_actor(),
                }
            )
        except asyncio.QueueFull:
            logger.warning(
                "access_logger.queue_full",
                entity_type=entity_type,
                entity_id=str(entity_id),
            )
```

`_flush_batch` doesn't need to change: it does `sa.insert(access_log)` with the dicts as-is, and the `actor` key now maps to an existing column.

- [ ] **Step 4: Verify the pass**

Run: `uv run pytest tests/unit/services/test_access_logger_actor.py tests/unit/services/test_access_logger.py -v`
Expected: PASS. If an existing test builds an expected event without `actor`, **do not modify it to make it pass** — first check that it doesn't describe a behavior you'd be breaking.

- [ ] **Step 5: Green and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/
git add src/brain_v42/services/access_logger.py tests/unit/services/test_access_logger_actor.py
git commit -m "feat(provenance): figer l'acteur d'un accès au moment de la mise en file"
```

---

### Task 6: Aggregate human reads

**Files:**
- Modify: `src/brain_v42/repositories/pg_access_log.py` (`aggregate_in_session`, lines 33-95)
- Modify: `src/brain_v42/services/decay_flusher.py` (`_update_entities_batch`, lines 148-250)
- Test: `tests/unit/repositories/test_pg_access_log_actor.py`

**Interfaces:**
- Consumes: `is_human_actor` (Task 2), `access_log.actor` (Task 4), `actor` key (Task 5).
- Produces: `aggregate_in_session` now returns `{"max_accessed": datetime, "count": int, "count_human": int}` for each `(entity_type, entity_id)`.

**Note:** do NOT touch `aggregate_and_flush` (deprecated, no longer called in production — `tests/unit/services/test_decay_flusher_atomic.py:96` proves it must not be).

- [ ] **Step 1: Write the failing test**

```python
"""L'agrégation sépare les lectures humaines des lectures du dream."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.integration

from brain_v42.repositories.pg_access_log import PgAccessLogRepo


async def _insert(session, entity_id, actor: str) -> None:
    await session.execute(
        sa.text(
            "INSERT INTO access_log (entity_type, entity_id, access_type, actor) "
            "VALUES ('learning', :id, 'get_by_id', :actor)"
        ),
        {"id": entity_id, "actor": actor},
    )


class TestAggregateByActor:
    async def test_splits_human_from_system(self, db_session, session_factory) -> None:
        entity_id = uuid.uuid4()
        await _insert(db_session, entity_id, "red-lab")
        await _insert(db_session, entity_id, "dream-codex-reorg")
        await _insert(db_session, entity_id, "dream-codex-synth")
        await db_session.commit()

        repo = PgAccessLogRepo(session_factory)
        async with session_factory() as session:
            aggregated = await repo.aggregate_in_session(session)
            await session.commit()

        stats = aggregated[("learning", entity_id)]
        assert stats["count"] == 3
        assert stats["count_human"] == 1

    async def test_unknown_actor_is_not_human(self, db_session, session_factory) -> None:
        entity_id = uuid.uuid4()
        await _insert(db_session, entity_id, "unknown")
        await db_session.commit()

        repo = PgAccessLogRepo(session_factory)
        async with session_factory() as session:
            aggregated = await repo.aggregate_in_session(session)
            await session.commit()

        stats = aggregated[("learning", entity_id)]
        assert stats["count"] == 1
        assert stats["count_human"] == 0
```

- [ ] **Step 2: Verify the failure**

Run: `uv run pytest tests/unit/repositories/test_pg_access_log_actor.py -v`
Expected: FAIL — `KeyError: 'count_human'`

- [ ] **Step 3: Aggregate by actor**

In `pg_access_log.py`, the aggregation can't classify in SQL without duplicating the Python rule. So we also group by `actor` and classify on the Python side — a single source of truth for `is_human_actor`.

Replace steps 2 and 3 of `aggregate_in_session`:

```python
        # 2. Aggregate only snapshotted rows, split by actor so the
        #    human/system rule stays in ONE place (brain_v42.provenance).
        stmt = (
            sa.select(
                access_log.c.entity_type,
                access_log.c.entity_id,
                access_log.c.actor,
                sa.func.max(access_log.c.accessed_at).label("max_accessed"),
                sa.func.count().label("cnt"),
            )
            .where(access_log.c.id <= max_id)
            .group_by(access_log.c.entity_type, access_log.c.entity_id, access_log.c.actor)
        )
        result = await session.execute(stmt)
        rows = result.mappings().all()

        if not rows:
            return {}

        # 3. Build result dict, folding the per-actor groups back together
        aggregated: dict[tuple[str, UUID], dict[str, Any]] = {}
        for row in rows:
            key = (row["entity_type"], row["entity_id"])
            entry = aggregated.setdefault(
                key,
                {"max_accessed": row["max_accessed"], "count": 0, "count_human": 0},
            )
            entry["count"] += row["cnt"]
            if is_human_actor(row["actor"]):
                entry["count_human"] += row["cnt"]
            if row["max_accessed"] > entry["max_accessed"]:
                entry["max_accessed"] = row["max_accessed"]
```

Add the import at the top of the file:

```python
from brain_v42.provenance import is_human_actor
```

- [ ] **Step 4: Write the human counter**

In `decay_flusher.py`, `_update_entities_batch` method:

1. Add the column to the selection (after `table.c.access_count`):

```python
            table.c.access_count_human,
```

2. In the loop, after `new_access_count = row["access_count"] + stats["count"]`:

```python
            new_access_count_human = row["access_count_human"] + stats.get("count_human", 0)
```

3. In `params`:

```python
                "access_count_human": new_access_count_human,
```

4. In both `sa.update(...).values(...)` (`upd_same` and `upd_changed`), add:

```python
                    access_count_human=sa.bindparam("access_count_human"),
```

- [ ] **Step 5: Verify the pass**

Run: `uv run pytest tests/unit/repositories/test_pg_access_log_actor.py tests/unit/services/test_decay_flusher.py tests/unit/services/test_decay_flusher_atomic.py -v`
Expected: PASS.

- [ ] **Step 6: Green and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/
uv run pytest tests/unit -q
git add src/brain_v42/repositories/pg_access_log.py src/brain_v42/services/decay_flusher.py \
        tests/unit/repositories/test_pg_access_log_actor.py
git commit -m "feat(provenance): agréger les lectures humaines séparément du dream"
```

---

### Task 7: Rewire PROMOTE

**Files:**
- Modify: `scripts/dream/promote_prepare.py` (`_CANDIDATE_SQL`, lines 29-68)
- Test: `tests/integration/dream/test_promote_prepare_provenance.py`

**Interfaces:**
- Consumes: `content_updated_at`, `access_count_human` (Task 4), fed by Task 6.
- Produces: `fetch_candidates()` keeps exactly its signature and return shape.

- [ ] **Step 1: Write the failing tests**

```python
"""Le pool de PROMOTE cesse de réadmettre un verdict encore valide."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.integration

from scripts.dream.promote_prepare import fetch_candidates


class TestTerminalCache:
    async def test_uncertain_verdict_survives_a_counter_write(
        self, db_session, session_factory
    ) -> None:
        """Le défaut de production : un verdict rendu, puis une lecture, et le
        candidat revenait la nuit suivante."""
        lid = uuid.uuid4()
        await db_session.execute(
            sa.text(
                "INSERT INTO learnings "
                "(id, topic, insight, project_key, access_count, access_count_human, "
                " confidence, created_at) "
                "VALUES (:id, 'sujet', 'corps', 'brain-v42', 9, 9, 'high', "
                "        NOW() - INTERVAL '30 days')"
            ),
            {"id": lid},
        )
        await db_session.execute(
            sa.text(
                "INSERT INTO dream_promotions "
                "(source_learning_id, target_type, created_at) "
                "VALUES (:id, 'classification_uncertain', NOW())"
            ),
            {"id": lid},
        )
        # A read after the verdict — this is what broke the cache.
        await db_session.execute(
            sa.text("UPDATE learnings SET access_count = access_count + 1 WHERE id = :id"),
            {"id": lid},
        )
        await db_session.commit()

        candidates = await fetch_candidates(session_factory, "brain-v42", limit=10)
        assert str(lid) not in {c["id"] for c in candidates}

    async def test_real_content_edit_readmits_the_candidate(
        self, db_session, session_factory
    ) -> None:
        lid = uuid.uuid4()
        await db_session.execute(
            sa.text(
                "INSERT INTO learnings "
                "(id, topic, insight, project_key, access_count, access_count_human, "
                " confidence, created_at) "
                "VALUES (:id, 'sujet', 'corps', 'brain-v42', 9, 9, 'high', "
                "        NOW() - INTERVAL '30 days')"
            ),
            {"id": lid},
        )
        await db_session.execute(
            sa.text(
                "INSERT INTO dream_promotions "
                "(source_learning_id, target_type, created_at) "
                "VALUES (:id, 'classification_uncertain', NOW() - INTERVAL '1 day')"
            ),
            {"id": lid},
        )
        await db_session.execute(
            sa.text("UPDATE learnings SET insight = 'corps révisé' WHERE id = :id"),
            {"id": lid},
        )
        await db_session.commit()

        candidates = await fetch_candidates(session_factory, "brain-v42", limit=10)
        assert str(lid) in {c["id"] for c in candidates}


class TestMaturityGate:
    async def test_dream_reads_alone_do_not_mature_a_learning(
        self, db_session, session_factory
    ) -> None:
        """access_count élevé mais purement machine : pas candidat."""
        lid = uuid.uuid4()
        await db_session.execute(
            sa.text(
                "INSERT INTO learnings "
                "(id, topic, insight, project_key, access_count, access_count_human, "
                " confidence, created_at) "
                "VALUES (:id, 'sujet', 'corps', 'brain-v42', 40, 0, 'high', "
                "        NOW() - INTERVAL '30 days')"
            ),
            {"id": lid},
        )
        await db_session.commit()

        candidates = await fetch_candidates(session_factory, "brain-v42", limit=10)
        assert str(lid) not in {c["id"] for c in candidates}

    async def test_human_reads_mature_a_learning(self, db_session, session_factory) -> None:
        lid = uuid.uuid4()
        await db_session.execute(
            sa.text(
                "INSERT INTO learnings "
                "(id, topic, insight, project_key, access_count, access_count_human, "
                " confidence, created_at) "
                "VALUES (:id, 'sujet', 'corps', 'brain-v42', 40, 4, 'high', "
                "        NOW() - INTERVAL '30 days')"
            ),
            {"id": lid},
        )
        await db_session.commit()

        candidates = await fetch_candidates(session_factory, "brain-v42", limit=10)
        assert str(lid) in {c["id"] for c in candidates}
```

- [ ] **Step 2: Verify the failure**

Run: `uv run pytest tests/integration/dream/test_promote_prepare_provenance.py -v`
Expected: FAIL — the first test fails (the candidate is readmitted), so does the third (`access_count` still suffices).

- [ ] **Step 3: Modify the query**

In `scripts/dream/promote_prepare.py`, two changes in `_CANDIDATE_SQL`:

1. Replace `AND l.access_count >= 3` with:

```sql
      AND l.access_count_human >= 3
```

2. Replace the line `AND u.created_at >= l.updated_at` with:

```sql
            AND u.created_at >= COALESCE(l.content_updated_at, l.created_at)
```

And replace the "Terminal-unpromotable cache" block comment with:

```sql
      -- Terminal-unpromotable cache: skip a learning already judged
      -- classification_uncertain on its CURRENT version. The comparison is
      -- against content_updated_at, NOT updated_at: the latter moves on
      -- every counter write, so a mere read by a later dream phase used to
      -- invalidate the verdict rendered two minutes earlier (observed: a
      -- learning re-evaluated 23 nights in a row). The fallback to
      -- created_at is deliberate — without backfill, content_updated_at is
      -- NULL, and falling back to updated_at would reproduce the defect
      -- identically.
```

Leave `AND NOT (l.confidence = 'low' AND l.access_count < 5)` unchanged: this guardrail is about total volume, not human maturity.

- [ ] **Step 4: Verify the pass**

Run: `uv run pytest tests/integration/dream/test_promote_prepare_provenance.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Prove the production loop stops**

```bash
uv run python -m scripts.dream.promote_prepare --project-key brain-v42 --limit 10 \
  | jq -r '.[].id'
```
Expected: `1d1037e8-acb1-4cb7-b0b5-9ccd3b97c0c0` **absent** from the output. This is acceptance criterion #2 of the spec.

The pool will probably be **empty**: `access_count_human` is 0 everywhere, with no backfill. That's the designed behavior, not a regression — note it and move on.

- [ ] **Step 6: Green and commit**

```bash
uv run ruff check src/ tests/ scripts/ && uv run ruff format --check src/ tests/ scripts/ && uv run mypy src/
git add scripts/dream/promote_prepare.py tests/integration/dream/test_promote_prepare_provenance.py
git commit -m "fix(dream): fonder le cache de PROMOTE sur la date du contenu, pas de la ligne"
```

---

### Task 8: Rewire the preflight gate

**Files:**
- Modify: `scripts/dream/dream_preflight.py` (`_ENTITY_TABLES` and `_fetch_signals`, lines 37-74)
- Test: `tests/unit/dream/test_dream_preflight_provenance.py`

**Interfaces:**
- Consumes: `content_updated_at` (Task 4).
- Produces: `should_skip_opus_phases()` keeps exactly its signature.

- [ ] **Step 1: Write the failing tests**

```python
"""Le signal de mutation ignore le bruit de compteur et la production du dream."""

from __future__ import annotations

from scripts.dream.dream_preflight import _mutation_sql


class TestMutationSignal:
    def test_uses_content_updated_at_only(self) -> None:
        """Aucune occurrence de `updated_at` qui ne soit `content_updated_at`."""
        sql = _mutation_sql()
        assert sql.count("content_updated_at") == 5
        assert sql.count("updated_at") == sql.count("content_updated_at")

    def test_excludes_dream_generated_entities(self) -> None:
        """Sinon SYNTH garantit que la nuit suivante synthétise sur sa propre sortie."""
        sql = _mutation_sql()
        assert sql.count("dream:generated") == 5

    def test_covers_the_five_knowledge_tables(self) -> None:
        for table in ("decisions", "learnings", "snippets", "runbooks", "adrs"):
            assert f"FROM {table}" in _mutation_sql()
```

- [ ] **Step 2: Verify the failure**

Run: `uv run pytest tests/unit/dream/test_dream_preflight_provenance.py -v`
Expected: FAIL — `ImportError: cannot import name '_mutation_sql'`

- [ ] **Step 3: Extract and fix the query**

In `scripts/dream/dream_preflight.py`, add the function after `_ENTITY_TABLES`:

```python
def _mutation_sql() -> str:
    """Bâtir la requête du signal de mutation d'origine non-dream.

    Deux exclusions par rapport à la version d'origine :

    - `content_updated_at` remplace `updated_at`, qui bougeait à chaque
      écriture de compteur — cause principale des 48 RUN pour 2 SKIP mesurés
      sur 50 nuits ;
    - les entités taguées `dream:generated` sortent du signal, sinon SYNTH
      garantit en créant ses insights que la nuit suivante synthétisera
      par-dessus sa propre production.

    `tags` est NOT NULL DEFAULT '{}' sur les cinq tables (vérifié le
    2026-08-06, zéro NULL en base), donc `ANY(tags)` ne peut pas rendre NULL
    et faire disparaître une ligne du signal. Si cette contrainte tombait un
    jour, il faudrait un COALESCE : un prédicat NULL exclurait la ligne et
    produirait un SKIP à tort, ce que ce module promet impossible.
    """
    return " UNION ALL ".join(
        f"SELECT max(greatest(created_at, content_updated_at)) AS ts FROM {t} "
        f"WHERE NOT ('dream:generated' = ANY(tags))"
        for t in _ENTITY_TABLES
    )
```

Then, in `_fetch_signals`, replace the construction of `union`:

```python
        latest_mutation: datetime | None = await conn.fetchval(
            f"SELECT max(ts) FROM ({_mutation_sql()}) m"
        )
```

and delete the line `union = " UNION ALL ".join(...)` that it replaces.

- [ ] **Step 4: Verify the pass**

Run: `uv run pytest tests/unit/dream/test_dream_preflight_provenance.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 5: Verify the gate stays fail-safe**

```bash
uv run python -m scripts.dream.dream_preflight --date "$(date +%F)"
```
Expected: a line starting with `RUN` or `SKIP:`. Any error must print `RUN (preflight error: …)` — the fail-safe property must not have moved.

- [ ] **Step 6: Green and commit**

```bash
uv run ruff check src/ tests/ scripts/ && uv run ruff format --check src/ tests/ scripts/ && uv run mypy src/
uv run pytest tests/unit -q
git add scripts/dream/dream_preflight.py tests/unit/dream/test_dream_preflight_provenance.py
git commit -m "fix(dream): exclure le bruit de compteur et la sortie du dream du gate préflight"
```

---

## Verification after the first night

To do at the morning check following the deployment, **without modifying anything**:

| Criterion (spec §6) | Command | Expected |
|---|---|---|
| The actor arrives | `docker exec brain_v42_postgres psql -U brain -d brain -c "select actor, count(*) from access_log group by 1 order by 2 desc;"` | `dream-codex-*` present. Table often empty (purged at flush) — query during a dream run, or read `access_count_human` on the entities. |
| The loop stops | `uv run python -m scripts.dream.promote_prepare --project-key brain-v42 --limit 10 \| jq -r '.[].id'` | `1d1037e8…` absent |
| Content no longer moves for nothing | `docker exec brain_v42_postgres psql -U brain -d brain -c "select count(*) from learnings where content_updated_at >= current_date;"` | `0` after a night of REORG that only normalized tags |
| The gate comes back to life | `docker exec brain_v42_postgres psql -U brain -d brain -c "select run_date, phase, status from dream_runs where run_date >= current_date - 14 and phase='synth';"` + `grep PREFLIGHT logs/dream/*.log` | SKIP rate over 2 weeks, against the baseline **2/50** |

## Optional follow-up — remove the monkey-patch

**Settled on 2026-08-06 by the Q2 re-measurement (real compact catalog): `on_call_tool` sees `['brain_call_tool', 'inner_tool']` — the real tool name is visible behind the gateway. Removal is VIABLE.** Brain ticket `c352eaaa-3e3a-4e57-92c4-986b6d87512f`. Constraints: ignore gateway names (`brain_call_tool`, `brain_find_tool`) to avoid double counting, preserve `instrument_tool`'s `AuthorizationError` capture and latency measurement, don't touch `instrument_embedding`/`instrument_reranker`. To do after T5–T8; coordinate with red-monitor's live workload panel (ticket `2dfbb83d`), a consumer of per-tool metrics.

## Tickets to open in the brain

- **Durable access log**: keep `access_log` with the actor (retention rather than purge on aggregation) and derive counters on demand, to measure real corpus usage. Without it, `access_count_human` cannot be recomputed if the `is_human_actor` rule changes.
- **Workstreams B, C, D** (out of scope for this plan): SYNTH insight review surface, PROMOTE gate grounded in the verdict, SYNTH project routing.
