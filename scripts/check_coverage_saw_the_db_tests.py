#!/usr/bin/env python3
"""Refuser une couverture mesurée sur un sous-ensemble du code testé.

`tests/conftest.py::require_test_db_url()` fait SAUTER tout test adossé à une
base quand `BRAIN_V42_TEST_DB_URL` est absente. C'est une garde juste — elle
existe pour qu'un `pytest tests/unit` local ne pollue pas la production — mais
elle a un angle mort en CI : **un test qui saute ne fait pas rougir un job**.

Le job `test-coverage` tournait sans service Postgres, sans cette variable et
sans schéma appliqué. Ses tests adossés à une base sautaient donc en silence, et
le pourcentage publié décrivait un sous-ensemble du code réellement testé sans
que rien ne le dise à qui le lisait. Mesuré le 2026-08-22 : **60 tests sautés**.

Ce garde-fou est le vrai livrable du ticket `f779092b`, pas le pourcentage
corrigé : réparer le chiffre seul le laisserait redériver au prochain écart de
recette entre les deux jobs.

**Il ne compare aucun compte à un nombre gravé**, et c'est délibéré. Le
commentaire du workflow annonçait « 51 tests », le ticket « 55 », la mesure en
trouve « 60 » — trois nombres, trois dates, trois mensonges programmés. Un seuil
serait le quatrième. On exige seulement qu'AUCUN test n'ait sauté POUR CETTE
CAUSE, ce qui reste vrai quel que soit le nombre de tests adossés à une base
demain.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

#: Le motif de saut qu'on refuse. On le reconnaît par le NOM DE LA VARIABLE que
#: `require_test_db_url()` cite dans son message : c'est la seule chaîne stable
#: entre la garde et ce contrôle. Un saut pour une autre raison — service GPU
#: absent, plateforme — reste légitime et n'est pas compté ici.
_DB_SKIP_MARKER = "BRAIN_V42_TEST_DB_URL"

_MAX_NAMED = 10


def db_skipped_tests(report: Path) -> list[str]:
    """Rendre les tests que le rapport JUnit dit sautés faute de base."""
    root = ET.parse(report).getroot()
    return [
        f"{case.get('classname')}::{case.get('name')}"
        for case in root.iter("testcase")
        for skipped in case.findall("skipped")
        if _DB_SKIP_MARKER in (skipped.get("message") or "")
    ]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <junit-xml>", file=sys.stderr)
        return 2
    report = Path(argv[1])
    if not report.is_file():
        # Fail-closed : un rapport absent veut dire qu'on ne SAIT PAS si les
        # tests ont tourné. Passer ici rendrait ce contrôle creux exactement
        # dans le cas où pytest s'est effondré avant d'écrire son rapport.
        print(
            f"missing JUnit report {report} — cannot prove the DB-backed tests ran", file=sys.stderr
        )
        return 1

    skipped = db_skipped_tests(report)
    if not skipped:
        print("No DB-backed test was skipped: coverage covers the same world as test-unit.")
        return 0

    print(
        f"{len(skipped)} DB-backed test(s) were SKIPPED, so this coverage describes "
        "a subset of the tested code. Give this job the test-unit recipe: the "
        "postgres service, BRAIN_V42_TEST_DB_URL, and `alembic upgrade head`.",
        file=sys.stderr,
    )
    for name in skipped[:_MAX_NAMED]:
        print(f"  - {name}", file=sys.stderr)
    if len(skipped) > _MAX_NAMED:
        # Pas de troncature muette : une liste coupée sans son reste se lit
        # « il n'y en avait que dix ».
        print(f"  ... and {len(skipped) - _MAX_NAMED} more", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
