"""Sonde de vivacité des modèles configurés — item (3) du ticket 911bb6f5.

Ce ticket avait délibérément différé cet item, avec une condition explicite :
« À rouvrir si un deuxième EOL passe. » Il est passé. Le remplaçant choisi le
2026-08-05 après canary — `deepseek-ai/deepseek-v4-flash` — a atteint sa fin de
vie le 2026-08-07, deux jours plus tard, et la nuit du 2026-08-10 est repartie
sur son secours 8B. Le ticket 2fad6cc5 de red-arena le signale dans les mêmes
termes.

La machinerie construite le 05 a FONCTIONNÉ : la ligne `DÉGRADÉ` est bien dans le
rapport et `dream_runs.model` est renseigné. Le défaut restant n'est plus le
silence, c'est la LATENCE — on apprend la mort d'un modèle en lisant le rapport
du lendemain matin, après une nuit servie en dégradé sur dix projets.

Un 410 n'est pas une erreur transitoire : aucun retry ne le réparera jamais. Une
sonde hors run permet de le savoir AVANT la nuit, et de choisir un remplaçant sur
mesure plutôt que sur la fiche du fournisseur.

Cette sonde n'est câblée à aucun run : c'est un outil d'opérateur, et le ticket
notait que le canary d'origine vivait dans `/tmp`.
"""

from __future__ import annotations

import httpx
import pytest
from scripts.probe_model_liveness import (
    Verdict,
    classify_status,
    configured_models,
    probe_models,
)


class TestConfiguredModels:
    def test_the_inventory_comes_from_the_modules_that_use_them(self) -> None:
        """Retaper la liste serait rejouer le défaut : deux vérités qui dérivent.

        Un modèle remplacé dans `roadmap_curate` et oublié ici produirait une
        sonde verte sur un modèle que plus personne n'appelle, pendant que le
        vrai primaire meurt sans être vu.
        """
        from scripts.roadmap_curate import DEFAULT_ROADMAP_MODEL

        models = configured_models()

        assert DEFAULT_ROADMAP_MODEL in {entry.model for entry in models}

    def test_every_entry_names_where_it_is_used(self) -> None:
        """Un verdict sans usage n'est pas actionnable : « lequel je remplace ? »."""
        for entry in configured_models():
            assert entry.used_by, f"{entry.model} ne dit pas qui l'utilise"

    def test_no_consumer_of_a_shared_model_is_invisible(self) -> None:
        """`extract` et `domain_backfill` partagent UNE constante.

        Elle est donc listée une seule fois — une seule valeur à remplacer — mais
        son entrée doit nommer les DEUX consommateurs. Sinon un opérateur qui lit
        « domain_backfill » croit ne casser qu'un backfill en changeant la valeur,
        alors qu'il déplace aussi la phase EXTRACT de la nuit.
        """
        entries = configured_models()
        shared = [e for e in entries if "extract" in e.used_by]

        assert shared, "le modèle d'extract a disparu de l'inventaire"
        assert any("backfill" in e.used_by for e in shared), (
            "l'entrée d'extract ne dit pas qu'elle sert aussi au backfill"
        )

    def test_the_extract_fallback_is_probed_as_its_own_site(self) -> None:
        """Un maillon DORMANT meurt sans signal : la nuit ne sonde que ce qu'elle
        exerce.

        Le secours d'extract n'est appelé que quand le primaire tombe. Tant qu'il
        était ÉGAL au primaire (promotion du 2026-08-21), la sonde le couvrait par
        coïncidence ; dès qu'il diverge, il redevient invisible — exactement le
        mode de panne mesuré la nuit du 2026-08-28, où le 410 du secours roadmap
        n'a été vu qu'au milieu de la nuit. L'inventaire doit donc nommer le SITE
        `ticket_extract.DEFAULT_EXTRACT_FALLBACK_MODEL`, pas espérer que sa valeur
        recoupe celle d'une autre entrée.
        """
        from scripts.ticket_extract import DEFAULT_EXTRACT_FALLBACK_MODEL

        sites = [
            e for e in configured_models() if "DEFAULT_EXTRACT_FALLBACK_MODEL" in e.used_by
        ]

        assert sites, "le secours d'extract n'a pas d'entrée propre dans l'inventaire"
        assert [e.model for e in sites] == [DEFAULT_EXTRACT_FALLBACK_MODEL]


class TestClassify:
    def test_410_is_gone_and_never_transient(self) -> None:
        """C'est la distinction qui vaut la sonde : aucun retry ne répare un EOL."""
        assert classify_status(410) is Verdict.GONE

    def test_200_is_alive(self) -> None:
        assert classify_status(200) is Verdict.ALIVE

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 529])
    def test_overload_is_busy_not_gone(self, status: int) -> None:
        """529 compris : il manquait à RETRYABLE_STATUS et renvoyait une nuit entière
        sur le secours (commit 0eda7e18). Le confondre avec un EOL ferait remplacer
        un modèle parfaitement vivant."""
        assert classify_status(status) is Verdict.BUSY

    def test_an_unknown_status_is_never_silently_alive(self) -> None:
        """Fail-closed : un 401 mal lu ferait conclure « tous les modèles sont morts »."""
        assert classify_status(401) is Verdict.OTHER


class TestProbe:
    @staticmethod
    def _client(statuses: dict[str, int]) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            import json

            model = json.loads(request.content)["model"]
            return httpx.Response(statuses[model], json={})

        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_a_dead_model_is_named_with_its_use_site(self) -> None:
        entries = configured_models()
        statuses = {entry.model: 200 for entry in entries}
        dead = entries[0].model
        statuses[dead] = 410

        results = probe_models(entries, client=self._client(statuses), api_key="k")

        gone = [r for r in results if r.verdict is Verdict.GONE]
        assert [r.entry.model for r in gone] == [dead]
        assert gone[0].entry.used_by

    def test_the_probe_never_writes_anything(self) -> None:
        """Lecture seule : le canary d'origine ne persistait rien, celui-ci non plus.

        `max_tokens` minimal et aucune persistance — une sonde qui écrirait dans
        la base ferait de la vérification un effet de bord.
        """
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            body = json.loads(request.content)
            seen.append(body)
            return httpx.Response(200, json={})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        probe_models(configured_models(), client=client, api_key="k")

        assert seen, "aucune requête émise"
        for body in seen:
            assert body["max_tokens"] <= 8, "la sonde consomme plus que nécessaire"

    def test_the_api_key_never_reaches_the_result(self) -> None:
        """Un verdict imprimé ou journalisé ne doit pas transporter le secret."""
        secret = "nvapi-SENTINEL-DO-NOT-LEAK"
        statuses = {entry.model: 200 for entry in configured_models()}

        results = probe_models(configured_models(), client=self._client(statuses), api_key=secret)

        assert secret not in repr(results)
