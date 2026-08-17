"""Unit tests for project-scope leakage in the dream phase prompt templates.

Lot 1 of the v2 delivery order (spec §8): the phases are about to be run for
projects other than ``brain-v42``, and three template lines name it in a
scope-bearing position rather than going through ``{{PROJECT_KEY}}``. Rendered
for any other project, those lines instruct the agent to write into brain-v42.

Covers:
  1. Rendered for a project that is NOT brain-v42, no phase prompt still names
     brain-v42 in a scope-bearing position. This is the behavioural assertion —
     the one that would have caught the defect.
  2. The templates themselves carry no bare ``brain-v42`` outside a named
     allowlist, so a new occurrence has to be argued for rather than typed.
  3. The allowlist is pinned, and its single entry is a domain-taxonomy example
     in phase_connect.md — a topic label, not a scope. Substituting it would
     corrupt the taxonomy, which is why it is excluded by name and not by luck.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DREAM_DIR = _REPO_ROOT / "scripts" / "dream"

sys.path.insert(0, str(_DREAM_DIR))
from _render_prompt import render  # noqa: E402

#: Occurrences of the literal project key that are NOT a scope, with the reason
#: each one is allowed to stay. Keyed by template filename.
ALLOWED_LITERAL_MENTIONS: dict[str, int] = {
    # "memory — knowledge graph, brain-v42, embeddings, vector search, …": a
    # domain taxonomy example listing what the memory domain covers. Templating
    # it would rewrite the taxonomy per project instead of naming a topic.
    "phase_connect.md": 1,
}

_PHASE_TEMPLATES = sorted(_DREAM_DIR.glob("phase_*.md"))


def _render_for(path: pathlib.Path, project_key: str) -> str:
    return render(
        path.read_text(),
        {
            "PROJECT_KEY": project_key,
            "DATE": "2026-08-08",
            "DRY_RUN": "true",
            "CANDIDATE_POOL_JSON": "[]",
            "RECENT_PROMOTIONS_JSON": "[]",
        },
    )


def test_there_are_phase_templates_to_check() -> None:
    """Guard the guard: a bad glob would make every test below vacuously pass."""
    assert len(_PHASE_TEMPLATES) >= 6


class TestNoScopeLeakWhenRenderedForAnotherProject:
    """The behavioural assertion."""

    @pytest.mark.parametrize("path", _PHASE_TEMPLATES, ids=lambda p: p.name)
    def test_rendered_prompt_does_not_name_brain_v42_as_scope(self, path: pathlib.Path) -> None:
        rendered = _render_for(path, "red-shrik")
        allowed = ALLOWED_LITERAL_MENTIONS.get(path.name, 0)
        assert rendered.count("brain-v42") == allowed, (
            f"{path.name} still names brain-v42 after rendering for red-shrik; "
            f"expected {allowed} allowlisted mention(s)"
        )


class TestTemplatesCarryNoBareProjectKey:
    @pytest.mark.parametrize("path", _PHASE_TEMPLATES, ids=lambda p: p.name)
    def test_template_source_is_clean(self, path: pathlib.Path) -> None:
        allowed = ALLOWED_LITERAL_MENTIONS.get(path.name, 0)
        assert path.read_text().count("brain-v42") == allowed


class TestSubstitutionActuallyHappens:
    """Negative twin: a template with no placeholder at all would pass the
    tests above trivially. Every phase must genuinely receive the project key."""

    @pytest.mark.parametrize("path", _PHASE_TEMPLATES, ids=lambda p: p.name)
    def test_project_key_reaches_the_rendered_prompt(self, path: pathlib.Path) -> None:
        assert "red-shrik" in _render_for(path, "red-shrik")


class TestAllowlistIsPinned:
    def test_single_entry(self) -> None:
        assert ALLOWED_LITERAL_MENTIONS == {"phase_connect.md": 1}

    def test_the_allowed_mention_is_the_taxonomy_line(self) -> None:
        lines = [
            line
            for line in (_DREAM_DIR / "phase_connect.md").read_text().splitlines()
            if "brain-v42" in line
        ]
        assert len(lines) == 1
        assert "knowledge graph" in lines[0], (
            "the allowlisted mention is no longer the domain-taxonomy line; "
            "re-argue the exception instead of widening it"
        )
