"""The French briefing line of a fact — rendered output, driven by the declared policy.

Data on one side (`Measured` / `Unreadable`), rendering on the other: this
module is the only place a fact becomes prose, and the prose never says more
than the probe proved. The first fact proves the PostgreSQL side of the
projection — an outbox with no observed backlog, a lease armed and held — and
nothing about Neo4j's content, which is why "à jour" is not a word of this
module. Loudness comes from the fact's declared policy (`late_after_seconds`),
never from a constant of the renderer, so the briefing and a lot B claim read
one definition of "late".

Unreadable renders; it never vanishes. Silence would send the reader back to
the prose this section exists to contradict.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from brain_v42.facts.model import Measured, Measurement, Unreadable
from brain_v42.facts.probe import FactDescriptor

#: A cached reading older than this says its age; younger ones stay quiet.
AGE_SUFFIX_AFTER_SECONDS = 60
#: The generic line cuts the value there: visible, ugly, and therefore fixed quickly.
GENERIC_VALUE_CHARS = 120

#: The one unreadable case a reader must never mistake for a transient.
_UNREADABLE_LABELS: Mapping[str, str] = {"target_mismatch": "cible inattendue"}


def _int(value: object) -> int:
    """The value schema guarantees an int; anything else renders as zero, never crashes."""
    return value if type(value) is int else 0


def _duration(seconds: int) -> str:
    """`4j 2h`, `12 min`, `45 s` — coarse on purpose: a lag is not a stopwatch."""
    if seconds >= 86400:
        days, rest = divmod(seconds, 86400)
        hours = rest // 3600
        return f"{days}j {hours}h" if hours else f"{days}j"
    if seconds >= 3600:
        hours, rest = divmod(seconds, 3600)
        minutes = rest // 60
        return f"{hours}h {minutes:02d}" if minutes else f"{hours}h"
    if seconds >= 60:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def _render_graph_projection_lag(measured: Measured, descriptor: FactDescriptor) -> str:
    value = measured.value
    late_after = int(descriptor.policies.get("late_after_seconds", 300))
    lag = _int(value.get("lag_seconds"))
    pending = _int(value.get("pending"))
    exhausted = _int(value.get("exhausted"))
    healthy = value.get("healthy") is True
    generation = value.get("generation")
    armed = value.get("armed") is True
    lease_active = value.get("lease_active") is True
    recovery_active = value.get("recovery_active") is True

    details = [f"{pending} en attente"]
    if generation is None:
        details.append("sans bail")
    else:
        details.append(f"génération {generation} {'armée' if armed else 'NON armée'}")
        details.append("bail tenu" if lease_active else "bail perdu")
    if exhausted:
        details.append(f"{exhausted} épuisées")
    if recovery_active:
        details.append("récupération en cours")

    loud = not healthy or lag > late_after or exhausted > 0
    if not loud:
        head = "aucun retard observé dans l'outbox"
    elif lag > late_after:
        head = f"EN RETARD de {_duration(lag)}"
    else:
        head = "EN RETARD"
    return f"- Projection graphe : {head} — {', '.join(details)}"


_RENDERERS: Mapping[str, Callable[[Measured, FactDescriptor], str]] = {
    "graph_projection_lag": _render_graph_projection_lag,
}


def _render_generic(measured: Measured, descriptor: FactDescriptor) -> str:
    text = measured.value_json
    if len(text) > GENERIC_VALUE_CHARS:
        text = text[:GENERIC_VALUE_CHARS] + "…"
    return f"- {descriptor.name} : {text}"


def _render_unreadable(unreadable: Unreadable, descriptor: FactDescriptor) -> str:
    label = _UNREADABLE_LABELS.get(unreadable.error_code, unreadable.error_code)
    subject = "Projection graphe" if descriptor.name == "graph_projection_lag" else descriptor.name
    return f"- {subject} : illisible ({label})"


def render_fact_line(
    measurement: Measurement, descriptor: FactDescriptor, *, age_seconds: float | None
) -> str:
    """One line for the `### État technique (mesuré)` section.

    `age_seconds` is the monotonic age of a cached reading as the registry
    knows it (never wall-clock arithmetic on `measured_at`); past a minute the
    line says it, so a reader knows the number is a memory, not a measurement
    taken for them.
    """
    if isinstance(measurement, Unreadable):
        line = _render_unreadable(measurement, descriptor)
    else:
        line = _RENDERERS.get(descriptor.name, _render_generic)(measurement, descriptor)
    if age_seconds is not None and age_seconds >= AGE_SUFFIX_AFTER_SECONDS:
        line += f" (mesuré il y a {_duration(int(age_seconds))})"
    return line
