"""Le câblage d'une instance FastMCP a UNE définition, pour deux consommateurs.

Ticket `83d8785b` : le banc metrics importait le singleton de module et
héritait des tools enregistrés par le banc mcp/ collecté avant lui — 20
« Component already exists » mesurés, chacun fermé sur un engine déjà
`dispose()`. Un vert obtenu sur ces fermetures-là ne prouve rien, et l'ordre
de collecte pytest devenait signifiant.

L'isolement passe par une factory dans `server.py`, PAS par un double câblage
dans le banc : la docstring de `build_server` a déjà tranché qu'un harnais qui
reproduit le câblage à la main finit vert à propos d'un serveur qui n'existe
nulle part. La factory porte le middleware de provenance (indispensable au
banc d'attribution) ; le singleton de production en est le premier produit.
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
    """Le middleware vit dans la factory, jamais dans le consommateur.

    C'est lui qui relaie `X-Brain-Agent` : une instance de banc qui le
    perdrait rendrait l'attribution silencieusement vide, le mode de panne
    exact que le banc metrics existe pour mesurer.
    """
    for instance in (create_mcp_instance(), mcp):
        assert any(
            isinstance(middleware, ProvenanceMiddleware) for middleware in instance.middleware
        )


def test_the_module_singleton_is_a_product_of_the_factory() -> None:
    """`/health` s'ajoute par décorateur sur le singleton ; le reste du câblage
    doit être exactement celui de la factory — un divergent se verrait ici."""
    fresh = create_mcp_instance()

    assert type(mcp) is type(fresh)
    assert [type(m) for m in mcp.middleware] == [type(m) for m in fresh.middleware]
