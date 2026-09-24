"""One OpenAI-compatible chat completion, run in a killable child process.

The parent (:mod:`headless_agents.providers.openai_compat`) writes a JSON
envelope on this process's stdin -- the endpoint, the key, the request body and
a socket timeout -- and reads ONE JSON object back on stdout. The key travels on
stdin only: never in argv (visible in ``ps``), never in the environment, never
in anything this process writes.

Failures come back as a closed category and an HTTP status, NEVER as the
exception's text or the response body: a provider's error body may quote the
request, the URL or the key itself. A success returns the parsed response
body, which the parent reads the answer from.

Standard library only (``urllib``): the package keeps its single runtime
dependency.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

USER_AGENT = "headless-agents (openai-compat)"


def _failure(category: str, status: int | None = None) -> dict[str, Any]:
    return {"ok": False, "category": category, "status": status}


def perform(envelope: dict[str, Any]) -> dict[str, Any]:
    """POST the body and classify the outcome; never raises, never echoes a body."""
    # The parent validates the URL already; the worker does not rely on it:
    # urlopen would happily read file:, data: or ftp: URLs.
    if urlsplit(envelope["url"]).scheme not in {"http", "https"}:
        return _failure("error")
    request = urllib.request.Request(
        envelope["url"],
        data=json.dumps(envelope["body"]).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {envelope['api_key']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        # nosec B310: the scheme is checked to be http(s) just above.
        with urllib.request.urlopen(  # nosec B310
            request, timeout=float(envelope["timeout"])
        ) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        # The body is deliberately never read: it may quote the key.
        exc.close()
        return _failure("http", exc.code)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return _failure("timeout")
        return _failure("unreachable")
    except TimeoutError:
        return _failure("timeout")
    except OSError:
        return _failure("unreachable")
    try:
        parsed = json.loads(raw)
    except ValueError:
        return _failure("malformed", status)
    if not isinstance(parsed, dict):
        return _failure("malformed", status)
    return {"ok": True, "status": status, "response": parsed}


def main() -> int:
    try:
        envelope = json.loads(sys.stdin.read())
        outcome = perform(envelope)
    except BaseException:  # noqa: BLE001 - process boundary: never echo the cause
        outcome = _failure("error")
    sys.stdout.write(json.dumps(outcome))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the parent
    raise SystemExit(main())
