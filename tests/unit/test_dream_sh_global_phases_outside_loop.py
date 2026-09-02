"""The three global phases are OUTSIDE the per-project unit — pinned textually.

Spec `2026-08-08-dream-project-pool-design.md` §7: `extract`, `roadmap` and
`sweep` have no project dimension and sit outside the loop. The decisive
measurement, phase by phase:

- `sweep` — `session_sweep` exposes only `--wet` and `--older-than-days`. Over
  eight passes, the first abandons and the next seven write seven `done` rows
  over nothing, inflating `_clean_dry_streak` by seven fictitious nights.
- `extract` — `ticket_extract` selects `extraction_status = 'pending'` with no
  project filter. The first pass empties the queue, the next seven consume their
  `--run-budget-seconds 540` all the same.
- `roadmap` — `roadmap_curate` ALREADY does its own multi-project rotation, and
  `day_ordinal` is identical across the eight invocations: the same window would
  be curated eight times, at the night's highest API cost (259.9 s/night
  measured).

§7 requires this to be a **structural guarantee, not a convention**: "a convention
is lost at the first refactor; a textual anchor fails loudly". Hence this file. It
does not read a comment, it checks where the three blocks fall relative to the
body of the function that serves one project.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"

# Extraction anchors: real code, not comments. A rework that makes them disappear
# breaks this test with a ValueError, not by leaving it green.
_PROJECT_FN_OPEN = "run_project_phases() {"
_EXTRACT_ANCHOR = "# --- EXTRACT:"
_ROADMAP_ANCHOR = "# --- ROADMAP:"
_SWEEP_ANCHOR = "# --- SWEEP:"


def _source() -> str:
    return DREAM_SH.read_text(encoding="utf-8")


def _project_function_body() -> str:
    """Body of `run_project_phases`, from its `{` to its closing `}`.

    The closing brace is recognized by being in column 0: the script indents a
    function's whole body, so `\\n}` appears only at the end.
    """
    content = _source()
    start = content.index(_PROJECT_FN_OPEN)
    end = content.index("\n}\n", start)
    return content[start:end]


def test_the_six_agent_phases_live_in_a_per_project_function() -> None:
    """The phase loop is inside a function that receives a project.

    Extracting the loop into a function is not cosmetic: §9 counts five
    `continue`s belonging to the phase loop. Nesting a project loop AROUND them
    would turn them into `continue`s of the wrong loop — the project would move to
    the next one instead of the next phase, and the night would be green having
    done nothing. A function boundary makes that confusion impossible: the body
    has only one loop.
    """
    body = _project_function_body()

    assert 'for phase_spec in "${PHASES[@]}"' in body
    for phase in ("promote", "reorg", "connect"):
        assert phase in body, f"la phase {phase} a quitté l'unité par projet"


def test_extract_roadmap_and_sweep_are_outside_that_function() -> None:
    """The three global phases fall AFTER the per-project body, not inside it."""
    content = _source()
    body = _project_function_body()

    for anchor in (_EXTRACT_ANCHOR, _ROADMAP_ANCHOR, _SWEEP_ANCHOR):
        assert anchor in content, f"ancre disparue du script : {anchor}"
        assert anchor not in body, (
            f"{anchor} est entré dans le corps par projet — il tournerait une fois "
            "par projet du pool, ce que §7 mesure comme faux pour les trois."
        )


def test_the_three_global_blocks_run_after_the_project_loop() -> None:
    """Textual order: the project loop closes before the first global phase.

    Checking only "not inside the function" would let through a call placed BEFORE
    the loop, which would run over a corpus the night has not yet mutated.
    """
    content = _source()
    loop_end = content.index("done  # fin de la boucle de projets")

    for anchor in (_EXTRACT_ANCHOR, _ROADMAP_ANCHOR, _SWEEP_ANCHOR):
        assert content.index(anchor) > loop_end, (
            f"{anchor} précède la fermeture de la boucle de projets"
        )


def test_the_global_phase_logs_keep_no_project_component() -> None:
    """§3.2: the three global phases' logs are NOT projected.

    Seven templates gain a project component, these do not — projecting them would
    manufacture N empty files for a phase that runs once.
    """
    content = _source()

    for phase in ("extract", "roadmap", "sweep"):
        assert f'"$LOG_DIR/${{TIMESTAMP}}_{phase}.log"' in content, (
            f"le journal de {phase} a été projeté par projet, ou renommé"
        )
