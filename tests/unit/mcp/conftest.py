"""Hygiène de processus pour les tests qui traversent le vrai ``_run_mcp``.

``_install_session_idle_timeout`` substitue un symbole DANS le module de
FastMCP, faute de point d'extension public pour l'échéance d'inactivité des
sessions avec état. En production c'est sans conséquence : un seul processus,
une seule installation, au démarrage. Dans une suite de tests, non — plusieurs
tests appellent le vrai ``_run_mcp`` dans le même interpréteur, et la
substitution survivrait au test qui l'a provoquée.

Mesuré en écrivant ce chantier : sans cette restauration, cinq tests de
``test_dream_capability_http.py`` échouaient alors qu'ils passaient tous
isolément — le symptôme classique d'un état de processus qui fuit d'un test à
l'autre, et le genre d'échec qu'on impute à tort au test suivant.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _restore_fastmcp_session_manager() -> Iterator[None]:
    """Rendre à FastMCP son gestionnaire de sessions après chaque test."""
    from fastmcp.server import http as fastmcp_http

    original = fastmcp_http.StreamableHTTPSessionManager
    try:
        yield
    finally:
        fastmcp_http.StreamableHTTPSessionManager = original
