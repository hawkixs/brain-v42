"""Compatibility reexports for Dream project authorization primitives."""

from __future__ import annotations

from brain_v42.dream_project_errors import DreamProjectAuthorizationError, DreamProjectDenialReason
from brain_v42.services.dream_project_scope import (
    _CURRENT_DREAM_PROJECT_SCOPE,
    PROJECT_TOOL_POLICIES,
    DreamEntityType,
    DreamObjectReference,
    DreamProjectAudit,
    DreamProjectAuthorizationResult,
    DreamProjectReferenceResolver,
    DreamProjectScope,
    DreamProjectToolPolicy,
    DreamTypedReferenceRule,
    PostgresDreamProjectResolver,
    _deny,
    authorize_dream_project_request,
    bind_dream_project_scope,
    get_dream_project_scope,
)

__all__ = (
    "PROJECT_TOOL_POLICIES",
    "DreamEntityType",
    "DreamObjectReference",
    "DreamProjectAudit",
    "DreamProjectAuthorizationError",
    "DreamProjectAuthorizationResult",
    "DreamProjectDenialReason",
    "DreamProjectReferenceResolver",
    "DreamProjectScope",
    "DreamProjectToolPolicy",
    "DreamTypedReferenceRule",
    "PostgresDreamProjectResolver",
    "_CURRENT_DREAM_PROJECT_SCOPE",
    "_deny",
    "authorize_dream_project_request",
    "bind_dream_project_scope",
    "get_dream_project_scope",
)
