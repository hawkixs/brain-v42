"""La nuit rapproche elle-même ses phases OK de ses lignes écrites (b95c5742).

Les 15-16/08, la boucle a signé « 61/63 phases OK » pendant que `dream_runs`
ne recevait que 2 lignes — 240 `InvalidPasswordError` avalées en best-effort.
Depuis, l'absence d'une ligne `done` ne prouve pas un échec de phase : elle
peut prouver un INSERT perdu, et toute analyse de fiabilité menée sur la table
seule est fausse dans le sens pessimiste.

L'INSERT reste best-effort — c'est la leçon de la 042, un `NOT NULL` y ferait
un avertissement imprimé sur tous — mais l'écart devient VISIBLE : dream.sh
passe son compteur `OK_TOTAL` à `post_run_alert`, qui imprime une ligne
machine `RECONCILIATION phases_ok=N pairs_written=M gap=K`. Un `gap` non nul
au matin est exactement la perte des 15-16/08, lisible sans croiser le journal.

Le repli in-band du manifeste (e30a1cec) est gardé au même endroit : quand la
ligne COVERAGE dit `mode=fallback` alors que dream.sh vient d'écrire son
manifeste, le moteur le DIT (FAIL) et le grave (record_coverage_gap) — sans
toucher au code de sortie : le rapporteur garde son « jamais 2 » (paires
indécidables), c'est le SEUL appelant qui sait que le manifeste devait exister
qui escalade, et il n'escalade que la visibilité.
"""

from __future__ import annotations

from pathlib import Path

from scripts.dream.post_run_alert import format_reconciliation_line

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = (REPOSITORY_ROOT / "scripts" / "dream.sh").read_text(encoding="utf-8")


def _row(phase: str, status: str, project_key: str | None) -> dict[str, object]:
    return {"phase": phase, "status": status, "project_key": project_key}


def test_a_night_that_loses_an_insert_produces_a_nonzero_gap() -> None:
    """Le scénario des 15-16/08 : 61 phases OK, 2 lignes — gap 59, lisible."""
    rows = [_row("extract", "done", "*"), _row("roadmap", "done", "*")]

    line = format_reconciliation_line(61, rows)

    assert line == "RECONCILIATION phases_ok=61 pairs_written=2 gap=59"


def test_a_complete_night_reconciles_to_zero() -> None:
    rows = [
        _row("extract", "done", "*"),
        _row("clean", "done", "brain-v42"),
        _row("reorg", "done", "brain-v42"),
    ]

    line = format_reconciliation_line(3, rows)

    assert line.endswith("gap=0")


def test_a_fallback_retry_counts_its_pair_once() -> None:
    """« dream_runs compte des tentatives, dream.sh compte des phases » :
    l'attempt codex mort + le rattrapage gemini font DEUX lignes, UNE paire —
    sans quoi les six nuits de secours d'août auraient toutes un gap négatif."""
    rows = [
        _row("clean", "fail", "brain-v42"),
        _row("clean", "done", "brain-v42"),
    ]

    line = format_reconciliation_line(1, rows)

    assert line.endswith("pairs_written=1 gap=0")


def test_a_partial_row_is_a_written_row() -> None:
    """`partial` = la phase a écrit PUIS le validateur l'a invalidée : la
    ligne existe, la compter perdue déclencherait une chasse à l'INSERT sur
    chaque nuit où G4 fait son travail."""
    rows = [_row("reorg", "partial", "brain-v42")]

    line = format_reconciliation_line(1, rows)

    assert line.endswith("pairs_written=1 gap=0")


def test_pure_failure_rows_do_not_count_as_written_success() -> None:
    """Une paire qui n'a QUE des échecs n'explique pas une phase OK."""
    rows = [
        _row("clean", "fail", "brain-v42"),
        _row("connect", "timeout", "brain-v42"),
    ]

    line = format_reconciliation_line(2, rows)

    assert line.endswith("pairs_written=0 gap=2")


def test_a_negative_gap_is_printed_never_masked() -> None:
    """Plus de paires écrites que de phases OK (skips enregistrés, rejeux) :
    l'écart s'imprime tel quel — un clamp à zéro serait un compteur qui ment."""
    rows = [
        _row("promote", "done", "red-lab"),
        _row("promote", "done", "brain-v42"),
    ]

    line = format_reconciliation_line(1, rows)

    assert line.endswith("gap=-1")


# ---------------------------------------------------------------------------
# Le câblage moteur — dream.sh est du shell, son contrat est textuel, comme
# pour les validateurs (test_dream_sh_reorg_validator.py).
# ---------------------------------------------------------------------------


def test_dream_sh_passes_its_own_ok_counter() -> None:
    assert '--phases-ok "$OK_TOTAL"' in DREAM_SH


def test_dream_sh_logs_the_reconciliation_and_warns_on_gap() -> None:
    assert "grep -m1 '^RECONCILIATION '" in DREAM_SH
    assert 'log "=== dream_runs $reconciliation_line ==="' in DREAM_SH
    # Le WARN ne tire que sur écart non nul — une nuit saine reste silencieuse.
    assert '"$reconciliation_line" != *" gap=0"*' in DREAM_SH


def test_dream_sh_records_an_in_band_fallback_durably() -> None:
    """e30a1cec : le rapporteur garde son « jamais 2 » ; c'est dream.sh — le
    seul qui SAIT avoir écrit un manifeste quelques minutes plus tôt — qui
    grave le repli (FAIL au journal + ligne dream_runs `coverage`), sans
    toucher au code de sortie de la nuit."""
    assert '"$coverage_line" == *"mode=fallback"*' in DREAM_SH
    fallback_block = DREAM_SH.split('*"mode=fallback"*', 1)[1].split("fi\n", 1)[0]
    assert "record_coverage_gap" in fallback_block
    assert "FAIL " in fallback_block
    assert "alert_rc" not in fallback_block, (
        "le repli in-band ne touche PAS au code de sortie — escalade de "
        "visibilité, jamais de rouge sur une indécidable"
    )
