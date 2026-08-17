"""Failure-first tests for scoped Dream project request authorization."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

import pytest
from fastmcp.exceptions import AuthorizationError
from structlog.testing import capture_logs

from brain_v42.mcp import dream_project_authorization as authorization
from brain_v42.mcp.dream_capabilities import DREAM_PHASE_TOOL_ALLOWLISTS
from brain_v42.mcp.dream_project_authorization import (
    PROJECT_TOOL_POLICIES,
    DreamObjectReference,
    DreamProjectAudit,
    DreamProjectAuthorizationError,
    DreamProjectScope,
    PostgresDreamProjectResolver,
    authorize_dream_project_request,
    bind_dream_project_scope,
    get_dream_project_scope,
)

PROJECT_KEY = "sec1b-project"
AUDIT = DreamProjectAudit(principal="dream-codex-synth", phase="synth")
DENIAL_REASONS = (
    "invalid_project_claim",
    "policy_missing",
    "resolver_missing",
    "project_argument_mismatch",
    "project_group_forbidden",
    "dream_run_forbidden",
    "ownership_field_forbidden",
    "invalid_reference",
    "object_not_authorized",
    "resolver_failure",
)


def test_legacy_authorization_module_reexports_canonical_identities() -> None:
    from brain_v42.dream_project_errors import (
        DreamProjectAuthorizationError as canonical_error,
    )
    from brain_v42.services import dream_project_scope as canonical_scope

    assert DreamProjectAuthorizationError is canonical_error
    assert DreamProjectScope is canonical_scope.DreamProjectScope
    assert get_dream_project_scope is canonical_scope.get_dream_project_scope
    assert bind_dream_project_scope is canonical_scope.bind_dream_project_scope
    assert (
        authorization._CURRENT_DREAM_PROJECT_SCOPE is canonical_scope._CURRENT_DREAM_PROJECT_SCOPE
    )


class FakeResolver:
    def __init__(self, *, allowed: object = True, error: Exception | None = None) -> None:
        self.allowed = allowed
        self.error = error
        self.calls: list[tuple[str, tuple[DreamObjectReference, ...]]] = []

    async def references_belong_to_project(
        self,
        project_key: str,
        references: Sequence[DreamObjectReference],
    ) -> bool:
        self.calls.append((project_key, tuple(references)))
        if self.error is not None:
            raise self.error
        return self.allowed  # type: ignore[return-value]


class FakeMappingResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> FakeMappingResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class FakeSession:
    def __init__(self, result_rows: list[list[dict[str, Any]]]) -> None:
        self._result_rows = list(result_rows)
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> FakeMappingResult:
        self.statements.append(statement)
        return FakeMappingResult(self._result_rows.pop(0))


class FakeSessionContext:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> FakeSession:
        return self._session

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakeSessionFactory:
    def __init__(self, result_rows: list[list[dict[str, Any]]]) -> None:
        self.session = FakeSession(result_rows)

    def __call__(self) -> FakeSessionContext:
        return FakeSessionContext(self.session)


async def _authorize(
    tool_name: str,
    arguments: Mapping[str, Any] | None = None,
    *,
    resolver: FakeResolver | None = None,
    project_key: str = PROJECT_KEY,
):
    return await authorize_dream_project_request(
        tool_name=tool_name,
        arguments=arguments,
        project_key=project_key,
        resolver=resolver if resolver is not None else FakeResolver(),
        audit=AUDIT,
    )


def test_project_policy_is_exhaustive_for_current_dream_catalog() -> None:
    allowed_tools = {
        tool_name
        for phase_tools in DREAM_PHASE_TOOL_ALLOWLISTS.values()
        for tool_name in phase_tools
    }

    assert set(PROJECT_TOOL_POLICIES) == allowed_tools
    assert len(PROJECT_TOOL_POLICIES) == 19


def test_production_policy_does_not_import_phase_capabilities() -> None:
    assert "dream_capabilities" not in inspect.getsource(authorization)


def test_policy_mapping_is_immutable() -> None:
    with pytest.raises(TypeError):
        PROJECT_TOOL_POLICIES["new-tool"] = object()  # type: ignore[index,assignment]


@pytest.mark.parametrize(
    "tool_name",
    [
        "brain_list",
        "brain_search",
        "brain_learn",
        "brain_save_snippet",
        "brain_propose_adr",
        "brain_create_runbook",
        "brain_list_adrs",
    ],
)
@pytest.mark.asyncio
async def test_project_key_is_injected_only_into_existing_public_binding(tool_name: str) -> None:
    original = {"content": "unchanged"}

    result = await _authorize(tool_name, original)

    assert result.arguments == {"content": "unchanged", "project_key": PROJECT_KEY}
    assert original == {"content": "unchanged"}


@pytest.mark.asyncio
async def test_equal_canonical_project_is_accepted() -> None:
    result = await _authorize("brain_search", {"project_key": PROJECT_KEY})
    assert result.arguments["project_key"] == PROJECT_KEY


@pytest.mark.parametrize("forged", ["foreign-project", "sec1b_project", " brain-v42 ", 42])
@pytest.mark.asyncio
async def test_mismatched_or_noncanonical_project_is_denied(forged: object) -> None:
    with pytest.raises(DreamProjectAuthorizationError) as caught:
        await _authorize("brain_search", {"project_key": forged})
    assert caught.value.reason == "project_argument_mismatch"


@pytest.mark.asyncio
async def test_authorized_arguments_are_deep_copied_before_injection() -> None:
    relation_id = str(uuid4())
    original = {
        "topic": "deep-copy",
        "tags": ["before"],
        "related_to": [{"id": relation_id, "metadata": {"level": ["before"]}}],
    }

    result = await _authorize("brain_learn", original)
    original["tags"].append("mutated")
    original["related_to"][0]["id"] = str(uuid4())
    original["related_to"][0]["metadata"]["level"].append("mutated")

    assert result.arguments["tags"] == ["before"]
    assert result.arguments["related_to"] == [
        {"id": relation_id, "metadata": {"level": ["before"]}}
    ]
    assert result.arguments["tags"] is not original["tags"]
    assert result.arguments["related_to"] is not original["related_to"]


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_types"),
    [
        (
            "brain_get",
            lambda a, _b: {"entity_type": "decision", "entity_id": str(a)},
            ("decision",),
        ),
        ("brain_get", lambda a, _b: {"entity_type": "plan", "entity_id": str(a)}, ("plan",)),
        ("brain_delete", lambda a, _b: {"entity_type": "adr", "entity_id": str(a)}, ("adr",)),
        (
            "brain_merge_entities",
            lambda a, b: {"entity_type": "snippet", "source_id": str(a), "target_id": str(b)},
            ("snippet", "snippet"),
        ),
        ("brain_assign_domain", lambda a, _b: {"entity_id": str(a)}, (None,)),
        ("brain_get_neighbors", lambda a, _b: {"entity_id": str(a)}, (None,)),
        (
            "brain_graph_path",
            lambda a, b: {"source_id": str(a), "target_id": str(b)},
            (None, None),
        ),
        (
            "brain_propose_adr",
            lambda a, _b: {"source_learning_id": str(a)},
            ("learning",),
        ),
        (
            "brain_create_runbook",
            lambda a, _b: {"source_learning_id": str(a)},
            ("learning",),
        ),
        (
            "brain_learn",
            lambda a, b: {"related_to": [{"id": str(a)}, {"id": str(b)}]},
            (None, None),
        ),
        (
            "brain_save_snippet",
            lambda a, b: {"related_to": [{"id": str(a)}, {"id": str(b)}]},
            (None, None),
        ),
        (
            "brain_update",
            lambda a, b: {
                "entity_type": "runbook",
                "entity_id": str(a),
                "fields": {"title": "safe"},
                "related_to": [{"id": str(b)}],
            },
            ("runbook", None),
        ),
    ],
)
@pytest.mark.asyncio
async def test_typed_generic_nested_merge_path_and_update_references_are_extracted(
    tool_name: str,
    arguments,
    expected_types: tuple[str | None, ...],
) -> None:
    first, second = uuid4(), uuid4()
    resolver = FakeResolver()

    await _authorize(tool_name, arguments(first, second), resolver=resolver)

    assert resolver.calls == [
        (
            PROJECT_KEY,
            tuple(
                DreamObjectReference(entity_id=entity_id, entity_type=entity_type)
                for entity_id, entity_type in zip((first, second), expected_types, strict=False)
            ),
        )
    ]


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("brain_get", {"entity_type": "decision", "entity_id": "12345678"}),
        ("brain_get", {"entity_type": "unknown", "entity_id": str(uuid4())}),
        ("brain_graph_path", {"source_id": "not-a-uuid", "target_id": str(uuid4())}),
        ("brain_learn", {"related_to": {"id": "not-a-list"}}),
        ("brain_save_snippet", {"related_to": [{}]}),
        ("brain_update", {"entity_type": "learning", "entity_id": str(uuid4()), "fields": []}),
    ],
)
@pytest.mark.asyncio
async def test_malformed_or_partial_references_fail_closed(
    tool_name: str,
    arguments: Mapping[str, Any],
) -> None:
    with pytest.raises(DreamProjectAuthorizationError) as caught:
        await _authorize(tool_name, arguments)
    assert caught.value.reason == "invalid_reference"


@pytest.mark.asyncio
async def test_non_null_project_group_is_denied() -> None:
    with pytest.raises(DreamProjectAuthorizationError) as caught:
        await _authorize("brain_search", {"project_group": "red-triad"})
    assert caught.value.reason == "project_group_forbidden"


@pytest.mark.parametrize("tool_name", ["brain_propose_adr", "brain_create_runbook"])
@pytest.mark.asyncio
async def test_non_null_dream_run_is_denied(tool_name: str) -> None:
    with pytest.raises(DreamProjectAuthorizationError) as caught:
        await _authorize(tool_name, {"dream_run_id": 12})
    assert caught.value.reason == "dream_run_forbidden"


@pytest.mark.parametrize(
    "field_name",
    [
        "project_key",
        "project_group",
        "project_keys",
        "owner_project_key",
        "dream_run_id",
        "superseded_by",
    ],
)
@pytest.mark.asyncio
async def test_update_rejects_ownership_fields(field_name: str) -> None:
    with pytest.raises(DreamProjectAuthorizationError) as caught:
        await _authorize(
            "brain_update",
            {
                "entity_type": "learning",
                "entity_id": str(uuid4()),
                "fields": {field_name: "forged"},
            },
        )
    assert caught.value.reason == "ownership_field_forbidden"


@pytest.mark.asyncio
async def test_false_resolver_result_is_non_enumerating_denial() -> None:
    with pytest.raises(DreamProjectAuthorizationError) as caught:
        await _authorize(
            "brain_get",
            {"entity_type": "decision", "entity_id": str(uuid4())},
            resolver=FakeResolver(allowed=False),
        )
    assert caught.value.reason == "object_not_authorized"
    assert str(caught.value) == "Dream project authorization denied"


@pytest.mark.asyncio
async def test_missing_policy_and_resolver_fail_closed() -> None:
    with pytest.raises(DreamProjectAuthorizationError) as missing_policy:
        await _authorize("unknown-tool", {})
    with pytest.raises(DreamProjectAuthorizationError) as missing_resolver:
        await authorize_dream_project_request(
            tool_name="brain_list",
            arguments={},
            project_key=PROJECT_KEY,
            resolver=None,
            audit=AUDIT,
        )
    assert missing_policy.value.reason == "policy_missing"
    assert missing_resolver.value.reason == "resolver_missing"


@pytest.mark.parametrize("truthy_result", [1, "yes", object()])
@pytest.mark.asyncio
async def test_resolver_allows_only_the_literal_true_singleton(truthy_result: object) -> None:
    with pytest.raises(DreamProjectAuthorizationError) as caught:
        await _authorize(
            "brain_get",
            {"entity_type": "decision", "entity_id": str(uuid4())},
            resolver=FakeResolver(allowed=truthy_result),
        )
    assert caught.value.reason == "object_not_authorized"


@pytest.mark.parametrize("reason", DENIAL_REASONS)
def test_every_denial_reason_has_one_bounded_non_enumerating_audit(reason: str) -> None:
    with capture_logs() as logs:
        with pytest.raises(DreamProjectAuthorizationError) as caught:
            authorization._deny(  # type: ignore[attr-defined]
                reason=reason,
                audit=AUDIT,
                project_key=PROJECT_KEY,
                tool_name="brain_get",
            )

    assert str(caught.value) == "Dream project authorization denied"
    assert logs == [
        {
            "event": "dream_project.authorization_denied",
            "principal": "dream-codex-synth",
            "phase": "synth",
            "project_key": PROJECT_KEY,
            "requested_tool": "brain_get",
            "reason": reason,
            "log_level": "warning",
        }
    ]


@pytest.mark.asyncio
async def test_resolver_exception_never_leaks_exception_arguments_ids_or_bearer() -> None:
    entity_id = uuid4()
    arguments = {
        "entity_type": "decision",
        "entity_id": str(entity_id),
        "content": "argument-super-secret",
        "bearer": "profile-super-secret",
    }
    resolver = FakeResolver(error=RuntimeError("SELECT profile-super-secret traceback"))

    with capture_logs() as logs:
        with pytest.raises(AuthorizationError) as caught:
            await _authorize("brain_get", arguments, resolver=resolver)

    assert isinstance(caught.value, DreamProjectAuthorizationError)
    assert caught.value.reason == "resolver_failure"
    rendered = f"{logs!r} {caught.value!s} {caught.value!r}"
    for secret in ("argument-super-secret", "profile-super-secret", str(entity_id), "SELECT"):
        assert secret not in rendered
    assert logs[0]["reason"] == "resolver_failure"


@pytest.mark.asyncio
async def test_unknown_tool_name_is_redacted_from_audit() -> None:
    with capture_logs() as logs:
        with pytest.raises(DreamProjectAuthorizationError):
            await _authorize("evil\nBearer secret\x00", {"content": "argument-secret"})

    assert logs[0]["requested_tool"] == "<redacted>"
    assert "secret" not in repr(logs)


def test_scope_binding_resets_after_success_exception_and_cancellation() -> None:
    scope = DreamProjectScope(PROJECT_KEY, FakeResolver(), AUDIT, "brain_get")
    assert get_dream_project_scope() is None

    with bind_dream_project_scope(scope):
        assert get_dream_project_scope() is scope
    assert get_dream_project_scope() is None

    with pytest.raises(RuntimeError):
        with bind_dream_project_scope(scope):
            raise RuntimeError("handler failed")
    assert get_dream_project_scope() is None

    with pytest.raises(asyncio.CancelledError):
        with bind_dream_project_scope(scope):
            raise asyncio.CancelledError
    assert get_dream_project_scope() is None


@pytest.mark.asyncio
async def test_scope_revalidates_one_typed_id_and_batches_returned_ids() -> None:
    resolver = FakeResolver()
    scope = DreamProjectScope(PROJECT_KEY, resolver, AUDIT, "brain_graph_path")
    typed_id, first, second = uuid4(), uuid4(), uuid4()

    await scope.revalidate_id(typed_id, entity_type="learning")
    await scope.revalidate_ids([first, second])

    assert resolver.calls == [
        (PROJECT_KEY, (DreamObjectReference(typed_id, "learning"),)),
        (
            PROJECT_KEY,
            (DreamObjectReference(first), DreamObjectReference(second)),
        ),
    ]


@pytest.mark.parametrize(
    ("entity_type", "table_name"),
    [
        ("decision", "decisions"),
        ("learning", "learnings"),
        ("snippet", "snippets"),
        ("runbook", "runbooks"),
        ("adr", "adrs"),
        ("plan", "indexed_plans"),
    ],
)
@pytest.mark.asyncio
async def test_postgres_resolver_checks_the_exact_typed_table(
    entity_type: str,
    table_name: str,
) -> None:
    entity_id = uuid4()
    factory = FakeSessionFactory([[{"id": entity_id, "project_key": PROJECT_KEY}]])
    resolver = PostgresDreamProjectResolver(factory)  # type: ignore[arg-type]

    allowed = await resolver.references_belong_to_project(
        PROJECT_KEY,
        [DreamObjectReference(entity_id, entity_type)],  # type: ignore[arg-type]
    )

    assert allowed is True
    assert len(factory.session.statements) == 1
    assert table_name in str(factory.session.statements[0])


@pytest.mark.parametrize(
    "rows",
    [
        [],
        lambda entity_id: [{"id": entity_id, "project_key": "foreign-project"}],
        lambda entity_id: [{"id": entity_id, "project_key": None}],
    ],
)
@pytest.mark.asyncio
async def test_postgres_typed_missing_foreign_and_null_project_fail_closed(rows) -> None:
    entity_id = uuid4()
    resolved_rows = rows(entity_id) if callable(rows) else rows
    factory = FakeSessionFactory([resolved_rows])
    resolver = PostgresDreamProjectResolver(factory)  # type: ignore[arg-type]

    allowed = await resolver.references_belong_to_project(
        PROJECT_KEY,
        [DreamObjectReference(entity_id, "decision")],
    )

    assert allowed is False


@pytest.mark.asyncio
async def test_postgres_generic_reference_searches_exactly_five_graph_tables() -> None:
    entity_id = uuid4()
    factory = FakeSessionFactory([[{"entity_id": entity_id, "project_key": PROJECT_KEY}]])
    resolver = PostgresDreamProjectResolver(factory)  # type: ignore[arg-type]

    allowed = await resolver.references_belong_to_project(
        PROJECT_KEY,
        [DreamObjectReference(entity_id)],
    )

    assert allowed is True
    assert len(factory.session.statements) == 1
    rendered = str(factory.session.statements[0])
    for table_name in ("decisions", "learnings", "snippets", "runbooks", "adrs"):
        assert table_name in rendered
    assert "indexed_plans" not in rendered


@pytest.mark.parametrize(
    "rows_factory",
    [
        lambda _entity_id: [],
        lambda entity_id: [{"entity_id": entity_id, "project_key": "foreign-project"}],
        lambda entity_id: [{"entity_id": entity_id, "project_key": None}],
        lambda entity_id: [
            {"entity_id": entity_id, "project_key": PROJECT_KEY},
            {"entity_id": entity_id, "project_key": PROJECT_KEY},
        ],
    ],
)
@pytest.mark.asyncio
async def test_postgres_generic_missing_foreign_null_and_ambiguous_fail_closed(
    rows_factory,
) -> None:
    entity_id = uuid4()
    factory = FakeSessionFactory([rows_factory(entity_id)])
    resolver = PostgresDreamProjectResolver(factory)  # type: ignore[arg-type]

    allowed = await resolver.references_belong_to_project(
        PROJECT_KEY,
        [DreamObjectReference(entity_id)],
    )

    assert allowed is False
