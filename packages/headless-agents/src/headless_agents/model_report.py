"""Merge the catalogue, the live lists, and ha's own use into one report (lot 6, §3.6).

Pure: :func:`build_model_report` never queries a provider and never opens a
file. It reads a :class:`~headless_agents.model_catalog.Catalogue` and a
mapping of :class:`~headless_agents.model_live.LiveList` that the caller
already obtained, plus the operator's declared roles and ``models.toml``
defaults, and reports where they agree and where they drift -- it never
rewrites the catalogue.

An unqueried or unavailable live list must never make a catalogue entry look
gone: ``catalogued_gone`` and ``live_uncatalogued`` are only ever reported
for a provider whose live list actually answered (``status == "available"``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import date

from .model_catalog import Catalogue, ModelEntry
from .model_live import LiveList
from .registry import PROVIDER_NAMES
from .roles import Role

DEFAULT_MAX_AGE_DAYS = 30


def _catalogue_dict(entry: ModelEntry) -> dict[str, object]:
    tasks: list[dict[str, object]] = []
    for kind, effort in entry.tasks:
        task: dict[str, object] = {"kind": kind}
        if effort is not None:
            task["effort"] = effort
        tasks.append(task)
    return {
        "purpose": entry.purpose,
        "tasks": tasks,
        "cost": {
            "kind": entry.cost_kind,
            "windows": list(entry.windows),
            "note": entry.cost_note,
        },
        "pitfalls": list(entry.pitfalls),
        "verified_at": entry.verified_at.isoformat(),
        "source": entry.source,
    }


def _by_provider(catalogue: Catalogue) -> dict[str, dict[str, ModelEntry]]:
    grouped: dict[str, dict[str, ModelEntry]] = defaultdict(dict)
    for entry in catalogue.entries:
        grouped[entry.provider][entry.model] = entry
    return grouped


def _used_by(
    roles: Mapping[str, Role], defaults: Mapping[str, str]
) -> dict[tuple[str, str], set[str]]:
    used: dict[tuple[str, str], set[str]] = defaultdict(set)
    for role in roles.values():
        for link in role.links:
            effective = link.model or role.model or defaults.get(link.provider, "")
            if effective:
                used[(link.provider, effective)].add(role.name)
    return used


def build_model_report(
    catalogue: Catalogue,
    live: Mapping[str, LiveList],
    roles: Mapping[str, Role],
    defaults: Mapping[str, str] | None = None,
    *,
    today: date,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> list[dict[str, object]]:
    defaults = defaults or {}
    by_provider = _by_provider(catalogue)
    used_by = _used_by(roles, defaults)

    rows: list[dict[str, object]] = []
    for provider in PROVIDER_NAMES:
        entries = by_provider.get(provider, {})
        live_list = live.get(provider, LiveList("catalogue-only", (), "not queried"))
        live_ids = set(live_list.models) if live_list.status == "available" else set()
        role_ids = {model for (name, model) in used_by if name == provider}
        default_id = defaults.get(provider) or None
        all_ids = set(entries) | live_ids | role_ids | ({default_id} if default_id else set())

        models: list[dict[str, object]] = []
        for model_id in sorted(all_ids):
            entry = entries.get(model_id)
            labels = set(used_by.get((provider, model_id), set()))
            if default_id == model_id:
                labels.add("models.toml")
            models.append(
                {
                    "id": model_id,
                    "catalogue": _catalogue_dict(entry) if entry is not None else None,
                    "live": (model_id in live_ids) if live_list.status == "available" else None,
                    "used_by": sorted(labels),
                }
            )

        drift: list[dict[str, object]] = []
        if live_list.status == "available":
            for model_id in sorted(live_ids - entries.keys()):
                drift.append(
                    {
                        "kind": "live_uncatalogued",
                        "model": model_id,
                        "detail": f"{provider}/{model_id} is offered live but not catalogued",
                    }
                )
            for model_id in sorted(entries.keys() - live_ids):
                drift.append(
                    {
                        "kind": "catalogued_gone",
                        "model": model_id,
                        "detail": (
                            f"{provider}/{model_id} is catalogued but no longer offered live"
                        ),
                    }
                )

        for role in roles.values():
            for link in role.links:
                if link.provider != provider:
                    continue
                effective = link.model or role.model or defaults.get(provider, "")
                if effective and effective not in entries:
                    drift.append(
                        {
                            "kind": "unknown_role_model",
                            "model": effective,
                            "detail": (
                                f"role {role.name!r} points at {provider}/{effective}, "
                                "which the catalogue does not know"
                            ),
                        }
                    )

        for model_id, entry in entries.items():
            if (today - entry.verified_at).days > max_age_days:
                drift.append(
                    {
                        "kind": "stale_verification",
                        "model": model_id,
                        "detail": (
                            f"{provider}/{model_id} verified {entry.verified_at.isoformat()}, "
                            f"older than {max_age_days} days"
                        ),
                    }
                )

        rows.append(
            {
                "provider": provider,
                "live_status": live_list.status,
                "live_detail": live_list.detail,
                "models": models,
                "drift": drift,
            }
        )
    return rows


__all__ = ["DEFAULT_MAX_AGE_DAYS", "build_model_report"]
