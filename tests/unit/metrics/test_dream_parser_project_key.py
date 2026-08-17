"""Les DEUX parsers exigent `--project-key` et le font descendre jusqu'au bind SQL.

Il n'y a qu'un site d'`INSERT` (`dream_parser._insert_dream_run`) mais deux points
d'entrée en ligne de commande, et `dream.sh` leur construit un tableau
d'arguments UNIQUE (`dream.sh:315-317`). Le rail réellement emprunté en
production est `codex_dream_parser` — `BRAIN_DREAM_AGENT_PROVIDER` vaut `codex`
par défaut (`dream.sh:19`) — et il n'avait jusqu'ici aucun test de CLI.

N'enseigner le flag qu'à `dream_parser` répare donc le rail de repli et casse le
rail vivant : `argparse` sort en 2, `set -euo pipefail` propage, et `dream.sh`
journalise `WARN codex_dream_parser failed for $name (non-fatal)`. La nuit perd
ses six lignes par projet, en silence. C'est ce que ces tests interdisent.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.metrics import codex_dream_parser, dream_parser

_INSERT = re.compile(
    r"INSERT\s+INTO\s+dream_runs\s*\((?P<cols>[^)]*)\)\s*VALUES\s*\((?P<vals>[^)]*)\)",
    re.IGNORECASE | re.DOTALL,
)


def _insert_shape(sql: str) -> tuple[list[str], list[str]]:
    """Colonnes et placeholders de l'INSERT, dans l'ordre où ils sont écrits."""
    match = _INSERT.search(sql)
    assert match is not None, f"INSERT dream_runs introuvable dans : {sql}"
    columns = [part.strip() for part in match.group("cols").split(",")]
    placeholders = [part.strip() for part in match.group("vals").split(",")]
    return columns, placeholders


_COMMON = [
    "--phase",
    "scan",
    "--model",
    "gpt-5.6-sol",
    "--date",
    "2026-08-09",
    "--status",
    "done",
    "--duration",
    "12",
]


@pytest.mark.parametrize("module", [dream_parser, codex_dream_parser], ids=["claude", "codex"])
def test_both_parsers_refuse_to_run_without_a_project_key(module: object) -> None:
    with pytest.raises(SystemExit) as excinfo:
        module._build_arg_parser().parse_args([*_COMMON, "log.txt"])

    assert excinfo.value.code == 2


@pytest.mark.parametrize("module", [dream_parser, codex_dream_parser], ids=["claude", "codex"])
def test_neither_parser_offers_a_default_project_key(module: object) -> None:
    """Un défaut `brain-v42` est exactement la classe de bug visée : il
    étiquetterait la nuit d'un autre projet sans que rien ne le signale."""
    action = next(
        candidate
        for candidate in module._build_arg_parser()._actions
        if "--project-key" in candidate.option_strings
    )

    assert action.required is True
    assert action.default is None


@pytest.mark.parametrize("module", [dream_parser, codex_dream_parser], ids=["claude", "codex"])
def test_both_parsers_accept_the_flag_dream_sh_will_pass(module: object) -> None:
    args = module._build_arg_parser().parse_args(
        [*_COMMON, "--project-key", "red-shrik", "log.txt"]
    )

    assert args.project_key == "red-shrik"


@pytest.mark.asyncio
async def test_insert_dream_run_binds_the_project_key_before_phase_dry_run() -> None:
    """`project_key` est `$14`, `phase_dry_run` reste le DERNIER lié.

    L'ordre n'est pas cosmétique : `test_dream_parser_phase_dry_run` épingle
    `bind_args[-1] is True` pour prouver que le drapeau de répétition à blanc
    n'est pas perdu. Insérer la clé en fin de `VALUES` rendrait ce pin rouge
    pour une raison sans rapport avec ce qu'il garde.
    """
    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock()
    mock_conn.close = AsyncMock()

    with patch(
        "brain_v42.metrics.dream_parser.asyncpg.connect", new=AsyncMock(return_value=mock_conn)
    ):
        await dream_parser._insert_dream_run(
            run_date="2026-08-09",
            phase="scan",
            model="gpt-5.6-sol",
            status="done",
            duration_s=12.0,
            telemetry=None,
            error_message=None,
            phase_dry_run=True,
            project_key="red-shrik",
        )

    sql, *bind_args = mock_conn.execute.await_args.args
    columns, placeholders = _insert_shape(sql)

    # Colonne ↔ placeholder ↔ argument, joints — jamais lus comme trois faits
    # séparés. Les quatre écrivains `sa.text` ont cette garde (le zip de
    # `_written_project_key`) ; celui-ci, qui écrit le plus de lignes, ne
    # l'avait pas : permuter la seule liste de colonnes laissait les assertions
    # vertes tout en liant `project_key` au booléen et `phase_dry_run` à la clé.
    assert len(columns) == len(placeholders) == len(bind_args) == 15
    assert placeholders == [f"${index + 1}" for index in range(15)], (
        "les placeholders doivent être en ordre : sinon l'index d'une colonne "
        "ne dit plus quel argument elle reçoit"
    )
    assert bind_args[columns.index("project_key")] == "red-shrik"
    assert bind_args[columns.index("phase_dry_run")] is True
    assert columns.index("phase_dry_run") == 14, "phase_dry_run reste le dernier lié"


@pytest.mark.asyncio
async def test_insert_dream_run_demands_the_project_key() -> None:
    """Aucun défaut au niveau de la fonction non plus : les deux `main()`
    doivent la fournir, sinon l'oubli d'un seul appelant passerait inaperçu."""
    with pytest.raises(TypeError, match="project_key"):
        await dream_parser._insert_dream_run(  # type: ignore[call-arg]
            run_date="2026-08-09",
            phase="scan",
            model="gpt-5.6-sol",
            status="done",
            duration_s=12.0,
            telemetry=None,
        )


@pytest.mark.parametrize(
    ("module", "name"),
    [(dream_parser, "dream_parser"), (codex_dream_parser, "codex_dream_parser")],
    ids=["claude", "codex"],
)
def test_both_mains_forward_the_project_key_to_the_insert(
    module: object, name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """Le test qui compte : le flag ne sert à rien s'il s'arrête à `argparse`."""
    inserted = AsyncMock()
    monkeypatch.setattr(module, "_insert_dream_run", inserted)
    monkeypatch.setattr(
        "sys.argv",
        [name, *_COMMON, "--project-key", "red-shrik", str(tmp_path / "absent.log")],
    )

    module.main()

    assert inserted.await_args.kwargs["project_key"] == "red-shrik"
