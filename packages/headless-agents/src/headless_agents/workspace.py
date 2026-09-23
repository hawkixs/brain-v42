"""What the four CLI rails share once a run carries a workspace or a context bundle."""

from __future__ import annotations

from .profile import Workspace
from .spec import RunSpec


def workspace_of(spec: RunSpec) -> Workspace | None:
    """The run's workspace. ``RunSpec.workspace`` (codex's legacy ``-C``) and
    ``profile.workspace`` name the same thing: both set is refused."""
    if spec.workspace is not None and spec.profile.workspace is not None:
        raise ValueError("RunSpec.workspace and profile.workspace are both set: pick one")
    return spec.profile.workspace


def workspace_summary(workspace: Workspace | None) -> dict[str, object] | None:
    if workspace is None:
        return None
    return {"path": str(workspace.path), "write": workspace.write, "shell": workspace.shell}


def rail_preamble(spec: RunSpec, *, tools_note: str) -> str:
    """The workspace note, then the bundle's preamble -- repository content
    included in EVERY mode: the preamble is the only channel that reaches all
    four rails (see :mod:`headless_agents.context`)."""
    workspace = workspace_of(spec)
    parts: list[str] = []
    if workspace is not None:
        mode = "read and edit" if workspace.write else "read (no edits)"
        parts.append(
            f'<workspace path="{workspace.path}" mode="{mode}">\n'
            f"Work inside {workspace.path} only; use absolute paths under it. {tools_note}\n"
            "</workspace>"
        )
    if spec.context is not None:
        block = spec.context.preamble()
        if block:
            parts.append(block)
    return "\n\n".join(parts)


def prepend(preamble: str, prompt: str) -> str:
    if not preamble:
        return prompt
    return f"{preamble}\n\n<task>\n{prompt}\n</task>"


def argv_prompt_or_refusal(prompt: str, limit: int) -> str | None:
    size = len(prompt.encode("utf-8"))
    if size <= limit:
        return None
    return f"prompt with context too long for argv: {size} bytes > {limit}"
