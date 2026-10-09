#!/usr/bin/env python3
"""UserPromptSubmit operator gesture, copied and registered by the operator.

Set BRAIN_ADMIN_URL to the elevation endpoint and BRAIN_ADMIN_CREDENTIAL_FILE to
its elevation-only bearer file (0600). BRAIN_ADMIN_STATE_FILE names the session's
local JSON state, maintained by the Brain session integration, with these keys:
claude_session_id, brain_session_id, prompt_count (number of preceding prompts).
Missing, first-prompt or foreign-session state grants nothing. State and settings
are local inputs; the submitted prompt supplies only the duration and reason.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

_COMMAND = re.compile(r"/brain-admin ([0-9]+[smh]) (.+)")
_WRAPPER = re.compile(
    r"<(?:cross[-_]session(?:[-_]message)?|agent[-_]message|teammate[-_]message|"
    r"task_notification|message)\b|^Message Type: (?:MESSAGE|FINAL_ANSWER)\b|"
    r"^\[(?:Message from|cross-session)\b",
    re.IGNORECASE | re.MULTILINE,
)
_REFUSED = "Brain elevation refused.\n"


class NoRedirect(HTTPRedirectHandler):
    """A redirect must never forward the hook's bearer to another endpoint."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _read_credential(path: Path) -> str:
    """Check the opened inode so symlinks and permission races cannot bypass 0600."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r", encoding="utf-8") as file:
        metadata = os.fstat(file.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
        ):
            raise ValueError("credential file must be private")
        token = file.read(4097).strip()
    if not token or len(token) > 4096 or any(char.isspace() for char in token):
        raise ValueError("invalid credential file")
    return token


def run_hook(
    payload: Mapping[str, Any],
    *,
    environ: Mapping[str, str],
    opener: Callable[..., Any] | None = None,
) -> str:
    """Return only a bounded status line; never echo credentials or HTTP error bodies."""
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt or _WRAPPER.search(prompt):
        return ""
    command = _COMMAND.fullmatch(prompt.splitlines()[0])
    if command is None:
        return ""
    state_path = environ.get("BRAIN_ADMIN_STATE_FILE")
    if not state_path:
        return ""
    try:
        with Path(state_path).open(encoding="utf-8") as file:
            state = json.loads(file.read(16385))
        if not isinstance(state, dict):
            return ""
        if not payload.get("session_id") or state.get("claude_session_id") != payload["session_id"]:
            return ""
        count = state.get("prompt_count")
        if type(count) is not int or count < 1:
            return ""
        session_id = str(UUID(state["brain_session_id"]))
        duration, reason = command.groups()
        ttl = int(duration[:-1]) * {"s": 1, "m": 60, "h": 3600}[duration[-1]]
        if not 0 < ttl <= 14400 or not reason.strip() or len(reason) > 200:
            return _REFUSED
        url = environ["BRAIN_ADMIN_URL"]
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != "/admin/elevations"
            or parsed.query
            or parsed.fragment
        ):
            return _REFUSED
        token = _read_credential(Path(environ["BRAIN_ADMIN_CREDENTIAL_FILE"]))
        request = Request(
            url,
            data=json.dumps(
                {
                    "session_id": session_id,
                    "ttl_seconds": ttl,
                    "reason": reason,
                }
            ).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method="POST",
        )
        # Explicitly ignore ambient proxy settings and never retry the privileged POST.
        send = opener or build_opener(ProxyHandler({}), NoRedirect()).open
        with send(request, timeout=5) as response:
            data = json.loads(response.read(4097))
        expiry = datetime.fromisoformat(data["expires_at"])
        if expiry.tzinfo is None:
            return _REFUSED
        return f"Brain admin until {expiry.isoformat()}.\n"
    except HTTPError as exc:
        exc.close()
        return _REFUSED
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return _REFUSED


def main() -> int:
    """Fail closed without preventing the operator's ordinary prompt submission."""
    try:
        payload = json.loads(sys.stdin.read(1024 * 1024 + 1))
        if isinstance(payload, dict):
            sys.stdout.write(run_hook(payload, environ=os.environ))
    except (ValueError, OSError, RecursionError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
