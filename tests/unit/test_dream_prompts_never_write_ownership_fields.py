"""A phase prompt must never order a write the server refuses.

Since the capability scope was armed (2026-08-10), `brain_update` carries
`reject_update_ownership_fields=True`: any `fields` containing an ownership
field — `project_key` first among them — is refused `ownership_field_forbidden`.
The refusal is BY NAME, so it fires even when the value is the bearer's own
project.

Yet `phase_reorg.md` ordered
`brain_update(entity_type, entity_id, fields={tags: ..., project_key: ...})`.
Measured on 2026-08-17 by running the authorisation layer:

    {tags}                        -> allowed
    {tags, project_key} (same project) -> REFUSED
    {project_key}                 -> REFUSED
    {freshness_status: archived}  -> allowed

In other words REORG's Part 1 could write STRICTLY NOTHING — its tag
normalisation included, since the prompt always attached `project_key` to the
same call. Nobody saw it because REORG runs in DRY and never writes: the failure
would only appear on the switch to WET.

`test_dream_prompts_match_phase_allowlists.py` could not catch it: it compares
tool NAMES, and `brain_update` is indeed in reorg's allowlist. It is the ARGUMENT
that is refused. This test closes that blind spot, and it derives its forbidden
list from `_OWNERSHIP_FIELDS` so that a field added to the server policy is
automatically covered here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain_v42.services.dream_project_scope import _OWNERSHIP_FIELDS

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = REPO_ROOT / "scripts" / "dream"

# `fields={...}` as a prompt writes it, braces not nested.
_FIELDS_BLOCK = re.compile(r"fields\s*=\s*\{([^}]*)\}")


def _prompts() -> list[Path]:
    found = sorted(PROMPT_DIR.glob("phase_*.md"))
    assert found, f"aucun prompt de phase trouvé sous {PROMPT_DIR}"
    return found


@pytest.mark.parametrize("prompt_path", _prompts(), ids=lambda p: p.stem)
def test_no_phase_prompt_instructs_writing_an_ownership_field(prompt_path: Path) -> None:
    """No prompt `fields={...}` names an ownership field."""
    text = prompt_path.read_text(encoding="utf-8")

    offenders: list[tuple[int, str, str]] = []
    for match in _FIELDS_BLOCK.finditer(text):
        body = match.group(1)
        line_number = text.count("\n", 0, match.start()) + 1
        for field in sorted(_OWNERSHIP_FIELDS):
            # Word boundaries: `project_keys` must not match `project_key` by
            # prefix, and vice versa.
            if re.search(rf"(?<![\w-]){re.escape(field)}(?![\w-])", body):
                offenders.append((line_number, field, body.strip()[:80]))

    assert not offenders, (
        f"{prompt_path.name} ordonne une écriture que le serveur refuse "
        f"`ownership_field_forbidden` (refus PAR NOM, même si la valeur est le "
        f"projet du bearer) — l'appel entier échoue, y compris les champs "
        f"légitimes du même `fields`: {offenders}"
    )


def test_the_forbidden_set_is_read_from_the_server_policy() -> None:
    """The forbidden list is not retyped here: it comes from the policy.

    An ownership field added on the server side must tighten this test on its own.
    Without that dependency, the two lists would diverge exactly as the prompt and
    the policy diverged.
    """
    assert "project_key" in _OWNERSHIP_FIELDS
    assert len(_OWNERSHIP_FIELDS) >= 2
