"""Minting the Dream capability registry — step 8 of the dream v2 spec.

The `MCP_HTTP_DREAM_TOKENS` registry is what moves the principal from `unscoped`
to `scoped`. As long as it is absent, `on_call_tool` lets everything through with
no scope: that is the state of production so far, and it is why a night launched
for one project can read and mutate another's corpus.

This batch's failure mode is KNOWN and it is green. On 2026-07-03, a missing
bearer made every phase run in 401 — zero brain tools — and the night returned
"6/6 OK". The `token.conf` drop-in exists because of that. An incomplete or
malformed registry produces exactly the same night.

Hence this file's central guard: the minting tool validates its own output by
passing it back through `parse_dream_capability_registry`, THE function the
server uses. Not a copy of its rules — the function. A registry the server would
refuse therefore cannot come out of the tool.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from scripts import mint_dream_capability_registry as mint

from brain_v42.mcp.dream_capabilities import (
    DREAM_PHASE_TOOL_ALLOWLISTS,
    DreamCapabilityConfigurationError,
    parse_dream_capability_registry,
)

_ADMIN = "admin-token-for-tests-only-not-a-real-secret"


def _registry_payload(path: Path) -> dict:
    line = path.read_text(encoding="utf-8").strip()
    assert line.startswith("MCP_HTTP_DREAM_TOKENS="), line[:40]
    return json.loads(line.removeprefix("MCP_HTTP_DREAM_TOKENS="))


def test_the_matrix_is_complete_for_every_project(tmp_path: Path) -> None:
    """The parser requires the SIX phases per project, otherwise it refuses everything.

    This is not a politeness: a five-phase project makes
    `parse_dream_capability_registry` raise at MCP server STARTUP, so production
    does not come back up. Better that the minting tool cannot produce that.
    """
    out = tmp_path / "dream-tokens.env"
    mint.mint(["alpha", "beta"], out, admin_token=_ADMIN)

    payload = _registry_payload(out)
    for project in ("alpha", "beta"):
        phases = {key.rsplit(":", 1)[1] for key in payload if key.startswith(f"{project}:")}
        assert phases == set(DREAM_PHASE_TOOL_ALLOWLISTS)


def test_the_output_round_trips_through_the_production_parser(tmp_path: Path) -> None:
    """THE guard. The tool cannot mint what the server would refuse."""
    out = tmp_path / "dream-tokens.env"
    mint.mint(["alpha", "beta"], out, admin_token=_ADMIN)

    registry = parse_dream_capability_registry(
        json.dumps(_registry_payload(out)), admin_token=_ADMIN
    )

    assert len(registry.profiles) == 2 * len(DREAM_PHASE_TOOL_ALLOWLISTS)
    for phase in DREAM_PHASE_TOOL_ALLOWLISTS:
        assert registry.active_token_for("alpha", phase).get_secret_value()


def test_every_token_is_distinct(tmp_path: Path) -> None:
    """The parser rejects a duplicate, but the tool must not produce one.

    A duplicate between two profiles would mean two (project, phase) sharing one
    identity: the middleware would read the wrong one and scope the wrong project.
    """
    out = tmp_path / "dream-tokens.env"
    mint.mint(["alpha", "beta", "gamma"], out, admin_token=_ADMIN)

    payload = _registry_payload(out)
    tokens = [profile["active"] for profile in payload.values()]
    tokens += [token for profile in payload.values() for token in profile["accepted"]]

    assert len(tokens) == len(set(tokens))


def test_a_collision_with_the_admin_token_is_refused(tmp_path: Path) -> None:
    """A profile carrying the admin bearer would give `brain:admin` to a phase."""
    out = tmp_path / "dream-tokens.env"
    with pytest.raises(DreamCapabilityConfigurationError):
        mint.mint(["alpha"], out, admin_token=_ADMIN, _token_source=lambda: _ADMIN)


def test_the_file_is_written_private(tmp_path: Path) -> None:
    """0600. The MCP preflight requires it and refuses the service otherwise."""
    out = tmp_path / "dream-tokens.env"
    mint.mint(["alpha"], out, admin_token=_ADMIN)

    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_no_token_material_reaches_stdout_or_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The runbook forbids it: never in the history, a diff or a ticket."""
    out = tmp_path / "dream-tokens.env"
    mint.mint(["alpha"], out, admin_token=_ADMIN)
    captured = capsys.readouterr()

    payload = _registry_payload(out)
    for profile in payload.values():
        assert profile["active"] not in captured.out
        assert profile["active"] not in captured.err
    assert _ADMIN not in captured.out
    assert _ADMIN not in captured.err


def test_an_existing_file_is_never_silently_overwritten(tmp_path: Path) -> None:
    """Overwriting a live registry invalidates the bearers the phases carry.

    The result would be 401 for the whole following night, hence "6/6 OK" on
    nothing. We refuse, and the caller decides.
    """
    out = tmp_path / "dream-tokens.env"
    mint.mint(["alpha"], out, admin_token=_ADMIN)

    with pytest.raises(FileExistsError):
        mint.mint(["alpha"], out, admin_token=_ADMIN)


def test_the_project_keys_are_canonicalised_and_rejected_when_malformed(
    tmp_path: Path,
) -> None:
    """The parser requires the CANONICAL key: `brain_v42` would make it raise."""
    out = tmp_path / "dream-tokens.env"
    mint.mint(["brain_v42"], out, admin_token=_ADMIN)

    assert all(key.startswith("brain-v42:") for key in _registry_payload(out))

    with pytest.raises(ValueError):
        mint.mint(["Not A Key"], tmp_path / "other.env", admin_token=_ADMIN)


def test_a_duplicate_project_is_refused(tmp_path: Path) -> None:
    """The same project twice would silently overwrite its six profiles."""
    with pytest.raises(ValueError):
        mint.mint(["alpha", "alpha"], tmp_path / "x.env", admin_token=_ADMIN)


def test_an_empty_pool_is_refused(tmp_path: Path) -> None:
    """The parser requires at least one complete project; the tool stops earlier."""
    with pytest.raises(ValueError):
        mint.mint([], tmp_path / "x.env", admin_token=_ADMIN)
