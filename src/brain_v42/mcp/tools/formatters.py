"""LLM-first markdown formatters for brain-v42 MCP tool responses.

Converts Pydantic models into structured markdown optimized for LLM consumption.
Generalizes the session_start markdown pattern to all tools.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Never, cast
from uuid import UUID

from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from brain_v42.models.adr import ADR
from brain_v42.models.brain import KnowledgeByType, SearchResult
from brain_v42.models.decision import Decision
from brain_v42.models.indexed_plan_chunk import IndexedPlanChunk
from brain_v42.models.learning import Learning
from brain_v42.models.project_context import ProjectContext
from brain_v42.models.runbook import Runbook
from brain_v42.models.snippet import Snippet

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def format_id(uuid_val: str | UUID) -> str:
    """Render an entity identifier for an LLM reader: the FULL UUID.

    It was called `short_id` until 2026-08-11, and had long since stopped
    truncating anything — its docstring said the opposite of its name, right
    below it. Some forty sites called it believing they were shortening.

    Why full: an LLM chains tool calls with the identifier it was just handed
    (`brain_get`, `brain_ticket_transition`…). A truncated identifier would force
    a resolution round trip, or worse, invent the ending.

    Why this function still exists when all it does is `str()`: it is the ONLY
    point where the identifier rendering policy is decided. Inlining it across
    the ~40 call sites would dissolve the decision where nobody would know it had
    been taken — and the day we wanted to go back to truncation, there would be
    no place left to do it. The name now states the ROLE (format), not today's
    result: if the policy changes, it will not become a lie again.
    """
    return str(uuid_val)


def short_date(dt: datetime) -> str:
    """ISO date YYYY-MM-DD."""
    return dt.strftime("%Y-%m-%d")


def format_confirmation(
    action: str,
    title: str,  # noqa: ARG001 — kept for call-site compat; not echoed back.
    *,
    id: str | None = None,
    project: str | None = None,  # noqa: ARG001 — same reason as title.
    **extra: object,
) -> str:
    """Compact write-confirmation for MCP tools.

    The LLM already holds ``title`` and ``project`` in its context (it just
    passed them as arguments), so echoing them burns tokens for no gain.
    Only ``id`` (server-generated) and extra kwargs (ADR number, step
    count, …) are surfaced. ``✓`` is dropped in favour of ``ok`` — same
    semantic, cheaper tokenization.
    """
    parts: list[str] = []
    if id:
        parts.append(f"id:{format_id(id)}")
    for k, v in extra.items():
        parts.append(f"{k}:{v}")
    if not parts:
        return f"ok {action}"
    return f"ok {action} ({', '.join(parts)})"


def format_error(message: str) -> Never:
    """Surface an unprefixed expected business failure through the MCP error channel."""
    raise ToolError(message)


# ---------------------------------------------------------------------------
# Private item formatters
# ---------------------------------------------------------------------------


def _format_learning_item(lr: Learning, index: int) -> str:
    """Format a single learning for list display."""
    badge = f" [{lr.confidence}]" if lr.confidence else ""
    validated = ""
    if lr.validated_at:
        validated = f" Validated: {short_date(lr.validated_at)}"
    header = (
        f"{index}. **{lr.topic}**{badge}{validated} "
        f"({short_date(lr.created_at)}, id:{format_id(lr.id)})"
    )
    lines = [header, f"   {lr.insight}"]
    if lr.tags:
        lines.append(f"   Tags: {', '.join(lr.tags)}")
    return "\n".join(lines)


def _format_learning_summary(lr: Learning, index: int) -> str:
    """Token-bounded learning row for full-corpus pagination (REORG Part 1).

    Excludes the insight body, which dominates payload size. Surfaces the
    metadata REORG actually inspects: project_key, tags, access_count,
    freshness_status. Two lines per row; ~100 tokens vs ~500-1500 in full mode.
    """
    badge = f" [{lr.confidence}]" if lr.confidence else ""
    archived = " [archived]" if lr.freshness_status == "archived" else ""
    header = f"{index}. **{lr.topic}**{badge}{archived} (id:{format_id(lr.id)})"
    pk = lr.project_key or "—"
    tags = ", ".join(lr.tags) if lr.tags else "—"
    access = lr.access_count or 0
    return f"{header}\n   project:{pk} | tags: {tags} | access:{access}"


def _format_decision_item(d: Decision, index: int) -> str:
    """Format a single decision for list display."""
    header = (
        f"{index}. **{d.title}** [{d.status}] ({short_date(d.created_at)}, id:{format_id(d.id)})"
    )
    lines = [header, f"   {d.description}"]
    if d.reasoning:
        lines.append(f"   Reasoning: {d.reasoning}")
    if d.status == "superseded" and d.superseded_by:
        lines.append(f"   Superseded by: id:{format_id(d.superseded_by)}")
    if d.tags:
        lines.append(f"   Tags: {', '.join(d.tags)}")
    return "\n".join(lines)


def _format_decision_summary(d: Decision, index: int) -> str:
    """Token-bounded decision row for full-corpus pagination (REORG Part 1)."""
    archived = " [archived]" if d.freshness_status == "archived" else ""
    header = f"{index}. **{d.title}** [{d.status}]{archived} (id:{format_id(d.id)})"
    pk = d.project_key or "—"
    tags = ", ".join(d.tags) if d.tags else "—"
    access = d.access_count or 0
    return f"{header}\n   project:{pk} | tags: {tags} | access:{access}"


_SNIPPET_INTENTION_MAX = 120


def _format_snippet_item(s: Snippet, index: int) -> str:
    """Compact snippet line for list/search display.

    Verbose fields (code, gotchas, deps, usage_example, use_count, tags) are
    reserved for ``format_snippet_detail`` (brain_get) — they would inflate
    a 20-result search by 5-10k tokens with content the LLM rarely uses
    until it has identified the snippet of interest.
    """
    intention = s.intention or ""
    if len(intention) > _SNIPPET_INTENTION_MAX:
        intention = intention[: _SNIPPET_INTENTION_MAX - 1].rstrip() + "…"
    return f"{index}. **{s.title}** [{s.language}] (id:{format_id(s.id)}) — {intention}"


def _format_runbook_item(rb: Runbook, index: int) -> str:
    """Format a single runbook for list display."""
    header = f"{index}. **{rb.title}** ({short_date(rb.created_at)}, id:{format_id(rb.id)})"
    lines = [header, f"   Trigger: {rb.trigger}"]
    lines.append(f"   Steps: {len(rb.steps)}")
    if rb.tags:
        lines.append(f"   Tags: {', '.join(rb.tags)}")
    return "\n".join(lines)


def _format_adr_item(adr: ADR, index: int) -> str:
    """Format a single ADR for list display."""
    header = (
        f"{index}. **ADR #{adr.number}: {adr.title}** [{adr.status}] "
        f"({short_date(adr.created_at)}, id:{format_id(adr.id)})"
    )
    lines = [header, f"   {adr.decision}"]
    if adr.status == "superseded" and adr.superseded_by is not None:
        lines.append(f"   Superseded by: ADR #{adr.superseded_by}")
    if adr.tags:
        lines.append(f"   Tags: {', '.join(adr.tags)}")
    return "\n".join(lines)


def _format_plan_chunk_item(chunk: IndexedPlanChunk, index: int) -> str:
    """Format a single indexed plan chunk for list display.

    The rendered ``id:`` is the **parent plan UUID**, not the chunk's own
    primary key — only the parent id is resolvable via
    ``brain_get(entity_type='plan', entity_id=...)``. Chunks are not
    addressable individually through MCP, so leaking ``chunk.id`` would
    invite the LLM to call ``brain_get`` with an id that always 404s.
    """
    header = (
        f"{index}. **{chunk.section_title}** [{chunk.plan_type}] "
        f"({short_date(chunk.created_at)}, id:{format_id(chunk.plan_id)})"
    )
    lines = [header, f"   Path: {chunk.section_path}"]
    if chunk.tags:
        lines.append(f"   Tags: {', '.join(chunk.tags)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entity formatters
# ---------------------------------------------------------------------------


def format_learnings(
    learnings: list[Learning],
    query: str | None = None,
    summary_only: bool = False,
) -> str:
    """Format learning list as markdown.

    summary_only=True drops the insight body and surfaces the REORG-relevant
    metadata (project_key, tags, access_count, freshness_status) instead.
    """
    n = len(learnings)
    s = "s" if n != 1 else ""
    header = f"## {n} learning{s} found"
    if query:
        header += f' for "{query}"'

    if n == 0:
        hint = ""
        if query:
            hint = f'\n\n\u2192 Try brain_search("{query}") for cross-type results'
        return header + hint

    item_fn = _format_learning_summary if summary_only else _format_learning_item
    items = [item_fn(lr, i) for i, lr in enumerate(learnings, 1)]
    return header + "\n\n" + "\n\n".join(items)


def format_decisions(
    decisions: list[Decision],
    query: str | None = None,
    summary_only: bool = False,
) -> str:
    """Format decision list as markdown.

    summary_only=True drops description/reasoning and surfaces project_key,
    tags, access_count, freshness_status instead \u2014 used by REORG Part 1
    pagination to avoid token-budget overflow on full-corpus scans.
    """
    n = len(decisions)
    s = "s" if n != 1 else ""
    if query:
        header = f'## {n} decision{s} matching "{query}"'
    else:
        header = f"## {n} decision{s} found"

    if n == 0:
        hint = ""
        if query:
            hint = f'\n\n\u2192 Try brain_search("{query}") for cross-type results'
        return header + hint

    item_fn = _format_decision_summary if summary_only else _format_decision_item
    items = [item_fn(d, i) for i, d in enumerate(decisions, 1)]
    return header + "\n\n" + "\n\n".join(items)


def format_snippets(snippets: list[Snippet], query: str | None = None) -> str:
    """Format snippet list as markdown."""
    n = len(snippets)
    s = "s" if n != 1 else ""
    if query:
        header = f'## {n} snippet{s} matching "{query}"'
    else:
        header = f"## {n} snippet{s} found"

    if n == 0:
        hint = ""
        if query:
            hint = f'\n\n\u2192 Try brain_search("{query}") for cross-type results'
        return header + hint

    items = [_format_snippet_item(sn, i) for i, sn in enumerate(snippets, 1)]
    return header + "\n\n" + "\n\n".join(items)


def format_runbook(rb: Runbook) -> str:
    """Format a single runbook in detail."""
    lines = [f"## Runbook: {rb.title} (id:{format_id(rb.id)})"]
    lines.append("")
    lines.append(f"**Trigger**: {rb.trigger}")
    if rb.prerequisites:
        lines.append(f"**Prerequisites**: {', '.join(rb.prerequisites)}")
    if rb.estimated_duration:
        lines.append(f"**Estimated duration**: {rb.estimated_duration}")
    lines.append("")
    lines.append("### Steps")
    for step in rb.steps:
        cmd = f": `{step.command}`" if step.command else ""
        lines.append(f"{step.order}. **{step.title}**{cmd}")
        if step.description:
            lines.append(f"   {step.description}")
    if rb.rollback_steps:
        lines.append("")
        lines.append("### Rollback")
        for step in rb.rollback_steps:
            cmd = f": `{step.command}`" if step.command else ""
            lines.append(f"{step.order}. **{step.title}**{cmd}")
    if rb.last_executed_at:
        status = rb.last_execution_status or "unknown"
        lines.append("")
        lines.append(
            f"Last executed: {short_date(rb.last_executed_at)} "
            f"({status}, {rb.execution_count} times total)"
        )
    return "\n".join(lines)


def format_runbooks(runbooks: list[Runbook], project_key: str | None = None) -> str:
    """Format runbook list as markdown."""
    n = len(runbooks)
    s = "s" if n != 1 else ""
    if project_key:
        header = f'## {n} runbook{s} for project "{project_key}"'
    else:
        header = f"## {n} runbook{s} found"
    if n == 0:
        return header
    items = [_format_runbook_item(rb, i) for i, rb in enumerate(runbooks, 1)]
    return header + "\n\n" + "\n\n".join(items)


def format_adrs(adrs: list[ADR], project_key: str | None = None) -> str:
    """Format ADR list as markdown."""
    n = len(adrs)
    s = "s" if n != 1 else ""
    if project_key:
        header = f'## {n} ADR{s} for project "{project_key}"'
    else:
        header = f"## {n} ADR{s} found"
    if n == 0:
        return header
    items = [_format_adr_item(adr, i) for i, adr in enumerate(adrs, 1)]
    return header + "\n\n" + "\n\n".join(items)


def format_project_context(ctx: ProjectContext) -> str:
    """Format project context as markdown."""
    lines = [f"## Project: {ctx.project_key} ({ctx.name})", ""]
    if ctx.current_phase:
        lines.append(f"- **Phase**: {ctx.current_phase}")
    if ctx.current_focus:
        lines.append(f"- **Focus**: {ctx.current_focus}")
    if ctx.languages:
        lines.append(f"- **Languages**: {', '.join(ctx.languages)}")
    if ctx.frameworks:
        lines.append(f"- **Frameworks**: {', '.join(ctx.frameworks)}")
    if ctx.databases:
        lines.append(f"- **Databases**: {', '.join(ctx.databases)}")
    blockers = ", ".join(ctx.blockers) if ctx.blockers else "none"
    lines.append(f"- **Blockers**: {blockers}")
    if ctx.related_projects:
        lines.append(f"- **Related projects**: {', '.join(ctx.related_projects)}")
    stats = (
        f"{ctx.decisions_count} decisions, {ctx.learnings_count} learnings, "
        f"{ctx.snippets_count} snippets, {ctx.runbooks_count} runbooks, "
        f"{ctx.adrs_count} ADRs"
    )
    lines.append(f"- **Stats**: {stats}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Detail formatters (brain_get)
# ---------------------------------------------------------------------------


def format_decision_detail(d: Decision) -> str:
    """Format a single decision in full detail (for brain_get)."""
    lines = [f"## Decision: {d.title} (id:{format_id(d.id)})"]
    lines.append(f"**Status**: {d.status}")
    lines.append(f"**Created**: {short_date(d.created_at)}")
    if d.project_key:
        lines.append(f"**Project**: {d.project_key}")
    lines.append("")
    lines.append(f"**Description**: {d.description}")
    if d.reasoning:
        lines.append(f"**Reasoning**: {d.reasoning}")
    if d.alternatives:
        lines.append(f"**Alternatives**: {', '.join(d.alternatives)}")
    if d.consequences:
        lines.append(f"**Consequences**: {d.consequences}")
    if d.status == "superseded" and d.superseded_by:
        lines.append(f"**Superseded by**: id:{format_id(d.superseded_by)}")
    if d.tags:
        lines.append(f"**Tags**: {', '.join(d.tags)}")
    if d.access_count:
        lines.append(f"**Access count**: {d.access_count}")
    if d.freshness_status and d.freshness_status != "fresh":
        lines.append(f"**Freshness**: {d.freshness_status}")
    return "\n".join(lines)


def format_learning_detail(lr: Learning) -> str:
    """Format a single learning in full detail (for brain_get)."""
    badge = f" [{lr.confidence}]" if lr.confidence else ""
    lines = [f"## Learning: {lr.topic}{badge} (id:{format_id(lr.id)})"]
    lines.append(f"**Created**: {short_date(lr.created_at)}")
    if lr.project_key:
        lines.append(f"**Project**: {lr.project_key}")
    if lr.source:
        lines.append(f"**Source**: {lr.source} ({lr.source_type})")
    if lr.validated_at:
        lines.append(f"**Validated**: {short_date(lr.validated_at)}")
    lines.append("")
    lines.append(lr.insight)
    if lr.tags:
        lines.append(f"\n**Tags**: {', '.join(lr.tags)}")
    if lr.access_count:
        lines.append(f"**Access count**: {lr.access_count}")
    if lr.freshness_status and lr.freshness_status != "fresh":
        lines.append(f"**Freshness**: {lr.freshness_status}")
    return "\n".join(lines)


def format_snippet_detail(s: Snippet) -> str:
    """Format a single snippet with full code (for brain_get)."""
    lines = [f"## Snippet: {s.title} [{s.language}] (id:{format_id(s.id)})"]
    lines.append(f"**Intention**: {s.intention}")
    if s.project_key:
        lines.append(f"**Project**: {s.project_key}")
    if s.dependencies:
        lines.append(f"**Dependencies**: {', '.join(s.dependencies)}")
    lines.append("")
    lines.append(f"```{s.language}")
    lines.append(s.code)
    lines.append("```")
    if s.usage_example:
        lines.append(f"\n**Example**: `{s.usage_example}`")
    if s.gotchas:
        lines.append(f"**Gotchas**: {s.gotchas}")
    if s.use_count and s.use_count > 0:
        last = f" (last: {short_date(s.last_used_at)})" if s.last_used_at else ""
        lines.append(f"**Used**: {s.use_count} times{last}")
    if s.tags:
        lines.append(f"**Tags**: {', '.join(s.tags)}")
    return "\n".join(lines)


def format_adr_detail(adr: ADR) -> str:
    """Format a single ADR in full detail (for brain_get)."""
    lines = [f"## ADR #{adr.number}: {adr.title} [{adr.status}] (id:{format_id(adr.id)})"]
    lines.append(f"**Project**: {adr.project_key}")
    lines.append(f"**Created**: {short_date(adr.created_at)}")
    if adr.decided_at:
        lines.append(f"**Decided**: {short_date(adr.decided_at)}")
    lines.append("")
    lines.append(f"**Context**: {adr.context}")
    lines.append(f"**Decision**: {adr.decision}")
    lines.append(f"**Consequences**: {adr.consequences}")
    if adr.alternatives_considered:
        lines.append("\n**Alternatives considered**:")
        for alt in adr.alternatives_considered:
            title = alt.title if hasattr(alt, "title") else str(alt)
            lines.append(f"- {title}")
    if adr.tags:
        lines.append(f"\n**Tags**: {', '.join(adr.tags)}")
    return "\n".join(lines)


def format_projects_list(contexts: list[ProjectContext]) -> str:
    """Format project list summary (for brain_list_projects)."""
    n = len(contexts)
    s = "s" if n != 1 else ""
    header = f"## {n} project{s}"
    if n == 0:
        return header
    items: list[str] = []
    for ctx in contexts:
        focus = f" — {ctx.current_focus}" if ctx.current_focus else ""
        phase = f" [{ctx.current_phase}]" if ctx.current_phase else ""
        group = f" (group: {ctx.project_group})" if ctx.project_group else ""
        items.append(f"- **{ctx.project_key}** ({ctx.name}){phase}{group}{focus}")
    return header + "\n\n" + "\n".join(items)


def format_supersession_chain(chain: list[Decision]) -> str:
    """Format a decision supersession chain as timeline."""
    if not chain:
        return "No supersession chain found."
    n = len(chain)
    header = f"## Supersession chain ({n} decision{'s' if n != 1 else ''})"
    items: list[str] = []
    for i, d in enumerate(chain):
        arrow = " -->" if i < n - 1 else " (current)"
        items.append(
            f"{i + 1}. **{d.title}** [{d.status}] "
            f"({short_date(d.created_at)}, id:{format_id(d.id)}){arrow}"
        )
    return header + "\n\n" + "\n".join(items)


# ---------------------------------------------------------------------------
# Graph-related formatters
# ---------------------------------------------------------------------------


def format_graph_path(path: list[dict]) -> str:
    """Render a graph path as a single line.

    Input: list of dicts from GraphService.get_path — each node has
        {id, type, label}, and all but the last also have rel_to_next.

    Output: '[Type] Label --REL--> [Type] Label --REL--> [Type] Label'
    """
    if not path:
        return "(empty path)"
    parts: list[str] = []
    for i, node in enumerate(path):
        label = node.get("label") or "untitled"
        parts.append(f"[{node['type']}] {label}")
        if "rel_to_next" in node and i < len(path) - 1:
            parts.append(f"--{node['rel_to_next']}-->")
    return " ".join(parts)


def format_related_section(related: list[dict]) -> str:
    """Format a graph 'Related' section appended to search results.

    Args:
        related: List of neighbor dicts from graph.get_related_ids(), each with
            keys: ``id``, ``type``, ``rel``, ``title``.

    Returns:
        Markdown string with a ``### Related`` section, or empty string if no items.
    """
    if not related:
        return ""
    lines = ["\n### Related"]
    for item in related:
        rel = item.get("rel", "RELATED_TO")
        title = item.get("title", "untitled")
        ntype = item.get("type", "unknown")
        nid = str(item.get("id", "?"))
        lines.append(f'- {rel}: "{title}" ({ntype}, id:{nid})')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Search formatters
# ---------------------------------------------------------------------------

_MODEL_MAP: dict[str, type[BaseModel]] = {
    "learning": Learning,
    "decision": Decision,
    "snippet": Snippet,
    "runbook": Runbook,
    "adr": ADR,
    "plan": IndexedPlanChunk,
}

_ITEM_FORMATTER_MAP: dict[str, Any] = {
    "learning": _format_learning_item,
    "decision": _format_decision_item,
    "snippet": _format_snippet_item,
    "runbook": _format_runbook_item,
    "adr": _format_adr_item,
    "plan": _format_plan_chunk_item,
}

_SEARCH_SUMMARY_FORMATTER_MAP: dict[str, Any] = {
    "learning": _format_learning_summary,
    "decision": _format_decision_summary,
}

_TYPE_LABELS = {
    "learning": "Learnings",
    "decision": "Decisions",
    "snippet": "Snippets",
    "runbook": "Runbooks",
    "adr": "ADRs",
    "plan": "Plans",
}


def _format_search_item(
    type_key: str,
    model: BaseModel,
    index: int,
    *,
    full: bool,
) -> str:
    """Render one search hit with bounded default decision/learning bodies."""
    if not full and type_key in _SEARCH_SUMMARY_FORMATTER_MAP:
        summary_formatter = _SEARCH_SUMMARY_FORMATTER_MAP[type_key]
        return cast(str, summary_formatter(model, index))

    item = cast(str, _ITEM_FORMATTER_MAP[type_key](model, index))
    if full and type_key == "decision":
        decision = cast(Decision, model)
        if decision.consequences:
            item += f"\n   Consequences: {decision.consequences}"
    return item


def format_search_results(
    results: list[SearchResult],
    query: str,
    degraded: dict[str, Any] | None = None,
    full: bool = False,
) -> str:
    """Format cross-type search results grouped by type.

    Within each type section, items are sorted by ``score`` descending and
    each is prefixed with ``[s:0.XX]`` so the LLM can rank/filter without
    issuing a second tool call.

    Args:
        results: Search results from BrainService.search().
        query: The original query string.
        degraded: Optional dict from SearchResponse.degraded. When set, a
            visible warning banner is prepended so the LLM knows results ran
            in degraded mode (rrf_fallback or fts_fallback).
        full: When True, include complete decision and learning bodies.
            The default renders their existing compact summaries instead.
    """
    # Build degraded banner (Fix 1 + Fix 2 + MINOR 2: rrf_only)
    banner_lines: list[str] = []
    if degraded:
        rerank_mode = degraded.get("rerank_mode")
        search_mode = degraded.get("search_mode")
        if search_mode == "fts_fallback":
            banner_lines.append(
                "degraded: embedding service indisponible — résultats FTS uniquement (ordre textuel)"
            )
        elif rerank_mode == "rrf_fallback":
            banner_lines.append(
                "degraded: reranker indisponible — ordre RRF (pas de re-scoring cross-encoder)"
            )
        elif rerank_mode == "rrf_only":
            banner_lines.append(
                "note: reranker non configuré — ordre RRF (pas de re-scoring cross-encoder)"
            )

    n = len(results)
    header = f'## {n} result{"s" if n != 1 else ""} for "{query}" (across all types)'

    if not results:
        if banner_lines:
            return "\n".join(banner_lines) + "\n" + header
        return header

    grouped: dict[str, list[SearchResult]] = defaultdict(list)
    for r in results:
        grouped[r.type].append(r)

    sections: list[str] = []
    for type_key in ["decision", "learning", "snippet", "runbook", "adr", "plan"]:
        items = grouped.get(type_key, [])
        if not items:
            continue
        items = sorted(items, key=lambda sr: sr.score, reverse=True)
        label = _TYPE_LABELS[type_key]
        model_cls = _MODEL_MAP[type_key]
        section_lines = [f"### {label} ({len(items)})"]
        for i, sr in enumerate(items, 1):
            model = model_cls.model_validate(sr.item)
            item = _format_search_item(type_key, model, i, full=full)
            section_lines.append(f"[s:{sr.score:.2f}] {item}")
        sections.append("\n".join(section_lines))

    body = header + "\n\n" + "\n\n".join(sections)
    if banner_lines:
        return "\n".join(banner_lines) + "\n" + body
    return body


def format_knowledge_by_type(
    by_type: KnowledgeByType,
    topic: str,
    degraded: dict[str, Any] | None = None,
    full: bool = False,
) -> str:
    """Format grouped knowledge results (brain_what_do_i_know_about / group_by_type=True).

    Args:
        by_type: Results grouped by knowledge type.
        topic: The original topic/query string.
        degraded: Optional dict from WhatDoIKnowResponse.degraded. When set, a
            visible warning banner is prepended so the LLM knows results ran
            in degraded mode. Same semantics as format_search_results:
            - rerank_mode='rrf_fallback': reranker was down, scores are rank-based
            - rerank_mode='rrf_only': no reranker configured, scores are RRF-based
            - search_mode='fts_fallback': embedding service was down, FTS only
        full: When True, include complete decision and learning bodies.
            The default renders their existing compact summaries instead.
    """
    # Build degraded banner — mirrors format_search_results
    banner_lines: list[str] = []
    if degraded:
        rerank_mode = degraded.get("rerank_mode")
        search_mode = degraded.get("search_mode")
        if search_mode == "fts_fallback":
            banner_lines.append(
                "degraded: embedding service indisponible — résultats FTS uniquement (ordre textuel)"
            )
        elif rerank_mode == "rrf_fallback":
            banner_lines.append(
                "degraded: reranker indisponible — ordre RRF (pas de re-scoring cross-encoder)"
            )
        elif rerank_mode == "rrf_only":
            banner_lines.append(
                "note: reranker non configuré — ordre RRF (pas de re-scoring cross-encoder)"
            )

    total = sum(
        len(getattr(by_type, attr, []) or [])
        for attr in ["decisions", "learnings", "snippets", "runbooks", "adrs", "plans"]
    )
    header = f'## Everything known about "{topic}" ({total} items)'
    if total == 0:
        if banner_lines:
            return "\n".join(banner_lines) + "\n" + header
        return header

    sections: list[str] = []
    for type_key, attr_name in [
        ("decision", "decisions"),
        ("learning", "learnings"),
        ("snippet", "snippets"),
        ("runbook", "runbooks"),
        ("adr", "adrs"),
        ("plan", "plans"),
    ]:
        items = getattr(by_type, attr_name, []) or []
        if not items:
            continue
        label = _TYPE_LABELS[type_key]
        model_cls = _MODEL_MAP[type_key]
        section_lines = [f"### {label} ({len(items)})"]
        for i, sr in enumerate(items, 1):
            model = model_cls.model_validate(sr.item)
            section_lines.append(_format_search_item(type_key, model, i, full=full))
        sections.append("\n".join(section_lines))

    body = header + "\n\n" + "\n\n".join(sections)
    if banner_lines:
        return "\n".join(banner_lines) + "\n" + body
    return body


# ---------------------------------------------------------------------------
# Special formatters (decay, consolidation, roadmap)
# ---------------------------------------------------------------------------


def format_decay_status(data: dict) -> str:
    """Compact decay-status summary.

    One line per entity type with only non-zero buckets emitted. Saves
    ~200 tokens on sparse corpora compared to the old markdown table.
    Layout::

        ## Decay status
        decision: 15f 3s 2a 1d
        learning: 20f 5s 1a
        snippet: 8f
    """
    stats = data.get("stats") or {}
    deletions = data.get("deletion_candidates") or {}
    lines = ["## Decay status"]
    for type_key in ("decision", "learning", "snippet", "runbook", "adr", "plan"):
        s = stats.get(type_key) or {}
        buckets: list[str] = []
        for count, suffix in (
            (s.get("fresh", 0), "f"),
            (s.get("stale", 0), "s"),
            (s.get("archived", 0), "a"),
            (deletions.get(type_key, 0), "d"),
        ):
            if count:
                buckets.append(f"{count}{suffix}")
        if buckets:
            lines.append(f"{type_key}: {' '.join(buckets)}")
    if len(lines) == 1:
        lines.append("all 0")
    return "\n".join(lines)


def format_consolidation_candidates(candidates: list[dict]) -> str:
    """Format consolidation candidates as markdown."""
    n = len(candidates)
    s = "s" if n != 1 else ""
    header = f"## {n} consolidation candidate{s}"
    if n == 0:
        return header
    items: list[str] = []
    for i, c in enumerate(candidates, 1):
        sim = c.get("similarity", 0)
        etype = c.get("entity_type", "unknown")
        items.append(f"{i}. **{etype}** similarity:{sim:.2f}")
        t_a = c.get("title_a", c.get("id_a", "?"))
        t_b = c.get("title_b", c.get("id_b", "?"))
        id_a = format_id(c.get("id_a", "")) if c.get("id_a") else ""
        id_b = format_id(c.get("id_b", "")) if c.get("id_b") else ""
        items.append(f'   - "{t_a}" (id:{id_a})')
        items.append(f'   - "{t_b}" (id:{id_b})')
    return header + "\n\n" + "\n".join(items)


def _format_artifact_summary(artifact_count: dict[str, int]) -> str:
    """Build a compact artifact summary string (non-zero counts only)."""
    labels = {
        "decision": "dec",
        "learning": "learn",
        "snippet": "snip",
        "runbook": "rb",
        "adr": "adr",
        "plan": "plan",
        "gitlab_event": "gl",
    }
    parts: list[str] = []
    for key, abbrev in labels.items():
        count = artifact_count.get(key, 0)
        if count:
            parts.append(f"{count}{abbrev}")
    return " ".join(parts) if parts else "\u2014"


LIST_LIMIT_MAX = 100
"""Server cap for the list tools, applied to bound the token budget."""


def clamp_list_limit(limit: int, maximum: int = LIST_LIMIT_MAX) -> tuple[int, str]:
    """Clamp *limit* into [1, *maximum*] and return the notice that SAYS SO.

    A cap applied silently makes the result lie: the caller who asks for 500 and
    receives 100 rows cannot tell "there were only 100" from "there were 500".
    The rule already exists in this file — see ``_format_plan_detail``, "no
    content is silently dropped" — these paths were not following it.

    The notice is EMPTY in the nominal case: announcing it on every call would
    teach the reader to stop reading it, which amounts to announcing nothing.

    Returns ``(effective_value, notice)``.
    """
    if limit > maximum:
        return maximum, (
            f"\n… (limit {limit} demandé, plafonné à {maximum} — utiliser offset pour la suite)"
        )
    if limit < 1:
        return 1, f"\n… (limit {limit} demandé, remonté à 1 — une page vide ne dit rien)"
    return limit, ""


_ROADMAP_MAX_FEATURES_PER_PROJECT = 20
_ROADMAP_MAX_PROJECTS = 10


def _project_last_activity(project: dict) -> str:
    """Sort key over ``str | datetime | None`` \u2014 NULL sorts last, never first.

    The tool path serialises to ISO strings; fixtures and some callers pass a
    ``datetime`` or ``None``. A missing timestamp means "never measured", so it must
    not win the recency cut it has no evidence for.
    """
    activity = project.get("last_activity")
    if isinstance(activity, datetime):
        return activity.isoformat()
    if isinstance(activity, str):
        return activity
    return ""


_OPEN_FEATURE_STATUSES = ("building", "design", "planned", "research")
"""Statuses that describe REMAINING work.

``done``, ``deployed`` and ``archived`` are excluded: they do not contradict a
phase announcing completion, they support it. Counting them would drown the line
on any old project, where history always dominates work in progress.
"""


def _open_work_counts(features: list[dict]) -> str:
    """Count of features still open, ordered from most to least committed.

    Returns an EMPTY string when nothing is in progress: a "0 in progress" line
    on every finished project would teach the reader to skip it, and so to miss
    it the day it contradicts the declared phase.
    """
    tally = dict.fromkeys(_OPEN_FEATURE_STATUSES, 0)
    for feature in features:
        status = feature.get("status")
        if status in tally:
            tally[status] += 1
    return " · ".join(f"{count} {status}" for status, count in tally.items() if count)


def format_roadmap(
    projects: list[dict],
    max_features_per_project: int = _ROADMAP_MAX_FEATURES_PER_PROJECT,
    max_projects: int = _ROADMAP_MAX_PROJECTS,
) -> str:
    """Format roadmap as markdown with tables per project.

    Two caps, because two different things grow. When a project has more than
    *max_features_per_project* features its table is capped; when there are more
    than *max_projects* projects, only the most recently active ones are rendered.
    Both notices name what was dropped so the caller can go and fetch it.

    The project cut is by RECENCY, not by input order: callers hand projects over
    in ``ORDER BY project_key``, so slicing the head would keep whatever sorts
    alphabetically first and silently drop active work. Measured on production,
    a naive slice dropped 14 projects touched within 30 days \u2014 one of them the
    previous day \u2014 while keeping three that had been idle for months.

    Escape hatches: ``brain_get_roadmap(project_key=\u2026)`` for every feature of one
    project, ``full=True`` for every project.
    """
    lines = ["## Roadmap"]
    cap = max(1, max_features_per_project)
    project_cap = max(1, max_projects)
    ordered = sorted(projects, key=_project_last_activity, reverse=True)
    rendered_projects = ordered[:project_cap]
    omitted_projects = [p["project_key"] for p in ordered[project_cap:]]
    for p in rendered_projects:
        phase = p.get("current_phase", "")
        phase_str = f" ({phase})" if phase else ""
        lines.append("")
        lines.append(f"### {p['project_key']}{phase_str}")
        open_counts = _open_work_counts(p.get("features", []))
        if open_counts:
            # The declared phase is free text: nothing links it to this table,
            # so nothing contradicts it as it ages. We do not judge the prose, we
            # put the measurement beside it — the same gesture as the briefing's
            # "Technical state (measured)" block, for the same reason.
            lines.append(f"_en cours : {open_counts}_")
        lines.append("| Feature | Status | Artifacts | Last activity |")
        lines.append("|---------|--------|-----------|---------------|")
        all_features = p.get("features", [])
        rendered_features = all_features[:cap]
        omitted_features = len(all_features) - len(rendered_features)
        for f in rendered_features:
            activity = f.get("last_activity")
            if activity is None:
                activity = "\u2014"
            elif isinstance(activity, datetime):
                activity = short_date(activity)
            elif isinstance(activity, str) and len(activity) > 10:
                activity = activity[:10]
            status = f["status"]
            if f.get("pinned"):
                status += " [pinned]"
            artifacts = _format_artifact_summary(f.get("artifact_count", {}))
            lines.append(f"| {f['name']} | {status} | {artifacts} | {activity} |")
        if omitted_features > 0:
            lines.append(
                f"\n\u2026 ({omitted_features} feature(s) omitted \u2014 use "
                f"brain_get_roadmap(project_key='{p['project_key']}') for the full list)"
            )
    if omitted_projects:
        named = ", ".join(sorted(omitted_projects))
        lines.append(
            f"\n\u2026 ({len(omitted_projects)} project(s) omitted, least recently active "
            f"first \u2014 use brain_get_roadmap(full=True) for every project, or "
            f"brain_get_roadmap(project_key='\u2026') for one of: {named})"
        )
    return "\n".join(lines)
