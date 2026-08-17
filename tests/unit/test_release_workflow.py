"""Le rail de release doit pouvoir tourner RUNNER ÉTEINT.

Mesuré le 2026-08-14 : 19 runs du rail de livraison, 19 `cancelled`, 0 succès.
Le runner `[self-hosted, Linux, X64, red-ci]` est démarré à la demande et son état
`offline` est NORMAL, documenté. Un job de release posé là n'aurait jamais tourné :
il aurait attendu indéfiniment qu'un opérateur démarre une VM, c'est-à-dire
exactement le geste manuel que la release est censée remplacer.

Ce rail n'attache AUCUNE image. Le registre est privé et à DNS interne
(`registry.hawkixs.local`), donc son adresse ne veut rien dire pour un
consommateur de release ; et une image cuirait l'ONNX du reranker, dont NOTICE
déclare la licence amont indéterminée.

La vérification que la wheel porte bien les migrations est RÉUTILISÉE, jamais
recopiée : `tests/unit/test_wheel_ships_migrations.py` construit déjà une vraie
wheel et lit l'ensemble attendu sur le disque. Une seconde implémentation dans du
YAML dériverait de la première sans que rien ne le signale.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW_PATH = REPO_ROOT / ".github/workflows/release.yml"
WHEEL_MIGRATION_TEST = "tests/unit/test_wheel_ships_migrations.py"

HOSTED_RUNNER = "ubuntu-24.04"
ON_DEMAND_RUNNER_LABEL = "red-ci"

# `uses: owner/repo@<40 hexadécimaux>` — une balise mobile (`@v4`) se réécrit
# sous le même nom, un SHA non.
PINNED_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


@pytest.fixture(scope="module")
def release_workflow() -> dict[Any, Any]:
    assert RELEASE_WORKFLOW_PATH.is_file(), (
        f"{RELEASE_WORKFLOW_PATH.relative_to(REPO_ROOT)} est absent : sans lui, publier "
        "une release reste un geste manuel non reproductible"
    )
    loaded = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture(scope="module")
def release_body() -> str:
    return RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def release_job(release_workflow: dict[Any, Any]) -> dict[Any, Any]:
    jobs = release_workflow["jobs"]
    assert len(jobs) == 1, f"un seul job attendu sur ce rail, obtenu {sorted(jobs)}"
    job = next(iter(jobs.values()))
    assert isinstance(job, dict)
    return job


def _run_scripts(job: dict[Any, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


def test_release_triggers_only_on_a_version_tag(release_workflow: dict[Any, Any]) -> None:
    """Un push sur une branche ne publie rien : seul un tag `v*` déclenche."""
    # PyYAML 1.1 lit une clé `on:` nue comme le booléen True.
    assert release_workflow[True] == {"push": {"tags": ["v*"]}}


def test_release_runs_on_a_hosted_runner(release_job: dict[Any, Any]) -> None:
    """Le cœur du lot : ce rail doit tourner runner ÉTEINT.

    Mesure : 19 runs de livraison, 19 `cancelled`, 0 succès, parce que le runner
    à la demande est hors ligne par conception.
    """
    runner = release_job["runs-on"]
    assert runner == HOSTED_RUNNER, (
        f"la release doit tourner sur le runner hébergé {HOSTED_RUNNER}, pas sur {runner!r}"
    )
    assert ON_DEMAND_RUNNER_LABEL not in str(runner)


def test_release_asks_for_nothing_beyond_writing_the_release(
    release_workflow: dict[Any, Any],
) -> None:
    """`contents: write` et RIEN d'autre — le jeton par défaut est trop large."""
    assert release_workflow["permissions"] == {"contents": "write"}


def test_release_job_does_not_widen_the_workflow_permissions(
    release_job: dict[Any, Any],
) -> None:
    """Une permission posée au niveau du job REMPLACE celle du workflow."""
    assert "permissions" not in release_job


def test_every_action_is_pinned_by_sha(release_body: str) -> None:
    """Une balise mobile se réécrit sous le même nom ; un SHA non."""
    referenced = re.findall(r"^\s*uses:\s*(\S+)", release_body, flags=re.MULTILINE)
    assert referenced, "le rail doit référencer au moins une action (checkout)"
    unpinned = [action for action in referenced if not PINNED_ACTION.match(action)]
    assert not unpinned, f"action(s) non épinglée(s) par SHA : {unpinned}"


def test_release_builds_both_distributions(release_job: dict[Any, Any]) -> None:
    """Une wheel seule ne suffit pas : le sdist porte la source correspondante."""
    scripts = _run_scripts(release_job)
    assert "uv build" in scripts, "le rail doit construire les distributions"
    assert ".whl" in scripts and ".tar.gz" in scripts, (
        "le rail doit nommer les deux artefacts, wheel ET sdist"
    )


def test_release_reuses_the_wheel_migration_contract(release_job: dict[Any, Any]) -> None:
    """La vérification des migrations est RÉUTILISÉE, jamais réimplémentée en YAML.

    Le test référencé construit une vraie wheel et lit l'ensemble attendu sur le
    disque : recopier sa logique ici la ferait dériver en silence.
    """
    assert (REPO_ROOT / WHEEL_MIGRATION_TEST).is_file(), (
        f"{WHEEL_MIGRATION_TEST} est le contrat réutilisé ; il doit exister"
    )
    scripts = _run_scripts(release_job)
    assert WHEEL_MIGRATION_TEST in scripts, (
        "le rail doit lancer le contrat existant, pas en écrire un second dans du YAML"
    )
    assert "alembic/versions" not in scripts, (
        "réimplémentation détectée : la vérification des migrations appartient à "
        f"{WHEEL_MIGRATION_TEST}"
    )


def test_release_refuses_a_tag_that_does_not_name_the_built_version(
    release_job: dict[Any, Any],
) -> None:
    """Sans cette garde, `v9.9.9` publierait une wheel 0.2.0.

    C'est toute la promesse de la feature : le numéro de la release est celui que
    `/health` annonce. Un tag qui ne nomme pas la version construite la casse en
    silence, et la release resterait publiée.
    """
    scripts = _run_scripts(release_job)
    assert "github.ref_name" in scripts or "GITHUB_REF_NAME" in scripts, (
        "le rail doit lire le tag pour le confronter à la version construite"
    )
    assert "brain_v42-${" in scripts, (
        "le rail doit exiger sur le disque les artefacts nommés par le tag, "
        "sinon rien ne lie la release à la version qu'elle publie"
    )


def test_release_attaches_the_two_artifacts_with_generated_notes(
    release_job: dict[Any, Any],
) -> None:
    """191 des 199 sujets des 200 derniers commits sont conventionnels : les notes
    générées disent quelque chose."""
    scripts = _run_scripts(release_job)
    assert "gh release create" in scripts
    assert "--generate-notes" in scripts
    assert "--verify-tag" in scripts, (
        "sans --verify-tag, `gh` créerait le tag manquant au lieu de refuser"
    )


def test_release_attaches_no_container_image(release_body: str) -> None:
    """Le registre est privé et à DNS interne ; et une image cuirait l'ONNX du
    reranker, dont NOTICE déclare la licence amont indéterminée."""
    body = "\n".join(
        line for line in release_body.splitlines() if not line.lstrip().startswith("#")
    )
    for forbidden in ("docker", "CI_REGISTRY", "registry.hawkixs.local", "REGISTRY_PASSWORD"):
        assert forbidden not in body, (
            f"le rail de release ne doit pas toucher au registre d'images : {forbidden!r}"
        )


def test_release_never_invokes_a_module_entrypoint(release_job: dict[Any, Any]) -> None:
    """`scripts/check_container_image_pins.py` est fail-closed sur `python -m <module>`
    et refuserait le workflow — ce qui rougirait la porte unitaire ENTIÈRE."""
    assert "python -m " not in _run_scripts(release_job)
