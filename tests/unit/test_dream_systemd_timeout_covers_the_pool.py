"""Le plafond systemd couvre le pool, et il est DÉRIVÉ, pas recopié.

Spec `2026-08-08-dream-project-pool-design.md` §4.3 et §10.

`TimeoutStartSec` doit être calé sur la borne (b) — le plafond CONFIGURÉ — et
pas sur la moyenne mesurée, parce que systemd tue à la borne. Le calcul :

    N × (somme des timeouts de phase) + budget de retries + globales

À 180 min (10800 s), la valeur d'avant le pool, **deux projets suffisaient à
dépasser** : 2 × 53 + 43 + 35 = 227 min. La nuit serait tuée au milieu du
deuxième projet, et les projets suivants n'auraient AUCUNE ligne dans
`dream_runs` — invisible pour un lecteur `DISTINCT ON (phase)` qui verrait les
phases des projets déjà traités.

Ce test ne recopie aucun nombre. Il lit les timeouts réels dans `PHASES` et
dans les trois `timeout Nm` des phases globales, puis vérifie que le template
versionné couvre `_MAX_POOL` projets. Un timeout de phase relevé sans relever
le plafond échoue ici, en nommant le manque.

ET IL VÉRIFIE LE TEMPLATE, PAS L'UNITÉ VIVANTE. `deploy/systemd/install.sh`
régénère l'unité depuis le template, et son garde-fou n'avertit que sur les
lignes `Environment=` ajoutées à la main — `TimeoutStartSec` n'en est pas une.
Un plafond relevé à la main dans ~/.config/systemd/user/ serait réécrit à la
prochaine réinstallation, sans un mot. C'est le jumeau de l'incident du
2026-06-30 (PROMOTE+REORG éteints deux nuits par une régénération).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"
TEMPLATE = REPO_ROOT / "deploy" / "systemd" / "brain-v42-dream.service.tmpl"

# Taille de pool que le plafond versionné doit soutenir. Les « 10 gros » de la
# décision 4158d142. Élargir au-delà est un nombre à changer ICI d'abord — et
# ce test est ce qui force à s'en apercevoir.
_MAX_POOL = 10


def _agent_phase_minutes() -> int:
    """Somme des timeouts des six phases agent, lue dans PHASES."""
    content = DREAM_SH.read_text(encoding="utf-8")
    entries = re.findall(r'"(\w+):(?:fast|deep):(\d+):\d+"', content)
    assert len(entries) == 6, f"attendu 6 phases agent dans PHASES, mesuré {len(entries)}"
    return sum(int(minutes) for _, minutes in entries)


def _global_phase_minutes() -> int:
    """Somme des trois `timeout Nm` des phases globales."""
    content = DREAM_SH.read_text(encoding="utf-8")
    total = 0
    for module in ("scripts.ticket_extract", "scripts.roadmap_curate", "session_sweep"):
        match = re.search(rf"timeout (\d+)m uv run python -m [\w.]*{re.escape(module)}", content)
        assert match, f"garde-fou `timeout Nm` introuvable pour {module}"
        total += int(match.group(1))
    return total


def _retry_budget() -> int:
    content = DREAM_SH.read_text(encoding="utf-8")
    match = re.search(r'BRAIN_DREAM_RETRY_BUDGET="\$\{BRAIN_DREAM_RETRY_BUDGET:-(\d+)\}"', content)
    assert match, "BRAIN_DREAM_RETRY_BUDGET introuvable dans dream.sh"
    return int(match.group(1))


def _longest_retriable_phase_minutes() -> int:
    """La phase la plus chère qui puisse être retentée.

    PROMOTE est explicitement exclue du retry, et un timeout n'est jamais
    retenté — seul un échec dur l'est.
    """
    content = DREAM_SH.read_text(encoding="utf-8")
    entries = re.findall(r'"(\w+):(?:fast|deep):(\d+):\d+"', content)
    return max(int(minutes) for name, minutes in entries if name != "promote")


def _template_timeout_seconds() -> int:
    content = TEMPLATE.read_text(encoding="utf-8")
    match = re.search(r"^TimeoutStartSec=(\d+)$", content, re.MULTILINE)
    assert match, "TimeoutStartSec introuvable dans le template versionné"
    return int(match.group(1))


def test_the_versioned_template_covers_the_configured_worst_case() -> None:
    ceiling_minutes = (
        _MAX_POOL * _agent_phase_minutes()
        + _retry_budget() * _longest_retriable_phase_minutes()
        + _global_phase_minutes()
    )

    assert _template_timeout_seconds() >= ceiling_minutes * 60, (
        f"TimeoutStartSec={_template_timeout_seconds()}s ne couvre pas le pire cas "
        f"configuré à {_MAX_POOL} projets ({ceiling_minutes} min = "
        f"{ceiling_minutes * 60}s). systemd tuerait la nuit au milieu d'un projet, "
        "et les projets suivants n'auraient aucune ligne dans dream_runs."
    )


def test_the_old_ceiling_could_not_have_served_the_intended_pool() -> None:
    """Combien de projets l'ancien plafond couvrait-il ? Dérivé, pas recopié.

    §4.3 chiffre « 227 min à deux projets, déjà dépassé ». Ce nombre était juste
    SOUS L'ANCIEN RÉGIME de retry, où chaque projet portait ses +43 min
    éligibles. L'allocation de nuit livrée avec la boucle a racheté cette
    marge : à deux projets on est maintenant à 171 min, sous les 180.

    L'ancien plafond casse donc à TROIS projets, pas deux. La conclusion de la
    spec tient — il ne pouvait pas servir les dix — mais son chiffre décrit un
    script qui a changé depuis. On mesure.
    """
    fixed = _retry_budget() * _longest_retriable_phase_minutes() + _global_phase_minutes()
    per_project = _agent_phase_minutes()
    covered = (10800 // 60 - fixed) // per_project

    assert covered < _MAX_POOL, (
        f"l'ancien plafond de 180 min couvrirait {covered} projets, soit le pool "
        f"visé de {_MAX_POOL} : l'arithmétique de §4.3 est à remesurer"
    )
    assert covered >= 1, (
        "l'ancien plafond ne couvrirait plus même un projet — les timeouts de "
        "phase ont explosé et c'est ça qu'il faut regarder, pas le plafond"
    )


def test_the_ceiling_is_not_absurdly_oversized() -> None:
    """Un plafond doit rester un plafond, pas une absence de plafond.

    Le timer est quotidien : au-delà de 24 h, une nuit pourrait chevaucher la
    suivante — et `Type=oneshot` fait alors PERDRE le déclenchement, sans file
    ni erreur.
    """
    assert _template_timeout_seconds() < 24 * 3600
