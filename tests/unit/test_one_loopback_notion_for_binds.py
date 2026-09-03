"""Three loopback predicates, three questions, and why unifying them is wrong.

Ticket `5987d4d9`, residue of `eac03668`. The ask was to collapse two predicates
into one. Measured, there are THREE, they answer three different questions, and
the difference between them is load-bearing. This module pins that, so the next
reader does not have to rediscover it -- and does not "tidy" it the way this lot
nearly did.

    host          config bind   metrics bind   metrics PEER   mcp Host header
    127.0.0.1     True          True           True           True
    ::1           True          True           True           True
    localhost     True          True           False          True
    127.42.0.9    False         True           True           False
    0.0.0.0       False         False          False          False

WHY `metrics bind` IS WIDER THAN `config bind`, and must stay so. A bind on
`127.42.0.9` is not reachable from any network: refusing to register the POST
receivers there would remove a working local ingestion path for no security gain.
`test_client_activity_endpoint.py` pins that as a POSITIVE control, deliberately,
and its negative control lists only genuinely reachable addresses (`0.0.0.0`,
`::`, `192.0.2.8`, `metrics.internal`). Tightening `metrics bind` onto the strict
predicate was tried in this lot and broke three tests that were RIGHT.

WHY `config bind` IS STRICTER, and must stay so. It guards four validators, and
two of them are EGRESS urls (`client_activity_url`, `otel_endpoint`), not binds.
Widening it to `127.0.0.0/8` would loosen where this process is willing to SEND,
which is a different risk from where it is willing to listen. Its own docstring
already reserves that: "widening a security predicate is not a tidy-up".

The cost of the disagreement, stated so nobody thinks it is free: `METRICS_HOST`
in `127.0.0.0/8` but not `127.0.0.1` needs `METRICS_ALLOW_NON_LOOPBACK` to start,
for an address that exposes nothing. That is an opt-in demanded without a threat
behind it -- an annoyance, not a hole, and the arbitration belongs to the
operator rather than to a worker's clean-up.

WHY THE PEER CHECK IS WIDER AGAIN. `_has_loopback_tcp_peer` asks "is the party
that CONNECTED local", and the whole of `127.0.0.0/8` is same-host by
construction. Tightening it would refuse a legitimate local client, and blindly:
measured 2026-09-03, the sidecar logs no peer address and `/metrics` reports
`receiver_rejections` empty for all three receivers, so such a client would break
with nothing having recorded that it ever existed.

WHY `mcp.http_security._LOOPBACK_HOSTS` IS STRICT FOR A THIRD REASON. It
validates a HOST HEADER an attacker controls, against DNS rebinding. Same word,
three threat models.
"""

from __future__ import annotations

import pytest

from brain_v42.config import _is_loopback_host
from brain_v42.mcp import http_security
from brain_v42.metrics import server as metrics_server


@pytest.mark.parametrize(
    "host,config_bind,metrics_bind,metrics_peer",
    [
        ("127.0.0.1", True, True, True),
        ("::1", True, True, True),
        ("localhost", True, True, False),
        ("127.42.0.9", False, True, True),
        ("127.0.0.2", False, True, True),
        ("0.0.0.0", False, False, False),  # noqa: S104 - the value under test
        ("192.0.2.8", False, False, False),
    ],
)
def test_the_three_predicates_keep_their_intended_answers(
    host: str, config_bind: bool, metrics_bind: bool, metrics_peer: bool
) -> None:
    """The table above, executable.

    A change to any column is a DECISION, not a refactor: this test is where it
    has to be argued.
    """
    assert _is_loopback_host(host) is config_bind
    assert metrics_server._is_loopback_bind(host) is metrics_bind
    assert metrics_server._is_loopback_ip(host) is metrics_peer


def test_the_divergence_is_confined_to_the_loopback_range() -> None:
    """Outside `127.0.0.0/8` the three agree — the gap is bounded, not sprawling.

    This is what makes the open arbitration small: no address that is reachable
    from a network is judged differently by any of them.
    """
    for reachable in ("0.0.0.0", "192.0.2.8", "10.1.2.3", "::"):  # noqa: S104
        assert _is_loopback_host(reachable) is False
        assert metrics_server._is_loopback_bind(reachable) is False
        assert metrics_server._is_loopback_ip(reachable) is False


def test_the_host_header_check_stays_strict_against_rebinding() -> None:
    """The third notion, and the one whose threat model is not shared."""
    assert "127.0.0.1" in http_security._LOOPBACK_HOSTS
    assert "127.42.0.9" not in http_security._LOOPBACK_HOSTS
