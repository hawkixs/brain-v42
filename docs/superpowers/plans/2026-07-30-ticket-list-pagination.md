# Ticket List Pagination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rendre chaque ticket du backlog accessible depuis `brain_ticket_list`, signaler chaque coupe et remplacer l'ordre par âge croissant par un ordre d'activité documenté.

**Architecture:** Le dépôt garde la sélection complète des trois catégories et applique un ordre déterministe commun à tous ses consommateurs. Le tool MCP page ensuite chaque catégorie en mémoire, conserve les totaux et produit une notice navigable. Cette séparation évite une migration et préserve `TicketGroups` ainsi que le briefing de session.

**Tech Stack:** Python 3.12+, FastMCP 3.x, SQLAlchemy 2 async, Pydantic 2, pytest, Ruff, mypy.

## Global Constraints

- Suivre RED–GREEN–REFACTOR ; aucun code de production avant un test qui échoue pour la raison attendue.
- Utiliser la venv ignorée du worktree en Python 3.12.12 et invoquer chaque outil par `.venv/bin/python -m ...`.
- Exécuter l'impact GitNexus upstream avant de modifier chaque symbole existant.
- Ne muter aucun ticket frère, ne lancer aucune requête SQL directe et ne déployer aucun changement.
- Conserver les catégories « À traiter », « À confirmer » et « En attente de l'autre côté ».
- Borner `limit` à `[1, 100]`, normaliser `offset` à `>= 0` et garder les valeurs par défaut `limit=10`, `offset=0`.
- Ordonner par `updated_at DESC`, `created_at DESC`, puis `id ASC`.

---

### Task 1: Pagination et notices MCP

**Files:**
- Modify: `tests/unit/mcp/test_ticket_tools.py`
- Modify: `src/brain_v42/mcp/tools/ticket_tools.py`

**Interfaces:**
- Consumes: `TicketGroups` complet renvoyé par `TicketService.list_grouped(project_key)`.
- Produces: `brain_ticket_list(project_key: str, limit: int = 10, offset: int = 0) -> str` et `_format_groups(groups, project_key, limit, offset) -> str`.

- [ ] **Step 1: Ajouter le test RED de notice par défaut**

```python
async def test_list_default_page_reports_exact_omission_and_next_call(self):
    tickets = [_ticket(title=f"ticket-{index}") for index in range(12)]
    svc = MagicMock()
    svc.list_grouped = AsyncMock(return_value=TicketGroups(a_traiter=tickets))
    tool = await _tool(_mcp_with(svc), "brain_ticket_list")

    result = await tool.fn(project_key=TO)

    assert "À traiter (12)" in result
    assert "2 omis" in result
    assert "limit=10, offset=10" in result
```

- [ ] **Step 2: Exécuter le test et vérifier l'échec attendu**

Run: `.venv/bin/python -m pytest tests/unit/mcp/test_ticket_tools.py::TestListAndGet::test_list_default_page_reports_exact_omission_and_next_call -q`

Expected: FAIL, car le rendu actuel coupe à 10 sans notice.

- [ ] **Step 3: Ajouter le test RED d'accès aux pages ultérieures**

```python
async def test_list_offset_reaches_later_tickets_in_every_category(self):
    incoming = [_ticket(title=f"in-{index}") for index in range(12)]
    outgoing = [_ticket(title=f"out-{index}") for index in range(12)]
    svc = MagicMock()
    svc.list_grouped = AsyncMock(
        return_value=TicketGroups(a_traiter=incoming, en_attente=outgoing)
    )
    tool = await _tool(_mcp_with(svc), "brain_ticket_list")

    result = await tool.fn(project_key=TO, limit=5, offset=10)

    assert "in-10" in result and "in-11" in result
    assert "out-10" in result and "out-11" in result
    assert "in-9" not in result and "out-9" not in result
    assert result.count("10 avant") == 2
```

- [ ] **Step 4: Exécuter le test et vérifier l'échec attendu**

Run: `.venv/bin/python -m pytest tests/unit/mcp/test_ticket_tools.py::TestListAndGet::test_list_offset_reaches_later_tickets_in_every_category -q`

Expected: FAIL avec un argument `limit` inattendu, car le contrat MCP n'expose aucune pagination.

- [ ] **Step 5: Implémenter la page et la notice minimales**

```python
_LIST_DEFAULT_LIMIT = 10
_LIST_MAX_LIMIT = 100


def _format_group_page(
    lines: list[str],
    *,
    label: str,
    tickets: list[Ticket],
    direction: str,
    project_key: str,
    limit: int,
    offset: int,
) -> None:
    if not tickets:
        return
    page = tickets[offset : offset + limit]
    lines.append(f"\n### {label} ({len(tickets)})")
    lines.extend(_ticket_line(ticket, direction=direction) for ticket in page)
    omitted_before = min(offset, len(tickets))
    omitted_after = max(0, len(tickets) - offset - len(page))
    omitted = len(tickets) - len(page)
    if omitted:
        notice = f"… ({omitted} omis sur cette page; {omitted_before} avant, {omitted_after} après"
        if omitted_after:
            notice += (
                "; suite: brain_ticket_list("
                f"project_key='{project_key}', limit={limit}, offset={offset + limit})"
            )
        lines.append(notice + ")")
```

Mettre `brain_ticket_list` en version `1.1`, borner `limit`, normaliser `offset`, passer les paramètres à `_format_groups` et documenter la pagination par catégorie.

- [ ] **Step 6: Exécuter les tests MCP ciblés**

Run: `.venv/bin/python -m pytest tests/unit/mcp/test_ticket_tools.py -q`

Expected: PASS.

### Task 2: Ordre d'activité déterministe

**Files:**
- Modify: `tests/unit/repositories/test_pg_ticket.py`
- Modify: `src/brain_v42/repositories/pg_ticket.py`

**Interfaces:**
- Consumes: table SQLAlchemy `tickets` avec `updated_at`, `created_at` et `id`.
- Produces: trois listes `TicketGroups` dans le même ordre récent et stable.

- [ ] **Step 1: Ajouter le test RED des trois requêtes**

```python
class TestListGrouped:
    async def test_each_category_orders_recent_activity_first_with_stable_ties(self) -> None:
        empty = MagicMock()
        empty.mappings.return_value.all.return_value = []
        session = _session(empty, empty, empty)
        repo = _repo_with_session(session)

        await repo.list_grouped("brain-v42")

        for call in session.execute.await_args_list:
            sql = str(call.args[0].compile(compile_kwargs={"literal_binds": True}))
            assert "ORDER BY tickets.updated_at DESC, tickets.created_at DESC, tickets.id ASC" in sql
```

- [ ] **Step 2: Exécuter le test et vérifier l'échec attendu**

Run: `.venv/bin/python -m pytest tests/unit/repositories/test_pg_ticket.py::TestListGrouped::test_each_category_orders_recent_activity_first_with_stable_ties -q`

Expected: FAIL, car les requêtes utilisent seulement `tickets.created_at ASC`.

- [ ] **Step 3: Implémenter l'ordre minimal**

Remplacer l'ordre existant par :

```python
.order_by(
    tickets.c.updated_at.desc(),
    tickets.c.created_at.desc(),
    tickets.c.id.asc(),
)
```

- [ ] **Step 4: Exécuter les suites ciblées**

Run: `.venv/bin/python -m pytest tests/unit/mcp/test_ticket_tools.py tests/unit/repositories/test_pg_ticket.py tests/unit/services/test_ticket_service.py -q`

Expected: PASS.

### Task 3: Inventaire et livraison contrôlée

**Files:**
- Modify only if a defect is found by verification: files already listed in Tasks 1 and 2.

**Interfaces:**
- Consumes: tools Brain de lecture `brain_ticket_list` et `brain_ticket_get`.
- Produces: inventaire final des parents partiels, tickets sortants et tickets sans enfant ; commit local ; verdicts `red-reviewer` et `red-tester` sur un HEAD commun.

- [ ] **Step 1: Inventorier sans mutation**

Lire les tickets cités par le ticket fe1c8c33 et consigner pour chacun son statut, son propriétaire, son résidu ou son blocage. Ne lancer ni `brain_ticket_reply` ni `brain_ticket_transition`.

- [ ] **Step 2: Vérifier le périmètre GitNexus**

Run: `gitnexus_detect_changes(scope="all", worktree="/home/hawixs/.codex/worktrees/3aeb/brain_v42")`

Expected: seuls `brain_ticket_list`, `_format_groups`, le nouveau helper de rendu, `PgTicketRepo.list_grouped` et leurs tests sont affectés.

- [ ] **Step 3: Exécuter les contrôles complets**

```bash
.venv/bin/python -m pytest tests/unit -q
.venv/bin/python -m ruff check src/ tests/
.venv/bin/python -m ruff format --check src/ tests/
.venv/bin/python -m mypy src/
```

Expected: quatre commandes à code retour 0.

- [ ] **Step 4: Committer le périmètre du ticket**

```bash
git add docs/superpowers/specs/2026-07-30-ticket-list-pagination-design.md \
  docs/superpowers/plans/2026-07-30-ticket-list-pagination.md \
  src/brain_v42/mcp/tools/ticket_tools.py \
  src/brain_v42/repositories/pg_ticket.py \
  tests/unit/mcp/test_ticket_tools.py \
  tests/unit/repositories/test_pg_ticket.py
git commit -m "fix(tickets): expose complete paginated backlog"
```

- [ ] **Step 5: Obtenir deux verdicts frais sur le HEAD final**

Lancer `red-reviewer` et `red-tester` sur le même SHA. Corriger chaque constat actionnable en RED–GREEN–REFACTOR, committer, puis relancer les deux rôles sur le nouveau SHA jusqu'à deux verdicts positifs frais.
