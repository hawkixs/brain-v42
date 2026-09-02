"""Mint the Dream capability registry — step 8 of the dream v2 spec.

The ``MCP_HTTP_DREAM_TOKENS`` registry is what takes a phase's principal from
``unscoped`` to ``scoped``. While it is absent, ``on_call_tool`` lets calls
through unscoped and a night launched for one project can read and mutate every
other project's corpus.

CLI (the secret NEVER travels through an argument nor through stdout) ::

    MCP_HTTP_TOKEN=... uv run python -m scripts.mint_dream_capability_registry \\
        --output ~/.config/brain-v42/dream-tokens.env \\
        --project-key brain-v42 --project-key red

    # or, taking exactly the live unit's pool:
    MCP_HTTP_TOKEN=... uv run python -m scripts.mint_dream_capability_registry \\
        --output ~/.config/brain-v42/dream-tokens.env --from-drop-in

This project's failure mode is KNOWN and it is green. On 2026-07-03, a missing
bearer made every phase run at 401 — zero brain tools — and the night reported
"6/6 OK". An incomplete registry produces exactly the same night. Hence the
central guard: the output is put back through
``parse_dream_capability_registry``, THE function the server runs at startup.
Not a copy of its rules, the function. A registry the server would refuse can
therefore not leave this file.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from brain_v42.mcp.dream_capabilities import (
    DREAM_PHASE_TOOL_ALLOWLISTS,
    parse_dream_capability_registry,
)
from brain_v42.models.project_key import canonicalize_project_key

ENV_KEY = "MCP_HTTP_DREAM_TOKENS"
ADMIN_TOKEN_ENV = "MCP_HTTP_TOKEN"
# 32 bytes of entropy in base64url. The registry compares in constant time
# (hmac.compare_digest) and the tokens are opaque: their only useful property is
# being unpredictable and distinct.
_TOKEN_BYTES = 32


def _default_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def mint(
    project_keys: Sequence[str],
    output: Path,
    *,
    admin_token: str,
    _token_source: Callable[[], str] = _default_token,
) -> int:
    """Write the registry for ``project_keys``. Returns the number of profiles.

    Refuses to overwrite an existing file: the live bearers are the ones the
    phases already carry, and replacing them unintentionally would give 401 for
    the whole following night — hence "6/6 OK" over nothing.
    """
    if not project_keys:
        raise ValueError("at least one project key is required")

    canonical: list[str] = []
    for raw in project_keys:
        key = canonicalize_project_key(raw)
        if key in canonical:
            raise ValueError(f"duplicate project key: {key}")
        canonical.append(key)

    phases = sorted(DREAM_PHASE_TOOL_ALLOWLISTS)
    payload: dict[str, dict[str, object]] = {}
    for project_key in canonical:
        for phase in phases:
            # `accepted` stays empty at the initial mint: it only serves a
            # rotation overlap, where the old token must stay honoured until the
            # clients pick up the new one.
            payload[f"{project_key}:{phase}"] = {"active": _token_source(), "accepted": []}

    serialised = json.dumps(payload, separators=(",", ":"), sort_keys=True)

    # THE guard: validate with the server's parser, not with a re-reading of
    # its rules. It checks the complete matrix, duplicates, collision with the
    # admin bearer and key canonicity — and it raises if one is missing, BEFORE
    # the file reaches the disk.
    parse_dream_capability_registry(serialised, admin_token=admin_token)

    # `x`: exclusive creation. An existing registry is never overwritten.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{ENV_KEY}={serialised}\n")
    # os.open honours the umask; we re-set the mode explicitly, because the MCP
    # preflight refuses to serve if the file is not exactly 0600.
    output.chmod(0o600)
    return len(payload)


def _pool_from_drop_in() -> list[str]:
    from brain_v42.dream_killswitches import (  # noqa: PLC0415
        KILLSWITCHES_PATH,
        parse_project_pool,
    )

    pool = parse_project_pool(KILLSWITCHES_PATH.read_text())
    if not pool:
        raise ValueError(
            f"no BRAIN_DREAM_PROJECT_POOL in {KILLSWITCHES_PATH} — "
            "pass --project-key explicitly, or open the pool first"
        )
    return pool


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--project-key",
        action="append",
        default=[],
        dest="project_keys",
        help="Repeatable. Mutually exclusive with --from-drop-in.",
    )
    parser.add_argument(
        "--from-drop-in",
        action="store_true",
        help="Read the pool from the live systemd drop-in instead.",
    )
    args = parser.parse_args(argv)

    if bool(args.project_keys) == args.from_drop_in:
        print("choose exactly one of --project-key or --from-drop-in", file=sys.stderr)
        return 2

    admin_token = os.environ.get(ADMIN_TOKEN_ENV)
    if not admin_token:
        # It does not come in through an argument: the command line is
        # readable in /proc and in the shell history.
        print(f"{ADMIN_TOKEN_ENV} must be set in the environment", file=sys.stderr)
        return 2

    try:
        project_keys = _pool_from_drop_in() if args.from_drop_in else args.project_keys
        count = mint(project_keys, args.output, admin_token=admin_token)
    except FileExistsError:
        print(f"refusing to overwrite {args.output} — move it aside first", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        # `exc` may quote a project key, never a token: the parser's errors are
        # secret-safe by construction.
        print(f"mint failed: {exc}", file=sys.stderr)
        return 1

    # The count, never the material. Projects are public, tokens are not.
    print(f"wrote {count} profiles for {len(project_keys)} projects to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
