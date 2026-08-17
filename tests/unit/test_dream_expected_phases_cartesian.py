"""L'attendu devient `{phase} × {projet du pool}` — sinon il se désarme seul.

Spec `2026-08-08-dream-project-pool-design.md` §6, « la contrainte non
négociable qui vient avec ».

`expected_dream_phases()` transforme « phase armée » en « alarme si absente de
`dream_runs` ». C'est le mécanisme anti-crash-silencieux du 2026-05-02, quand
deux crashes de PROMOTE sont passés inaperçus deux jours.

À plusieurs projets, il **se désarme tout seul** : si un seul projet saute
`promote`, la phase reste « observée » globalement grâce aux autres, et
l'alarme ne sonne plus. Le mécanisme ne casse pas bruyamment — il devient
silencieusement inutile, ce qui est le pire des deux.

La bascule est conditionnée à la CONNAISSANCE du pool. Tant que le drop-in ne
porte pas `BRAIN_DREAM_PROJECT_POOL`, la paire n'est pas calculable et le
comportement d'aujourd'hui est conservé à l'identique : c'est ce qui rend ce
lot livrable sans qu'une nuit change.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.dream import post_run_alert

from brain_v42.dream_killswitches import parse_project_pool
from brain_v42.metrics.collector_dream import (
    expected_dream_phase_pairs,
    expected_dream_phases,
)

_DROP_IN_WITH_POOL = """\
[Service]
Environment=BRAIN_DREAM_PROMOTE_ENABLED=true
Environment=BRAIN_DREAM_REORG_ENABLED=true
Environment=BRAIN_DREAM_EXTRACT_ENABLED=true
Environment=BRAIN_DREAM_PROJECT_POOL=brain-v42,red,red-lab:architect
"""

_DROP_IN_WITHOUT_POOL = """\
[Service]
Environment=BRAIN_DREAM_PROMOTE_ENABLED=true
Environment=BRAIN_DREAM_REORG_ENABLED=true
"""


# --- Le parseur de pool -----------------------------------------------------


def test_the_pool_is_read_from_the_drop_in_and_split_on_commas() -> None:
    assert parse_project_pool(_DROP_IN_WITH_POOL) == [
        "brain-v42",
        "red",
        "red-lab:architect",
    ]


def test_an_absent_pool_key_yields_an_empty_list() -> None:
    """Pas « brain-v42 par défaut ».

    Le parseur ne peut pas connaître le positionnel de `ExecStart=`. Rendre une
    valeur devinée ferait fabriquer des attentes pour un projet que la nuit n'a
    peut-être jamais servi — une alarme inventée, exactement ce que le docstring
    d'`expected_dream_phases` refuse pour un drop-in illisible.
    """
    assert parse_project_pool(_DROP_IN_WITHOUT_POOL) == []


def test_a_quoted_whitespace_value_does_not_silently_become_one_key() -> None:
    """`Environment="…=a b"` arrive entier, avec son blanc.

    Le traiter comme une clé unique fabriquerait un `project_key` que
    canonicalize_project_key rejette. Ici on rend les deux clés, comme
    `dream.sh` rendrait la main en exit 2 : dans les deux cas, la forme
    espace-séparée ne rétrécit pas le pool en silence.
    """
    content = '[Service]\nEnvironment="BRAIN_DREAM_PROJECT_POOL=alpha beta"\n'

    assert parse_project_pool(content) == ["alpha", "beta"]


def test_the_killswitch_flags_still_parse_next_to_a_list_valued_key() -> None:
    """La clé de liste ne doit pas empoisonner le `dict[str, bool]` partagé.

    `parse_killswitches` coerce par `value.lower() == "true"` : une clé de liste
    qui y entrerait deviendrait `False` et éteindrait une phase dans le
    briefing de session et dans `/metrics`, sans toucher la nuit.
    """
    from brain_v42.dream_killswitches import parse_killswitches

    flags = parse_killswitches(_DROP_IN_WITH_POOL)

    assert flags == {"promote": True, "reorg": True, "extract": True}


# --- Le produit cartésien ---------------------------------------------------


def test_loop_phases_are_multiplied_by_the_pool_and_globals_are_not(
    tmp_path: Path,
) -> None:
    """`promote`/`reorg` par projet ; `extract`/`roadmap`/`sweep` une fois.

    Les trois globales n'ont pas de dimension de projet — elles écrivent la
    sentinelle `'*'` dans `dream_runs.project_key`, et l'attendu doit parler la
    même langue que ce qu'il compare.
    """
    drop_in = tmp_path / "killswitches.conf"
    drop_in.write_text(_DROP_IN_WITH_POOL, encoding="utf-8")

    assert expected_dream_phase_pairs(drop_in) == {
        ("promote", "brain-v42"),
        ("promote", "red"),
        ("promote", "red-lab:architect"),
        ("reorg", "brain-v42"),
        ("reorg", "red"),
        ("reorg", "red-lab:architect"),
        ("extract", "*"),
    }


def test_without_a_pool_the_pairs_are_empty_and_the_flat_set_is_unchanged(
    tmp_path: Path,
) -> None:
    """La propriété qui rend le lot livrable sans qu'une nuit change."""
    drop_in = tmp_path / "killswitches.conf"
    drop_in.write_text(_DROP_IN_WITHOUT_POOL, encoding="utf-8")

    assert expected_dream_phase_pairs(drop_in) == set()
    assert expected_dream_phases(drop_in) == {"promote", "reorg"}


def test_an_unreadable_drop_in_expects_nothing(tmp_path: Path) -> None:
    """Même posture que `expected_dream_phases` : ne jamais fabriquer d'alarme."""
    assert expected_dream_phase_pairs(tmp_path / "absent.conf") == set()


# --- Le désarmement, qui est tout l'objet du lot ---------------------------


def test_one_project_missing_promote_still_alerts_when_the_others_ran_it() -> None:
    """LE défaut. Sans les paires, cette nuit-là est verte.

    `red` n'a pas de ligne `promote`. `brain-v42` en a une. Comparé sur les noms
    de phase seuls, `promote` est « observé » et l'absence de `red` disparaît.
    """
    rows = [
        {"phase": "promote", "status": "done", "project_key": "brain-v42"},
        {"phase": "scan", "status": "done", "project_key": "red"},
    ]

    failed = post_run_alert.include_missing_expected_phases(
        rows,
        set(),
        persisted_failures=[],
        expected_pairs={("promote", "brain-v42"), ("promote", "red")},
    )

    assert len(failed) == 1
    assert failed[0]["phase"] == "promote"
    assert failed[0]["project_key"] == "red"
    assert failed[0]["status"] == "partial"


def test_a_fully_observed_cartesian_expectation_is_silent() -> None:
    rows = [
        {"phase": "promote", "status": "done", "project_key": "brain-v42"},
        {"phase": "promote", "status": "done", "project_key": "red"},
    ]

    failed = post_run_alert.include_missing_expected_phases(
        rows,
        set(),
        persisted_failures=[],
        expected_pairs={("promote", "brain-v42"), ("promote", "red")},
    )

    assert failed == []


def test_the_flat_path_survives_untouched_when_no_pairs_are_supplied() -> None:
    """Régression : sans pool, le comportement d'aujourd'hui, à l'identique."""
    rows = [{"phase": "scan", "status": "done"}]

    failed = post_run_alert.include_missing_expected_phases(
        rows, {"promote", "scan"}, persisted_failures=[]
    )

    assert len(failed) == 1
    assert failed[0]["phase"] == "promote"


# --- §11 : le rapport se lit, à cinquante lignes comme à cinq --------------


def test_the_report_groups_its_lines_by_project() -> None:
    """Sans groupement, l'échec d'un projet est noyé dans une liste plate."""
    failed = [
        {"phase": "synth", "status": "fail", "project_key": "red", "error_message": "boom"},
        {"phase": "scan", "status": "fail", "project_key": "brain-v42", "error_message": "bam"},
        {"phase": "reorg", "status": "fail", "project_key": "red", "error_message": "bim"},
        {"phase": "extract", "status": "fail", "project_key": "*", "error_message": "bum"},
    ]

    report = post_run_alert.build_alert_insight(__import__("datetime").date(2026, 8, 10), failed)

    assert "red:" in report
    assert "brain-v42:" in report
    # La sentinelle se lit comme ce qu'elle est : les phases sans projet.
    assert "global:" in report


def test_the_per_project_cap_cannot_let_one_project_hide_another() -> None:
    """`MAX_REPORTED_FAILURES = 20` était dimensionné pour 9 phases par nuit.

    À dix projets la nuit compte 63 phases : un plafond global laisserait le
    premier projet consommer les vingt lignes et « N additional records
    omitted » masquerait des projets ENTIERS. Le plafond est donc par projet.
    """
    failed = [
        {
            "phase": f"phase-{index}",
            "status": "fail",
            "project_key": "noisy",
            "error_message": "boom",
        }
        for index in range(40)
    ] + [
        {
            "phase": "synth",
            "status": "fail",
            "project_key": "quiet",
            "error_message": "the one that matters",
        }
    ]

    report = post_run_alert.build_alert_insight(__import__("datetime").date(2026, 8, 10), failed)

    assert "the one that matters" in report, (
        "un projet bruyant a évincé un projet silencieux du rapport"
    )
    assert "omitted" in report


@pytest.mark.parametrize("phase", ["extract", "roadmap", "sweep"])
def test_the_three_global_phases_are_never_multiplied(tmp_path: Path, phase: str) -> None:
    drop_in = tmp_path / "killswitches.conf"
    drop_in.write_text(
        f"[Service]\nEnvironment=BRAIN_DREAM_{phase.upper()}_ENABLED=true\n"
        "Environment=BRAIN_DREAM_PROJECT_POOL=a,b,c\n",
        encoding="utf-8",
    )

    assert expected_dream_phase_pairs(drop_in) == {(phase, "*")}
