"""The extract phase's THIRD link: `agy` (Google Gemini) in tool-less headless mode.

`ticket_extract` normally speaks to NVIDIA's OpenAI-compatible endpoint over
one `httpx.AsyncClient`, with a primary and one fallback model on the SAME
transport (`domain_backfill._post_chat`). When NVIDIA is entirely unreachable
— both models withdrawn or a provider-wide outage — this module gives the
phase a route that does not depend on that transport at all: the `agy` CLI, a
subscription-backed Gemini client, invoked once per completion in headless
print mode.

THE PROMPT GOES IN ARGV, and that is not a choice: measured 2026-08-11 (see
`scripts/dream/agy_runner.py::build_agy_command`), agy IGNORES stdin — a
prompt on stdin is answered with an off-topic greeting, and the process still
exits 0. Refused BEFORE execve past `_MAX_PROMPT_BYTES`: an E2BIG deep inside
a subprocess is an opaque `OSError`, where this names the cause and the size.
The prompt is ticket text and previous model output, never a secret, so argv
exposure is accepted here exactly as it is for the dream-phase rail.

FLATTENING. `agy_chat_completion` receives the same `messages` shape
`_post_chat` does — `[{"role": "system", ...}, {"role": "user", ...}]`, and on
the corrective re-prompt `[..., {"role": "assistant", ...}, {"role": "user",
...}]` — and folds it into ONE prompt, deterministically: one `### <role>`
section per message, in list order, joined with a blank line:

    ### system
    <content>

    ### user
    <content>

No role is special-cased, so the corrective re-prompt's assistant turn simply
becomes another `### assistant` section ahead of the closing `### user`.

TOOL-LESS. Unlike the nightly dream-phase `agy_runner` (which wires a scoped
Brain MCP server and a `PreToolUse` guard so agy can act), extract has nothing
for agy to write: the ephemeral HOME's `mcp_config.json` declares NO MCP
servers at all, and `--dangerously-skip-permissions` is never passed — there
is no tool call to pre-approve. A stray attempt is bounded by
`--print-timeout` (and the `asyncio.wait_for` backstop below) rather than
granted blanket approval.

CREDENTIALS. Symlinked from the real HOME's `.gemini/oauth_creds.json` and
`.gemini/antigravity-cli/antigravity-oauth-token`, never copied — duplicating
a human's OAuth tokens would make copies to revoke one by one.

NO IMPORT FROM `scripts/`: `src/brain_v42/` must not depend on the top-level
`scripts/` tree (it is not installed with the package). The ephemeral-home and
child-environment patterns mirror `scripts/dream/agy_runner.py` and
`scripts/dream/_agent_capability.py::build_child_environment` in INTENT, not
by import — a second, smaller statement of the same idea, not a shared one.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

AGY_BIN_ENV = "BRAIN_DREAM_AGY_BIN"
DEFAULT_AGY_EXECUTABLE = "agy"

# Kernel limit on a SINGLE argv argument (MAX_ARG_STRLEN = 32 pages). Beyond
# it, execve returns E2BIG. Refusing before that point names the cause and the
# size instead of surfacing an opaque OSError.
_MAX_PROMPT_BYTES = 120_000

# Credential files read from the real HOME. SYMLINKED, never copied.
_CREDENTIAL_PATHS = (
    ".gemini/oauth_creds.json",
    ".gemini/antigravity-cli/antigravity-oauth-token",
)

# Bound on the raw stderr embedded in an `AgyLinkError` message, ahead of the
# caller's own `_safe_error` truncation/redaction: a multi-KB crash dump must
# not travel any further than it has to before that gate runs.
_MAX_STDERR_CHARS = 500


class AgyLinkError(RuntimeError):
    """The agy transport produced no usable answer.

    Raised on a non-zero exit, non-JSON stdout, a `status` other than
    `SUCCESS`, or a missing/empty `response` field — every shape that leaves
    nothing for `parse_and_validate` to work with.
    """


def resolve_agy_executable(environ: Mapping[str, str]) -> str:
    """The same executable the nightly dream-phase rail resolves — see
    `scripts/dream/agy_runner.py`'s ``--agy-executable`` default."""
    return environ.get(AGY_BIN_ENV) or DEFAULT_AGY_EXECUTABLE


def flatten_messages(messages: list[dict[str, str]]) -> str:
    """See the module docstring: one ``### <role>`` section per message."""
    return "\n\n".join(f"### {message['role']}\n{message['content']}" for message in messages)


def build_agy_completion_command(
    *,
    model: str,
    prompt: str,
    agy_executable: str,
    timeout_seconds: float,
) -> list[str]:
    """The headless, tool-less command line of one completion.

    ``--output-format json`` (not `stream-json`, unlike the dream-phase rail):
    a single-shot completion has no steps to stream, and the envelope's
    ``response`` field is the whole answer. No
    ``--dangerously-skip-permissions``: see the module docstring.
    """
    prompt_bytes = len(prompt.encode("utf-8"))
    if prompt_bytes > _MAX_PROMPT_BYTES:
        raise ValueError(f"prompt too long for argv: {prompt_bytes} bytes > {_MAX_PROMPT_BYTES}")
    command = [
        agy_executable,
        "--print",
        prompt,
        "--output-format",
        "json",
        "--print-timeout",
        f"{int(timeout_seconds)}s",
        "--disable-slash-commands",
    ]
    if model.strip():
        command.extend(("--model", model))
    return command


def build_toolless_home(root: Path, *, real_home: Path | None = None) -> Path:
    """Compose a tool-less ephemeral HOME: no MCP servers, symlinked credentials.

    ``real_home`` defaults to the process's own ``HOME`` — parametrised so
    tests never touch the real one.
    """
    home = root / "agy-extract-home"
    config_dir = home / ".gemini" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)

    config_path = config_dir / "mcp_config.json"
    config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    config_path.chmod(0o600)

    source_home = (
        real_home if real_home is not None else Path(os.environ.get("HOME", str(Path.home())))
    )
    for relative in _CREDENTIAL_PATHS:
        source = source_home / relative
        target = home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists() and not target.exists():
            target.symlink_to(source)

    return home


def _child_environment(home: Path) -> dict[str, str]:
    """A minimal environment: HOME (the ephemeral one), PATH, and locale.

    Mirrors the INTENT of `scripts/dream/_agent_capability.py::build_child_environment`
    — a base allowlist, never the ambient environment — without importing it.
    """
    lang = os.environ.get("LANG", "C.UTF-8")
    return {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": lang,
        "LC_ALL": os.environ.get("LC_ALL", lang),
    }


def _bounded_stderr(raw: bytes) -> str:
    text = " ".join(raw.decode("utf-8", errors="replace").split())
    return text[:_MAX_STDERR_CHARS]


async def agy_chat_completion(
    *,
    model: str,
    messages: list[dict[str, str]],
    agy_executable: str | None = None,
    timeout_seconds: float = 120.0,
) -> tuple[str, dict[str, Any] | None]:
    """Run one agy print-mode completion; return ``(response, usage)``.

    ``usage`` mirrors the shape ``ticket_extract.thinking_tokens_from_usage``
    reads: agy's ``usage.thinking_tokens`` is remapped to ``reasoning_tokens``
    so the extract rail's existing telemetry path picks it up unchanged. An
    envelope that carries no thinking-token count maps to ``{}`` — never a
    fabricated ``reasoning_tokens: 0`` — so the caller reads it as "not
    measured", the same contract migration 049 exists to preserve.

    Raises ``AgyLinkError`` on anything that leaves no usable answer.
    """
    executable = agy_executable or resolve_agy_executable(os.environ)
    prompt = flatten_messages(messages)

    with TemporaryDirectory(prefix="brain-v42-extract-agy-") as raw_root:
        home = build_toolless_home(Path(raw_root))
        try:
            command = build_agy_completion_command(
                model=model,
                prompt=prompt,
                agy_executable=executable,
                timeout_seconds=timeout_seconds,
            )
        except ValueError as exc:
            raise AgyLinkError(str(exc)) from None

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=home,
                env=_child_environment(home),
            )
        except OSError as exc:
            raise AgyLinkError(f"cannot start agy: {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise AgyLinkError(f"agy timed out after {timeout_seconds:g}s") from None

        if process.returncode != 0:
            raise AgyLinkError(f"agy exited {process.returncode}: {_bounded_stderr(stderr)}")

    try:
        envelope = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgyLinkError(f"agy stdout is not valid JSON: {exc}") from None
    if not isinstance(envelope, dict) or envelope.get("status") != "SUCCESS":
        status = envelope.get("status") if isinstance(envelope, dict) else None
        raise AgyLinkError(f"agy status={status!r}, expected SUCCESS")

    response = envelope.get("response")
    if not isinstance(response, str) or not response:
        raise AgyLinkError("agy envelope carries no usable response")

    usage_raw = envelope.get("usage")
    usage: dict[str, Any] = {}
    if isinstance(usage_raw, dict) and usage_raw.get("thinking_tokens") is not None:
        usage["reasoning_tokens"] = usage_raw["thinking_tokens"]
    return response, usage
