"""MCP tool: brain_reindex_plans — re-scan and index plan files."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from brain_v42.mcp.tools.tool_annotations import _DESTRUCTIVE_ANNOTATIONS
from brain_v42.models.project_key import canonicalize_project_key

if TYPE_CHECKING:
    from brain_v42.services.plan_indexer import PlanIndexer

logger = structlog.get_logger(__name__)

_MAX_REPORTED_FAILURES = 20
"""Plafond de la liste rendue — annoncé, jamais muet (voir la notice ci-dessous)."""


def register_plan_tools(mcp: Any, plan_indexer: PlanIndexer) -> None:
    """Register brain_reindex_plans on mcp."""

    @mcp.tool(version="1.1", annotations=_DESTRUCTIVE_ANNOTATIONS)
    async def brain_reindex_plans(project_key: str | None = None) -> str:
        """Re-scan and index plan files for a project or all projects.

        Compares file content hashes to skip unchanged files.
        Links new/updated plans to features via ClusterGuard.
        Consolidates pre-existing mirror-path duplicates (rows with the same
        content_hash) before scanning, so reindexing recovers from past
        duplicate-indexing bugs without manual intervention.

        Args:
            project_key: If provided, index only this project. If omitted, index all.
        """
        project_key = canonicalize_project_key(project_key, strict=False)
        logger.debug("mcp.brain_reindex_plans", project_key=project_key)

        deduped: dict[str, int] = {}
        if project_key:
            deleted = await plan_indexer.dedupe_plans(project_key)
            if deleted:
                deduped[project_key] = deleted
            results = await plan_indexer.index_project(project_key)
            if not results:
                return f"No plan_scan_paths configured for project '{project_key}'"
            results_dict = {project_key: results}
        else:
            results_dict = await plan_indexer.index_all_projects()
            for pk in results_dict:
                deleted = await plan_indexer.dedupe_plans(pk)
                if deleted:
                    deduped[pk] = deleted

        lines = ["## Plan Indexing Results\n"]
        failures: list[tuple[str, dict[str, str]]] = []
        for pk, stats in results_dict.items():
            chunks = stats.get("chunks_created", 0)
            errors = stats["errors"]
            line = (
                f"**{pk}**: {stats['indexed']} indexed, "
                f"{stats['skipped']} skipped, {stats['linked']} linked, "
                f"{chunks} chunks created, {errors} errors"
            )
            if pk in deduped:
                line += f", {deduped[pk]} duplicates removed"
            lines.append(line)
            failures.extend((pk, failure) for failure in stats.get("failures", []))

        # Nommer les fichiers, pas seulement les compter. Un compteur dit qu'il
        # manque quelque chose et jamais QUOI : pour répondre à « aucun plan
        # attendu n'est silencieusement absent ? » l'opérateur devait quitter le
        # tool et grepper le journal du serveur. Le type d'erreur remonte, la
        # valeur fautive jamais — même contrat que plan_indexer.file_error.
        if failures:
            lines.append(f"\n### Fichiers non indexés ({len(failures)})\n")
            for pk, failure in failures[:_MAX_REPORTED_FAILURES]:
                lines.append(f"- `{failure['file_path']}` [{pk}] — {failure['error_type']}")
            hidden = len(failures) - _MAX_REPORTED_FAILURES
            if hidden > 0:
                lines.append(
                    f"\n… ({hidden} autre(s) non affiché(s) — journal du serveur, "
                    f"événement `plan_indexer.file_error`)"
                )
        return "\n".join(lines)
