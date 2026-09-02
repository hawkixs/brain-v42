"""Session management tools for brain-v42 MCP — action-forward briefing."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, date, datetime
from typing import Any

import structlog

from brain_v42.config import get_settings
from brain_v42.mcp.tools.formatters import format_id
from brain_v42.mcp.tools.session_lifecycle_tools import (
    NEXT_FOCUS_MAX_LENGTH,
    register_session_lifecycle_tools,
)
from brain_v42.mcp.tools.workflow_guide_tools import format_workflow_guidance_briefing
from brain_v42.models.project_key import canonicalize_project_key
from brain_v42.services.dream_run_service import (
    KillswitchState,
    LastFailureRow,
)

logger = structlog.get_logger(__name__)

_CAP = 5
_TICKETS_CAP = 5


def _section_tickets(groups: Any | None) -> str:
    """### Tickets — the actionable ones at the top of the briefing (tickets spec §5).

    Shows only the actionable: to handle (I am the recipient) and to confirm (my
    resolved/wontfix requests). Capped at _TICKETS_CAP in total.

    awaiting_requester_confirmation (spec 2026-08-03 §2.3) is never listed — no
    legal transition on our side — but its count appears in the header as soon
    as it is non-empty, and it alone can trigger rendering the section when the
    other two are empty.
    """
    if groups is None:
        return ""
    a_traiter = list(groups.a_traiter)
    a_confirmer = list(groups.a_confirmer)
    awaiting = list(groups.awaiting_requester_confirmation)
    if not a_traiter and not a_confirmer and not awaiting:
        return ""
    header = f"### Tickets ({len(a_traiter)} à traiter · {len(a_confirmer)} à confirmer"
    if awaiting:
        header += f" · {len(awaiting)} livrés à valider"
    header += ")"
    lines = [header]
    budget = _TICKETS_CAP
    shown_traiter = a_traiter[:budget]
    for t in shown_traiter:
        age = max(0, (datetime.now(UTC) - t.created_at).days)
        suffix = "— à ack" if t.kind.value == "fyi" else f"({t.status.value} · {age}j)"
        lines.append(
            f"⬅️ #{format_id(str(t.id))} [{t.kind.value}] de {t.from_project} : "
            f"« {t.title} » {suffix}"
        )
    budget -= len(shown_traiter)
    shown_confirmer = a_confirmer[:budget]
    for t in shown_confirmer:
        lines.append(
            f"➡️ #{format_id(str(t.id))} vers {t.to_project} : « {t.title} » — "
            f"{t.status.value}, vérifie et confirme"
        )
    # Silenced count (ticket 259cfbe5, AC2): the cap must say how many
    # tickets it hides, not just point at brain_ticket_list. awaiting_
    # requester_confirmation is never silenced-by-cap — it is never
    # listed at all, by design (spec §2.3) — so it is excluded here.
    silenced = (len(a_traiter) - len(shown_traiter)) + (len(a_confirmer) - len(shown_confirmer))
    if silenced > 0:
        noun = "ticket tu" if silenced == 1 else "tickets tus"
        lines.append(f"→ {silenced} {noun} par le cap (brain_ticket_list pour le reste)")
        # The title convention ("REVUE 2026-09-03 — …") becomes operative
        # BEYOND the cap (ticket 259cfbe5). Measured on 2026-08-29: the only
        # dated deadline in the book sat at rank 49/62, invisible — sorting by
        # recency punishes precisely the tickets nobody touches while their date
        # approaches. A DATE is falsifiable and expires by itself: reviewing it
        # (touching the ticket) lifts it back up the recency order and turns this
        # line off. No hand-placed rank, no column: the deadline migration
        # decision (batch C12) stays entirely open.
        silenced_tickets = [*a_traiter[len(shown_traiter) :], *a_confirmer[len(shown_confirmer) :]]
        for due, hidden in _dated(silenced_tickets)[:2]:
            gap = (due - datetime.now(UTC).date()).days
            when = f"dépassée de {-gap}j" if gap < 0 else f"dans {gap}j"
            lines.append(
                f"→ daté hors cap : #{format_id(str(hidden.id))} « {hidden.title} » ({due}, {when})"
            )
    return "\n".join(lines)


_TITLE_DATE = re.compile(r"20\d{2}-\d{2}-\d{2}")


def _dated(tickets: list[Any]) -> list[tuple[date, Any]]:
    """The tickets whose TITLE carries a valid ISO date, NEAREST TO TODAY first.

    Nearest to today, not oldest first. A deadline's claim on attention peaks on
    its date and decays in both directions, so the distance is absolute and a
    breach wins the tie against an upcoming date at equal distance.

    Ordering by the raw date instead put the STALEST breaches first, and they
    never move: measured on the production notebook on 2026-09-02, the two slots
    went to breaches 10 and 8 days old while the only deadline still ahead — one
    day out — was hidden. That is open question 2 of ticket 259cfbe5 ("must a
    passed deadline stay displayed indefinitely? Otherwise we recreate the
    permanent alarm one stops reading") reaching production. An ancient breach now
    loses its slot by arithmetic, so no timer and no hand-placed rank is needed.
    """
    today = datetime.now(UTC).date()
    found: list[tuple[date, Any]] = []
    for candidate in tickets:
        match = _TITLE_DATE.search(candidate.title)
        if match is None:
            continue
        try:
            found.append((date.fromisoformat(match.group(0)), candidate))
        except ValueError:
            continue
    found.sort(key=lambda pair: (abs((pair[0] - today).days), (pair[0] - today).days))
    return found


def _ds_relative(d: datetime | None) -> str:
    if d is None:
        return ""
    now = datetime.now(tz=UTC)
    days = (now - d).days
    if days <= 0:
        return "today"
    return f"{days}d ago"


def _section_header(ctx: Any | None) -> str:
    if ctx is None:
        return "## Session Briefing (no project context found)"
    return f"## {ctx.project_key} — Session Briefing"


def _section_killswitches(
    state: KillswitchState, *, graph_enabled: bool = False, unavailable: bool = False
) -> str:
    if unavailable:
        # Distinct anchor when killswitch_state() raised — silent "no activity"
        # would lie when the pipeline is actually running but the query crashed.
        return "### Killswitches (status unavailable — see logs)"
    if state.last_run_date is None:
        return "### Killswitches (no dream pipeline activity in 7d)"
    lines = [f"### Killswitches (as of {state.last_run_date.isoformat()})"]

    def _row(label: str, enabled: bool, dry: bool, streak: int) -> str:
        if not enabled:
            return f"- {label}: disabled"
        mode = "dry" if dry else "wet"
        streak_str = f" · {streak} clean DRY nights" if dry else ""
        return f"- {label}: enabled ({mode}{streak_str})"

    lines.append(
        _row("PROMOTE", state.promote_enabled, state.promote_dry, state.promote_clean_dry_nights)
    )
    lines.append(
        _row("REORG  ", state.reorg_enabled, state.reorg_dry, state.reorg_clean_dry_nights)
    )
    lines.append(
        _row("EXTRACT", state.extract_enabled, state.extract_dry, state.extract_clean_dry_nights)
    )
    lines.append(
        _row("ROADMAP", state.roadmap_enabled, state.roadmap_dry, state.roadmap_clean_dry_nights)
    )
    lines.append(
        _row("SWEEP  ", state.sweep_enabled, state.sweep_dry, state.sweep_clean_dry_nights)
    )
    # graph_enabled is resolved by the caller from the canonical config
    # (config.graph_enabled), not a divergent os.getenv default. This helper
    # stays pure — it must not trigger a Settings() load (which requires
    # POSTGRES_URL) so it remains testable without env. See learning 80b4e8a6.
    lines.append(f"- GRAPH:   {'enabled' if graph_enabled else 'disabled'}")
    return "\n".join(lines)


def _section_last_failure(failure: LastFailureRow | None) -> str:
    if failure is None:
        return ""
    err = (failure.error_message or "(no message)").splitlines()[0]
    rd_str = (
        failure.run_date.isoformat()
        if isinstance(failure.run_date, date)
        else str(failure.run_date)
    )
    return (
        f"### Last failure\n"
        f"{failure.phase} on {rd_str} — {err}\n"
        f"→ drill in: brain_get(decision, …) or journalctl -u brain-v42-dream"
    )


def _section_roadmap(items: list[Any]) -> str:
    """### Roadmap — features vivantes (spec 2026-07-04 §5, remplace In-flight)."""
    if not items:
        return ""
    lines = [f"### Roadmap ({min(len(items), _CAP)})"]
    for f in items[:_CAP]:
        if f.last_artifact_at is not None:
            days = max(0, (datetime.now(UTC) - f.last_artifact_at).days)
            suffix = f"{f.artifact_count} artifacts, dernier il y a {days}j"
        else:
            suffix = f"{f.artifact_count} artifact"
        lines.append(f"- {f.name} [{f.status}] — {suffix}")
    return "\n".join(lines)


def _section_stale_pinned(items: list[Any]) -> str:
    if not items:
        return ""
    lines = [f"### Stale-pinned ({min(len(items), _CAP)})"]
    for f in items[:_CAP]:
        rel = _ds_relative(f.updated_at)
        lines.append(f"- {f.name} — pinned, last access {rel}")
    return "\n".join(lines)


def _section_focus(ctx: Any | None) -> str:
    if ctx is None or not ctx.current_focus:
        return "### Focus\n(no focus set)"
    return f"### Focus\n{ctx.current_focus}"


def _section_blockers(blockers: list[str] | None) -> str:
    if not blockers:
        return ""
    capped = blockers[:_CAP]
    lines = [f"### Blockers ({len(capped)})"]
    for b in capped:
        lines.append(f"- {b}")
    return "\n".join(lines)


def _section_recap(decisions: list[Any], learnings: list[Any]) -> str:
    if not decisions and not learnings:
        return ""
    lines = ["### Recap"]
    for d in decisions[:3]:
        lines.append(f"- d: {d.title}")
    for lr in learnings[:3]:
        snip = (lr.insight[:60] + "…") if len(lr.insight) > 60 else lr.insight
        lines.append(f"- l: {lr.topic}: {snip}")
    return "\n".join(lines)


def _section_cross_project(block: Any | None) -> str:
    if block is None or not block.entries:
        return ""
    lines = [f"### Cross-project ({', '.join(block.domains)})"]
    for e in block.entries[:_CAP]:
        day = e.created_at.date().isoformat() if e.created_at else "?"
        lines.append(f"- [{e.project_key}] {e.entity_type} · {day} · {e.display}")
    return "\n".join(lines)


def _format_focus_age(written_at: datetime, now: datetime) -> str:
    """Render how long ago the focus prose was authored, coarsely.

    Coarse on purpose: the reader needs "stale enough to re-measure?", not a
    duration. Negative skew (a stamp from the future) renders as "à l'instant"
    rather than a negative age.
    """
    elapsed = now - written_at
    minutes = int(elapsed.total_seconds() // 60)
    if minutes < 1:
        return "à l'instant"
    if minutes < 60:
        return f"il y a {minutes}min"
    hours = minutes // 60
    if hours < 24:
        return f"il y a {hours}h"
    return f"il y a {hours // 24}j"


def _focus_margin_line(focus_length: int, focus_octets: int | None = None) -> str:
    """What is left before `brain_session_end` refuses to close.

    `next_focus` is MANDATORY and capped; it REPLACES `current_focus` when the
    compare-and-swap succeeds. The other writer of the same column,
    `brain_update_project_focus`, has no bound: the project can therefore reach a
    state the closure cannot rewrite. On 2026-08-22, revision 217 carried 12,157
    characters for sixteen hours, and its closure removed 3,635 of them — 30 % of
    the focus — in one write.

    Under the cap, the line is a number. At zero or negative margin it stops
    being one: that is the only moment anyone needs it, and a negative margin
    written bare would read as a counting detail. Both outcomes are therefore
    named — compress, and so lose text the author chose, with no diff and no
    trace, or be refused.
    """
    margin = NEXT_FOCUS_MAX_LENGTH - focus_length
    head = f"- Focus : {focus_length} / {NEXT_FOCUS_MAX_LENGTH} caractères"
    if margin > 0:
        # The "which of the two do we count" question, reopened on 2026-08-29:
        # the bound counts CHARACTERS, and the same focus was 9,977 characters
        # for 10,285 bytes — a byte bound would already be crossed. Both numbers
        # on the NOMINAL line only: the noisy branches stay pure, exactly where
        # the original decision argued for it.
        if focus_octets is not None:
            return f"{head} (marge {margin} ; {focus_octets} octets)"
        return f"{head} (marge {margin})"
    if margin == 0:
        return (
            f"{head} — MARGE NULLE : un caractère de plus et toute fermeture "
            "sera refusée tant que le focus n'aura pas été compressé"
        )
    return (
        f"{head} — DÉPASSÉ de {-margin} : une fermeture devra compresser le focus, "
        "et ce qui tombe ne laisse ni diff ni trace — sinon elle sera refusée"
    )


def _section_technical_state(
    revision: str | None,
    *,
    unavailable: bool = False,
    focus_updated_at: datetime | None = None,
    focus_tracked: bool = False,
    focus_length: int | None = None,
    focus_octets: int | None = None,
    now: datetime | None = None,
) -> str:
    """The ``### État technique (mesuré)`` briefing section.

    The heading stays French because it is RENDERED output, pinned by
    tests/fixtures/briefing_full.md and by four tests; only this prose is
    translated. Derived at briefing time, never stored.

    The focus is free text: `focus_revision` guards it against concurrent
    overwrite, never against becoming false. On 2026-08-04 both the focus and
    CLAUDE.md still asserted migration 037 while the database had been on 039
    for three days — nothing reconciles a written claim against the schema, so
    every session that copied the paragraph forward carried the lie.

    Measuring the revision here costs one query and puts it directly above the
    focus prose, so a contradiction is visible at start/resume.

    An unavailable read renders explicitly rather than vanishing: silence would
    send the reader back to the very narrative this section exists to contradict.
    A caller that does not supply the value at all (the legacy 7-arg shape) gets
    no section — "not requested" is distinct from "requested and failed", the
    same split the killswitch section already makes.

    The focus age answers the second half of the same question: not only "is
    the schema what the prose claims?" but "how old is the prose?".
    `updated_at` cannot answer it — it moves on any write to the row, counters
    included — which is why migration 040 added `focus_updated_at`, written
    only when the focus text really changes. NULL renders as "inconnu": 040
    backfills nothing, and showing an unmeasured focus as fresh would invent
    the very kind of fact this section exists to retire.
    """
    lines = []
    if unavailable:
        lines.append("- Schéma : indisponible")
    elif revision:
        lines.append(f"- Schéma : {revision}")
    if focus_tracked:
        age = (
            _format_focus_age(focus_updated_at, now or datetime.now(UTC))
            if focus_updated_at is not None
            else "inconnu (jamais horodaté)"
        )
        lines.append(f"- Focus écrit : {age}")
    if focus_length is not None:
        lines.append(_focus_margin_line(focus_length, focus_octets))
    if not lines:
        return ""
    return "\n".join(["### État technique (mesuré)", *lines])


def _section_drill_in_hint() -> str:
    return "→ More: brain_search · brain_get_roadmap · brain_list types=…"


def _format_session_briefing(
    ctx: Any | None,
    decisions: list[Any],
    learnings: list[Any],
    killswitches: KillswitchState,
    last_failure: LastFailureRow | None,
    roadmap_items: list[Any],
    stale_pinned: list[Any],
    *,
    graph_enabled: bool = False,
    killswitch_unavailable: bool = False,
    cross_block: Any | None = None,
    ticket_groups: Any | None = None,
    schema_revision: str | None = None,
    schema_unavailable: bool = False,
) -> str:
    blockers = list(getattr(ctx, "blockers", []) or []) if ctx else []
    sections = [
        _section_header(ctx),
        _section_killswitches(
            killswitches, graph_enabled=graph_enabled, unavailable=killswitch_unavailable
        ),
        _section_last_failure(last_failure),
        _section_tickets(ticket_groups),
        _section_roadmap(roadmap_items),
        _section_stale_pinned(stale_pinned),
        # Deliberately adjacent to the focus: the measured value sits directly
        # above the prose that may contradict it (ticket 87ac8b7a).
        _section_technical_state(
            schema_revision,
            unavailable=schema_unavailable,
            focus_updated_at=getattr(ctx, "focus_updated_at", None),
            focus_tracked=ctx is not None,
            focus_length=(len(ctx.current_focus) if ctx and ctx.current_focus else None),
            focus_octets=(
                len(ctx.current_focus.encode("utf-8")) if ctx and ctx.current_focus else None
            ),
        ),
        _section_focus(ctx),
        _section_blockers(blockers),
        _section_recap(decisions, learnings),
        _section_cross_project(cross_block),
        format_workflow_guidance_briefing(),
        _section_drill_in_hint(),
    ]
    return "\n\n".join(s for s in sections if s)


def register_session_tools(
    mcp: Any,
    project_context_svc: Any,
    decision_svc: Any,
    learning_svc: Any,
    dream_run_svc: Any,
    feature_svc: Any,
    brain_session_svc: Any,
    *,
    cross_project_svc: Any | None = None,
    ticket_svc: Any | None = None,
    schema_state_svc: Any | None = None,
) -> None:
    """Register explicit lifecycle tools with the action-forward briefing."""

    async def load_briefing(project_key: str) -> str:
        """Build the action-forward project briefing in ~500-800 tokens.

        Returns: killswitches → last failure → roadmap (features vivantes) →
        stale pinned → focus → blockers → recent decisions/learnings.

        Graceful degrade (spec §9): if any service call raises, the
        offending section is dropped and a structlog warning is emitted —
        the briefing still renders the rest.
        """
        project_key = canonicalize_project_key(project_key, strict=False)
        results = await asyncio.gather(
            project_context_svc.get_by_key(project_key),
            decision_svc.list_all(project_key=project_key, limit=3),
            learning_svc.list_all(project_key=project_key, limit=3),
            dream_run_svc.killswitch_state(),
            dream_run_svc.last_failure(within_days=7),
            feature_svc.roadmap_alive(project_key=project_key, limit=5),
            feature_svc.stale_pinned(project_key=project_key, stale_days=30, limit=5),
            return_exceptions=True,
        )
        # Track whether killswitch_state() itself crashed so the section can
        # render "unavailable" instead of misleading "no activity in 7d".
        killswitch_unavailable = isinstance(results[3], Exception)
        for r in results:
            if isinstance(r, Exception):
                logger.warning("brain_session_start_partial_failure", error=str(r))
        cleaned: list[Any] = [None if isinstance(r, Exception) else r for r in results]
        ctx, decisions, learnings, killswitches, last_failure, roadmap_items, stale_pinned = cleaned
        if killswitches is None:
            killswitches = KillswitchState(
                last_run_date=None,
                promote_enabled=False,
                promote_dry=False,
                reorg_enabled=False,
                reorg_dry=False,
                promote_clean_dry_nights=0,
                reorg_clean_dry_nights=0,
            )
        decisions = decisions or []
        learnings = learnings or []
        roadmap_items = roadmap_items or []
        stale_pinned = stale_pinned or []

        # Canonical graph state. Guarded so a config load failure degrades the
        # GRAPH row to "disabled" rather than crashing the whole briefing —
        # consistent with this tool's graceful-degrade contract (spec §9).
        try:
            graph_enabled = get_settings().graph_enabled
        except Exception as exc:
            logger.warning("brain_session_start_graph_enabled_unresolved", error=str(exc))
            graph_enabled = False

        # Cross-project section (Spec C MVP β) — env-gated, fully optional.
        # Any failure degrades to "section omitted" per the graceful-degrade
        # contract; the killswitch keeps this a zero-overhead no-op when off.
        cross_block = None
        if cross_project_svc is not None:
            try:
                if get_settings().brain_dream_cross_project_enabled:
                    cross_block = await cross_project_svc.fetch_block(project_key)
            except Exception as exc:
                logger.warning("brain_session_start_cross_project_failed", error=str(exc))

        # Tickets section — actionnable, guarded (spec tickets §5).
        # Any failure degrades to "section omitted" — same graceful-degrade
        # contract as the rest of the briefing.
        ticket_groups = None
        if ticket_svc is not None:
            try:
                ticket_groups = await ticket_svc.list_grouped(project_key)
            except Exception as exc:
                logger.warning("brain_session_start_tickets_failed", error=str(exc))

        # Technical state — measured, never carried forward (ticket 87ac8b7a).
        # Unlike the other optional sections, a failure does NOT degrade to
        # "section omitted": silence would send the reader back to the focus
        # prose, which is the stale claim this section exists to contradict.
        schema_revision: str | None = None
        schema_unavailable = False
        if schema_state_svc is not None:
            try:
                schema_revision = await schema_state_svc.current_revision()
                schema_unavailable = schema_revision is None
            except Exception as exc:
                logger.warning("brain_session_start_schema_state_failed", error=str(exc))
                schema_unavailable = True

        return _format_session_briefing(
            ctx,
            decisions,
            learnings,
            killswitches,
            last_failure,
            roadmap_items,
            stale_pinned,
            graph_enabled=graph_enabled,
            killswitch_unavailable=killswitch_unavailable,
            cross_block=cross_block,
            ticket_groups=ticket_groups,
            schema_revision=schema_revision,
            schema_unavailable=schema_unavailable,
        )

    register_session_lifecycle_tools(mcp, brain_session_svc, load_briefing)
