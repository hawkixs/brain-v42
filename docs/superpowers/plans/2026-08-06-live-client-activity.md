# Activité live des clients du brain — plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remplacer le panneau « Codex activity » de red-monitor par un panneau « Live workload » qui montre une ligne par session cliente du brain, en fusionnant la télémétrie OTLP des agents et l'activité observée côté brain.

**Architecture:** Deux processus. Le serveur MCP (`:8765`) observe ses appelants dans `ProvenanceMiddleware` et pousse des observations bornées en loopback vers le sidecar métriques (`:9200`). Le sidecar reçoit aussi l'OTLP des CLI (Codex et Claude Code, deux décodeurs), hache tous les identifiants à la réception avec son secret de processus, et fusionne le tout dans un registre unique borné exposé par `/api/cockpit`. red-monitor reproxifie sans changement Go ; seul le panneau SolidJS bouge.

**Tech Stack:** Python 3.12, aiohttp (sidecar), FastMCP 3.x (middleware), httpx (émetteur), pytest / pytest-asyncio, SolidJS + Vitest (red-monitor).

**Spec:** `docs/superpowers/specs/2026-08-06-live-client-activity-design.md`
**Ticket brain:** `2dfbb83d-f6cf-4570-9b13-502acc8c776c`

## Global Constraints

- **TDD obligatoire.** Aucun code d'implémentation sans un test qui échoue d'abord. Ne jamais modifier un test pour faire passer du code.
- **Vert avant commit**, dans cet ordre exact : `pytest tests/unit`, `ruff check src/ tests/`, `ruff format --check src/ tests/`, `mypy src/`. Le CI lance `ruff format --check` — `ruff check` seul ne suffit pas.
- **Commits atomiques, Conventional Commits**, messages en français comme le reste du dépôt. Terminer chaque message par `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- **Type hints partout**, `from __future__ import annotations` en tête de chaque module.
- **Jamais `print`** : `structlog` uniquement.
- **`null`, jamais `0`, pour tout champ sans source de données.** Doctrine déjà écrite dans `cockpit.py` : un `0.0` cosmétique est indiscernable d'un vrai zéro mesuré.
- **Tout récepteur HTTP du sidecar est loopback-only et borné**, avec le durcissement de `/v1/logs` : rejet non-loopback en 403, corps hors borne en 413, saturation en 503, malformé en 400.
- **Aucun identifiant brut ne sort dans le payload.** Le hachage est fait à la réception, côté sidecar, avec le secret de processus.
- Ne pas citer le learning `b77dba43` : réfuté par `310a9953`.
- **Ne jamais appeler un tool `brain_session_*`** au cours de ce plan. Le cycle de session appartient à l'utilisateur.

---

## Structure des fichiers

**Créés — `brain_v42` :**

| Fichier | Responsabilité |
|---------|----------------|
| `src/brain_v42/metrics/client_activity.py` | Le registre fusionné : état borné, jointure, projection `clients[]` |
| `src/brain_v42/metrics/claude_telemetry.py` | Décodeur OTLP Claude Code (schéma distinct de Codex) |
| `src/brain_v42/metrics/client_observation.py` | Format de fil des observations brain-side : décodage borné |
| `src/brain_v42/mcp/activity_reporter.py` | Émetteur loopback côté processus MCP |
| `tests/fixtures/claude_otlp_logs.json` | Capture réelle produite par la tâche 1, oracle des noms d'attributs |

**Modifiés — `brain_v42` :**

| Fichier | Changement |
|---------|-----------|
| `src/brain_v42/provenance.py` | ContextVars de session et de profondeur d'appel |
| `src/brain_v42/mcp/provenance_middleware.py` | Pose la session, garde de ré-entrance, déclenche l'émetteur |
| `src/brain_v42/metrics/codex_telemetry.py` | Garde son décodeur ; le registre déménage |
| `src/brain_v42/metrics/server.py` | Route `/v1/client-activity`, câblage du registre |
| `src/brain_v42/metrics/cockpit.py` | Champ `clients[]` |
| `src/brain_v42/config.py` | Réglages de l'émetteur |

**Modifiés — `red-monitor`** (dépôt `~/hawkixs_infra/git_repo/ReD_v1/projects/red-monitor`) :
`frontend/src/tabs/brain/BrainActivity.jsx`, `brainPresentation.js`, `BrainStatusBar.jsx`, `Brain.test.jsx`.

**Non touchés, volontairement :** `internal/web/brain.go` (proxifie les octets bruts), `metrics/flusher.py` et `process_metrics` (cadence 30 s, écartée par la spec), `db/tables.py` (le registre est en mémoire).

---

### Task 1: Spike — prouver la jointure de session

**Porte du plan.** Ne pas enchaîner sans son résultat écrit. Aucune ligne de code d'implémentation ici : c'est une mesure.

**Files:**
- Create: `tests/fixtures/claude_otlp_logs.json`
- Create: `docs/upstream/2026-08-06-claude-otlp-session-join.md`

**Interfaces:**
- Consumes: rien.
- Produces: le fichier de capture, oracle des noms d'attributs pour la tâche 5 ; le verdict `JOINTURE POSSIBLE` ou `JOINTURE IMPOSSIBLE` qui conditionne la tâche 8.

- [ ] **Step 1: Ajouter l'en-tête de session à la configuration MCP locale**

Dans `~/.claude.json`, à côté de `"X-Brain-Agent": "${PWD}"` (vers la ligne 3807), ajouter :

```json
"X-Brain-Session": "${CLAUDE_CODE_SESSION_ID}"
```

- [ ] **Step 2: Ouvrir une session Claude Code INTERACTIVE et lire l'en-tête reçu**

La mesure du 2026-08-06 vient d'une session `sdk-cli` avec `CLAUDE_CODE_CHILD_SESSION=1`. Une session interactive peut exporter un environnement différent : c'est précisément ce qu'on vérifie.

Dans la session interactive, noter la valeur de `CLAUDE_CODE_SESSION_ID` :

```bash
echo "$CLAUDE_CODE_SESSION_ID"
```

Puis capturer l'en-tête réellement reçu par le serveur MCP. Le plus simple sans toucher au code : un enregistrement temporaire dans le middleware.

```bash
# Dans le processus MCP, ajouter TEMPORAIREMENT à provenance_middleware.py,
# ligne 33, puis redémarrer le serveur :
#   import structlog; structlog.get_logger(__name__).warning(
#       "spike.headers", session=headers.get("x-brain-session"))
journalctl --user -u brain-v42-mcp -f | grep spike.headers
```

Faire un appel de tool brain quelconque depuis la session interactive, lire la valeur.

**Retirer l'enregistrement temporaire avant toute suite.** Il ne doit rien laisser dans l'arbre.

- [ ] **Step 3: Activer la télémétrie OTLP de Claude Code et capturer une charge réelle**

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
```

Le port `4318` et non `9200` : on capture d'abord la charge brute avec un récepteur jetable, sans la faire traverser le récepteur de production.

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

Lancer une session Claude Code interactive avec ces variables, envoyer deux ou trois prompts, arrêter le récepteur.

- [ ] **Step 4: Répondre aux deux questions et écrire le verdict**

Ouvrir `/tmp/claude_otlp_capture.json` et relever :

1. Le nom exact de l'attribut portant l'identifiant de session (attendu `session.id`).
2. Sa **valeur** : est-elle **identique** à `$CLAUDE_CODE_SESSION_ID` et à l'en-tête `X-Brain-Session` vu au Step 2 ?
3. Les noms exacts des événements (attendus `claude_code.user_prompt`, `claude_code.api_request`).
4. Les noms exacts des attributs de compteur (attendus `input_tokens`, `output_tokens`, `cost_usd`, `model`).

Écrire `docs/upstream/2026-08-06-claude-otlp-session-join.md` avec, en clair : le verdict `JOINTURE POSSIBLE` ou `JOINTURE IMPOSSIBLE`, les quatre relevés ci-dessus, et la version de Claude Code mesurée.

- [ ] **Step 5: Réduire la capture en fixture**

Copier la charge dans `tests/fixtures/claude_otlp_logs.json`, **en remplaçant tout UUID réel par** `12345678-1234-4abc-8def-1234567890ab` (la même constante que `tests/unit/test_codex_telemetry_endpoint.py:33`). Aucune donnée de prompt ne doit subsister : ne garder que les enregistrements dont les attributs sont ceux du relevé.

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

**Si le verdict est `JOINTURE IMPOSSIBLE`** : le plan continue tel quel. Toutes les lignes Claude deviendront `unattributed` côté brain plus des lignes OTLP-only, ce que la conception prévoit. Seule la tâche 8 change : son test de jointure devient un test d'absence de jointure. Le noter dans le document de verdict.

---

### Task 2: Identité de session dans la couche de provenance

**Files:**
- Modify: `src/brain_v42/provenance.py`
- Test: `tests/unit/test_provenance.py`

**Interfaces:**
- Consumes: rien.
- Produces: `normalize_session(value: str | None) -> str | None`, `set_current_session(session: str | None) -> None`, `get_current_session() -> str | None`. Renvoie `None` quand aucune session valide n'est déclarée.

- [ ] **Step 1: Écrire les tests qui échouent**

Ajouter à `tests/unit/test_provenance.py` :

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
        # Une seule forme canonique, sinon deux clients écrivant la même
        # session sous deux casses produiraient deux lignes distinctes.
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

Ajouter `normalize_session`, `set_current_session`, `get_current_session` à l'import en tête du fichier.

- [ ] **Step 2: Vérifier l'échec**

Run: `pytest tests/unit/test_provenance.py -v`
Expected: FAIL — `ImportError: cannot import name 'normalize_session'`

- [ ] **Step 3: Implémenter le minimum**

Dans `src/brain_v42/provenance.py`, après `normalize_agent` :

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

Ajouter `import uuid` en tête du module.

- [ ] **Step 4: Vérifier que ça passe**

Run: `pytest tests/unit/test_provenance.py -v`
Expected: PASS, tous les tests existants inclus.

- [ ] **Step 5: Vérifier le vert complet**

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

### Task 3: Garde de ré-entrance

Sans elle, `brain_calls` compte double en profil `compact`, qui est le profil de production. Mesuré : commit `58329a84`.

**Files:**
- Modify: `src/brain_v42/provenance.py`
- Modify: `src/brain_v42/mcp/provenance_middleware.py`
- Test: `tests/unit/test_provenance.py`, `tests/unit/test_provenance_middleware.py`

**Interfaces:**
- Consumes: tâche 2.
- Produces: `enter_call() -> Token[int]`, `exit_call(token: Token[int]) -> None`, `is_outermost_call() -> bool`. `is_outermost_call()` vaut `True` uniquement entre `enter_call()` et son `exit_call()` au premier niveau d'imbrication.

- [ ] **Step 1: Écrire les tests qui échouent**

Dans `tests/unit/test_provenance.py` :

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

Créer `tests/unit/test_provenance_middleware.py` :

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
        # Le tool passerelle ré-entre dans la chaîne de middlewares.
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

- [ ] **Step 2: Vérifier l'échec**

Run: `pytest tests/unit/test_provenance.py tests/unit/test_provenance_middleware.py -v`
Expected: FAIL — `ImportError: cannot import name 'enter_call'`, puis `AttributeError: _report`.

- [ ] **Step 3: Implémenter**

Dans `src/brain_v42/provenance.py` :

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

Ajouter `from contextvars import ContextVar, Token` en tête.

Réécrire `src/brain_v42/mcp/provenance_middleware.py` :

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

Mettre à jour le docstring du module : la ré-entrance n'est plus « inoffensive », elle est **gérée**.

- [ ] **Step 4: Vérifier que ça passe**

Run: `pytest tests/unit/test_provenance.py tests/unit/test_provenance_middleware.py -v`
Expected: PASS

- [ ] **Step 5: Vérifier le vert complet**

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

### Task 4: Déménager le registre sans changer son comportement

Refactor pur. Aucun test existant ne doit être modifié, et tous doivent rester verts : c'est la preuve que le déménagement ne change rien.

**Files:**
- Create: `src/brain_v42/metrics/client_activity.py`
- Modify: `src/brain_v42/metrics/codex_telemetry.py`, `src/brain_v42/metrics/server.py`, `src/brain_v42/metrics/cockpit.py`
- Test: `tests/unit/test_client_activity.py`

**Interfaces:**
- Consumes: rien.
- Produces: `ClientActivityRegistry` dans `metrics/client_activity.py`, avec `ingest_otlp_json(payload: bytes) -> None` et `snapshot() -> dict[str, object]` au comportement identique à `CodexConversationRegistry`. `CodexConversationRegistry = ClientActivityRegistry` reste en alias dans `codex_telemetry.py`.

- [ ] **Step 1: Écrire le test d'équivalence**

Créer `tests/unit/test_client_activity.py` :

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

- [ ] **Step 2: Vérifier l'échec**

Run: `pytest tests/unit/test_client_activity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'brain_v42.metrics.client_activity'`

- [ ] **Step 3: Déménager**

Déplacer la classe `CodexConversationRegistry` et les dataclasses `_Conversation` de `codex_telemetry.py` vers `client_activity.py`, **sans toucher au corps des méthodes**. Y déplacer aussi les constantes qu'elle seule utilise : `MAX_ACTIVE_CONVERSATIONS`, `ACTIVITY_TTL_SECONDS`, `MAX_FINGERPRINTS`, `FINGERPRINT_TTL_SECONDS`.

`client_activity.py` importe le décodage depuis `codex_telemetry` :

```python
from brain_v42.metrics.codex_telemetry import _COMPLETION_EVENTS, _ProjectedRecord, _decode
```

Renommer la classe en `ClientActivityRegistry` et laisser dans `codex_telemetry.py` :

```python
from brain_v42.metrics.client_activity import ClientActivityRegistry

# Nom historique. Le registre n'est plus spécifique à Codex depuis qu'il
# fusionne les sources, mais l'ancien nom reste importé par les tests.
CodexConversationRegistry = ClientActivityRegistry
```

Attention à l'import circulaire : `codex_telemetry` ne doit importer `client_activity` **qu'en fin de module**, après la définition de `_decode`. Si `ruff` s'en plaint (`E402`), inverser la dépendance en déplaçant l'alias dans `metrics/__init__.py` à la place.

Mettre à jour les imports de `server.py` et `cockpit.py` pour pointer sur `client_activity`.

- [ ] **Step 4: Vérifier que TOUT passe, y compris les anciens tests**

```bash
pytest tests/unit/test_client_activity.py tests/unit/test_codex_telemetry_endpoint.py tests/unit/test_metrics_cockpit_collector.py -v
```
Expected: PASS partout. Un seul échec ici signifie que le déménagement a changé un comportement — le corriger avant d'avancer, ne pas ajuster le test.

- [ ] **Step 5: Vérifier le vert complet**

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

### Task 5: Décodeur OTLP Claude Code

**Files:**
- Create: `src/brain_v42/metrics/claude_telemetry.py`
- Test: `tests/unit/test_claude_telemetry.py`

**Interfaces:**
- Consumes: la fixture de la tâche 1 ; `codex_telemetry._load_json`, `_string_value`, `_token_value`, `_canonical_uuid`, `CodexTelemetryLimitError`, `CodexTelemetryMalformedError`.
- Produces: `decode_claude_logs(payload: bytes) -> tuple[ClaudeRecord, ...]` et la dataclass gelée `ClaudeRecord(session_id: str, event_name: str, model: str, input_tokens: int | None, cache_read_tokens: int | None, cache_creation_tokens: int | None, output_tokens: int | None, cost_usd: float | None, timestamp: int | None)`.

**Oracle des noms :** `tests/fixtures/claude_otlp_logs.json`, capture réelle produite par la tâche 1. Verdict et relevés complets dans `docs/upstream/2026-08-06-claude-otlp-session-join.md`.

**Trois corrections mesurées, déjà appliquées au code ci-dessous — ne pas les défaire :**

1. **`event.name` n'est PAS préfixé.** Mesuré : l'attribut vaut `user_prompt`,
   `api_request`, `assistant_response`. Le préfixe `claude_code.` est dans
   `body.stringValue`, pas dans l'attribut. Un filtre sur `claude_code.*` via
   `event.name` ne reconnaîtrait rien.
2. **`input_tokens` ne mesure pas le contexte.** Relevé réel : `input_tokens=10`
   quand `cache_read_tokens=11776` et `cache_creation_tokens=6804`. Il faut donc
   projeter les trois compteurs ; la tâche 8 en fera la somme.
3. **Chaque enregistrement porte des données personnelles en clair** — `user.email`,
   `user.id`, `user.account_uuid`, `organization.id` — y compris sur les événements
   de hook et de plugin. La projection par liste blanche est la seule chose qui les
   empêche d'entrer dans un registre exposé par HTTP. Elle a son propre test.

- [ ] **Step 1: Écrire les tests qui échouent**

Créer `tests/unit/test_claude_telemetry.py` :

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

- [ ] **Step 2: Vérifier l'échec**

Run: `pytest tests/unit/test_claude_telemetry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'brain_v42.metrics.claude_telemetry'`

- [ ] **Step 3: Implémenter**

Créer `src/brain_v42/metrics/claude_telemetry.py` :

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

# Liste blanche STRICTE. Mesuré le 2026-08-06 : les enregistrements réels
# portent aussi user.email, user.id, user.account_uuid et organization.id en
# clair. Tout ce qui n'est pas ici est jeté avant d'entrer dans le registre.
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
# Sans préfixe : `event.name` vaut `user_prompt`, le corps vaut
# `claude_code.user_prompt`. Mesuré sur Claude Code 2.1.220.
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
    if cost != cost or cost < 0.0 or cost > _MAX_COST_USD:  # NaN, négatif, aberrant
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

`_model_value` renvoie déjà `"unknown"` quand la valeur est absente ou non conforme — c'est ce que veut `test_absent_model_falls_back_to_unknown`.

- [ ] **Step 4: Vérifier que ça passe**

Run: `pytest tests/unit/test_claude_telemetry.py -v`
Expected: PASS. Si `TestRealCapture` échoue, comparer les noms d'attributs du code à ceux de la fixture et **corriger le code**, jamais la fixture.

- [ ] **Step 5: Vérifier le vert complet**

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

### Task 6: Format de fil des observations brain-side

**Files:**
- Create: `src/brain_v42/metrics/client_observation.py`
- Test: `tests/unit/test_client_observation.py`

**Interfaces:**
- Consumes: `codex_telemetry._load_json`, `_raise_malformed`, `CodexTelemetryLimitError`, `CodexTelemetryMalformedError`.
- Produces: `MAX_OBSERVATION_BYTES: int`, `MAX_OBSERVATIONS: int`, la dataclass gelée `ClientObservation(actor: str, session_id: str | None, calls: int)`, et `decode_observations(payload: bytes) -> tuple[ClientObservation, ...]`.

Format de fil, volontairement minuscule :

```json
{"observations": [{"actor": "brain-v42", "session": "12345678-…", "calls": 1}]}
```

- [ ] **Step 1: Écrire les tests qui échouent**

Créer `tests/unit/test_client_observation.py` :

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
        # Une session illisible dégrade la ligne en « non attribué ».
        # Rejeter tout le lot punirait les observations valides du même envoi.
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

- [ ] **Step 2: Vérifier l'échec**

Run: `pytest tests/unit/test_client_observation.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implémenter**

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

`_load_json` borne déjà la profondeur JSON et le nombre de conteneurs ; `MAX_OBSERVATION_BYTES` est vérifié avant lui parce qu'il est bien plus serré que `MAX_REQUEST_BYTES`.

- [ ] **Step 4: Vérifier que ça passe**

Run: `pytest tests/unit/test_client_observation.py -v`
Expected: PASS

- [ ] **Step 5: Vérifier le vert complet**

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

### Task 7: Émetteur côté processus MCP

**Files:**
- Create: `src/brain_v42/mcp/activity_reporter.py`
- Modify: `src/brain_v42/mcp/provenance_middleware.py`, `src/brain_v42/config.py`
- Test: `tests/unit/test_activity_reporter.py`

**Interfaces:**
- Consumes: tâche 3 (`_report`), tâche 6 (format de fil).
- Produces: `ActivityReporter(url: str, timeout: float = 1.0, max_in_flight: int = 8)` avec `report(actor: str, session_id: str | None) -> None` et `async close() -> None` ; `get_activity_reporter() -> ActivityReporter | None`.

**Contrainte : l'émission ne doit jamais ralentir ni casser un appel de tool.** Elle est en tâche de fond, plafonnée, et toute erreur est avalée après journalisation.

- [ ] **Step 1: Écrire les tests qui échouent**

Créer `tests/unit/test_activity_reporter.py` :

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
        await reporter.drain()  # ne doit pas lever
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
            reporter.report("brain-v42", None)  # doit rendre la main aussitôt
        assert reporter.dropped == 10
        release.set()
        await reporter.drain()
    await reporter.close()
```

- [ ] **Step 2: Vérifier l'échec**

Run: `pytest tests/unit/test_activity_reporter.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implémenter**

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

`report` doit rester **synchrone** : le middleware l'appelle sans `await`, donc aucune latence n'est ajoutée à l'appel de tool.

Câbler `_report` dans `provenance_middleware.py` — importer `get_activity_reporter` depuis `brain_v42.mcp.activity_reporter` :

```python
    def _report(self, actor: str, session: str | None) -> None:
        reporter = get_activity_reporter()
        if reporter is not None:
            reporter.report(actor, session)
```

Ajouter les réglages dans `src/brain_v42/config.py`, juste après `metrics_host` (ligne 109) :

```python
    client_activity_reporting_enabled: bool = True
    client_activity_url: str = "http://127.0.0.1:9200/v1/client-activity"
```

**Aucune modification de `mcp/server.py`.** L'émetteur naît à la première émission ; le middleware y est déjà enregistré à la ligne 261.

Les tests doivent remettre l'état global à zéro pour ne pas fuiter d'un test à l'autre :

```python
@pytest.fixture(autouse=True)
def _reset_reporter() -> Any:
    from brain_v42.mcp import activity_reporter

    activity_reporter.set_activity_reporter(None)
    yield
    activity_reporter.set_activity_reporter(None)
```

- [ ] **Step 4: Vérifier que ça passe**

Run: `pytest tests/unit/test_activity_reporter.py tests/unit/test_provenance_middleware.py -v`
Expected: PASS

- [ ] **Step 5: Vérifier le vert complet**

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

### Task 8: Fusion dans le registre

Le cœur du lot. C'est ici que la jointure existe ou non.

**Files:**
- Modify: `src/brain_v42/metrics/client_activity.py`
- Test: `tests/unit/test_client_activity.py`

**Interfaces:**
- Consumes: tâches 4, 5, 6.
- Produces: sur `ClientActivityRegistry` — `ingest_claude_otlp_json(payload: bytes) -> None`, `record_observations(observations: tuple[ClientObservation, ...]) -> None`, et `snapshot()` qui gagne la clé `clients` sans perdre `active_convs`, `ctx_tokens` ni `activeConvs`.

**Modèle de ligne :**

| `kind` | Clé interne | `id` | Champs remplis |
|--------|-------------|------|----------------|
| `session` | pseudonyme HMAC de l'UUID | `<agent>-<32hex>` | tous ceux dont une source existe |
| `unattributed` | `actor:<acteur>` | `unattributed:<acteur>` | `actor`, `brain_calls`, `last_seen_s` ; le reste `null` |

Une ligne `session` peut n'avoir que le côté OTLP (`actor` et `brain_calls` à `null`) ou que le côté brain (`tokens`, `turns`, `cost`, `model`, `agent` à `null`).

- [ ] **Step 1: Écrire les tests qui échouent**

Ajouter à `tests/unit/test_client_activity.py` :

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

- [ ] **Step 2: Vérifier l'échec**

Run: `pytest tests/unit/test_client_activity.py -v`
Expected: FAIL — `AttributeError: 'ClientActivityRegistry' object has no attribute 'ingest_claude_otlp_json'`

- [ ] **Step 3: Implémenter**

Dans `client_activity.py`, ajouter les imports nécessaires :

```python
from brain_v42.metrics.claude_telemetry import decode_claude_logs
from brain_v42.metrics.client_observation import ClientObservation
```

`client_observation.py` importe `provenance`, `claude_telemetry` importe
`codex_telemetry` : aucun cycle, `client_activity` est en bout de chaîne.

Ajouter à côté de `_Conversation` :

```python
@dataclass(frozen=True, slots=True)
class _BrainActivity:
    actor: str
    calls: int
    last_seen: float
```

Ajouter au registre un second dictionnaire `self._brain: dict[str, _BrainActivity]`, dont la clé est le pseudonyme HMAC quand une session est déclarée, et `f"actor:{actor}"` sinon.

Ajouter les trois méthodes :

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
                # Le contexte réel est la somme des trois compteurs d'entrée.
                # input_tokens seul vaut 10 là où le contexte en fait 18590
                # (mesuré le 2026-08-06) : l'afficher seul mentirait d'un
                # facteur 1000. On garde la valeur du DERNIER événement, pas un
                # cumul : c'est une taille de contexte, pas un débit.
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

`_pseudonym` prend désormais l'agent en paramètre pour préfixer le pseudonyme (`codex-…` / `claude-…`), ce qui conserve la forme actuelle pour Codex. Le sel HMAC reste distinct par agent afin que deux agents ne puissent pas se croiser sur un même UUID :

```python
    def _pseudonym(self, identifier: str, agent: str = "codex") -> str:
        digest = hmac.new(
            self._secret,
            f"{agent}-conversation-id\0".encode("ascii") + identifier.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return f"{agent}-{digest[:32]}"
```

**Attention** : ce changement de sel casserait `tests/unit/test_codex_telemetry_endpoint.py` si celui-ci épingle un pseudonyme littéral. Vérifier avant, et si c'est le cas, garder le sel Codex historique exactement — `b"codex-conversation-id\0"` — et n'introduire le nouveau sel que pour `claude`.

Enfin, `snapshot()` construit `clients` en fusionnant les deux dictionnaires :

```python
            rows: list[dict[str, object]] = []
            for item in ordered:  # conversations OTLP, déjà triées
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

Ajouter `"clients": rows` au dictionnaire retourné, **sans retirer** `active_convs`, `ctx_tokens` ni `activeConvs`.

Ajouter aux `_Conversation` existantes les champs `agent: str` (défaut `"codex"`) et `cost: float | None` (défaut `None`), et faire passer `agent="codex"` dans `ingest_otlp_json`.

- [ ] **Step 4: Vérifier que ça passe**

```bash
pytest tests/unit/test_client_activity.py tests/unit/test_codex_telemetry_endpoint.py -v
```
Expected: PASS des deux côtés. Les tests Codex existants restent la preuve de non-régression.

- [ ] **Step 5: Vérifier le vert complet**

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

### Task 9: Récepteurs du sidecar et exposition dans le cockpit

**Files:**
- Modify: `src/brain_v42/metrics/server.py`, `src/brain_v42/metrics/cockpit.py`
- Test: `tests/unit/test_client_activity_endpoint.py`, `tests/unit/test_metrics_cockpit_collector.py`

**Interfaces:**
- Consumes: tâches 5, 6, 8.
- Produits: routes `POST /v1/client-activity` et `POST /v1/logs/claude` sur le sidecar ; clé `clients` dans la charge de `/api/cockpit`.

Deux routes OTLP distinctes plutôt qu'un seul récepteur qui devine le schéma : deviner obligerait à sonder les attributs d'une charge non encore validée.

- [ ] **Step 1: Écrire les tests qui échouent**

Créer `tests/unit/test_client_activity_endpoint.py`. Le harnais suit celui de
`tests/unit/test_codex_telemetry_endpoint.py` : la fixture `aiohttp_client` de
`pytest-aiohttp` pour le chemin nominal, `make_mocked_request` avec un transport
factice pour les rejets de pair.

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

`_loopback_transport` et `MAX_OBSERVATION_BYTES` doivent correspondre à ce que
`server.py` vérifie réellement : relire `_has_loopback_tcp_peer`
(`server.py:55`) avant d'écrire le mock, la forme du `peername` en dépend.

Ajouter à `tests/unit/test_metrics_cockpit_collector.py` :

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

- [ ] **Step 2: Vérifier l'échec**

Run: `pytest tests/unit/test_client_activity_endpoint.py tests/unit/test_metrics_cockpit_collector.py -v`
Expected: FAIL — routes 404, puis `KeyError: 'clients'`

- [ ] **Step 3: Implémenter**

Dans `server.py`, factoriser le durcissement existant de `_handle_codex_logs` en un helper commun, puis enregistrer les routes sous la même condition `_is_loopback_bind(self._host)` :

```python
        if _is_loopback_bind(self._host):
            app.router.add_post("/v1/logs", self._handle_codex_logs)
            app.router.add_post("/v1/logs/claude", self._handle_claude_logs)
            app.router.add_post("/v1/client-activity", self._handle_client_activity)
```

`_handle_claude_logs` reprend `_handle_codex_logs` à l'identique, en appelant `ingest_claude_otlp_json`.

`_handle_client_activity` applique le même contrôle de pair loopback et de sémaphore, lit un corps borné par `MAX_OBSERVATION_BYTES`, décode avec `decode_observations` et applique via `record_observations`. Les mêmes exceptions donnent les mêmes statuts : `CodexTelemetryLimitError` → 413, `CodexTelemetryMalformedError` → 400.

Dans `cockpit.py`, ajouter `"clients": codex_activity["clients"]` au dictionnaire retourné, et étendre le repli sans registre :

```python
        codex_activity = (
            self._codex_registry.snapshot()
            if self._codex_registry is not None
            else {"active_convs": 0, "ctx_tokens": 0, "activeConvs": [], "clients": []}
        )
```

- [ ] **Step 4: Vérifier que ça passe**

```bash
pytest tests/unit/test_client_activity_endpoint.py tests/unit/test_metrics_cockpit_collector.py tests/unit/test_codex_telemetry_endpoint.py tests/unit/test_cockpit_endpoint.py -v
```
Expected: PASS

- [ ] **Step 5: Vérifier le vert complet**

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

### Task 10: Panneau « Live workload » dans red-monitor

**Dépôt différent.** `cd ~/hawkixs_infra/git_repo/ReD_v1/projects/red-monitor`. Lire son `CLAUDE.md` avant de commencer : conventions et commandes de test lui appartiennent.

**Files:**
- Modify: `frontend/src/tabs/brain/BrainActivity.jsx`, `frontend/src/tabs/brain/brainPresentation.js`, `frontend/src/tabs/brain/BrainStatusBar.jsx`
- Test: `frontend/src/tabs/brain/Brain.test.jsx`

**Interfaces:**
- Consumes: la clé `clients[]` de `/api/brain/live`, produite par la tâche 9.
- Produces: rien en aval.

**Aucun travail Go.** `internal/web/brain.go` reproxifie les octets bruts : les champs neufs arrivent seuls.

- [ ] **Step 1: Écrire les tests qui échouent**

Dans `frontend/src/tabs/brain/Brain.test.jsx` :

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

- [ ] **Step 2: Vérifier l'échec**

Run: `cd frontend && npm test -- Brain.test.jsx`
Expected: FAIL — le panneau lit encore `live.activeConvs`.

- [ ] **Step 3: Implémenter**

Dans `BrainActivity.jsx` : remplacer `props.live.activeConvs` par `props.live.clients`, le titre `Codex activity` par `Live workload`, le `data-testid` `brain-codex` par `brain-clients`, et ajouter `data-testid={`client-${client.id}`}` sur chaque `<article>`.

Chaque colonne passe par un formateur qui rend `—` pour `null` ou `undefined` : `formatCompactNumber`, `formatCost` et `formatPercent` le font déjà (`brainPresentation.js:20-47`). Ne pas écrire `client.tokens || 0` — ce serait exactement le `0` cosmétique que la spec interdit.

Une ligne `kind === 'unattributed'` porte une classe distincte et le libellé « non attribué — la session n'est pas déclarée ».

Ajouter sous la liste une mention permanente : « acteur et session déclarés par le client, non prouvés ».

Dans `brainPresentation.js`, `shortPseudonym` retourne `'anonymous'` et non plus `'codex-anonymous'`. Dans `BrainStatusBar.jsx:55`, `label="Codex"` devient `label="Clients"` et la valeur compte `clients.length`.

- [ ] **Step 4: Vérifier que ça passe**

```bash
cd frontend && npm test
```
Expected: PASS, y compris les tests existants de `Brain.test.jsx`.

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

Le dépôt red-monitor utilise des commits à emoji (voir `git log`) et n'a pas la convention `Co-Authored-By` de brain_v42 : suivre l'usage local.

---

### Task 11: Vérification bout en bout et déclaration de la frontière réseau

**Files:**
- Modify: `CLAUDE.md` (brain_v42)

**Interfaces:**
- Consumes: toutes les tâches précédentes.
- Produces: rien.

**Ce que cette tâche ne peut pas faire seule.** Deux des trois chemins livrés sont
*silencieux quand ils échouent* : l'émetteur brain est livré FERMÉ (`client_activity_reporting_enabled=False`,
commit `e8951011`) et le récepteur OTLP répond `200` même quand il jette tout le lot. Une
vérification qui se contente de regarder le panneau conclut donc « pas de trafic » aussi bien
devant une chaîne saine et inactive que devant une chaîne mal câblée. Les Steps 3, 4 et 5
existent pour lever cette ambiguïté ; ne pas les sauter.

- [ ] **Step 0: Vérifier que les unités exécuteront bien le code livré**

Les deux unités systemd tournent sur la **racine de production**, pas sur un worktree :
`brain-metrics.service` a `WorkingDirectory=/home/hawixs/hawkixs_infra/git_repo/brain_v42` et
l'installation éditable du venv (`_editable_impl_brain_v42.pth`) pointe sur
`/home/hawixs/hawkixs_infra/git_repo/brain_v42/src`. Redémarrer depuis un worktree ne change
donc rien : la branche doit d'abord être fusionnée dans la racine.

```bash
BRAIN_ROOT=/home/hawixs/hawkixs_infra/git_repo/brain_v42
$BRAIN_ROOT/.venv/bin/python -c "import brain_v42.metrics.server as m; print(m.__file__)"
grep -n "v1/logs/claude\|v1/client-activity" $BRAIN_ROOT/src/brain_v42/metrics/server.py
grep -n "client_activity_reporting_enabled" $BRAIN_ROOT/src/brain_v42/config.py
```
Expected: le `__file__` est sous `$BRAIN_ROOT/src/`, et les trois `grep` trouvent leur ligne. Si
l'une manque, **arrêter** : la fusion n'est pas faite et tout ce qui suit mesurerait l'ancien code.

- [ ] **Step 1: Redémarrer les deux processus et sonder le récepteur brain**

Les unités s'appellent **`brain-metrics.service`** et **`brain-mcp-http.service`**. Il n'existe ni
`brain-v42-metrics` ni `brain-v42-mcp` : ces noms-là sortent sur `Unit … could not be found`,
**aucun processus ne redémarre**, et le `curl` suivant interroge alors l'ancien code — un `404`
qu'on lira comme une route cassée. Vérifier les noms plutôt que les recopier :

```bash
systemctl --user list-units --type=service --all --no-legend | grep -E 'brain-(metrics|mcp-http)\.service'
systemctl --user restart brain-metrics.service brain-mcp-http.service
systemctl --user is-active brain-metrics.service brain-mcp-http.service
```
Expected: `active` deux fois. (`brain-mcp-http.service` a deux `ExecStartPre` de préflight —
projecteur graphe et port MCP ; un échec de redémarrage se lit dans
`journalctl --user -u brain-mcp-http.service -n 30`.)

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H 'Content-Type: application/json' \
  -d '{"observations":[{"actor":"probe","calls":1}]}' \
  http://127.0.0.1:9200/v1/client-activity
```
Expected: `200`. Grille de lecture d'un autre statut : `404` = le processus tourne sur du code
antérieur à la tâche 9, ou son bind n'est pas loopback (les trois routes ne sont enregistrées que
si `METRICS_HOST` est loopback) ; `403` = le pair n'est pas loopback ; `415` = l'en-tête
`Content-Type` s'est perdu ; `413`/`400` = le corps sort des bornes ou n'est pas la forme attendue.

- [ ] **Step 2: Vérifier que la ligne de sonde apparaît**

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
Expected: une ligne `"id": "unattributed:probe"`, `"kind": "unattributed"`, `"brain_calls": 1`, et
`null` — jamais `0` — dans `agent`, `started`, `model`, `turns`, `tokens`, `cost`. La rétention du
registre est de 600 s (`ACTIVITY_TTL_SECONDS`) : lire dans les dix minutes qui suivent la sonde.

La clé `clients` **absente** est le symptôme mesuré d'un sidecar non migré (la charge d'avant la
tâche 9 n'expose que `activeConvs`) : revenir au Step 0 plutôt que de conclure « pas de client ».

À ce stade, seul le **récepteur** est prouvé. Rien n'a encore prouvé qu'un client émet.

- [ ] **Step 3: Armer l'émetteur brain — et savoir le désarmer**

Sans ce geste, le processus MCP n'émet **aucune** observation et la vérification du panneau
(Step 7) est inatteignable, quel que soit le reste. Deux caches rendent l'armement inséparable du
redémarrage :

- `get_activity_reporter()` est un singleton paresseux — une fois `_reporter` construit, il n'est
  **jamais** reconstruit et le killswitch n'est **jamais** relu ;
- `get_settings()` est `@lru_cache(maxsize=1)` — le killswitch est donc lu au plus une fois par
  processus, même avant toute construction.

Armer sans redémarrer `brain-mcp-http.service` ne fait donc rien du tout.

Armement par drop-in systemd, sur le modèle déjà présent de `brain-metrics.service.d/transport.conf`.
Un drop-in plutôt que le `.env` partagé : il est réversible d'un seul `rm`, il ne porte que sur
l'unité MCP (le sidecar lit le même `.env` via son `WorkingDirectory`), et une variable
d'environnement systemd l'emporte sur le dotenv dans pydantic-settings.

```bash
mkdir -p ~/.config/systemd/user/brain-mcp-http.service.d
cat > ~/.config/systemd/user/brain-mcp-http.service.d/client-activity.conf <<'EOF'
[Service]
# Armement du rapport d'activité client — geste d'opérateur, rollout du panneau Live workload.
Environment=CLIENT_ACTIVITY_REPORTING_ENABLED=true
EOF
systemctl --user daemon-reload
systemctl --user restart brain-mcp-http.service
systemctl --user show brain-mcp-http.service -p Environment | grep CLIENT_ACTIVITY
```
Expected: la variable apparaît dans `Environment=`, et l'unité est `active`.

Ne **pas** poser `CLIENT_ACTIVITY_URL` : le défaut vaut `http://127.0.0.1:9200/v1/client-activity`
et un `field_validator` refuse déjà toute cible non loopback — une valeur LAN ferait échouer le
démarrage, pas fuir la donnée.

**Désarmement** (à jouer tel quel si la vérification tourne mal) :

```bash
rm ~/.config/systemd/user/brain-mcp-http.service.d/client-activity.conf
rmdir --ignore-fail-on-non-empty ~/.config/systemd/user/brain-mcp-http.service.d
systemctl --user daemon-reload
systemctl --user restart brain-mcp-http.service
systemctl --user show brain-mcp-http.service -p Environment | grep CLIENT_ACTIVITY || echo "désarmé"
```
Preuve du désarmement : après un appel de tool brain, aucune ligne `unattributed:` nouvelle
n'apparaît dans `/api/cockpit`.

> **Arbitrage à trancher par l'opérateur, pas par ce plan** : le drop-in survit au reboot. Décider
> explicitement, à la fin du rollout, si l'émetteur reste armé en permanence (et migre alors vers
> le `.env` partagé, avec mise à jour de la section Configuration de `CLAUDE.md`) ou s'il est
> désarmé après la vérification. Ne pas laisser ce choix se faire par oubli.

- [ ] **Step 4: Pointer l'exportateur Claude sur la route Claude**

C'est le piège le plus coûteux du rollout, parce qu'il est **muet**. Mesuré sur Claude Code
2.1.223 : l'exportateur OTLP embarqué résout l'URL par
`convertLegacyHttpOptions(config, "LOGS", "v1/logs", …)`, soit `url = <signal-specific> ?? <générique + "/v1/logs">`.

| Variable | Traitement mesuré | Route atteinte |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:9200` | `v1/logs` **suffixé** | `/v1/logs` — décodeur **Codex** |
| `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://127.0.0.1:9200/v1/logs/claude` | utilisé **tel quel** (`new URL(v).toString()`) | `/v1/logs/claude` — décodeur Claude |

La variable signal-spécifique **prime** sur la générique.

Envoyé sur `/v1/logs`, un lot Claude entre dans le décodeur Codex : `event.name` y est nu
(`user_prompt`, `api_request`) alors que `_DIRECT_EVENTS` et `_COMPLETION_EVENTS` sont tous
préfixés `codex.`. Chaque enregistrement est donc écarté, **et le récepteur répond `200 {}`**. Un
endpoint mal configuré est rigoureusement indiscernable d'une absence de trafic.

Le symétrique est vrai et se lit dans le même code : un lot **Codex** arrivant sur
`/v1/logs/claude` est confronté à `_KNOWN_EVENTS = {"user_prompt", "api_request"}` ;
`codex.user_prompt` n'y est pas, l'enregistrement est écarté avant même que `session.id` soit lu,
et le récepteur répond `200 {}`. Aucune des deux erreurs ne se signale.

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://127.0.0.1:9200/v1/logs/claude
unset OTEL_EXPORTER_OTLP_ENDPOINT
```

Sans barre oblique finale : les routes `aiohttp` sont exactes et `new URL(...).toString()` préserve
le chemin verbatim (mesuré). Ne pas laisser la générique posée « au cas où » : elle masquerait la
mauvaise configuration sur toute machine où la signal-spécifique se perdrait.

Codex, lui, garde `/v1/logs` dans `~/.codex/config.toml`
(`endpoint = "http://127.0.0.1:9200/v1/logs"`, cf. `docs/plans/2026-07-19-codex-otlp-cockpit-bridge-plan.md:392`).
Les lots partent toutes les `OTEL_LOGS_EXPORT_INTERVAL` ms, **5000 par défaut** (mesuré) :
attendre au moins dix secondes après un prompt avant de conclure quoi que ce soit.

- [ ] **Step 5: Distinguer « aucun trafic » de « trafic jeté »**

Deux moitiés, deux instruments différents. Aucune des deux ne se lit dans le panneau.

**a) Côté brain (`/v1/client-activity`).** L'émetteur compte à part les refus
(`ActivityReporter.refused`, distinct de `dropped` qui ne compte que la contre-pression locale) et
journalise le statut seul. Le compteur vit en mémoire du processus MCP et n'est exposé par aucune
route : la trace observable est la ligne de journal. Les lignes `[debug]` structlog du serveur MCP
sortent bien dans le journal sans changer `LOG_LEVEL` (mesuré : `access_log.purged`,
`metrics_flusher.flushed` y sont visibles).

Faire un appel de tool brain depuis une session Claude, puis :

```bash
journalctl --user -u brain-mcp-http.service --since "-5 min" --no-pager \
  | grep -E 'activity_reporter\.(refused|post_failed|unavailable)'
```

| Observation | Diagnostic |
|---|---|
| une ligne `unattributed:<acteur>` dans `/api/cockpit` | accepté — rien à faire |
| pas de ligne + `activity_reporter.refused status=404` | émis et **refusé** : route absente (ancien processus, ou bind non loopback) |
| pas de ligne + `activity_reporter.refused status=403/413/415/400` | émis et **refusé** : pair, bornes ou format |
| pas de ligne + `activity_reporter.post_failed error=ConnectError` | sidecar arrêté ou mauvais port |
| pas de ligne + `activity_reporter.unavailable` | settings illisibles côté MCP |
| pas de ligne **et aucune de ces lignes** | rien n'a jamais été émis : killswitch encore fermé (Step 3 non appliqué ou non redémarré), ou aucun tool brain appelé |

**b) Côté OTLP Claude (`/v1/logs/claude`).** Ici il n'y a **ni compteur ni journal** : le sidecar
répond `200 {}` qu'il ait tout gardé ou tout jeté, et l'unité métriques n'émet aucune ligne d'accès
par requête (mesuré : `journalctl --user -u brain-metrics.service | grep -c 'POST /v1/'` vaut `0`).
Le silence n'est donc pas une preuve. Le seul discriminant honnête est de prouver d'abord que
l'exportateur émet, avec un récepteur jetable sur un autre port — le même qu'à la tâche 1, Step 3 :

```bash
# terminal A — récepteur jetable
/home/hawixs/hawkixs_infra/git_repo/brain_v42/.venv/bin/python - <<'PY'
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        print("captured", self.path, len(body), "bytes")
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
HTTPServer(("127.0.0.1", 4318), H).serve_forever()
PY

# terminal B — session Claude avec la même configuration qu'au Step 4, mais :
#   export OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://127.0.0.1:4318/v1/logs/claude
```

- rien de capturé après un prompt et dix secondes → **aucun trafic** : l'exportateur n'est pas armé
  dans l'environnement de cette session (`CLAUDE_CODE_ENABLE_TELEMETRY` / `OTEL_LOGS_EXPORTER`) ;
- capturé sur `4318`, puis aucune ligne `"agent": "claude"` dans `/api/cockpit` une fois repointé
  sur `9200` → **trafic jeté** : relire le chemin (Step 4), puis les noms d'événements et
  d'attributs contre l'oracle `tests/fixtures/claude_otlp_logs.json`.

Arrêter le récepteur jetable avant de continuer.

- [ ] **Step 6: Vérifier le refus depuis un pair non-loopback, sur les trois routes**

```bash
ssh arman@192.168.1.11 'for p in /v1/logs /v1/logs/claude /v1/client-activity; do \
  curl -s -m 5 -o /dev/null -w "$p %{http_code}\n" -X POST \
    -H "Content-Type: application/json" -d "{}" http://192.168.1.12:9200$p; done'
```

Expected: `000` pour les trois, avec un `curl` en échec de connexion (code de sortie 7) ou en
timeout (28). Le sidecar est lié à `127.0.0.1` : la connexion ne doit pas s'établir.

**Toute** réponse HTTP — y compris un `404` — signifie que la socket a répondu sur le LAN :
**arrêter et signaler**, le bind n'est pas celui que la configuration annonce. Un `404` est
d'ailleurs l'aveu le plus probable : sur un bind non loopback, les trois routes ne sont pas
enregistrées du tout. Un bind LAN n'expose donc pas ces récepteurs — il les désactive
silencieusement, ce qui casse la moitié brain du panneau sans rien dire.

- [ ] **Step 7: Vérifier le panneau**

Prérequis : la tâche 10 est fusionnée et red-monitor redémarré ; les Steps 3 et 4 sont appliqués.

Ouvrir red-monitor, onglet Brain. Le verdict du spike est **`JOINTURE IMPOSSIBLE`**
(`docs/upstream/2026-08-06-claude-otlp-session-join.md`) : ne pas attendre une ligne unique par
session. L'attendu réel est :

- **une ligne OTLP par session Claude vivante** (`kind: session`, `agent: claude`), avec `actor` et
  `brain_calls` à `—` : l'OTLP ne sait pas quel acteur appelle le brain ;
- **une ligne `unattributed:<acteur>` par ACTEUR** côté brain — pas par session. `X-Brain-Agent`
  vaut `${PWD}` réduit au basename du projet, donc plusieurs sessions Claude du même projet
  s'agrègent dans **une seule** ligne dont `brain_calls` est leur somme ;
- une ligne par conversation Codex vivante, plus `unattributed:codex` si Codex appelle le brain ;
- des tirets cadratins partout où rien n'est mesuré, et **aucun `0`** dans une colonne sans source.

Recouper au moins une valeur avec la source : la ligne lue dans le panneau doit correspondre à la
même ligne de `curl -s http://127.0.0.1:9200/api/cockpit` (Step 2). Le panneau lit
`/api/brain/live`, qui reproxifie ces octets : une divergence est un défaut de rendu, pas de mesure.

- [ ] **Step 8: Déclarer la frontière réseau**

Dans `CLAUDE.md`, bloc « Tracked network boundary », déclarer les **deux** faces du changement —
pas seulement les récepteurs :

1. **Entrées.** Le sidecar métriques enregistre trois récepteurs push, et seulement si son bind est
   loopback : `/v1/logs` (OTLP Codex), `/v1/logs/claude` (OTLP Claude Code, **neuf**) et
   `/v1/client-activity` (observations côté brain, **neuf**). Tous bornés (taille de corps, requêtes
   en vol, encodage `identity`, un seul `Content-Type: application/json`), pair loopback exigé,
   fail-closed, **sans authentification applicative**. Un bind non loopback ne les expose pas : il
   ne les enregistre pas.
2. **Sortie, neuve.** Le processus MCP (`brain-mcp-http.service`) devient **client HTTP local** du
   sidecar : un POST feu-et-oubli vers `CLIENT_ACTIVITY_URL` à chaque appel de tool le plus externe.
   C'est une sortie réseau nouvelle pour ce processus, contrainte au loopback par un
   `field_validator` sur `client_activity_url`, livrée **fermée**
   (`CLIENT_ACTIVITY_REPORTING_ENABLED=false`) ; son armement est un geste d'opérateur (Step 3).

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

## Ordre et dépendances

```
1 (spike) ──┬─→ 5 (décodeur Claude) ──┐
            │                          ├─→ 8 (fusion) ─→ 9 (routes + cockpit) ─→ 10 (panneau) ─→ 11 (vérif)
2 (session) ┴─→ 3 (garde) ─→ 7 (émetteur) ─┤
                              6 (fil) ──────┘
            4 (déménagement) ─────────────────┘
```

Les tâches 2, 4 et 6 ne dépendent de rien et peuvent être faites dans n'importe quel ordre. La tâche 4 doit précéder la 8. La tâche 1 est une porte pour la 5 et la 8.

## Ce que ce plan ne fait pas

- Aucune persistance : le registre est en mémoire et perd tout au redémarrage. Un historique agrégé est un autre chantier.
- Aucun retrait de `activeConvs` du payload : différé à une fois le panneau basculé et observé.
- Aucun changement à `process_metrics` ni à `flusher.py`.
- Aucun lien avec `brain_sessions` : la table n'est pas une source de liveness.
- Aucun changement d'authentification du sidecar.
