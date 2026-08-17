"""Fail-closed project authorization primitives for scoped Dream calls.

PostgreSQL is the ownership authority. The phase capability middleware composes
this independent policy so neither module needs to import the other.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Never, Protocol
from uuid import UUID

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import adrs, decisions, indexed_plans, learnings, runbooks, snippets
from brain_v42.dream_project_errors import DreamProjectAuthorizationError, DreamProjectDenialReason
from brain_v42.models.project_key import canonicalize_project_key

logger = structlog.get_logger(__name__)

type DreamEntityType = Literal["decision", "learning", "snippet", "runbook", "adr", "plan"]
_DREAM_PHASES = frozenset(("scan", "clean", "connect", "synth", "promote", "reorg"))
_GRAPH_ENTITY_TYPES = frozenset(("decision", "learning", "snippet", "runbook", "adr"))
_ALL_ENTITY_TYPES = _GRAPH_ENTITY_TYPES | {"plan"}
_OWNERSHIP_FIELDS = frozenset(
    (
        "project_key",
        "project_group",
        "project_keys",
        "owner_project_key",
        "dream_run_id",
        "superseded_by",
    )
)


@dataclass(frozen=True, slots=True)
class DreamTypedReferenceRule:
    """Describe typed UUID arguments in one public tool request."""

    id_arguments: tuple[str, ...]
    entity_type_argument: str | None = None
    fixed_entity_type: DreamEntityType | None = None
    allowed_entity_types: frozenset[str] = _ALL_ENTITY_TYPES
    optional: bool = False


@dataclass(frozen=True, slots=True)
class DreamProjectToolPolicy:
    """Immutable request-shape policy for one phase-exposed tool."""

    inject_project_key: bool = False
    typed_references: tuple[DreamTypedReferenceRule, ...] = ()
    generic_reference_arguments: tuple[str, ...] = ()
    nested_reference_arguments: tuple[str, ...] = ()
    forbid_dream_run_id: bool = False
    reject_update_ownership_fields: bool = False


_DYNAMIC_RESOURCE = DreamTypedReferenceRule(
    entity_type_argument="entity_type",
    id_arguments=("entity_id",),
)
_MERGE_RESOURCES = DreamTypedReferenceRule(
    entity_type_argument="entity_type",
    id_arguments=("source_id", "target_id"),
    allowed_entity_types=_GRAPH_ENTITY_TYPES,
)
_OPTIONAL_LEARNING_SOURCE = DreamTypedReferenceRule(
    fixed_entity_type="learning",
    id_arguments=("source_learning_id",),
    optional=True,
)

PROJECT_TOOL_POLICIES: Mapping[str, DreamProjectToolPolicy] = MappingProxyType(
    {
        "brain_decay_status": DreamProjectToolPolicy(),
        "brain_consolidation_candidates": DreamProjectToolPolicy(),
        "brain_list": DreamProjectToolPolicy(inject_project_key=True),
        "brain_search": DreamProjectToolPolicy(inject_project_key=True),
        "brain_get": DreamProjectToolPolicy(typed_references=(_DYNAMIC_RESOURCE,)),
        "brain_merge_entities": DreamProjectToolPolicy(typed_references=(_MERGE_RESOURCES,)),
        "brain_delete": DreamProjectToolPolicy(typed_references=(_DYNAMIC_RESOURCE,)),
        "brain_backfill_links_batch": DreamProjectToolPolicy(),
        "brain_list_orphans_for_classification": DreamProjectToolPolicy(),
        "brain_assign_domain": DreamProjectToolPolicy(generic_reference_arguments=("entity_id",)),
        "brain_get_clusters": DreamProjectToolPolicy(),
        "brain_learn": DreamProjectToolPolicy(
            inject_project_key=True,
            nested_reference_arguments=("related_to",),
        ),
        "brain_save_snippet": DreamProjectToolPolicy(
            inject_project_key=True,
            nested_reference_arguments=("related_to",),
        ),
        "brain_get_neighbors": DreamProjectToolPolicy(generic_reference_arguments=("entity_id",)),
        "brain_graph_path": DreamProjectToolPolicy(
            generic_reference_arguments=("source_id", "target_id")
        ),
        "brain_propose_adr": DreamProjectToolPolicy(
            inject_project_key=True,
            typed_references=(_OPTIONAL_LEARNING_SOURCE,),
            forbid_dream_run_id=True,
        ),
        "brain_create_runbook": DreamProjectToolPolicy(
            inject_project_key=True,
            typed_references=(_OPTIONAL_LEARNING_SOURCE,),
            forbid_dream_run_id=True,
        ),
        "brain_list_adrs": DreamProjectToolPolicy(inject_project_key=True),
        "brain_update": DreamProjectToolPolicy(
            typed_references=(_DYNAMIC_RESOURCE,),
            nested_reference_arguments=("related_to",),
            reject_update_ownership_fields=True,
        ),
    }
)


@dataclass(frozen=True, slots=True)
class DreamObjectReference:
    """Normalized full UUID and its optional authoritative table type."""

    entity_id: UUID
    entity_type: DreamEntityType | None = None


class DreamProjectReferenceResolver(Protocol):
    """Injected ownership resolver used before and during protected work."""

    async def references_belong_to_project(
        self,
        project_key: str,
        references: Sequence[DreamObjectReference],
    ) -> bool:
        """Return true only when every reference is uniquely project-owned."""
        ...


@dataclass(frozen=True, slots=True)
class DreamProjectAudit:
    """Bounded fields permitted in a project-authorization denial audit."""

    principal: str
    phase: str


def _safe_phase(phase: str) -> str:
    return phase if phase in _DREAM_PHASES else "unknown"


def _safe_principal(audit: DreamProjectAudit) -> str:
    phase = _safe_phase(audit.phase)
    expected = f"dream-codex-{phase}"
    return audit.principal if phase != "unknown" and audit.principal == expected else "<redacted>"


def _safe_tool_name(tool_name: str) -> str:
    return tool_name if tool_name in PROJECT_TOOL_POLICIES else "<redacted>"


def _deny(
    *,
    reason: DreamProjectDenialReason,
    audit: DreamProjectAudit,
    project_key: object,
    tool_name: str,
) -> Never:
    logger.warning(
        "dream_project.authorization_denied",
        principal=_safe_principal(audit),
        phase=_safe_phase(audit.phase),
        project_key=_canonical_project_key(project_key) or "unknown",
        requested_tool=_safe_tool_name(tool_name),
        reason=reason,
    )
    raise DreamProjectAuthorizationError(reason)


def _canonical_project_key(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        canonical = canonicalize_project_key(value)
    except ValueError:
        return None
    return value if canonical == value else None


def _parse_full_uuid(value: object) -> UUID | None:
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        return None
    return parsed if str(parsed) == value.lower() else None


def _extract_typed_references(
    arguments: Mapping[str, Any],
    rules: Sequence[DreamTypedReferenceRule],
) -> list[DreamObjectReference] | None:
    references: list[DreamObjectReference] = []
    for rule in rules:
        if rule.fixed_entity_type is not None:
            entity_type: str | None = rule.fixed_entity_type
        else:
            raw_type = arguments.get(rule.entity_type_argument or "")
            entity_type = raw_type if isinstance(raw_type, str) else None
        if entity_type not in rule.allowed_entity_types:
            return None
        for argument_name in rule.id_arguments:
            raw_id = arguments.get(argument_name)
            if raw_id is None and rule.optional:
                continue
            entity_id = _parse_full_uuid(raw_id)
            if entity_id is None:
                return None
            references.append(
                DreamObjectReference(
                    entity_id=entity_id,
                    entity_type=entity_type,  # type: ignore[arg-type]
                )
            )
    return references


def _extract_generic_references(
    arguments: Mapping[str, Any],
    argument_names: Sequence[str],
) -> list[DreamObjectReference] | None:
    references: list[DreamObjectReference] = []
    for argument_name in argument_names:
        entity_id = _parse_full_uuid(arguments.get(argument_name))
        if entity_id is None:
            return None
        references.append(DreamObjectReference(entity_id=entity_id))
    return references


def _extract_nested_references(
    arguments: Mapping[str, Any],
    argument_names: Sequence[str],
) -> list[DreamObjectReference] | None:
    references: list[DreamObjectReference] = []
    for argument_name in argument_names:
        raw_items = arguments.get(argument_name)
        if raw_items is None:
            continue
        if not isinstance(raw_items, list):
            return None
        for item in raw_items:
            if not isinstance(item, Mapping):
                return None
            entity_id = _parse_full_uuid(item.get("id"))
            if entity_id is None:
                return None
            references.append(DreamObjectReference(entity_id=entity_id))
    return references


@dataclass(frozen=True, slots=True)
class DreamProjectScope:
    """Request-local scope reused for point-of-use and returned-ID checks."""

    project_key: str
    resolver: DreamProjectReferenceResolver
    audit: DreamProjectAudit
    tool_name: str

    async def _require(self, references: Sequence[DreamObjectReference]) -> None:
        if not references:
            return
        try:
            allowed = await self.resolver.references_belong_to_project(
                self.project_key,
                references,
            )
        except Exception:  # noqa: BLE001 - never expose resolver exception material
            _deny(
                reason="resolver_failure",
                audit=self.audit,
                project_key=self.project_key,
                tool_name=self.tool_name,
            )
        if allowed is not True:
            _deny(
                reason="object_not_authorized",
                audit=self.audit,
                project_key=self.project_key,
                tool_name=self.tool_name,
            )

    async def revalidate_id(
        self,
        entity_id: UUID | str,
        *,
        entity_type: DreamEntityType | None = None,
    ) -> None:
        """Revalidate one typed or generic UUID immediately before use."""
        parsed = entity_id if isinstance(entity_id, UUID) else _parse_full_uuid(entity_id)
        if parsed is None or (entity_type is not None and entity_type not in _ALL_ENTITY_TYPES):
            _deny(
                reason="invalid_reference",
                audit=self.audit,
                project_key=self.project_key,
                tool_name=self.tool_name,
            )
        await self._require((DreamObjectReference(parsed, entity_type),))

    async def revalidate_ids(self, entity_ids: Sequence[UUID | str]) -> None:
        """Batch-check generic graph/result UUIDs against PostgreSQL ownership."""
        references: list[DreamObjectReference] = []
        seen: set[UUID] = set()
        for raw_id in entity_ids:
            parsed = raw_id if isinstance(raw_id, UUID) else _parse_full_uuid(raw_id)
            if parsed is None:
                _deny(
                    reason="invalid_reference",
                    audit=self.audit,
                    project_key=self.project_key,
                    tool_name=self.tool_name,
                )
            if parsed not in seen:
                references.append(DreamObjectReference(parsed))
                seen.add(parsed)
        await self._require(references)


_CURRENT_DREAM_PROJECT_SCOPE: ContextVar[DreamProjectScope | None] = ContextVar(
    "dream_project_scope",
    default=None,
)


def get_dream_project_scope() -> DreamProjectScope | None:
    """Return the current scoped request, or ``None`` for admin/STDIO calls."""
    return _CURRENT_DREAM_PROJECT_SCOPE.get()


@contextmanager
def bind_dream_project_scope(scope: DreamProjectScope) -> Iterator[DreamProjectScope]:
    """Bind and reliably reset scope across success, failure, or cancellation."""
    token = _CURRENT_DREAM_PROJECT_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _CURRENT_DREAM_PROJECT_SCOPE.reset(token)


@dataclass(frozen=True, slots=True)
class DreamProjectAuthorizationResult:
    """Deep-copied public arguments plus the internal request scope."""

    arguments: dict[str, Any]
    scope: DreamProjectScope


async def authorize_dream_project_request(
    *,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    project_key: str,
    resolver: DreamProjectReferenceResolver | None,
    audit: DreamProjectAudit,
) -> DreamProjectAuthorizationResult:
    """Deep-copy, inject, extract, and authorize one scoped Dream request."""
    canonical_project = _canonical_project_key(project_key)
    if canonical_project is None:
        _deny(
            reason="invalid_project_claim",
            audit=audit,
            project_key=project_key,
            tool_name=tool_name,
        )
    policy = PROJECT_TOOL_POLICIES.get(tool_name)
    if policy is None:
        _deny(
            reason="policy_missing",
            audit=audit,
            project_key=canonical_project,
            tool_name=tool_name,
        )
    if resolver is None:
        _deny(
            reason="resolver_missing",
            audit=audit,
            project_key=canonical_project,
            tool_name=tool_name,
        )
    if arguments is not None and not isinstance(arguments, Mapping):
        _deny(
            reason="invalid_reference",
            audit=audit,
            project_key=canonical_project,
            tool_name=tool_name,
        )

    copied_arguments = copy.deepcopy(dict(arguments or {}))
    if copied_arguments.get("project_group") is not None:
        _deny(
            reason="project_group_forbidden",
            audit=audit,
            project_key=canonical_project,
            tool_name=tool_name,
        )
    if policy.inject_project_key:
        supplied_project = copied_arguments.get("project_key")
        if supplied_project is None:
            copied_arguments["project_key"] = canonical_project
        elif (
            _canonical_project_key(supplied_project) is None
            or supplied_project != canonical_project
        ):
            _deny(
                reason="project_argument_mismatch",
                audit=audit,
                project_key=canonical_project,
                tool_name=tool_name,
            )
    if policy.forbid_dream_run_id and copied_arguments.get("dream_run_id") is not None:
        _deny(
            reason="dream_run_forbidden",
            audit=audit,
            project_key=canonical_project,
            tool_name=tool_name,
        )
    if policy.reject_update_ownership_fields:
        fields = copied_arguments.get("fields")
        if not isinstance(fields, Mapping):
            _deny(
                reason="invalid_reference",
                audit=audit,
                project_key=canonical_project,
                tool_name=tool_name,
            )
        if _OWNERSHIP_FIELDS.intersection(fields):
            _deny(
                reason="ownership_field_forbidden",
                audit=audit,
                project_key=canonical_project,
                tool_name=tool_name,
            )

    typed = _extract_typed_references(copied_arguments, policy.typed_references)
    generic = _extract_generic_references(copied_arguments, policy.generic_reference_arguments)
    nested = _extract_nested_references(copied_arguments, policy.nested_reference_arguments)
    if typed is None or generic is None or nested is None:
        _deny(
            reason="invalid_reference",
            audit=audit,
            project_key=canonical_project,
            tool_name=tool_name,
        )

    scope = DreamProjectScope(canonical_project, resolver, audit, tool_name)
    await scope._require((*typed, *generic, *nested))
    return DreamProjectAuthorizationResult(arguments=copied_arguments, scope=scope)


_SQL_RESOURCE_TABLES: Mapping[str, sa.Table] = MappingProxyType(
    {
        "decision": decisions,
        "learning": learnings,
        "snippet": snippets,
        "runbook": runbooks,
        "adr": adrs,
        "plan": indexed_plans,
    }
)
_SQL_GRAPH_TABLES = (decisions, learnings, snippets, runbooks, adrs)


class PostgresDreamProjectResolver:
    """Resolve exact typed and unique generic ownership from PostgreSQL."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def references_belong_to_project(
        self,
        project_key: str,
        references: Sequence[DreamObjectReference],
    ) -> bool:
        """Reject missing, foreign, null-project, or ambiguous UUIDs."""
        if _canonical_project_key(project_key) != project_key:
            return False
        if not references:
            return True

        typed: dict[str, set[UUID]] = defaultdict(set)
        generic: set[UUID] = set()
        for reference in references:
            if not isinstance(reference, DreamObjectReference):
                return False
            if reference.entity_type is None:
                generic.add(reference.entity_id)
            elif reference.entity_type in _SQL_RESOURCE_TABLES:
                typed[reference.entity_type].add(reference.entity_id)
            else:
                return False

        async with self._session_factory() as session:
            for entity_type, ids in typed.items():
                table = _SQL_RESOURCE_TABLES[entity_type]
                result = await session.execute(
                    sa.select(table.c.id, table.c.project_key).where(table.c.id.in_(ids))
                )
                rows = result.mappings().all()
                typed_matches = {row["id"]: row["project_key"] for row in rows}
                if len(typed_matches) != len(ids):
                    return False
                if any(typed_matches.get(entity_id) != project_key for entity_id in ids):
                    return False

            if generic:
                selects = [
                    sa.select(
                        table.c.id.label("entity_id"),
                        table.c.project_key.label("project_key"),
                    ).where(table.c.id.in_(generic))
                    for table in _SQL_GRAPH_TABLES
                ]
                result = await session.execute(sa.union_all(*selects))
                matches_by_id: dict[UUID, list[str | None]] = defaultdict(list)
                for row in result.mappings().all():
                    matches_by_id[row["entity_id"]].append(row["project_key"])
                for entity_id in generic:
                    if matches_by_id.get(entity_id, []) != [project_key]:
                        return False

        return True
