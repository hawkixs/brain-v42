"""Les trois lookups de `dream_runs.id` filtrent sur le projet.

Spec `2026-08-08-dream-project-pool-design.md` §12, l'argument qui impose la 042
AVANT la boucle : « Trois lecteurs ÉCRIVENT sur la ligne qu'ils ont mal
identifiée : promote_validate marque `partial` et backfille
dream_promotions.dream_run_id, connect_validate marque `partial`, REORG_RUN_ID
idem. Livrer la boucle d'abord produirait un audit des promotions faux et non
réparable — l'attribution correcte n'est pas récupérable depuis les lignes une
fois écrite. »

Les trois font `WHERE phase = X AND run_date = Y ORDER BY id DESC LIMIT 1`.

À plusieurs projets, ça sélectionne la ligne du DERNIER projet ayant écrit cette
phase aujourd'hui. Dans la boucle séquentielle actuelle c'est fortuitement le
bon : chaque projet écrit sa ligne puis relit immédiatement. Mais la correction
repose alors sur « personne n'écrit entre mon écriture et ma lecture » —
un invariant que rien n'impose, que la boucle ne déclare pas, et dont la
violation produit un `partial` posé sur le projet voisin.

La 042 est livrée. Le filtre est disponible. Ces tests l'exigent.
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from scripts.dream import _promote_helpers, connect_validate


def _rendered(statement: sa.Select) -> str:
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


def test_promote_run_id_lookup_filters_on_the_project() -> None:
    statement = _promote_helpers.dream_run_id_statement(dt.date(2026, 8, 10), "red")
    rendered = _rendered(statement)

    assert "dream_runs.project_key = 'red'" in rendered
    assert "dream_runs.phase = 'promote'" in rendered


def test_connect_run_id_lookup_filters_on_the_project() -> None:
    statement = connect_validate.connect_run_id_statement(dt.date(2026, 8, 10), "red-lab")
    rendered = _rendered(statement)

    assert "dream_runs.project_key = 'red-lab'" in rendered
    assert "dream_runs.phase = 'connect'" in rendered


def test_the_lookups_still_order_by_id_desc() -> None:
    """Le filtre s'AJOUTE, il ne remplace pas la désambiguïsation des re-runs.

    Un projet peut avoir deux lignes le même jour (re-run manuel après une
    panne). `ORDER BY id DESC LIMIT 1` reste le seul moyen de prendre la
    dernière.
    """
    for statement in (
        _promote_helpers.dream_run_id_statement(dt.date(2026, 8, 10), "red"),
        connect_validate.connect_run_id_statement(dt.date(2026, 8, 10), "red"),
    ):
        rendered = _rendered(statement)
        assert "ORDER BY dream_runs.id DESC" in rendered
        assert "LIMIT 1" in rendered


def test_the_project_key_is_required_on_both_clis() -> None:
    """Sans défaut, comme les trois écrivains.

    Un `default="brain-v42"` ici marquerait `partial` sur brain-v42 pendant que
    la phase de `red` a échoué — le contraire de ce que le validateur croit
    faire, et sans trace exploitable après coup.
    """
    import pytest

    with pytest.raises(SystemExit):
        _promote_helpers.main(["dream-run-id", "--date", "2026-08-10"])

    with pytest.raises(SystemExit):
        connect_validate.main(["--report-log", "/dev/null", "--run-date", "2026-08-10"])


def test_the_reorg_lookup_in_dream_sh_filters_on_the_project() -> None:
    """Le troisième vit en SQLAlchemy inline dans dream.sh, pas dans un module.

    Il n'a pas de témoin Python possible ailleurs qu'ici : c'est du texte de
    script. L'ancre échoue bruyamment si la requête est réécrite sans le filtre.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "scripts" / "dream.sh"
    content = source.read_text(encoding="utf-8")

    reorg_query_start = content.index("dream_runs.c.phase == 'reorg'")
    reorg_query = content[reorg_query_start : reorg_query_start + 400]

    assert "dream_runs.c.project_key ==" in reorg_query, (
        "la requête REORG_RUN_ID de dream.sh ne filtre pas sur le projet — "
        "elle marquerait `partial` sur la ligne d'un autre projet du pool"
    )


def test_the_inline_python_in_dream_sh_still_compiles() -> None:
    """Le témoin qui manquait, et son absence a coûté un bug.

    Ce programme voyage dans `uv run python -c "…"`. Il vit donc en COLONNE 0
    à l'intérieur d'un script dont tout le reste est indenté — et la mise en
    fonction de la boucle de phases lui a ajouté deux espaces à chaque ligne.
    Résultat : `IndentationError`, `REORG_RUN_ID` vide, `--dream-run-id` absent,
    et le validateur REORG qui perd sa capacité à marquer la ligne `partial`.

    Rien ne l'aurait vu. `bash -n` ne voit qu'une chaîne. Aucun test n'exécutait
    ce programme. La nuit serait restée verte en perdant une garde.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "scripts" / "dream.sh").read_text(
        encoding="utf-8"
    )
    start = source.index('uv run python -c "') + len('uv run python -c "')
    end = source.index('\n" 2>>', start)
    program = source[start:end]

    # Les interpolations shell deviennent des littéraux plausibles : on teste
    # la FORME du programme, pas la valeur du jour.
    program = program.replace("$TIMESTAMP", "2026-08-10").replace("$PROJECT_KEY", "brain-v42")

    compile(program, "dream.sh:inline", "exec")


def test_the_dream_script_passes_the_project_to_both_helper_clis() -> None:
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "scripts" / "dream.sh"
    content = source.read_text(encoding="utf-8")

    dream_run_id_call = content[content.index("_promote_helpers dream-run-id") :][:300]
    assert '--project-key "$PROJECT_KEY"' in dream_run_id_call

    connect_call = content[content.index("scripts.dream.connect_validate") :][:400]
    assert '--project-key "$PROJECT_KEY"' in connect_call
