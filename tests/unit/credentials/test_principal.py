"""Verified identity must remain independent of declared provenance."""

from contextvars import Context

from brain_v42 import provenance


def test_principal_defaults_to_none() -> None:
    assert Context().run(provenance.get_current_principal) is None


def test_principal_is_independent_of_actor() -> None:
    def request() -> None:
        provenance.set_current_principal("workstation-claude")
        provenance.set_current_actor("red-rail")
        assert provenance.get_current_principal() == "workstation-claude"
        provenance.set_current_principal(None)
        assert provenance.get_current_principal() is None
        assert provenance.get_current_actor() == "red-rail"

    Context().run(request)


def test_principal_context_does_not_leak() -> None:
    first, second = Context(), Context()
    first.run(provenance.set_current_principal, "red-rail")
    assert second.run(provenance.get_current_principal) is None
    assert first.run(provenance.get_current_principal) == "red-rail"


def test_transport_peer_is_context_local() -> None:
    first, second = Context(), Context()
    first.run(provenance.set_current_peer, "127.0.0.1")
    assert first.run(provenance.get_current_peer) == "127.0.0.1"
    assert second.run(provenance.get_current_peer) is None
