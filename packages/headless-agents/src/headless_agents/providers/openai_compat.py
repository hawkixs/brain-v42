"""``openai-compat`` -- text-only chat completions over HTTP, and its presets.

Spec 3.2 (headless-agents 0.4.0, lot 3). Four registry names share this
provider: ``openrouter``, ``mistral`` and ``nvidia`` fix their endpoint and the
NAME of the variable holding their key; ``openai-compat`` takes both from the
caller, in ``RunSpec.extra["base_url"]`` and ``RunSpec.extra["key_env"]``. The
package never reads a key from a file: the caller puts it in the environment.

Each call runs in a killable child (:mod:`._openai_worker`), so a deadline and
a process-group kill behave exactly as on the CLI rails. The prompt AND the key
travel on the child's stdin; the child's environment is the scoped base
allowlist, without the key variable.

Exit codes (never echoing an error body, which may contain the key):

========================================  ======  =========================
condition                                 code    chain
========================================  ======  =========================
the answer came back                      0
own deadline fired                        124     stops
HTTP 429 or 5xx, host unreachable         3       advances (no tool: safe)
HTTP 401/403, any other failure           1       stops (configuration)
the spec cannot work (no URL, no model)   2       stops, nothing sent
========================================  ======  =========================

No tools, no MCP, no workspace, no streaming: a profile declaring ``mcp`` or
``workspace`` raises ``ValueError``. Request options are limited to
``response_format``, ``temperature`` and ``max_tokens`` (``RunSpec.extra``).
The context bundle's preamble becomes a ``system`` message.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from ..capability import (
    INVALID_USAGE_EXIT_CODE,
    PROVIDER_FALLBACK_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    scoped_environment,
    terminate_process_group,
)
from ..result import RunResult, TokenUsage
from ..run_record import record, run_id_of
from ..spec import RunSpec

GENERIC_NAME: Final = "openai-compat"


@dataclass(frozen=True)
class Preset:
    """A provider whose endpoint and key variable are fixed."""

    name: str
    base_url: str
    key_env: str


PRESETS: Final[Mapping[str, Preset]] = {
    "openrouter": Preset("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "mistral": Preset("mistral", "https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "nvidia": Preset("nvidia", "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
}

#: The request options a caller may set through ``RunSpec.extra``.
REQUEST_OPTIONS: Final = frozenset({"response_format", "temperature", "max_tokens"})
#: The generic provider's configuration keys in ``RunSpec.extra``.
CONFIGURATION_KEYS: Final = frozenset({"base_url", "key_env"})

#: The worker's socket timeout exceeds the parent's deadline by this much, so
#: the deadline is always the parent's: a timeout is 124, never a category the
#: worker made up a moment earlier.
SOCKET_TIMEOUT_MARGIN_SECONDS: Final = 5.0

WORKER_MODULE: Final = "headless_agents.providers._openai_worker"


class _UsageError(ValueError):
    """The spec cannot work on this provider; nothing is sent."""


@dataclass(frozen=True)
class ParsedCompletion:
    text: str | None
    model_reported: str | None
    tokens: TokenUsage | None
    cost_usd: float | None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_completion(response: Mapping[str, Any]) -> ParsedCompletion:
    """Read the answer and usage from a chat-completions body.

    Raises ``ValueError`` when the body carries no ``choices[0].message``: a
    success that holds no answer is a malformed reply, not an empty answer.
    """
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise ValueError("no choices in the completion")
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        raise ValueError("no message in the first choice")
    content = message.get("content")
    text = content if isinstance(content, str) and content.strip() else None

    usage = response.get("usage")
    tokens: TokenUsage | None = None
    cost: float | None = None
    if isinstance(usage, Mapping):
        prompt_tokens = _int_or_none(usage.get("prompt_tokens"))
        prompt_details = usage.get("prompt_tokens_details")
        completion_details = usage.get("completion_tokens_details")
        cached = (
            _int_or_none(prompt_details.get("cached_tokens"))
            if isinstance(prompt_details, Mapping)
            else None
        )
        thinking = (
            _int_or_none(completion_details.get("reasoning_tokens"))
            if isinstance(completion_details, Mapping)
            else None
        )
        tokens = TokenUsage(
            input=prompt_tokens,
            output=_int_or_none(usage.get("completion_tokens")),
            cached=cached,
            # Fresh input is measured only when both halves are.
            fresh=prompt_tokens - cached
            if prompt_tokens is not None and cached is not None
            else None,
            thinking=thinking,
        )
        raw_cost = usage.get("cost")
        if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool):
            cost = float(raw_cost)
    model = response.get("model")
    return ParsedCompletion(
        text=text,
        model_reported=model if isinstance(model, str) and model else None,
        tokens=tokens,
        cost_usd=cost,
    )


def request_body(
    provider_name: str,
    *,
    model: str,
    messages: list[dict[str, str]],
    options: Mapping[str, object],
) -> dict[str, object]:
    """The chat-completions request body. OpenRouter alone reports a cost, on request."""
    body: dict[str, object] = {"model": model, "messages": messages}
    body.update({name: value for name, value in options.items() if name in REQUEST_OPTIONS})
    if provider_name == "openrouter":
        body["usage"] = {"include": True}
    return body


def _validated_base_url(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _UsageError("openai-compat needs extra['base_url']")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise _UsageError("base_url is not a valid URL") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise _UsageError("base_url must be an http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        # A key in the URL would be written wherever the URL is.
        raise _UsageError("base_url must not carry credentials")
    if parsed.query or parsed.fragment:
        raise _UsageError("base_url must not carry a query or a fragment")
    return value.rstrip("/")


def _write(path: Path | None, content: str, *, append: bool = False) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w", encoding="utf-8") as stream:
        stream.write(content)


class OpenAICompatProvider:
    """One registry name: a preset (``openrouter``, ``mistral``, ``nvidia``) or the generic one."""

    def __init__(self, name: str = GENERIC_NAME) -> None:
        if name != GENERIC_NAME and name not in PRESETS:
            raise ValueError(
                f"unknown openai-compat provider {name!r}; "
                f"valid names: {', '.join((*PRESETS, GENERIC_NAME))}"
            )
        self.name = name

    # ── configuration ─────────────────────────────────────────────────────

    def _endpoint(self, spec: RunSpec) -> tuple[str, str]:
        """``(base_url, key_env)`` for this run, or a usage error."""
        preset = PRESETS.get(self.name)
        if preset is not None:
            if CONFIGURATION_KEYS & spec.extra.keys():
                # The CLI rejects --base-url/--key-env with a preset; so does the
                # runtime, rather than silently ignoring the caller's endpoint.
                raise _UsageError(f"{self.name} fixes its own base_url and key_env")
            return preset.base_url, preset.key_env
        key_env = spec.extra.get("key_env")
        if not isinstance(key_env, str) or not key_env:
            raise _UsageError("openai-compat needs extra['key_env'] (a variable NAME)")
        return _validated_base_url(spec.extra.get("base_url")), key_env

    def _check(self, spec: RunSpec) -> tuple[str, str]:
        profile = spec.profile
        if profile.mcp is not None:
            raise ValueError(f"{self.name}: an HTTP provider has no tools; mcp is not supported")
        if profile.workspace is not None:
            raise ValueError(f"{self.name}: an HTTP provider cannot read a workspace")
        unknown = spec.extra.keys() - REQUEST_OPTIONS - CONFIGURATION_KEYS
        if unknown:
            raise _UsageError(f"unsupported request options: {', '.join(sorted(unknown))}")
        if not spec.model:
            raise _UsageError(f"{self.name} needs a model")
        return self._endpoint(spec)

    def _messages(self, spec: RunSpec) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        preamble = spec.context.preamble() if spec.context is not None else ""
        if preamble:
            messages.append({"role": "system", "content": preamble})
        messages.append({"role": "user", "content": spec.prompt})
        return messages

    # ── AgentProvider ─────────────────────────────────────────────────────

    def build_command(self, spec: RunSpec) -> list[str]:
        # -I: no PYTHONPATH, no user site -- the worker runs the INSTALLED package.
        return [spec.executable or sys.executable, "-I", "-m", WORKER_MODULE]

    def child_environment(self, spec: RunSpec, environ: Mapping[str, str]) -> dict[str, str] | None:
        child = scoped_environment(environ, passthrough=spec.profile.environment_passthrough)
        try:
            _, key_env = self._endpoint(spec)
        except _UsageError:
            return child
        # The key reaches the worker on stdin only, even if passthrough named it.
        child.pop(key_env, None)
        return child

    def prepare_home(self, spec: RunSpec) -> Path | None:
        return None

    def tool_call_completed(self, spec: RunSpec) -> bool:
        return False

    def run(self, spec: RunSpec) -> RunResult:
        spec = spec.with_run_dir_defaults()
        environ = spec.environment if spec.environment is not None else os.environ
        start = time.monotonic()
        try:
            base_url, key_env = self._check(spec)
        except _UsageError as exc:
            _write(spec.stderr_log, f"{exc}\n", append=True)
            return self._result(spec, INVALID_USAGE_EXIT_CODE, start)
        api_key = environ.get(key_env)
        if not api_key:
            _write(
                spec.stderr_log, f"missing required environment variable: {key_env}\n", append=True
            )
            return self._result(spec, 1, start)

        timeout = spec.effective_timeout_seconds()
        envelope = {
            "url": f"{base_url}/chat/completions",
            "api_key": api_key,
            "body": request_body(
                self.name,
                model=spec.model,
                messages=self._messages(spec),
                options={k: v for k, v in spec.extra.items() if k in REQUEST_OPTIONS},
            ),
            "timeout": timeout + SOCKET_TIMEOUT_MARGIN_SECONDS,
        }
        exit_code, outcome = self._call_worker(spec, environ, envelope, timeout)
        if outcome is None:
            return self._result(spec, exit_code, start)

        _write(
            spec.events_log,
            json.dumps(
                {
                    "ok": outcome.get("ok"),
                    "category": outcome.get("category"),
                    "status": outcome.get("status"),
                }
            )
            + "\n",
            append=True,
        )
        if not outcome.get("ok"):
            category, status = outcome.get("category"), outcome.get("status")
            _write(spec.stderr_log, f"request failed: {category} (HTTP {status})\n", append=True)
            return self._result(spec, self._failure_code(category, status), start)
        try:
            parsed = parse_completion(outcome["response"])
        except (ValueError, KeyError, TypeError):
            _write(spec.stderr_log, "request failed: malformed completion\n", append=True)
            return self._result(spec, 1, start)
        if parsed.text is not None:
            _write(spec.report_log, parsed.text)
        return self._result(spec, 0, start, parsed)

    # ── internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _failure_code(category: object, status: object) -> int:
        if category == "timeout":
            return TIMEOUT_EXIT_CODE
        if category == "unreachable":
            return PROVIDER_FALLBACK_EXIT_CODE
        if category == "http" and isinstance(status, int) and (status == 429 or status >= 500):
            return PROVIDER_FALLBACK_EXIT_CODE
        return 1

    def _call_worker(
        self,
        spec: RunSpec,
        environ: Mapping[str, str],
        envelope: Mapping[str, object],
        timeout: float,
    ) -> tuple[int, dict[str, Any] | None]:
        """Run the worker; ``(code, None)`` when it gave no usable outcome."""
        stderr_path = spec.stderr_log
        if stderr_path is not None:
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_target = stderr_path.open("a", encoding="utf-8") if stderr_path is not None else None
        try:
            try:
                process = subprocess.Popen(
                    self.build_command(spec),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr_target if stderr_target is not None else subprocess.DEVNULL,
                    env=self.child_environment(spec, environ),
                    text=True,
                    start_new_session=True,
                )
            except OSError as exc:
                _write(
                    stderr_path, f"unable to start the worker: {type(exc).__name__}\n", append=True
                )
                # Nothing was sent: the next link may run.
                return PROVIDER_FALLBACK_EXIT_CODE, None
            try:
                stdout, _ = process.communicate(input=json.dumps(envelope), timeout=timeout)
            except subprocess.TimeoutExpired:
                terminate_process_group(process)
                _write(stderr_path, f"deadline of {timeout:.1f} s reached\n", append=True)
                return TIMEOUT_EXIT_CODE, None
        finally:
            if stderr_target is not None:
                stderr_target.close()
        if process.returncode != 0:
            _write(stderr_path, f"worker exited {process.returncode}\n", append=True)
            return 1, None
        try:
            outcome = json.loads(stdout)
        except ValueError:
            _write(stderr_path, "worker returned no outcome\n", append=True)
            return 1, None
        if not isinstance(outcome, dict):
            return 1, None
        return 0, outcome

    def _result(
        self,
        spec: RunSpec,
        exit_code: int,
        start: float,
        parsed: ParsedCompletion | None = None,
    ) -> RunResult:
        return record(
            spec,
            RunResult(
                exit_code=exit_code,
                provider=self.name,
                model=spec.model,
                model_reported=parsed.model_reported if parsed is not None else None,
                report_path=spec.report_log,
                events_log=spec.events_log,
                stderr_log=spec.stderr_log,
                tokens=parsed.tokens if parsed is not None else None,
                cost_usd=parsed.cost_usd if parsed is not None else None,
                duration_seconds=time.monotonic() - start,
                tool_call_completed=False,
                text=parsed.text if parsed is not None and exit_code == 0 else None,
                run_id=run_id_of(spec),
                context=None if spec.context is None else tuple(spec.context.to_list()),
            ),
        )
