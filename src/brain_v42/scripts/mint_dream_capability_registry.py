"""Frapper le registre de capacités Dream — étape 8 de la spec dream v2.

Le registre ``MCP_HTTP_DREAM_TOKENS`` est ce qui fait passer le principal d'une
phase de ``unscoped`` à ``scoped``. Tant qu'il est absent, ``on_call_tool``
laisse passer sans périmètre et une nuit lancée pour un projet peut lire et
muter le corpus de tous les autres.

CLI (le secret ne transite JAMAIS par un argument ni par stdout) ::

    MCP_HTTP_TOKEN=... uv run python -m scripts.mint_dream_capability_registry \\
        --output ~/.config/brain-v42/dream-tokens.env \\
        --project-key brain-v42 --project-key red

    # ou, en reprenant exactement le pool de l'unité vivante :
    MCP_HTTP_TOKEN=... uv run python -m scripts.mint_dream_capability_registry \\
        --output ~/.config/brain-v42/dream-tokens.env --from-drop-in

Le mode d'échec de ce chantier est CONNU et il est vert. Le 2026-07-03, un
bearer manquant a fait tourner chaque phase en 401 — zéro outil brain — et la
nuit a rendu « 6/6 OK ». Un registre incomplet produit exactement la même nuit.
D'où la garde centrale : la sortie est repassée dans
``parse_dream_capability_registry``, LA fonction que le serveur exécute au
démarrage. Pas une copie de ses règles, la fonction. Un registre que le serveur
refuserait ne peut donc pas sortir d'ici.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from brain_v42.mcp.dream_capabilities import (
    DREAM_PHASE_TOOL_ALLOWLISTS,
    parse_dream_capability_registry,
)
from brain_v42.models.project_key import canonicalize_project_key

ENV_KEY = "MCP_HTTP_DREAM_TOKENS"
ADMIN_TOKEN_ENV = "MCP_HTTP_TOKEN"
# 32 octets d'entropie en base64url. Le registre compare en temps constant
# (hmac.compare_digest) et les tokens sont opaques : leur seule propriété utile
# est d'être imprévisibles et distincts.
_TOKEN_BYTES = 32


def _default_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def mint(
    project_keys: Sequence[str],
    output: Path,
    *,
    admin_token: str,
    _token_source: Callable[[], str] = _default_token,
) -> int:
    """Écrire le registre pour ``project_keys``. Rend le nombre de profils.

    Refuse d'écraser un fichier existant : les bearers vivants sont ceux que
    les phases portent déjà, et les remplacer sans le vouloir donnerait 401 sur
    toute la nuit suivante — donc « 6/6 OK » sur du vide.
    """
    if not project_keys:
        raise ValueError("at least one project key is required")

    canonical: list[str] = []
    for raw in project_keys:
        key = canonicalize_project_key(raw)
        if key in canonical:
            raise ValueError(f"duplicate project key: {key}")
        canonical.append(key)

    phases = sorted(DREAM_PHASE_TOOL_ALLOWLISTS)
    payload: dict[str, dict[str, object]] = {}
    for project_key in canonical:
        for phase in phases:
            # `accepted` reste vide à la frappe initiale : il ne sert qu'au
            # recouvrement d'une rotation, où l'ancien token doit rester honoré
            # le temps que les clients prennent le nouveau.
            payload[f"{project_key}:{phase}"] = {"active": _token_source(), "accepted": []}

    serialised = json.dumps(payload, separators=(",", ":"), sort_keys=True)

    # LA garde : valider avec le parseur du serveur, pas avec une relecture de
    # ses règles. Il vérifie la matrice complète, les doublons, la collision
    # avec le bearer admin et la canonicité des clés — et il lève si l'un
    # manque, AVANT que le fichier n'atteigne le disque.
    parse_dream_capability_registry(serialised, admin_token=admin_token)

    # `x` : création exclusive. Un registre existant n'est jamais écrasé.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{ENV_KEY}={serialised}\n")
    # os.open honore l'umask ; on repose le mode explicitement, parce que le
    # preflight MCP refuse le service si le fichier n'est pas exactement 0600.
    output.chmod(0o600)
    return len(payload)


def _pool_from_drop_in() -> list[str]:
    from brain_v42.dream_killswitches import (  # noqa: PLC0415
        KILLSWITCHES_PATH,
        parse_project_pool,
    )

    pool = parse_project_pool(KILLSWITCHES_PATH.read_text())
    if not pool:
        raise ValueError(
            f"no BRAIN_DREAM_PROJECT_POOL in {KILLSWITCHES_PATH} — "
            "pass --project-key explicitly, or open the pool first"
        )
    return pool


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--project-key",
        action="append",
        default=[],
        dest="project_keys",
        help="Repeatable. Mutually exclusive with --from-drop-in.",
    )
    parser.add_argument(
        "--from-drop-in",
        action="store_true",
        help="Read the pool from the live systemd drop-in instead.",
    )
    args = parser.parse_args(argv)

    if bool(args.project_keys) == args.from_drop_in:
        print("choose exactly one of --project-key or --from-drop-in", file=sys.stderr)
        return 2

    admin_token = os.environ.get(ADMIN_TOKEN_ENV)
    if not admin_token:
        # Il n'entre pas par un argument : la ligne de commande est lisible
        # dans /proc et dans l'historique du shell.
        print(f"{ADMIN_TOKEN_ENV} must be set in the environment", file=sys.stderr)
        return 2

    try:
        project_keys = _pool_from_drop_in() if args.from_drop_in else args.project_keys
        count = mint(project_keys, args.output, admin_token=admin_token)
    except FileExistsError:
        print(f"refusing to overwrite {args.output} — move it aside first", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        # `exc` peut citer une clé de projet, jamais un token : les erreurs du
        # parseur sont secret-safe par construction.
        print(f"mint failed: {exc}", file=sys.stderr)
        return 1

    # Le décompte, jamais la matière. Les projets sont publics, les tokens non.
    print(f"wrote {count} profiles for {len(project_keys)} projects to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
