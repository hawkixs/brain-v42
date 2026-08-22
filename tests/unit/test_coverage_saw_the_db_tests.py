"""Le garde-fou qui refuse une couverture mesurée sur un sous-ensemble.

Il vaut mieux que le pourcentage qu'il protège : un test qui SAUTE ne fait pas
rougir un job, donc sans lui la CI reste verte sur une couverture partielle.
C'est exactement l'état qu'avait `test-coverage` — pas de service Postgres, pas
de `BRAIN_V42_TEST_DB_URL`, donc 60 tests sautés en silence (mesuré 2026-08-22).

Les fixtures ci-dessous sont des rapports JUnit MINIMAUX écrits à la main, et
c'est assumé : ce qui est sous test est la LECTURE du rapport, pas pytest. La
preuve que le contrôle mord sur de vrais rapports a été faite par mutation dans
les deux sens, sur les JUnit des deux mondes réels.
"""

from __future__ import annotations

from pathlib import Path

from scripts.check_coverage_saw_the_db_tests import db_skipped_tests, main

_DB_SKIP = "BRAIN_V42_TEST_DB_URL not set — skipping DB-backed tests to avoid polluting prod"


def _report(tmp_path: Path, *cases: str) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(f"<testsuites><testsuite>{''.join(cases)}</testsuite></testsuites>")
    return path


def _skipped(name: str, message: str) -> str:
    return f'<testcase classname="tests.unit.t" name="{name}"><skipped message="{message}"/></testcase>'


def _passed(name: str) -> str:
    return f'<testcase classname="tests.unit.t" name="{name}"/>'


class TestItRefusesASubset:
    def test_a_db_skip_fails_the_job(self, tmp_path: Path, capsys) -> None:
        report = _report(tmp_path, _passed("ok"), _skipped("needs_db", _DB_SKIP))
        assert main(["prog", str(report)]) == 1
        assert "1 DB-backed test(s) were SKIPPED" in capsys.readouterr().err

    def test_no_skip_passes(self, tmp_path: Path) -> None:
        """TÉMOIN NÉGATIF : sans lui, un contrôle qui rougit TOUJOURS passerait."""
        report = _report(tmp_path, _passed("ok"), _passed("also_ok"))
        assert main(["prog", str(report)]) == 0

    def test_an_unrelated_skip_is_left_alone(self, tmp_path: Path) -> None:
        """Un saut légitime — GPU absent, plateforme — n'est pas de son ressort.

        Sans cette borne, le garde-fou deviendrait « aucun saut nulle part » et
        rougirait pour des raisons qui n'ont rien à voir avec la couverture.
        """
        report = _report(tmp_path, _skipped("no_gpu", "embedding service unavailable"))
        assert main(["prog", str(report)]) == 0


class TestItFailsClosed:
    def test_a_missing_report_fails(self, tmp_path: Path, capsys) -> None:
        """Rapport absent = on ne SAIT PAS si les tests ont tourné.

        Passer ici rendrait le contrôle creux exactement dans le cas où pytest
        s'est effondré avant d'écrire son rapport.
        """
        assert main(["prog", str(tmp_path / "absent.xml")]) == 1
        assert "cannot prove" in capsys.readouterr().err

    def test_wrong_usage_is_not_a_pass(self, tmp_path: Path) -> None:
        assert main(["prog"]) == 2


class TestItNamesWhatItFound:
    def test_it_lists_the_skipped_tests(self, tmp_path: Path, capsys) -> None:
        report = _report(tmp_path, _skipped("needs_db", _DB_SKIP))
        main(["prog", str(report)])
        assert "tests.unit.t::needs_db" in capsys.readouterr().err

    def test_a_long_list_declares_what_it_truncates(self, tmp_path: Path, capsys) -> None:
        """AUCUN PLAFOND SILENCIEUX : une liste coupée sans son reste ment."""
        report = _report(tmp_path, *(_skipped(f"t{i}", _DB_SKIP) for i in range(25)))
        main(["prog", str(report)])
        assert "... and 15 more" in capsys.readouterr().err


def test_the_parser_reads_the_marker_not_the_wording(tmp_path: Path) -> None:
    """Il reconnaît le saut par le NOM DE LA VARIABLE, seule chaîne stable.

    Le libellé de `require_test_db_url()` peut être réécrit ; le nom de la
    variable qu'il cite, non — c'est le contrat entre la garde et ce contrôle.
    """
    report = _report(tmp_path, _skipped("x", "please export BRAIN_V42_TEST_DB_URL first"))
    assert db_skipped_tests(report) == ["tests.unit.t::x"]
