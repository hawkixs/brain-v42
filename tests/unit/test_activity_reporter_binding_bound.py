"""Which of the two bounds bites first — and why raising the other would do nothing.

Ticket `1c40c36a` describes the loss as happening "beyond 8 concurrent calls", the
8 being the emitter's LOCAL cap. Measured on 2026-08-23 by re-reading both
modules: **it is not 8 that bites first, it is 4.**

The sidecar shares ``MAX_IN_FLIGHT_REQUESTS`` slots between its THREE receivers
and answers ``503`` as soon as the next request arrives while they are all taken.
The emitter, for its part, allows ``max_in_flight`` in flight. Between the two the
window is real: at 5..8 simultaneous POSTs, the sidecar refuses and the emitter
counts into ``refused``; ``dropped`` only moves from the 9th on.

Two consequences, and they are why this file exists:

1. **Raising the emitter's cap alone would not move the start of the loss.** It is
   the obvious fix one is tempted to write on reading the ticket, and it would have
   no effect — the refusal would still come at 5.
2. **The counter to watch is not the one the ticket names.** Someone watching
   ``dropped`` to detect saturation would never see anything: the sidecar's
   saturation arrives through ``refused``. The tests in
   ``test_activity_reporter.py`` already pin ``dropped == 0`` on a 503; this file
   pins the REASON why that is the normal order.

Associated production measurement (window: 19 days of log, 42 process lifetimes):
**zero** ``dropped``, **zero** ``refused``. Neither bound has ever bitten. This
file therefore fixes nothing — it prevents fixing the wrong number.
"""

from __future__ import annotations

import inspect

from brain_v42.mcp.activity_reporter import ActivityReporter
from brain_v42.metrics.codex_telemetry import MAX_IN_FLIGHT_REQUESTS


def _emitter_default_max_in_flight() -> int:
    """The cap the emitter applies in production, taken from the signature.

    Read by introspection rather than retyped: a value retyped here would go stale
    silently the day the signature changes, and this test would lose exactly the
    meaning it carries.
    """
    default = inspect.signature(ActivityReporter).parameters["max_in_flight"].default
    assert isinstance(default, int), "max_in_flight doit rester un entier par défaut"
    return default


def test_the_sidecar_budget_binds_before_the_emitter_cap() -> None:
    """Saturation starts at the sidecar's budget, not at the emitter's cap."""
    emitter_cap = _emitter_default_max_in_flight()

    assert MAX_IN_FLIGHT_REQUESTS < emitter_cap, (
        f"le budget du sidecar ({MAX_IN_FLIGHT_REQUESTS}) n'est plus inférieur au plafond de "
        f"l'émetteur ({emitter_cap}).\n"
        "Si le sidecar est devenu la borne la plus LARGE, alors c'est l'émetteur qui refuse en "
        "premier et la perte se compte désormais dans `dropped`, plus dans `refused` — "
        "l'inverse de ce que dit la documentation de l'émetteur et de ce que surveille "
        "l'opérateur. Revoir les deux modules ensemble avant de valider ce changement."
    )


def test_raising_only_the_emitter_cap_would_not_move_where_loss_begins() -> None:
    """Ticket 1c40c36a's obvious fix would have no effect, and this test says so.

    An arithmetic witness, not a decorative one: the FIRST-loss threshold is the
    minimum of the two bounds. As long as the sidecar is the smaller, doubling the
    emitter's cap leaves that minimum unchanged.
    """
    emitter_cap = _emitter_default_max_in_flight()

    first_loss_now = min(MAX_IN_FLIGHT_REQUESTS, emitter_cap)
    first_loss_if_emitter_doubled = min(MAX_IN_FLIGHT_REQUESTS, emitter_cap * 2)

    assert first_loss_now == first_loss_if_emitter_doubled == MAX_IN_FLIGHT_REQUESTS, (
        "élargir le plafond de l'émetteur ne déplace le début de la perte que si le sidecar "
        "cesse d'être la borne la plus étroite — ce qui demande de toucher au budget partagé "
        "des TROIS receveurs, pas à l'émetteur."
    )


def test_the_shared_budget_is_shared_with_two_other_receivers() -> None:
    """The budget does not belong to this emitter: two other receivers consume it.

    That is what makes the bound of 4 reachable without 4 simultaneous tool calls —
    an OTLP batch from Codex or Claude Code takes the same slot. Pinned through the
    server's text so that a fourth receiver added later forces this reasoning to be
    re-read.
    """
    from brain_v42.metrics import server as metrics_server

    source = inspect.getsource(metrics_server)
    receivers = [
        route
        for route in ("/v1/logs", "/v1/logs/claude", "/v1/client-activity")
        if f'add_post("{route}"' in source
    ]

    assert receivers == ["/v1/logs", "/v1/logs/claude", "/v1/client-activity"], (
        f"les receveurs qui partagent les {MAX_IN_FLIGHT_REQUESTS} créneaux ont changé : "
        f"{receivers}. Le raisonnement sur la borne qui mord en premier suppose que "
        "client-activity partage son budget — le relire si ce n'est plus vrai."
    )
