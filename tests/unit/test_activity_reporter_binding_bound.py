"""Laquelle des deux bornes mord en premier — et pourquoi relever l'autre ne ferait rien.

Le ticket `1c40c36a` décrit la perte comme survenant « au-delà de 8 appels
concurrents », le 8 étant le plafond LOCAL de l'émetteur. Mesuré le 2026-08-23 en
relisant les deux modules : **ce n'est pas 8 qui mord en premier, c'est 4.**

Le sidecar partage ``MAX_IN_FLIGHT_REQUESTS`` créneaux entre ses TROIS receveurs
et répond ``503`` dès que la requête suivante arrive alors qu'ils sont tous pris.
L'émetteur, lui, en autorise ``max_in_flight`` en vol. Entre les deux, la fenêtre
est réelle : à 5..8 POST simultanés, le sidecar refuse et l'émetteur compte dans
``refused`` ; ``dropped`` ne bouge qu'à partir du 9ᵉ.

Deux conséquences, et c'est pour elles que ce fichier existe :

1. **Relever le plafond de l'émetteur seul ne déplacerait pas le début de la
   perte.** C'est la correction évidente qu'on est tenté d'écrire en lisant le
   ticket, et elle serait sans effet — le refus viendrait toujours à 5.
2. **Le compteur à surveiller n'est pas celui que le ticket nomme.** Quelqu'un
   qui guette ``dropped`` pour détecter la saturation ne verrait jamais rien : la
   saturation du sidecar arrive par ``refused``. Les tests de
   ``test_activity_reporter.py`` épinglent déjà ``dropped == 0`` sur un 503 ;
   ce fichier-ci épingle la RAISON pour laquelle c'est l'ordre normal.

Mesure de production associée (fenêtre : 19 jours de journal, 42 durées de vie de
processus) : **zéro** ``dropped``, **zéro** ``refused``. Aucune des deux bornes
n'a jamais mordu. Ce fichier ne corrige donc rien — il empêche de corriger le
mauvais nombre.
"""

from __future__ import annotations

import inspect

from brain_v42.mcp.activity_reporter import ActivityReporter
from brain_v42.metrics.codex_telemetry import MAX_IN_FLIGHT_REQUESTS


def _emitter_default_max_in_flight() -> int:
    """Le plafond que l'émetteur applique en production, pris à la signature.

    Lu par introspection plutôt que recopié : une valeur recopiée ici
    vieillirait sans bruit le jour où la signature change, et ce test perdrait
    exactement le sens qu'il porte.
    """
    default = inspect.signature(ActivityReporter).parameters["max_in_flight"].default
    assert isinstance(default, int), "max_in_flight doit rester un entier par défaut"
    return default


def test_the_sidecar_budget_binds_before_the_emitter_cap() -> None:
    """La saturation commence au budget du sidecar, pas au plafond de l'émetteur."""
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
    """Le correctif évident du ticket 1c40c36a serait sans effet, et ce test le dit.

    Témoin arithmétique, pas décoratif : le seuil de PREMIÈRE perte est le
    minimum des deux bornes. Tant que le sidecar est le plus petit, doubler le
    plafond de l'émetteur laisse ce minimum inchangé.
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
    """Le budget n'appartient pas à cet émetteur : deux autres receveurs le consomment.

    C'est ce qui rend la borne de 4 atteignable sans 4 appels d'outil
    simultanés — un lot OTLP de Codex ou de Claude Code prend le même créneau.
    Épinglé par le texte du serveur pour qu'un quatrième receveur ajouté plus
    tard fasse relire ce raisonnement.
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
