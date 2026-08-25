#!/usr/bin/env python3
"""Baseline Phase 0 — produit un snapshot JSON daté, en LECTURE SEULE.

Usage :
    python3 docs/design/refonte-projets-sessions/baseline/snapshot.py
    python3 .../snapshot.py --stdout          # n'écrit rien, affiche
    python3 .../snapshot.py --container NAME  # autre conteneur Postgres

Ce script REJOUE la mesure ; il ne la recopie pas. C'est toute sa raison d'être :
chaque nombre de ce chantier est périssable, et le mode de panne documenté du
dossier est de citer une mesure morte. Un snapshot porte sa date DANS son nom de
fichier et DANS son contenu.

LECTURE SEULE, GARANTIE STRUCTURELLEMENT : la requête est encadrée par
`BEGIN READ ONLY` / `COMMIT`. Postgres refuse toute écriture dans une telle
transaction — ce n'est pas une intention, c'est le moteur qui l'impose.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
QUERIES = HERE / "queries.sql"
DEFAULT_CONTAINER = "brain_v42_postgres"
DEFAULT_USER = "brain"
DEFAULT_DB = "brain"


def run_snapshot(container: str, user: str, db: str) -> dict:
    """Jouer les requêtes dans UNE transaction read-only et rendre le JSON."""
    payload = f"BEGIN READ ONLY;\n{QUERIES.read_text(encoding='utf-8')}\nCOMMIT;\n"
    proc = subprocess.run(
        ["docker", "exec", "-i", container, "psql", "-U", user, "-d", db, "-Atq"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise SystemExit(f"psql a échoué (rc={proc.returncode}) :\n{proc.stderr}")
    out = proc.stdout.strip()
    if not out:
        raise SystemExit(f"psql n'a rien rendu. stderr :\n{proc.stderr}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"sortie psql non-JSON : {exc}\n{out[:2000]}") from exc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--container", default=DEFAULT_CONTAINER)
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--stdout", action="store_true", help="afficher sans écrire")
    args = ap.parse_args()

    snap = run_snapshot(args.container, args.user, args.db)
    # La date vient de la BASE mesurée, jamais de l'horloge locale : c'est la
    # seule qui soit dans la même transaction que les nombres qu'elle date.
    stamp = snap["measured_at_utc"].replace(":", "").replace("-", "")
    text = json.dumps(snap, indent=2, ensure_ascii=False, sort_keys=True)

    if args.stdout:
        print(text)
        return 0

    target = HERE / f"snapshot-{stamp}.json"
    target.write_text(text + "\n", encoding="utf-8")
    print(f"snapshot écrit : {target.relative_to(HERE.parents[3])}")
    print(f"  mesuré à     : {snap['measured_at_utc']}")
    print(f"  head alembic : {snap['alembic_head']['head']}")
    print(f"  mesures      : {len(snap['measurements'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
