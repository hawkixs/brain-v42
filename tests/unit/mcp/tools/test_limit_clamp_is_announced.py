"""Un plafond de `limit` appliqué en silence fait mentir le résultat.

Ticket af3b58dd, item 4. `brain_search`, `brain_list` et `brain_list_adrs` ramènent
`limit` dans [1, 100] par `max(1, min(limit, 100))` et rendent la page sans rien dire.
Un appelant qui demande 500 en reçoit 100 et ne peut pas distinguer « il n'y en avait
que 100 » de « il y en avait 500 et on t'en montre 100 ».

C'est le dépôt qui se contredit lui-même : dans le MÊME fichier,
`_format_plan_detail` documente « no content is silently dropped — the notice names
the number of omitted chunks ». La règle existe, ces trois chemins ne la suivent pas.

Le contrat CLAMP n'est pas remis en cause au profit du rejet dur de
`brain_session_list` : pour un appelant LLM, un refus coûte un aller-retour là où un
plafond annoncé coûte une phrase. Ce qui est corrigé, c'est le SILENCE, pas le plafond.
"""

from __future__ import annotations

import pytest

from brain_v42.mcp.tools.formatters import LIST_LIMIT_MAX, clamp_list_limit


class TestClampListLimit:
    def test_a_request_within_the_cap_is_returned_untouched_and_silent(self) -> None:
        """Le cas nominal ne doit produire AUCUN bruit.

        Sans cette assertion, on pourrait « corriger » en annonçant le plafond à
        chaque appel — le lecteur cesserait de lire la notice, exactement la dérive
        que ce dépôt documente ailleurs pour l'alarme qui sonne toutes les nuits.
        """
        value, notice = clamp_list_limit(20)

        assert value == 20
        assert notice == ""

    def test_a_request_above_the_cap_says_so(self) -> None:
        value, notice = clamp_list_limit(500)

        assert value == LIST_LIMIT_MAX
        assert str(LIST_LIMIT_MAX) in notice
        assert "500" in notice, "la notice doit rappeler ce qui a été DEMANDÉ"

    @pytest.mark.parametrize("asked", [0, -1, -100])
    def test_a_non_positive_request_says_so_too(self, asked: int) -> None:
        """Zéro rendait une page vide en silence — indiscernable d'un corpus vide."""
        value, notice = clamp_list_limit(asked)

        assert value == 1
        assert notice, f"limit={asked} a été corrigé sans le dire"

    def test_the_cap_is_the_one_the_tools_actually_apply(self) -> None:
        """Contrôle positif : un plafond découplé des tools ne garderait rien."""
        assert LIST_LIMIT_MAX == 100


class TestToolsAnnounceTheClamp:
    """Le témoin comportemental, sur les trois tools que le ticket nomme."""

    @staticmethod
    def _sources() -> list[str]:
        from pathlib import Path

        root = Path(__file__).resolve().parents[4] / "src" / "brain_v42" / "mcp" / "tools"
        return [
            (root / name).read_text(encoding="utf-8")
            for name in ("brain_tools.py", "crud_tools.py")
        ]

    def test_no_list_path_still_clamps_silently(self) -> None:
        """La sonde NÉGATIVE : elle retombe si quelqu'un recode le clamp en dur.

        RED avant correctif : `max(1, min(limit, 100))` apparaît trois fois.
        """
        offenders = [
            f"{index}: {line.strip()}"
            for index, source in enumerate(self._sources())
            for line in source.splitlines()
            if "min(limit, 100)" in line
        ]

        assert offenders == [], (
            "un plafond de limit est appliqué en dur, donc en silence ; utiliser "
            "clamp_list_limit qui rend aussi la notice :\n" + "\n".join(offenders)
        )
