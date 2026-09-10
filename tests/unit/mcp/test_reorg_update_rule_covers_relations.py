"""A phase bounded to `fields` may not reshape the graph through `related_to`.

`DreamUpdateFieldRule` says it "names the only fields a phase may touch". That
sentence was accurate about `fields` and silent about the other mutation the
same `brain_update` call can carry: `related_to` writes typed graph edges
(MOTIVATED_BY, IMPLEMENTS, ...) and is validated somewhere else entirely, by
`_extract_nested_references`, which checks that the targets belong to the
project and nothing more.

So REORG — a phase whose allowlist was tightened to `tags` and
`freshness_status` precisely so it could not rewrite content — could still
attach causal edges to any entity of its project, and `fields: {}` is accepted,
so it needed no field mutation at all to do it. Measured on 2026-09-10:
`phase_reorg.md` never mentions `related_to`, and no test covered the case.

The rule is not that relations are dangerous. It is that a phase whose written
bound is a field allowlist should not have a second, unbounded write channel
sitting beside it — an operator reading `allowed_fields=("tags",
"freshness_status")` concludes REORG cannot do anything else, and that
conclusion should be true.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from brain_v42.mcp.dream_project_authorization import (
    DreamProjectAudit,
    DreamProjectAuthorizationError,
    authorize_dream_project_request,
)

PROJECT_KEY = "sec1b-project"
REORG_AUDIT = DreamProjectAudit(principal="dream-codex-reorg", phase="reorg")
SYNTH_AUDIT = DreamProjectAudit(principal="dream-codex-synth", phase="synth")


class FakeResolver:
    """Accepts every reference: the point is the phase bound, not ownership."""

    async def references_belong_to_project(self, project_key: str, references: Any) -> bool:
        return True


async def _authorize(
    arguments: dict[str, Any],
    *,
    audit: DreamProjectAudit = REORG_AUDIT,
) -> Any:
    return await authorize_dream_project_request(
        tool_name="brain_update",
        arguments=arguments,
        project_key=PROJECT_KEY,
        resolver=FakeResolver(),
        audit=audit,
    )


def _update(**extra: Any) -> dict[str, Any]:
    return {
        "entity_type": "learning",
        "entity_id": str(uuid4()),
        "fields": {},
        **extra,
    }


@pytest.mark.asyncio
async def test_reorg_may_not_attach_relations_through_an_empty_field_update() -> None:
    """The exact call shape that makes the gap reachable with no field mutation."""
    with pytest.raises(DreamProjectAuthorizationError) as caught:
        await _authorize(
            _update(related_to=[{"id": str(uuid4()), "type": "MOTIVATED_BY"}]),
        )

    assert caught.value.reason == "field_not_allowed_for_phase", (
        "REORG carried a graph write past a guard whose allowlist names only "
        "`tags` and `freshness_status`."
    )


@pytest.mark.asyncio
async def test_reorg_may_not_attach_relations_alongside_a_permitted_field() -> None:
    """A permitted field must not become a carrier for an unpermitted write."""
    with pytest.raises(DreamProjectAuthorizationError) as caught:
        await _authorize(
            _update(
                fields={"tags": ["reorg:flagged-entity-type"]},
                related_to=[{"id": str(uuid4()), "type": "RELATED_TO"}],
            ),
        )

    assert caught.value.reason == "field_not_allowed_for_phase"


@pytest.mark.asyncio
async def test_reorg_still_updates_tags_when_no_relation_is_carried() -> None:
    """The counterweight: the guard must not break the phase's real work.

    Without this, refusing every REORG `brain_update` would satisfy the two
    assertions above and destroy the phase.
    """
    result = await _authorize(_update(fields={"tags": ["reorg:flagged-entity-type"]}))

    assert result.arguments["fields"] == {"tags": ["reorg:flagged-entity-type"]}


@pytest.mark.asyncio
async def test_an_explicit_null_related_to_is_not_treated_as_a_write() -> None:
    """`related_to=None` is the tool's own default, not an attempt to write.

    A guard that refused it would reject a caller who spelled the default out.
    """
    result = await _authorize(_update(related_to=None))

    assert result.arguments["fields"] == {}


@pytest.mark.asyncio
async def test_an_empty_related_to_list_is_not_treated_as_a_write() -> None:
    """An empty list adds no edge, so it is not the mutation being bounded."""
    result = await _authorize(_update(related_to=[]))

    assert result.arguments["fields"] == {}


@pytest.mark.asyncio
async def test_a_phase_without_a_field_rule_may_still_carry_relations() -> None:
    """EXACT SCOPE. This bound belongs to phases that declare a field rule.

    SYNTH writes knowledge and links it; it has no `phase_update_field_rules`
    entry, so nothing here should reach it. Pinning that keeps the fix from
    being read as "Dream may no longer write edges", which would be a much
    larger and undecided change.
    """
    relation = {"id": str(uuid4()), "type": "IMPLEMENTS"}

    result = await _authorize(_update(related_to=[relation]), audit=SYNTH_AUDIT)

    assert result.arguments["related_to"] == [relation]
