"""Bounded live model lists for the providers that have one (headless-agents 0.5.2, lot 6).

Only three of the eight rails answer a live-list query: ``opencode models``,
``agy models``, and OpenRouter's own ``GET /api/v1/models`` (with the key
:func:`headless_agents.keys.preset_key` resolves). The other five --
``claude``, ``codex``, ``mistral``, ``nvidia``, ``openai-compat`` -- are
reported ``catalogue-only``: this reflects the live lists specified in the
parallel-runs design (§3.6), never a claim that those providers have no
models.

A failed query is reported, never fatal, and never leaks a credential or a
response body: ``detail`` is always a short, fixed string naming the
provider and the kind of failure, never the exception text or the HTTP
body -- a query timing out or answering unreadable garbage must not be
mistaken for a catalogue entry having disappeared (that judgement belongs to
:mod:`headless_agents.model_report`, not here).
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .engine import executable_for
from .keys import PresetKey, preset_key

#: A recorded ``agy models`` line is ``<model-id><whitespace><capability tags>``
#: (``tests/unit/headless_agents/fixtures/model_lists/agy_models.txt``); the id
#: column is a lowercase dash/dot slug, never a header (``MODEL``) or a
#: diagnostic token (``ERROR:``).
_AGY_MODEL_ID_PATTERN = re.compile(r"[a-z0-9]+(?:[.\-][a-z0-9]+)*")

#: The only providers with a live-list query (parallel-runs design §3.6).
_QUERIED_PROVIDERS = frozenset({"opencode", "agy", "openrouter"})
_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


@dataclass(frozen=True)
class LiveList:
    status: str
    models: tuple[str, ...]
    detail: str


def _first_tokens(raw: str) -> list[str]:
    tokens: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped:
            tokens.append(stripped.split()[0])
    return tokens


def _dedupe(items: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return tuple(ordered)


def parse_opencode(raw: str) -> tuple[str, ...]:
    """Model IDs from ``opencode models``: the first column of every line naming one."""
    return _dedupe([token for token in _first_tokens(raw) if "/" in token])


def parse_agy(raw: str) -> tuple[str, ...]:
    """Model IDs from ``agy models``: the first column of every non-empty line.

    Every non-empty line is required to have the recorded shape, an id
    column followed by a capability-tags column: a line with only one
    column, or whose first token is not a lowercase dash/dot model id,
    raises rather than being read as a one-model catalogue -- a header
    (``MODEL ...``) or a diagnostic (``ERROR: unsupported models command``)
    on an ``agy models`` that still exited 0 must be reported unreadable,
    never mistaken for a live list.
    """
    ids: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        columns = stripped.split(maxsplit=1)
        if len(columns) < 2:
            raise ValueError(f"agy models line has no capability column: {stripped!r}")
        model_id = columns[0]
        if not _AGY_MODEL_ID_PATTERN.fullmatch(model_id):
            raise ValueError(f"agy models line has an invalid model id: {model_id!r}")
        ids.append(model_id)
    return _dedupe(ids)


def parse_openrouter(body: object) -> tuple[str, ...]:
    """Model IDs from OpenRouter's ``GET /api/v1/models`` body, in document order."""
    if not isinstance(body, dict):
        raise ValueError("openrouter response is not a JSON object")
    data = body.get("data")
    if not isinstance(data, list):
        raise ValueError("openrouter response has no data array")
    ids: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("openrouter data entry is not an object")
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("openrouter data entry has no id")
        ids.append(model_id)
    return _dedupe(ids)


def live_models(
    provider: str,
    *,
    environ: Mapping[str, str],
    home: Path,
    timeout_seconds: float = 5.0,
) -> LiveList:
    if provider not in _QUERIED_PROVIDERS:
        return LiveList("catalogue-only", (), "no live-list query specified")
    try:
        if provider == "openrouter":
            key = preset_key("openrouter", {**environ, "HOME": str(home)})
            if key is None:
                return LiveList("unavailable", (), "OpenRouter key unavailable")
            request = urllib.request.Request(
                _OPENROUTER_MODELS_URL,
                headers={
                    "Authorization": f"Bearer {key.value}",
                    "Accept": "application/json",
                },
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
                body = json.loads(response.read())
            models = parse_openrouter(body)
        else:
            executable = executable_for(provider, home) or provider
            result = subprocess.run(
                [executable, "models"],
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
                env=dict(environ),
            )
            if result.returncode != 0:
                return LiveList(
                    "unavailable",
                    (),
                    f"{provider} models exited {result.returncode}",
                )
            raw = result.stdout.decode("utf-8")
            models = parse_opencode(raw) if provider == "opencode" else parse_agy(raw)
    except subprocess.TimeoutExpired:
        return LiveList("unavailable", (), f"{provider} models timeout")
    except (OSError, urllib.error.URLError, TimeoutError, RuntimeError):
        return LiveList("unavailable", (), f"{provider} models unavailable")
    except (UnicodeDecodeError, ValueError, TypeError):
        return LiveList("unreadable", (), f"{provider} live list unreadable")
    if not models:
        return LiveList("unreadable", (), f"{provider} live list unreadable")
    return LiveList("available", models, "live list read")


__all__ = [
    "LiveList",
    "PresetKey",
    "live_models",
    "parse_agy",
    "parse_opencode",
    "parse_openrouter",
    "preset_key",
]
