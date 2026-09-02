#!/usr/bin/env python3
"""Probe the liveness of the configured NVIDIA models, outside any run.

Item (3) of ticket 911bb6f5, deferred on 2026-08-05 with an explicit condition —
"to reopen if a second EOL goes by". It did: `deepseek-v4-flash`, chosen by
canary that very day, reached its end of life two days later, and the night of
2026-08-10 fell back on its 8B standby.

The reporting machinery built then WORKS — the `DEGRADED` line does appear in the
report. What remains is the LATENCY: a model's death is learned by reading the
next day's report, after one degraded night across ten projects.

A 410 is not transient: no retry will ever repair it. Knowing it BEFORE the night
allows choosing a replacement on measurement rather than on the provider's
datasheet — and that is exactly the mistake the 08-05 canary had already avoided
once.

Read-only, wired into no run, no persistence.

Usage:
    set -a; . ~/.config/brain-v42/nvidia.env; set +a
    uv run python -m scripts.probe_model_liveness

Non-zero exit if at least one configured model is definitively absent.
"""

from __future__ import annotations

import enum
import os
import sys
from dataclasses import dataclass

import httpx

BASE_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
API_KEY_VAR = "BRAIN_NVIDIA_API_KEY"
PROBE_MAX_TOKENS = 8
# 90 s, not 30: gpt-oss-120b — a DORMANT link, exactly what this probe watches
# — answers in 75 s from a cold queue (measured 2026-08-29, then 2.6 s warm). At
# 30 s it returned OTHER every Monday, and OTHER exited 0: the unit stayed green
# on the one site it was not measuring.
PROBE_TIMEOUT_SECONDS = 90.0

# 529 is present deliberately: it was missing from RETRYABLE_STATUS and a single
# 529 sent a whole night onto the standby (commit 0eda7e18). Mistaking it for an
# EOL would have a perfectly live model replaced.
_BUSY_STATUSES = frozenset({429, 500, 502, 503, 504, 529})


class Verdict(enum.Enum):
    """What the probe can conclude — and what it refuses to conclude."""

    ALIVE = "alive"
    GONE = "gone"
    BUSY = "busy"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """One configured model, and the site that uses it.

    The usage site is not decorative: a verdict without it is not actionable,
    because it does not say which constant to replace.
    """

    model: str
    used_by: str


@dataclass(frozen=True, slots=True)
class ProbeResult:
    entry: ModelEntry
    status: int | None
    verdict: Verdict
    detail: str = ""


def configured_models() -> list[ModelEntry]:
    """Read the inventory FROM the modules that use it, never retype it.

    A list copied here would drift from the real configuration, and the probe
    would return green on a model nobody calls any more while the true primary
    dies unseen. That is the fault this repository corrects everywhere else:
    measure, do not copy.
    """
    from scripts.domain_backfill import DEFAULT_MODEL as DEFAULT_EXTRACT_MODEL
    from scripts.roadmap_curate import (
        DEFAULT_ROADMAP_FALLBACK_MODEL,
        DEFAULT_ROADMAP_MODEL,
        DEFAULT_WET_ROADMAP_FALLBACK_MODEL,
        DEFAULT_WET_ROADMAP_MODEL,
    )
    from scripts.ticket_extract import DEFAULT_EXTRACT_FALLBACK_MODEL

    def resolved(env_var: str, default: str, site: str) -> ModelEntry:
        """SITE precedence (env > default) — the unit loads nvidia.env.

        Without it, the probe would read the constant while the night serves the
        override: green on a model nobody calls any more. The site names the
        VARIABLE when it wins — that is what gets replaced then.
        """
        override = os.environ.get(env_var)
        if override:
            return ModelEntry(override, f"{site} — surchargé par {env_var}")
        return ModelEntry(default, site)

    return [
        resolved(
            "BRAIN_NVIDIA_ROADMAP_MODEL",
            DEFAULT_ROADMAP_MODEL,
            "roadmap_curate.DEFAULT_ROADMAP_MODEL (DRY primaire)",
        ),
        resolved(
            "BRAIN_NVIDIA_ROADMAP_FALLBACK_MODEL",
            DEFAULT_ROADMAP_FALLBACK_MODEL,
            "roadmap_curate.DEFAULT_ROADMAP_FALLBACK_MODEL (DRY secours)",
        ),
        resolved(
            "BRAIN_NVIDIA_ROADMAP_MODEL",
            DEFAULT_WET_ROADMAP_MODEL,
            "roadmap_curate.DEFAULT_WET_ROADMAP_MODEL (WET primaire)",
        ),
        resolved(
            "BRAIN_NVIDIA_ROADMAP_FALLBACK_MODEL",
            DEFAULT_WET_ROADMAP_FALLBACK_MODEL,
            "roadmap_curate.DEFAULT_WET_ROADMAP_FALLBACK_MODEL (WET secours)",
        ),
        resolved(
            "BRAIN_NVIDIA_MODEL",
            DEFAULT_EXTRACT_MODEL,
            "domain_backfill.DEFAULT_MODEL (extract + backfill)",
        ),
        # A dormant link: called only when the primary falls. Equal to the
        # primary it is covered by coincidence; divergent, it would be the one
        # constant neither the night nor the probe sees die.
        resolved(
            "BRAIN_NVIDIA_FALLBACK_MODEL",
            DEFAULT_EXTRACT_FALLBACK_MODEL,
            "ticket_extract.DEFAULT_EXTRACT_FALLBACK_MODEL (extract, secours)",
        ),
    ]


def classify_status(status: int) -> Verdict:
    """410 is definitive, 5xx/429 are transient, the rest is not guessed.

    Never fold an unknown status onto ALIVE: a misread 401 would conclude "every
    model is dead", and an optimistic fallback would do the opposite.
    """
    if status == 200:
        return Verdict.ALIVE
    if status == 410:
        return Verdict.GONE
    if status in _BUSY_STATUSES:
        return Verdict.BUSY
    return Verdict.OTHER


def probe_models(
    entries: list[ModelEntry], *, client: httpx.Client, api_key: str
) -> list[ProbeResult]:
    """One minimal request per model. No write, no persistence."""
    results: list[ProbeResult] = []
    for entry in entries:
        try:
            response = client.post(
                BASE_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": entry.model,
                    "messages": [{"role": "user", "content": "ok"}],
                    "max_tokens": PROBE_MAX_TOKENS,
                },
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            # The error type, never its text: a URL or a header can end up in
            # there, and this result is made to be printed.
            results.append(ProbeResult(entry, None, Verdict.OTHER, type(exc).__name__))
            continue
        detail = ""
        if classify_status(response.status_code) is Verdict.GONE:
            detail = "fin de vie chez le fournisseur — aucun retry ne la réparera"
        results.append(
            ProbeResult(entry, response.status_code, classify_status(response.status_code), detail)
        )
    return results


def exit_code_for(results: list[ProbeResult]) -> int:
    """GONE → 1, OTHER → 3, else 0. "I do not know" is not a green.

    Measured 2026-08-29: the dormant WET link answered past the probe timeout
    and exited 0 — the unit stayed green, every Monday, on the one site it had
    not measured. GONE dominates: a dead model is more urgent than an
    unreadable one. BUSY stays 0 — transient, and turning it into a failure
    would have a live model replaced (commit 0eda7e18).
    """
    if any(result.verdict is Verdict.GONE for result in results):
        return 1
    if any(result.verdict is Verdict.OTHER for result in results):
        return 3
    return 0


def _print_result(result: ProbeResult) -> None:
    status = result.status if result.status is not None else "—"
    line = f"{result.verdict.value.upper():<6} {status:<5} {result.entry.model}"
    print(f"{line}\n         {result.entry.used_by}", flush=True)
    if result.detail:
        print(f"         {result.detail}", flush=True)


def main() -> int:
    api_key = os.environ.get(API_KEY_VAR)
    if not api_key:
        print(f"{API_KEY_VAR} absente — source ~/.config/brain-v42/nvidia.env", file=sys.stderr)
        return 2

    # Entry by entry, the verdict printed AS IT COMES: an exception on site 5
    # must not carry away the four verdicts already earned — it is Monday
    # morning's log that says which constant to replace.
    results: list[ProbeResult] = []
    with httpx.Client() as client:
        for entry in configured_models():
            result = probe_models([entry], client=client, api_key=api_key)[0]
            _print_result(result)
            results.append(result)

    gone = [r for r in results if r.verdict is Verdict.GONE]
    if gone:
        print(f"\n{len(gone)} modèle(s) configuré(s) définitivement absent(s).", file=sys.stderr)
    unknown = [r for r in results if r.verdict is Verdict.OTHER]
    if unknown:
        print(
            f"{len(unknown)} verdict(s) OTHER — illisible n'est pas vivant, re-mesurer.",
            file=sys.stderr,
        )
    return exit_code_for(results)


if __name__ == "__main__":
    raise SystemExit(main())
