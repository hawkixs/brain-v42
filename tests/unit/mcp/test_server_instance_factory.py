"""The wiring of a FastMCP instance has ONE definition, for two consumers.

Ticket `83d8785b`: the metrics bench imported the module singleton and inherited
the tools registered by the mcp/ bench collected before it — 20 measured
"Component already exists", each closed on an already `dispose()`d engine. A green
obtained on those closures proves nothing, and pytest's collection order became
meaningful.

Isolation goes through a factory in `server.py`, NOT through duplicate wiring in
the bench: `build_server`'s docstring has already settled that a harness
reproducing the wiring by hand ends up green about a server that exists nowhere.
The factory carries the provenance middleware (indispensable to the attribution
bench); the production singleton is its first product.
"""

from __future__ import annotations

from brain_v42.mcp.provenance_middleware import ProvenanceMiddleware
from brain_v42.mcp.server import create_mcp_instance, mcp


def test_each_call_builds_a_new_isolated_instance() -> None:
    first = create_mcp_instance()
    second = create_mcp_instance()

    assert first is not second
    assert first is not mcp
    assert second is not mcp


def test_every_product_carries_the_provenance_middleware() -> None:
    """The middleware lives in the factory, never in the consumer.

    It is what relays `X-Brain-Agent`: a bench instance losing it would make
    attribution silently empty, the exact failure mode the metrics bench exists to
    measure.
    """
    for instance in (create_mcp_instance(), mcp):
        assert any(
            isinstance(middleware, ProvenanceMiddleware) for middleware in instance.middleware
        )


def test_the_module_singleton_is_a_product_of_the_factory() -> None:
    """`/health` is added by decorator on the singleton; the rest of the wiring
    must be exactly the factory's — a divergence would show up here."""
    fresh = create_mcp_instance()

    assert type(mcp) is type(fresh)
    assert [type(m) for m in mcp.middleware] == [type(m) for m in fresh.middleware]
