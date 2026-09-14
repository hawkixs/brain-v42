"""``CapabilityProfile`` -- what a caller hands the runtime instead of policy.

The runtime executes an agent CLI; it does not decide what the agent may
reach. Everything a consumer used to resolve for itself -- which MCP server
with which bearer and which tool allowlist, which ``PreToolUse`` guard, which
credential files, which extra ambient variables -- arrives as one value of
this shape. The nightly Dream builds its profile from its own phase policy;
an arena seat builds one with ``mcp=None``; both run the same providers.

Validation happens here, once, so no provider has to repeat it: an MCP URL
must be loopback unless the caller opts out by name, a credential path must
stay relative and inside the caller's HOME, a guard must be an absolute
path. Secrets are ``SecretStr`` and never appear in a ``repr``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .capability import validate_loopback_url

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class McpServer(BaseModel):
    """One MCP server the agent may reach, and the tools it may call there.

    ``bearer`` is the token VALUE, needed by a rail that writes a literal
    ``Authorization`` header into a config file (agy). ``bearer_env_var`` is
    the NAME under which a rail that reads the token from its environment
    (codex, claude) expects it; the caller is responsible for putting the value
    there, typically through :func:`headless_agents.capability.scoped_environment`.
    ``bearer=None`` means "the child environment already carries it" -- the
    inherit-everything rollback path some callers keep.
    """

    model_config = _FROZEN

    name: str = Field(min_length=1)
    url: str
    bearer: SecretStr | None = None
    bearer_env_var: str = Field(default="MCP_HTTP_TOKEN", min_length=1)
    headers: Mapping[str, str] = Field(default_factory=dict)
    tools: tuple[str, ...] = ()
    require_loopback: bool = True

    @field_validator("name", "bearer_env_var")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("tools", mode="before")
    @classmethod
    def _tools_tuple(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str) or not isinstance(value, list | tuple):
            raise ValueError("tools must be a sequence of tool names")
        tools = tuple(value)
        if any(not isinstance(tool, str) or not tool.strip() for tool in tools):
            raise ValueError("every tool name must be a non-blank string")
        return tools

    @field_validator("headers", mode="before")
    @classmethod
    def _headers_without_authorization(cls, value: object) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("headers must be a mapping")
        headers = {str(key): str(item) for key, item in value.items()}
        if any(key.lower() == "authorization" for key in headers):
            raise ValueError("Authorization is derived from the bearer, never passed as a header")
        return headers

    @model_validator(mode="after")
    def _loopback_unless_opted_out(self) -> McpServer:
        if self.require_loopback:
            validate_loopback_url(self.url)
        return self


class ToolGuard(BaseModel):
    """A ``PreToolUse`` hook script the CLI must consult before every tool call.

    The runtime ships no guard of its own: the script is versioned and tested
    by the caller, who passes its path. Pass it ABSOLUTE: the CLI resolves the
    hook command from its own working directory (the ephemeral HOME), where a
    relative path names nothing -- and the probe that proves the guard denies
    runs from the parent's, so it would not notice. The shape is not enforced
    here only because callers pin the written ``hooks.json`` with placeholder
    paths in their golden fixtures.
    """

    model_config = _FROZEN

    path: Path
    hook_name: str = Field(default="tool-guard", min_length=1)
    timeout_seconds: int = Field(default=10, gt=0)


class Credentials(BaseModel):
    """Credential files to expose inside the ephemeral HOME.

    Paths are RELATIVE to the caller's real HOME and must stay inside it: this
    list is data the caller reads from its own configuration, and without the
    guard a configured path could pull any file on the machine into the
    sandbox. ``symlink`` (default) never duplicates a token; ``copy`` writes a
    ``0600`` copy for a sandbox that must not point back at the real HOME.
    """

    model_config = _FROZEN

    paths: tuple[str, ...] = ()
    mode: Literal["symlink", "copy"] = "symlink"

    @field_validator("paths")
    @classmethod
    def _relative_and_contained(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for raw in value:
            if not raw or PurePosixPath(raw).is_absolute() or ".." in PurePosixPath(raw).parts:
                raise ValueError(f"credential path must be relative and inside HOME: {raw!r}")
        return value


class CapabilityProfile(BaseModel):
    """Everything the runtime lets one run reach. Empty means: nothing."""

    model_config = _FROZEN

    mcp: McpServer | None = None
    guard: ToolGuard | None = None
    credentials: Credentials = Field(default_factory=Credentials)
    # Ambient variables allowed into the child environment on top of the
    # base allowlist and the rail's own additions.
    environment_passthrough: tuple[str, ...] = ()
