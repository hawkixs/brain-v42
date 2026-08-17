"""La sentinelle de projet des phases Dream globales.

`dream_runs.project_key` (migration 042) distingue trois états, et les trois
sont porteurs :

- `NULL` — ligne écrite AVANT la 042. Pour toujours : il n'y a eu aucun
  backfill, et il n'y en aura pas. Toute mesure rétrospective par projet sur
  les lignes d'avant est impossible, définitivement. (Aucun décompte n'est
  épinglé ici : `dream_runs` gagne six à neuf lignes chaque nuit à 06:00, et
  un chiffre écrit dans un commentaire est faux le lendemain matin.)
- `'*'` — phase GLOBALE. `extract`, `roadmap` et `sweep` sortent de la boucle
  et tournent une fois par nuit, pour personne en particulier : elles n'ont pas
  de projet à nommer. `RESONANCE` la pose aussi, bien qu'il soit mort et non
  câblé, pour ne pas être le seul écrivain incohérent le jour où quelqu'un le
  rebranche.
- une clé kebab-case — phase PAR PROJET, telle que reçue par l'orchestrateur.

Ce module vit à la racine du paquet et n'importe RIEN, volontairement. Le
graphe de layering mesure `_root: []` alors que huit sous-paquets ciblent la
racine : une seule arête sortante d'ici refermerait huit cycles et ferait
sortir `scripts/check_module_layering.py` en `rc=2`, avant même pytest.

Et il vit sous `src/`, pas sous `scripts/`, parce que le sens autorisé est
`scripts → src`. Le `Dockerfile` ne copie jamais `scripts/` : un import
`src → scripts` serait vert en local, vert en CI, et casserait l'image de
production à l'import.
"""

from __future__ import annotations

# NE PAS faire transiter cette valeur par `canonicalize_project_key` : son
# motif `^[a-z0-9]+([:-][a-z0-9]+)*$` la rejette. Sur les trois écrivains
# best-effort l'exception serait avalée par conception, et la colonne
# resterait NULL en silence, chaque nuit, sur les phases globales.
GLOBAL_PHASE_PROJECT_KEY = "*"
