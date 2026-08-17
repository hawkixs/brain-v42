"""Les trois phases globales sont HORS de l'unité par projet — épinglé textuellement.

Spec `2026-08-08-dream-project-pool-design.md` §7 : `extract`, `roadmap` et
`sweep` n'ont aucune dimension de projet et sortent de la boucle. La mesure qui
tranche, phase par phase :

- `sweep` — `session_sweep` n'expose que `--wet` et `--older-than-days`. À huit
  passages, le premier abandonne et les sept suivants écrivent sept lignes
  `done` sur du vide, gonflant `_clean_dry_streak` de sept nuits fictives.
- `extract` — `ticket_extract` sélectionne `extraction_status = 'pending'` sans
  filtre de projet. Le premier passage vide la file, les sept suivants
  consomment quand même leur `--run-budget-seconds 540`.
- `roadmap` — `roadmap_curate` fait DÉJÀ sa propre rotation multi-projets, et
  `day_ordinal` est identique aux huit invocations : la même fenêtre serait
  curée huit fois, au prix API le plus élevé de la nuit (259,9 s/nuit mesurés).

§7 exige que ce soit une **garantie structurelle, pas une convention** : « une
convention se perd au premier refactor ; une ancre textuelle échoue
bruyamment ». D'où ce fichier. Il ne lit pas un commentaire, il vérifie où les
trois blocs tombent par rapport au corps de la fonction qui sert un projet.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"

# Ancres de découpe : du code réel, pas des commentaires. Un remaniement qui les
# fait disparaître casse ce test par ValueError, pas en le laissant vert.
_PROJECT_FN_OPEN = "run_project_phases() {"
_EXTRACT_ANCHOR = "# --- EXTRACT:"
_ROADMAP_ANCHOR = "# --- ROADMAP:"
_SWEEP_ANCHOR = "# --- SWEEP:"


def _source() -> str:
    return DREAM_SH.read_text(encoding="utf-8")


def _project_function_body() -> str:
    """Corps de `run_project_phases`, de son `{` à son `}` de fermeture.

    La fermeture se reconnaît à une accolade en colonne 0 : le script indente
    tout le corps d'une fonction, donc `\\n}` n'apparaît qu'à la fin.
    """
    content = _source()
    start = content.index(_PROJECT_FN_OPEN)
    end = content.index("\n}\n", start)
    return content[start:end]


def test_the_six_agent_phases_live_in_a_per_project_function() -> None:
    """La boucle de phases est dans une fonction qui reçoit un projet.

    Extraire la boucle en fonction n'est pas cosmétique : §9 relève cinq
    `continue` qui appartiennent à la boucle de phases. Imbriquer une boucle
    projet AUTOUR d'eux les transformerait en `continue` de la mauvaise boucle
    — le projet passerait au suivant au lieu de la phase suivante, et la nuit
    serait verte en n'ayant rien fait. Une frontière de fonction rend cette
    confusion impossible : le corps n'a qu'une boucle.
    """
    body = _project_function_body()

    assert 'for phase_spec in "${PHASES[@]}"' in body
    for phase in ("promote", "reorg", "connect"):
        assert phase in body, f"la phase {phase} a quitté l'unité par projet"


def test_extract_roadmap_and_sweep_are_outside_that_function() -> None:
    """Les trois globales tombent APRÈS le corps par projet, pas dedans."""
    content = _source()
    body = _project_function_body()

    for anchor in (_EXTRACT_ANCHOR, _ROADMAP_ANCHOR, _SWEEP_ANCHOR):
        assert anchor in content, f"ancre disparue du script : {anchor}"
        assert anchor not in body, (
            f"{anchor} est entré dans le corps par projet — il tournerait une fois "
            "par projet du pool, ce que §7 mesure comme faux pour les trois."
        )


def test_the_three_global_blocks_run_after_the_project_loop() -> None:
    """Ordre textuel : la boucle projet se ferme avant la première globale.

    Vérifier seulement « pas dans la fonction » laisserait passer un appel placé
    AVANT la boucle, qui tournerait sur un corpus que la nuit n'a pas encore
    muté.
    """
    content = _source()
    loop_end = content.index("done  # fin de la boucle de projets")

    for anchor in (_EXTRACT_ANCHOR, _ROADMAP_ANCHOR, _SWEEP_ANCHOR):
        assert content.index(anchor) > loop_end, (
            f"{anchor} précède la fermeture de la boucle de projets"
        )


def test_the_global_phase_logs_keep_no_project_component() -> None:
    """§3.2 : les journaux des trois globales ne sont PAS projetés.

    Sept gabarits gagnent une composante de projet, ceux-ci non — les projeter
    fabriquerait N fichiers vides pour une phase qui ne tourne qu'une fois.
    """
    content = _source()

    for phase in ("extract", "roadmap", "sweep"):
        assert f'"$LOG_DIR/${{TIMESTAMP}}_{phase}.log"' in content, (
            f"le journal de {phase} a été projeté par projet, ou renommé"
        )
