"""Le câblage de `--project-key` dans dream.sh, épinglé par grep — et ses limites.

Deux propriétés à prouver, et une troisième à PROTÉGER :

  - le flag entre dans le tableau PARTAGÉ `parser_args`, donc dans les deux
    rails (codex par défaut, claude en repli). Le câbler dans une seule branche
    laisserait l'autre sortir en `argparse` code 2, avalé en `WARN … non-fatal` ;
  - la sous-commande `record-empty-pool` le reçoit aussi : `promote` est une
    phase PAR PROJET, et depuis le filtre de maturité de la 041 c'est ce
    chemin-là qui écrit sa ligne la plupart des nuits ;
  - les phases GLOBALES n'en reçoivent AUCUN. `test_dream_sh_sweep.py` et
    `test_dream_sh_extract.py` l'interdisent déjà ; les tests d'ici le redisent
    depuis l'autre côté, pour qu'un futur lot ne « complète par symétrie » pas
    trois blocs qui n'ont pas de projet à nommer.
"""

from pathlib import Path

import pytest

_DREAM_SH = Path(__file__).parent.parent.parent / "scripts" / "dream.sh"
_FLAG = '--project-key "$PROJECT_KEY"'


def _content() -> str:
    return _DREAM_SH.read_text(encoding="utf-8")


def _between(content: str, start: str, end: str) -> str:
    assert start in content, f"marqueur de début absent : {start!r}"
    tail = content.split(start, maxsplit=1)[1]
    assert end in tail, f"marqueur de fin absent après {start!r} : {end!r}"
    return tail.split(end, maxsplit=1)[0]


@pytest.fixture
def parser_block() -> str:
    return _between(_content(), "local parser_args=(", 'return "$phase_rc"')


def test_the_flag_enters_the_shared_argument_array(parser_block: str) -> None:
    """Dans `parser_args`, donc avant la bifurcation entre rails.

    C'est la seule position qui les sert TOUS d'un seul geste. Le tableau est
    construit une fois et consommé une fois par rail — ils étaient deux, ils
    sont trois depuis l'arrivée d'agy, et cette position n'a pas eu à bouger.
    """
    assignment = parser_block.split("local scan_log=", maxsplit=1)[0]

    assert _FLAG in assignment


def test_every_rail_consumes_the_same_array(parser_block: str) -> None:
    """Preuve que la position ci-dessus suffit : aucun rail ne se construit ses
    propres arguments.

    Le compte est dérivé des parsers présents, pas figé à un nombre. Un
    quatrième rail qui se bricolerait ses arguments passerait sous un littéral
    mis à jour à la main — c'est exactement ce que ce test doit attraper.
    """
    parsers = ("agy_dream_parser", "codex_dream_parser", "brain_v42.metrics.dream_parser")
    for parser in parsers:
        assert parser in parser_block, parser

    assert parser_block.count('"${parser_args[@]}"') == len(parsers)


def test_the_empty_pool_row_is_recorded_for_a_named_project() -> None:
    empty_branch = _between(
        _content(),
        'if [[ "$pool_size" -eq 0 ]]; then',
        "export PROMOTE_CANDIDATE_POOL_JSON",
    )

    assert "scripts.dream._promote_helpers record-empty-pool" in empty_branch
    assert _FLAG in empty_branch


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("--- SWEEP", "=== Dream finished"),
        ("--- EXTRACT", "--- ROADMAP"),
        ("--- ROADMAP", "--- SWEEP"),
    ],
    ids=["sweep", "extract", "roadmap"],
)
def test_the_global_phases_receive_no_project_flag(start: str, end: str) -> None:
    """Une phase globale n'a pas de projet à nommer : sa sentinelle `'*'` vit
    dans son code Python, pas sur sa ligne de commande."""
    block = _between(_content(), start, end)

    assert "--project-key" not in block
