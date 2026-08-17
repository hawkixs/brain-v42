"""Le préfixe qui distingue une nuit DÉGRADÉE d'une nuit simplement bavarde.

Une phase Dream peut réussir en ayant été servie par son modèle de SECOURS :
`dream_runs.status` vaut `'done'`, et la phrase de dégradation voyage dans
`error_message` sans toucher au statut. C'est délibéré — un repli réussi n'est
pas un échec, et le confondre avec un échec a déjà coûté un ticket (`4480d3df`,
report confondu avec timeout).

Mais `error_message` n'est PAS réservée à la dégradation. `extract` y écrit
légitimement « N ticket(s) deferred or timed out before run deadline » sur des
runs `'done'`. Un lecteur qui se contenterait de « il y a un message » lirait
donc une nuit parfaitement propre comme une nuit dégradée. Le contrat porte sur
le PRÉFIXE, et sur lui seul.

NE PAS DÉSACCENTUER CETTE VALEUR, ni la réécrire « à l'identique » ailleurs.
Les lignes déjà en base la portent accentuée et il n'y a aucun backfill : une
variante ASCII n'orpheliner pas seulement les lignes passées, elle les rendrait
muettes sans qu'aucun test ne rougisse — le lecteur cesserait simplement de
trouver ce qu'il cherche. C'est pour ça que la valeur vit ici plutôt que dans
deux littéraux tenus d'accord par la discipline.

Ce module vit à la racine du paquet et n'importe RIEN, comme
`dream_run_project_key` et pour la même raison : le graphe de layering mesure
`_root: []`, et une seule arête sortante d'ici referme huit cycles et fait
sortir `scripts/check_module_layering.py` en `rc=2`, avant même pytest.

Et il vit sous `src/`, pas sous `scripts/`, parce que le sens autorisé est
`scripts → src`. Le `Dockerfile` ne copie jamais `scripts/` : un import
`src → scripts` serait vert en local, vert en CI, et casserait l'image de
production à l'import.
"""

from __future__ import annotations

# Écrit par `scripts/roadmap_curate.py::_degradation_notice`, lu par
# `brain_v42.services.dream_run_service`. Les deux côtés doivent le tenir de
# ce module — un littéral recopié dans le lecteur OU dans un test annule la
# garde, exactement comme la révision Alembic retapée du learning `8dc7e042`.
DEGRADED_PREFIX = "DÉGRADÉ"
