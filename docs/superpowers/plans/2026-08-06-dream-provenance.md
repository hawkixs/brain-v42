# Provenance du corpus — Plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Distinguer le métabolisme du dream de l'activité humaine, pour que le cache anti-rejugement de PROMOTE, son filtre de maturité et le gate préflight cessent de s'auto-invalider.

**Architecture:** Un middleware FastMCP pose l'identité de l'appelant (`X-Brain-Agent`, déjà envoyée) dans un `ContextVar` lu à la mise en file des accès. La migration 041 ajoute `access_log.actor`, un compteur `access_count_human` et une date `content_updated_at` alimentée par des triggers conditionnels sur le changement de valeur. Trois consommateurs basculent sur ces signaux.

**Tech Stack:** Python 3.12, FastMCP 3.4.2, SQLAlchemy 2.0 async, Alembic, PostgreSQL 16, pytest / pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-06-dream-provenance-design.md`

## Global Constraints

- **TDD strict, non négociable** (CLAUDE.md) : test rouge d'abord, jamais d'implémentation avant un test qui échoue. Ne jamais modifier un test pour faire passer le code.
- **NE PAS modifier `public.update_updated_at()`.** La migration 039 l'épingle par SHA256 `83ca0f7a3230405dae8b4f4e692b4983869b58e4225b6e60bbf96db3f6ae9a59` et par longueur `96` octets ; son downgrade le vérifie. Rendre ce trigger conditionnel casserait la 039. Créer une fonction **séparée**.
- **Aucun backfill.** `content_updated_at` reste `NULL` et `access_count_human` reste `0` sur l'existant : « jamais mesuré ».
- **Ne toucher à aucun killswitch ni variable d'environnement du dream.** `BRAIN_DREAM_*` reste tel quel.
- **Ne pas toucher** `instrument_embedding`, `InstrumentedEmbeddingService`, `InstrumentedReranker`, `InstrumentedGraphService` : ce ne sont pas des tools.
- Vert avant chaque commit : `pytest tests/unit`, `ruff check src/ tests/`, `ruff format --check src/ tests/`, `mypy src/`.
- Clé projet brain : toujours `brain-v42`.

## File Structure

| Fichier | Responsabilité |
|---|---|
| `src/brain_v42/provenance.py` | **Créé.** ContextVar de l'acteur, `normalize_agent()`, `is_human_actor()`. Aucune dépendance MCP ni DB. |
| `src/brain_v42/mcp/provenance_middleware.py` | **Créé.** `ProvenanceMiddleware` — pose l'acteur sur `on_call_tool`. |
| `src/brain_v42/metrics/instrument.py` | **Modifié.** `_normalize_agent` déménage vers `provenance.py` ; `instrument_tool` lit le ContextVar. |
| `src/brain_v42/mcp/server.py` | **Modifié.** Installe le middleware inconditionnellement. |
| `src/brain_v42/db/tables.py` | **Modifié.** Colonnes `actor`, `access_count_human`, `content_updated_at`. |
| `alembic/versions/041_corpus_provenance.py` | **Créé.** Colonnes + fonction + 5 triggers. |
| `src/brain_v42/services/access_logger.py` | **Modifié.** `log_access` capture l'acteur à la mise en file. |
| `src/brain_v42/repositories/pg_access_log.py` | **Modifié.** Agrégation avec `count_human`. |
| `src/brain_v42/services/decay_flusher.py` | **Modifié.** Écrit `access_count_human`. |
| `scripts/dream/promote_prepare.py` | **Modifié.** Cache + maturité. |
| `scripts/dream/dream_preflight.py` | **Modifié.** Signal de mutation. |

**Correction à la spec, à appliquer :** `decay_flusher._ENTITY_TABLES` contient **six** tables (`decisions, learnings, snippets, runbooks, adrs, indexed_plans`). `access_count_human` doit donc exister sur les six, sinon `_update_entities_batch` échouera sur les plans. `content_updated_at` reste sur les **cinq** tables de connaissance : `indexed_plans` n'est ni candidat à la promotion ni dans le signal préflight.

---

### Task 1: Spike — mesurer ce qu'on ne sait pas encore

**Résultats mesurés (2026-08-06) :**

- **Q1 — OUI.** `get_http_headers()` est joignable depuis `on_call_tool` en HTTP réel
  (via `_serve_loopback` + `_mcp_client`). Le header `X-Brain-Agent` envoyé est vu
  intact par le middleware. → Task 3 lit le header dans le middleware (chemin
  nominal, pas de variante fail-closed à écrire).
- **Q2 — non mesurée empiriquement par ce spike ; réponse assumée = passerelle
  seule, cohérente avec `tool_catalog.py`, à vérifier en Task 3.** Le code de spike
  du plan (Step 3) enregistre `inner_tool` directement sur un `FastMCP` nu et
  l'appelle par son nom réel via `client.call_tool("inner_tool", ...)` — sans
  jamais appliquer `apply_tool_catalog_profile(mcp, "compact")` ni passer par
  `brain_call_tool`. Le nom vu par le middleware (`['inner_tool']`) est donc
  trivialement celui du seul tool enregistré ; ce run ne prouve rien sur le
  comportement de la passerelle `brain_call_tool` en profil `compact` — il n'y a
  simplement pas de passerelle dans ce montage. La réponse « seulement la
  passerelle » reste celle déjà écrite dans le plan (ligne 151, *« Réponse
  attendue »*) et par la lecture de `tool_catalog.py` (`_RequestAwareBM25SearchTransform`
  n'expose que les 7 tools lifecycle + `brain_find_tool`/`brain_call_tool`), mais
  n'est pas confirmée par une mesure directe ici. Conséquence pratique : ceci ne
  bloque rien dans ce plan (la note du plan le dit déjà) ; le suivi optionnel
  « retirer le monkey-patch » reste à vérifier avec un montage qui active
  effectivement le catalogue `compact`, pas avec ce spike.

**Sortie brute :**

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

**Divergence par rapport au plan :** le Step 2 attend `headers_reachable` à `False`
en transport mémoire (« forcément None »). La mesure donne `True` : en transport
in-memory, `get_http_headers()` de FastMCP 3.4.2 ne retourne pas `None` mais un
dict vide (`{} is not None` → `True`), donc `headers_reachable` vaut `True` alors
qu'aucune requête HTTP n'a eu lieu. Sans conséquence : le plan indique déjà que
seule la mesure HTTP (Step 3) compte, et celle-ci confirme Q1 sans ambiguïté
(`headers_is_none: False`, `agent seen: dream-codex-scan`).

**Re-mesure Q2 (2026-08-06, catalogue compact réel) — le middleware voit LES DEUX
noms.** Spike jetable `tests/unit/mcp/test_spike_q2_compact_gateway.py` : `FastMCP`
nu + `inner_tool` + `apply_tool_catalog_profile(mcp, "compact")` + middleware
espion, appel via `client.call_tool("brain_call_tool", {"name": "inner_tool", ...})`
en transport mémoire. Sortie brute :

```
SPIKE Q2 catalog exposé      : ['brain_call_tool', 'brain_find_tool']
SPIKE Q2 résultat passerelle : 2
SPIKE Q2 noms vus par on_call_tool : ['brain_call_tool', 'inner_tool']
```

La réponse assumée (« passerelle seule ») était **fausse** : la passerelle de
`BaseSearchTransform` exécute l'appel interne via
`await ctx.fastmcp.call_tool(name, arguments)` sans désactiver `run_middleware`
(défaut `True`, fastmcp 3.4.2), donc la chaîne `on_call_tool` est ré-entrée avec
le nom du tool réel. Le dispatch est côté serveur, indépendant du transport : la
mesure mémoire suffit pour Q2 (contrairement à Q1, qui portait sur les headers
HTTP). Conséquences :

- le suivi optionnel « retirer le monkey-patch » est **viable** — ticket brain
  `c352eaaa-3e3a-4e57-92c4-986b6d87512f`, learning correctif
  `310a9953` (réfute `b77dba43`) ;
- un middleware de métriques devra **ignorer les noms de passerelle**
  (`brain_call_tool`, `brain_find_tool`) sous peine de double comptage ;
- `ProvenanceMiddleware` se déclenche deux fois par appel compact (passerelle
  puis tool interne) — inoffensif, il pose deux fois le même acteur.

Deux hypothèses non vérifiées conditionnent la suite. Ce spike les mesure et **ne livre aucun code de production**. Son résultat est écrit dans le plan.

**Files:**
- Create (jetable) : `tests/unit/mcp/test_spike_middleware_context.py`

**Interfaces:**
- Consomme : rien.
- Produit : deux réponses booléennes, consignées en fin de tâche.

- [ ] **Step 1: Écrire le spike**

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

- [ ] **Step 2: Lancer le spike**

Run: `uv run pytest tests/unit/mcp/test_spike_middleware_context.py -v -s`
Attendu : le test PASSE et imprime deux lignes `SPIKE …`. En transport mémoire `headers_reachable` sera `False` — c'est normal, il n'y a pas de requête HTTP.

- [ ] **Step 3: Mesurer en HTTP réel**

C'est la mesure qui compte : le transport mémoire n'a pas de headers. `tests/unit/mcp/test_dream_capability_http.py` fournit deux helpers réutilisables — `_serve_loopback(app)` (asynccontextmanager, ligne 366, rend une `base_url`) et `_mcp_client(base_url, token, headers=...)`. Les importer plutôt que les réécrire.

Ajouter au spike :

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

Si la signature de `_mcp_client` n'accepte pas `token=None`, passer le token attendu par le harnais — le spike ne teste pas l'autorisation.

Run: `uv run pytest tests/unit/mcp/test_spike_middleware_context.py -v -s`
Attendu : trois lignes `SPIKE …`. Q1 est répondue par `agent seen == "dream-codex-scan"`.

- [ ] **Step 4: Consigner les deux réponses**

Écrire les réponses en tête de ce plan, sous ce titre de tâche :

- **Q1 — `get_http_headers()` est-il joignable depuis `on_call_tool` en HTTP ?**
  - **OUI** → Task 3 lit le header dans le middleware (chemin nominal).
  - **NON** → Task 3 ne lit rien : le middleware appelle `set_current_actor(normalize_agent(...))` depuis la valeur que `instrument_tool` lit déjà. Adapter Task 3 Step 3 en conséquence et le noter ici.
- **Q2 — le middleware voit-il le nom du tool interne, ou seulement la passerelle ?**
  - Réponse attendue : seulement la passerelle en profil `compact`. Cette réponse **ne bloque rien dans ce plan** : elle décide seulement si le suivi optionnel « retirer le monkey-patch » est viable. La consigner et passer.

- [ ] **Step 5: Supprimer le spike, ne rien committer d'autre que la consignation**

```bash
rm tests/unit/mcp/test_spike_middleware_context.py
git add docs/superpowers/plans/2026-08-06-dream-provenance.md
git commit -m "docs(dream): consigner les mesures du spike middleware de provenance"
```

---

### Task 2: Module `provenance` — ContextVar et classification

**Files:**
- Create: `src/brain_v42/provenance.py`
- Test: `tests/unit/test_provenance.py`

**Interfaces:**
- Consomme : rien (module feuille, aucune dépendance MCP ni DB).
- Produit :
  - `normalize_agent(value: str | None) -> str`
  - `set_current_actor(actor: str) -> None`
  - `get_current_actor() -> str`
  - `is_human_actor(actor: str | None) -> bool`
  - `UNKNOWN_ACTOR: str` (vaut `"unknown"`)

- [ ] **Step 1: Écrire les tests qui échouent**

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

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/unit/test_provenance.py -v`
Attendu : FAIL — `ModuleNotFoundError: No module named 'brain_v42.provenance'`

- [ ] **Step 3: Écrire l'implémentation minimale**

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

# Préfixes des acteurs système qui se déclarent. Un acteur absent de cette
# liste et non sentinelle est traité comme humain.
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

- [ ] **Step 4: Vérifier le passage**

Run: `uv run pytest tests/unit/test_provenance.py -v`
Attendu : PASS, 14 tests.

- [ ] **Step 5: Vert et commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/
git add src/brain_v42/provenance.py tests/unit/test_provenance.py
git commit -m "feat(provenance): classifier l'acteur d'un appel et le porter en contexte"
```

---

### Task 3: Middleware de provenance

**Files:**
- Create: `src/brain_v42/mcp/provenance_middleware.py`
- Modify: `src/brain_v42/mcp/server.py` (près de `mcp = FastMCP(...)`, ligne 255)
- Modify: `src/brain_v42/metrics/instrument.py` (`_normalize_agent` et `instrument_tool`)
- Test: `tests/unit/mcp/test_provenance_middleware.py`

**Interfaces:**
- Consomme : `set_current_actor`, `normalize_agent`, `get_current_actor` (Task 2).
- Produit : `ProvenanceMiddleware` (classe sans argument de constructeur).

**Note de portée :** ce middleware **ne prend pas en charge les métriques**. `instrument_tool` et son monkey-patch restent en place — en profil `compact` ils sont les seuls à voir le nom du tool réel derrière la passerelle `brain_call_tool` (Task 1, Q2). Seule la lecture du header est mutualisée.

- [ ] **Step 1: Écrire les tests qui échouent**

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

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/unit/mcp/test_provenance_middleware.py -v`
Attendu : FAIL — `ModuleNotFoundError: No module named 'brain_v42.mcp.provenance_middleware'`

- [ ] **Step 3: Écrire le middleware**

Si Task 1 Q1 a répondu **NON**, remplacer le corps de `on_call_tool` par la variante consignée en Task 1 Step 4 et adapter les tests de Step 1 en conséquence.

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

- [ ] **Step 4: Vérifier le passage**

Run: `uv run pytest tests/unit/mcp/test_provenance_middleware.py -v`
Attendu : PASS, 5 tests.

- [ ] **Step 5: Installer le middleware inconditionnellement**

Dans `src/brain_v42/mcp/server.py`, juste après `mcp = FastMCP("brain", mask_error_details=True)` (ligne 255) :

```python
from brain_v42.mcp.provenance_middleware import ProvenanceMiddleware

mcp = FastMCP("brain", mask_error_details=True)
# Provenance : posé ici et non dans register_tools, pour être indépendant de
# l'activation des métriques et de l'ordre d'enregistrement des tools.
# `apply_tool_catalog_profile` et `maybe_apply_code_mode` retournent le MÊME
# objet, donc ce middleware survit aux deux.
mcp.add_middleware(ProvenanceMiddleware())
```

- [ ] **Step 6: Faire lire le ContextVar par `instrument_tool`**

Un seul point de lecture du header. Dans `src/brain_v42/metrics/instrument.py` :

1. Supprimer la fonction `_normalize_agent` (lignes 24-46) et l'import `from fastmcp.server.dependencies import get_http_headers`.
2. Ajouter `from brain_v42.provenance import get_current_actor, normalize_agent`.
3. Ajouter l'alias de compatibilité `_normalize_agent = normalize_agent` — `tests/unit/test_metrics_instrument.py` l'importe.
4. Dans `instrument_tool`, remplacer la ligne du bloc `finally` :

```python
# avant
agent = _normalize_agent((get_http_headers() or {}).get("x-brain-agent"))
# après
agent = get_current_actor()
```

- [ ] **Step 7: Vérifier la non-régression des métriques**

Run: `uv run pytest tests/unit/test_metrics_instrument.py tests/unit/metrics/ -v`
Attendu : PASS. Le test `test_decorator_records_successful_call` attend `agent="unknown"` hors contexte HTTP — la valeur par défaut du ContextVar est `UNKNOWN_ACTOR`, donc il passe sans modification.

Run: `uv run pytest tests/integration/metrics/test_agent_attribution.py -v`
Attendu : PASS. Ce test envoie de vrais headers en HTTP ; il valide bout en bout que le middleware alimente bien le ContextVar. **S'il échoue, ne pas modifier le test** — c'est le signal que le middleware n'est pas installé sur le serveur de test ou que Q1 valait NON.

- [ ] **Step 8: Vert et commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/
uv run pytest tests/unit -q
git add src/brain_v42/mcp/provenance_middleware.py src/brain_v42/mcp/server.py \
        src/brain_v42/metrics/instrument.py tests/unit/mcp/test_provenance_middleware.py
git commit -m "feat(provenance): poser l'acteur de l'appelant via un middleware FastMCP"
```

---

### Task 4: Migration 041 — colonnes, fonction, triggers

**Files:**
- Create: `alembic/versions/041_corpus_provenance.py`
- Modify: `src/brain_v42/db/tables.py`
- Test: `tests/integration/db/test_migration_041_provenance.py`

**Interfaces:**
- Consomme : rien.
- Produit : `access_log.actor`, `access_count_human` sur 6 tables, `content_updated_at` sur 5 tables, fonction `public.stamp_content_updated_at()`, 5 triggers `trg_<table>_content_updated`.

- [ ] **Step 1: Écrire les tests d'intégration qui échouent**

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

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/integration/db/test_migration_041_provenance.py -v`
Attendu : FAIL — `UndefinedColumn: column "content_updated_at" does not exist`

⚠️ Lancer depuis un worktree propre ou avec un `.env` de test : le `.env` du tronc est la config de PRODUCTION et fuit dans les tests d'intégration via pydantic-settings (learning `54fdfddc`).

- [ ] **Step 3: Écrire la migration**

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

# Tables suivies par le decay : toutes reçoivent le compteur humain, car
# decay_flusher._ENTITY_TABLES les met à jour uniformément.
_COUNTER_TABLES = (
    "learnings",
    "decisions",
    "snippets",
    "runbooks",
    "adrs",
    "indexed_plans",
)

# Tables de connaissance : colonnes de contenu par table. `indexed_plans` est
# absent — ni candidat à la promotion, ni dans le signal préflight.
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

- [ ] **Step 4: Déclarer les colonnes dans `tables.py`**

Dans `src/brain_v42/db/tables.py`, ajouter à la définition `access_log` (ligne 984) :

```python
    Column("actor", String(64), nullable=False, server_default=sa.text("'unknown'")),
```

Puis, sur chacune des tables `learnings`, `decisions`, `snippets`, `runbooks`, `adrs`, `indexed_plans` :

```python
    Column("access_count_human", sa.Integer, nullable=False, server_default=sa.text("0")),
```

Et sur les cinq tables de connaissance seulement (pas `indexed_plans`) :

```python
    Column("content_updated_at", DateTime(timezone=True), nullable=True),
```

- [ ] **Step 5: Appliquer et vérifier**

```bash
uv run alembic upgrade head
docker exec brain_v42_postgres psql -U brain -d brain -Atc \
  "select version_num from alembic_version;"
```
Attendu : `041`

Run: `uv run pytest tests/integration/db/test_migration_041_provenance.py -v`
Attendu : PASS, 12 tests (4 sur le trigger, 2 sur la forme, 6 paramétrés sur `access_count_human`).

- [ ] **Step 6: Vérifier que le downgrade est propre**

```bash
uv run alembic downgrade 040
docker exec brain_v42_postgres psql -U brain -d brain -Atc \
  "select count(*) from pg_proc where proname='stamp_content_updated_at';"
uv run alembic upgrade head
```
Attendu : `0` après le downgrade, et l'upgrade repasse sans erreur.

- [ ] **Step 7: Vert et commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/
git add alembic/versions/041_corpus_provenance.py src/brain_v42/db/tables.py \
        tests/integration/db/test_migration_041_provenance.py
git commit -m "feat(db): dater le contenu et compter les lectures humaines (041)"
```

---

### Task 5: Capturer l'acteur à la mise en file

**Files:**
- Modify: `src/brain_v42/services/access_logger.py`
- Test: `tests/unit/services/test_access_logger_actor.py`

**Interfaces:**
- Consomme : `get_current_actor` (Task 2), colonne `access_log.actor` (Task 4).
- Produit : chaque dict d'événement mis en file porte désormais la clé `actor`.

**Le piège de cette tâche :** `_flush_batch()` tourne dans une tâche de fond (`_run_loop`, toutes les 5 s), **hors du contexte de requête**. Y lire le ContextVar rendrait `unknown` pour tout le monde. L'acteur doit être lu dans `log_access()`, au moment de la mise en file.

- [ ] **Step 1: Écrire les tests qui échouent**

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

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/unit/services/test_access_logger_actor.py -v`
Attendu : FAIL — `KeyError: 'actor'`

- [ ] **Step 3: Écrire l'implémentation minimale**

Dans `src/brain_v42/services/access_logger.py`, ajouter l'import :

```python
from brain_v42.provenance import get_current_actor
```

Puis, dans `log_access`, remplacer le dict mis en file :

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

`_flush_batch` n'a pas besoin de changer : il fait `sa.insert(access_log)` avec les dicts tels quels, et la clé `actor` correspond désormais à une colonne existante.

- [ ] **Step 4: Vérifier le passage**

Run: `uv run pytest tests/unit/services/test_access_logger_actor.py tests/unit/services/test_access_logger.py -v`
Attendu : PASS. Si un test existant construit un événement attendu sans `actor`, **ne pas le modifier pour le faire passer** — vérifier d'abord qu'il ne décrit pas un comportement qu'on casse.

- [ ] **Step 5: Vert et commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/
git add src/brain_v42/services/access_logger.py tests/unit/services/test_access_logger_actor.py
git commit -m "feat(provenance): figer l'acteur d'un accès au moment de la mise en file"
```

---

### Task 6: Agréger les lectures humaines

**Files:**
- Modify: `src/brain_v42/repositories/pg_access_log.py` (`aggregate_in_session`, lignes 33-95)
- Modify: `src/brain_v42/services/decay_flusher.py` (`_update_entities_batch`, lignes 148-250)
- Test: `tests/unit/repositories/test_pg_access_log_actor.py`

**Interfaces:**
- Consomme : `is_human_actor` (Task 2), `access_log.actor` (Task 4), clé `actor` (Task 5).
- Produit : `aggregate_in_session` retourne désormais `{"max_accessed": datetime, "count": int, "count_human": int}` pour chaque `(entity_type, entity_id)`.

**Note :** ne PAS toucher `aggregate_and_flush` (déprécié, plus appelé en production — `tests/unit/services/test_decay_flusher_atomic.py:96` prouve qu'il ne doit pas l'être).

- [ ] **Step 1: Écrire le test qui échoue**

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

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/unit/repositories/test_pg_access_log_actor.py -v`
Attendu : FAIL — `KeyError: 'count_human'`

- [ ] **Step 3: Agréger par acteur**

Dans `pg_access_log.py`, l'agrégation ne peut pas classifier en SQL sans dupliquer la règle Python. On groupe donc aussi par `actor` et on classifie côté Python — une seule source de vérité pour `is_human_actor`.

Remplacer les étapes 2 et 3 de `aggregate_in_session` :

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

Ajouter l'import en tête de fichier :

```python
from brain_v42.provenance import is_human_actor
```

- [ ] **Step 4: Écrire le compteur humain**

Dans `decay_flusher.py`, méthode `_update_entities_batch` :

1. Ajouter la colonne à la sélection (après `table.c.access_count`) :

```python
            table.c.access_count_human,
```

2. Dans la boucle, après `new_access_count = row["access_count"] + stats["count"]` :

```python
            new_access_count_human = row["access_count_human"] + stats.get("count_human", 0)
```

3. Dans `params` :

```python
                "access_count_human": new_access_count_human,
```

4. Dans les deux `sa.update(...).values(...)` (`upd_same` et `upd_changed`), ajouter :

```python
                    access_count_human=sa.bindparam("access_count_human"),
```

- [ ] **Step 5: Vérifier le passage**

Run: `uv run pytest tests/unit/repositories/test_pg_access_log_actor.py tests/unit/services/test_decay_flusher.py tests/unit/services/test_decay_flusher_atomic.py -v`
Attendu : PASS.

- [ ] **Step 6: Vert et commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/
uv run pytest tests/unit -q
git add src/brain_v42/repositories/pg_access_log.py src/brain_v42/services/decay_flusher.py \
        tests/unit/repositories/test_pg_access_log_actor.py
git commit -m "feat(provenance): agréger les lectures humaines séparément du dream"
```

---

### Task 7: Recâbler PROMOTE

**Files:**
- Modify: `scripts/dream/promote_prepare.py` (`_CANDIDATE_SQL`, lignes 29-68)
- Test: `tests/integration/dream/test_promote_prepare_provenance.py`

**Interfaces:**
- Consomme : `content_updated_at`, `access_count_human` (Task 4), alimentés par Task 6.
- Produit : `fetch_candidates()` conserve exactement sa signature et sa forme de retour.

- [ ] **Step 1: Écrire les tests qui échouent**

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
        # Une lecture postérieure au verdict — ce qui cassait le cache.
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

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/integration/dream/test_promote_prepare_provenance.py -v`
Attendu : FAIL — le premier test échoue (le candidat est réadmis), le troisième aussi (`access_count` suffit encore).

- [ ] **Step 3: Modifier la requête**

Dans `scripts/dream/promote_prepare.py`, deux changements dans `_CANDIDATE_SQL` :

1. Remplacer `AND l.access_count >= 3` par :

```sql
      AND l.access_count_human >= 3
```

2. Remplacer la ligne `AND u.created_at >= l.updated_at` par :

```sql
            AND u.created_at >= COALESCE(l.content_updated_at, l.created_at)
```

Et remplacer le commentaire du bloc « Terminal-unpromotable cache » par :

```sql
      -- Terminal-unpromotable cache: skip a learning already judged
      -- classification_uncertain on its CURRENT version. La comparaison porte
      -- sur content_updated_at, PAS sur updated_at : ce dernier bouge à chaque
      -- écriture de compteur, donc une simple lecture par une phase ultérieure
      -- du dream invalidait le verdict rendu deux minutes plus tôt (observé :
      -- un learning réévalué 23 nuits d'affilée). Le repli sur created_at est
      -- délibéré — sans backfill, content_updated_at est NULL, et se replier
      -- sur updated_at reproduirait le défaut à l'identique.
```

Laisser `AND NOT (l.confidence = 'low' AND l.access_count < 5)` inchangé : ce garde-fou parle du volume total, pas de maturité humaine.

- [ ] **Step 4: Vérifier le passage**

Run: `uv run pytest tests/integration/dream/test_promote_prepare_provenance.py -v`
Attendu : PASS, 4 tests.

- [ ] **Step 5: Prouver que la boucle de production s'arrête**

```bash
uv run python -m scripts.dream.promote_prepare --project-key brain-v42 --limit 10 \
  | jq -r '.[].id'
```
Attendu : `1d1037e8-acb1-4cb7-b0b5-9ccd3b97c0c0` **absent** de la sortie. C'est le critère d'acceptation n°2 de la spec.

Le pool sera probablement **vide** : `access_count_human` vaut 0 partout, sans backfill. C'est le comportement conçu, pas une régression — le noter et passer.

- [ ] **Step 6: Vert et commit**

```bash
uv run ruff check src/ tests/ scripts/ && uv run ruff format --check src/ tests/ scripts/ && uv run mypy src/
git add scripts/dream/promote_prepare.py tests/integration/dream/test_promote_prepare_provenance.py
git commit -m "fix(dream): fonder le cache de PROMOTE sur la date du contenu, pas de la ligne"
```

---

### Task 8: Recâbler le gate préflight

**Files:**
- Modify: `scripts/dream/dream_preflight.py` (`_ENTITY_TABLES` et `_fetch_signals`, lignes 37-74)
- Test: `tests/unit/dream/test_dream_preflight_provenance.py`

**Interfaces:**
- Consomme : `content_updated_at` (Task 4).
- Produit : `should_skip_opus_phases()` conserve exactement sa signature.

- [ ] **Step 1: Écrire les tests qui échouent**

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

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/unit/dream/test_dream_preflight_provenance.py -v`
Attendu : FAIL — `ImportError: cannot import name '_mutation_sql'`

- [ ] **Step 3: Extraire et corriger la requête**

Dans `scripts/dream/dream_preflight.py`, ajouter la fonction après `_ENTITY_TABLES` :

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

Puis, dans `_fetch_signals`, remplacer la construction de `union` :

```python
        latest_mutation: datetime | None = await conn.fetchval(
            f"SELECT max(ts) FROM ({_mutation_sql()}) m"
        )
```

et supprimer la ligne `union = " UNION ALL ".join(...)` qu'elle remplace.

- [ ] **Step 4: Vérifier le passage**

Run: `uv run pytest tests/unit/dream/test_dream_preflight_provenance.py -v`
Attendu : PASS, 3 tests.

- [ ] **Step 5: Vérifier que le gate reste fail-safe**

```bash
uv run python -m scripts.dream.dream_preflight --date "$(date +%F)"
```
Attendu : une ligne commençant par `RUN` ou `SKIP:`. Toute erreur doit imprimer `RUN (preflight error: …)` — la propriété fail-safe ne doit pas avoir bougé.

- [ ] **Step 6: Vert et commit**

```bash
uv run ruff check src/ tests/ scripts/ && uv run ruff format --check src/ tests/ scripts/ && uv run mypy src/
uv run pytest tests/unit -q
git add scripts/dream/dream_preflight.py tests/unit/dream/test_dream_preflight_provenance.py
git commit -m "fix(dream): exclure le bruit de compteur et la sortie du dream du gate préflight"
```

---

## Vérification après la première nuit

À faire au check matinal suivant le déploiement, **sans rien modifier** :

| Critère (spec §6) | Commande | Attendu |
|---|---|---|
| L'acteur arrive | `docker exec brain_v42_postgres psql -U brain -d brain -c "select actor, count(*) from access_log group by 1 order by 2 desc;"` | `dream-codex-*` présent. Table souvent vide (purgée au flush) — interroger pendant le run du dream, ou lire `access_count_human` sur les entités. |
| La boucle s'arrête | `uv run python -m scripts.dream.promote_prepare --project-key brain-v42 --limit 10 \| jq -r '.[].id'` | `1d1037e8…` absent |
| Le contenu ne bouge plus pour rien | `docker exec brain_v42_postgres psql -U brain -d brain -c "select count(*) from learnings where content_updated_at >= current_date;"` | `0` après une nuit de REORG qui n'a normalisé que des tags |
| Le gate revit | `docker exec brain_v42_postgres psql -U brain -d brain -c "select run_date, phase, status from dream_runs where run_date >= current_date - 14 and phase='synth';"` + `grep PREFLIGHT logs/dream/*.log` | Taux de SKIP sur 2 semaines, contre la ligne de base **2/50** |

## Suivi optionnel — retirer le monkey-patch

**Tranché le 2026-08-06 par la re-mesure Q2 (catalogue compact réel) : `on_call_tool` voit `['brain_call_tool', 'inner_tool']` — le nom du tool réel est visible derrière la passerelle. Le retrait est VIABLE.** Ticket brain `c352eaaa-3e3a-4e57-92c4-986b6d87512f`. Contraintes : ignorer les noms de passerelle (`brain_call_tool`, `brain_find_tool`) pour éviter le double comptage, préserver la capture d'`AuthorizationError` et la mesure de latence d'`instrument_tool`, ne pas toucher `instrument_embedding`/`instrument_reranker`. À faire après T5–T8 ; coordonner avec le panneau live workload de red-monitor (ticket `2dfbb83d`), consommateur des métriques par tool.

## Tickets à ouvrir dans le brain

- **Journal d'accès durable** : conserver `access_log` avec l'acteur (rétention plutôt que purge à l'agrégation) et dériver les compteurs à la demande, pour mesurer l'usage réel du corpus. Sans lui, `access_count_human` ne pourra pas être recalculé si la règle `is_human_actor` change.
- **Chantiers B, C, D** (hors périmètre de ce plan) : surface de revue des insights SYNTH, porte de PROMOTE fondée sur le verdict, routage projet de SYNTH.
