"""Canonicalization + validation of brain project keys (single source of truth).

The repo directory, the Python package, the GitLab registry path and the
GitNexus index are all named ``brain_v42`` (underscore), but the canonical
brain ``project_key`` is ``brain-v42`` (hyphen). Historically the knowledge
write path stored ``project_key`` verbatim, so the underscore form silently
created a *phantom* project — invisible to the hyphen-scoped briefing/search
(drift documented in learning ``7bc821a1``).

Policy ("both" / ceinture+bretelles):
- Known confusables (``brain``, ``brain_v42``) are auto-canonicalized to
  ``brain-v42``.
- Anything else that is not already kebab-case (lowercase alphanumerics joined
  by ``-`` or ``:``) is rejected loudly with a hint — never silently coerced.
"""

import re
from typing import overload

from pydantic import BaseModel, field_validator

# Kebab-case with optional colon sub-partitions, e.g. ``red-lab:architect``.
_KEBAB = re.compile(r"^[a-z0-9]+([:-][a-z0-9]+)*$")

# Known confusables that map to the one canonical key. Matched exactly on the
# trimmed input (case-sensitive — uppercase is non-kebab and stays rejected, so
# we don't silently accept it). Everything NOT in this map must already be
# kebab-case.
_ALIASES: dict[str, str] = {
    "brain": "brain-v42",
    "brain_v42": "brain-v42",
}


@overload
def canonicalize_project_key(value: str, *, strict: bool = ...) -> str: ...
@overload
def canonicalize_project_key(value: None, *, strict: bool = ...) -> None: ...
@overload
def canonicalize_project_key(value: str | None, *, strict: bool = ...) -> str | None: ...
def canonicalize_project_key(value: str | None, *, strict: bool = True) -> str | None:
    """Return the canonical kebab-case ``project_key`` (or ``None``).

    ``None`` passes through unchanged (unscoped/global knowledge). Known
    confusables (``brain``, ``brain_v42``) are always mapped to ``brain-v42``.

    For any other value that is not already kebab-case:

    - ``strict=True`` (default, the WRITE path) raises ``ValueError`` with a
      suggested correction — a malformed key must never be persisted.
    - ``strict=False`` (the READ path) passes the value through unchanged — a
      lookup with a bad key simply yields no results, so reads stay forgiving
      and never raise.
    """
    if value is None:
        return None

    raw = value.strip()
    alias = _ALIASES.get(raw)
    if alias is not None:
        return alias

    if not _KEBAB.match(raw):
        if not strict:
            return raw
        suggestion = raw.lower().replace("_", "-").replace(" ", "-")
        raise ValueError(
            "project_key must be kebab-case (lowercase, hyphens/colons only): "
            f"{value!r}. Did you mean {suggestion!r}?"
        )
    return raw


class ProjectKeyCanonicalMixin(BaseModel):
    """Mixin that canonicalizes ``project_key`` on any model declaring the field.

    Inherit alongside the model's base (e.g.
    ``class DecisionCreate(DecisionBase, ProjectKeyCanonicalMixin)``). The
    validator uses ``check_fields=False`` so the mixin itself need not declare
    the field; it activates on whichever concrete model has ``project_key``.
    """

    @field_validator("project_key", check_fields=False)
    @classmethod
    def _canonicalize_project_key(cls, v: str | None) -> str | None:
        return canonicalize_project_key(v)
