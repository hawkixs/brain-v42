"""La fenêtre de LECTURE de process_metrics doit égaler sa fenêtre de RÉTENTION.

Mesuré le 2026-08-10 (ticket d2a669c6). ``collect_process_metrics`` filtrait sur
``updated_at > NOW() - INTERVAL '60 seconds'`` alors que la purge, elle, s'exécute à
``INTERVAL '1 hour'`` — et à DEUX endroits (``flusher.py``, ``runtime.py``). La fenêtre de
lecture était donc 60 fois plus étroite que la fenêtre de rétention, sans raison documentée.

Conséquence mesurée sur la production vivante à 19:36 : cinq lignes en base, dont ``codex``
(7 min d'âge, 7 outils) et ``hawixs`` (30 min, 4 outils). Le panneau n'en montrait que trois.
Deux appelants réels sur cinq étaient invisibles ALORS QUE LEURS LIGNES EXISTAIENT.

Ces tests gardent l'invariant plutôt que la valeur : trois littéraux SQL qui doivent
s'accorder sans que rien ne les relie finiront toujours par diverger. C'est cette dérive
qu'on épingle, pas le nombre ``3600``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

_METRICS_DIR = Path(__file__).resolve().parents[3] / "src" / "brain_v42" / "metrics"


def test_the_read_window_and_the_purge_window_come_from_one_constant() -> None:
    """Les deux prédicats dérivent du même intervalle, donc ne peuvent plus diverger.

    RED avant correctif : ``brain_v42.metrics.retention`` n'existe pas, l'import lève
    ModuleNotFoundError — bruyamment, ce qui est le bon échec.
    """
    from brain_v42.metrics.retention import (
        PROCESS_METRICS_FRESH_SQL,
        PROCESS_METRICS_RETENTION_SECONDS,
        PROCESS_METRICS_STALE_SQL,
    )

    seconds_in_fresh = re.findall(r"INTERVAL '(\d+) seconds'", PROCESS_METRICS_FRESH_SQL)
    seconds_in_stale = re.findall(r"INTERVAL '(\d+) seconds'", PROCESS_METRICS_STALE_SQL)

    assert seconds_in_fresh == [str(PROCESS_METRICS_RETENTION_SECONDS)], (
        f"le prédicat de fraîcheur doit nommer {PROCESS_METRICS_RETENTION_SECONDS}s, "
        f"il nomme {seconds_in_fresh}"
    )
    assert seconds_in_stale == [str(PROCESS_METRICS_RETENTION_SECONDS)], (
        f"le prédicat de péremption doit nommer {PROCESS_METRICS_RETENTION_SECONDS}s, "
        f"il nomme {seconds_in_stale}"
    )

    # Complémentaires stricts : ce qui survit à la purge est lisible, et réciproquement.
    assert ">" in PROCESS_METRICS_FRESH_SQL and "<" in PROCESS_METRICS_STALE_SQL


def test_the_retention_window_is_not_narrower_than_a_flush_period() -> None:
    """Une fenêtre plus courte que la période de flush rend des agents actifs invisibles.

    Contrôle de bon sens sur la valeur, pas sur son exactitude : le flush tourne à la
    minute, donc une rétention d'une minute ferait clignoter le panneau.
    """
    from brain_v42.metrics.retention import PROCESS_METRICS_RETENTION_SECONDS

    assert PROCESS_METRICS_RETENTION_SECONDS >= 300, (
        "une rétention sous 5 minutes fait disparaître du panneau des agents qui "
        "viennent d'appeler un tool"
    )


def test_no_metrics_module_hardcodes_a_process_metrics_window() -> None:
    """La sonde NÉGATIVE : elle doit échouer si quelqu'un recode un littéral en dur.

    RED avant correctif : trois sites la font échouer — collector_db.py (60 seconds),
    flusher.py (1 hour) et runtime.py (1 hour). C'est exactement la dérive du ticket.
    """
    offenders: list[str] = []

    # Le SQL est écrit sur plusieurs lignes : chercher ligne à ligne raterait
    # précisément le site du défaut d'origine (collector_db.py, où la table et
    # l'intervalle sont à 4 lignes d'écart). On cherche donc sur le texte entier.
    # Le ``(?!FROM)`` empêche de franchir une frontière de requête : sans lui, le motif
    # traverse le DELETE voisin sur search_log et signale son INTERVAL '30 days', qui ne
    # concerne pas cette table.
    near_table = re.compile(r"process_metrics(?:(?!FROM).){0,400}?INTERVAL\s*'([^']+)'", re.DOTALL)

    for module in sorted(_METRICS_DIR.glob("*.py")):
        if module.name == "retention.py":
            continue  # la source de vérité a le droit de nommer l'intervalle
        source = module.read_text(encoding="utf-8")
        for match in near_table.finditer(source):
            line_no = source.count("\n", 0, match.start()) + 1
            offenders.append(f"{module.name}:{line_no}: INTERVAL '{match.group(1)}'")

    assert offenders == [], (
        "un intervalle est codé en dur dans une requête sur process_metrics ; "
        "utiliser brain_v42.metrics.retention à la place :\n" + "\n".join(offenders)
    )


@pytest.mark.asyncio
async def test_collect_process_metrics_reads_with_the_shared_predicate() -> None:
    """Le témoin comportemental : la vraie requête porte le prédicat partagé.

    RED avant correctif : le SQL exécuté contient ``INTERVAL '60 seconds'``, pas le
    prédicat partagé, donc l'assertion mord. Si quelqu'un revient à un littéral plus
    tard, elle mord de nouveau — c'est ce que le témoin structurel seul ne garantit pas.
    """
    from brain_v42.metrics.collector_db import _DbCollectorsMixin
    from brain_v42.metrics.retention import PROCESS_METRICS_FRESH_SQL

    executed: list[str] = []

    class _FakeResult:
        def all(self) -> list[Any]:
            return []

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, statement: Any) -> _FakeResult:
            executed.append(str(statement))
            return _FakeResult()

    mixin = _DbCollectorsMixin.__new__(_DbCollectorsMixin)
    mixin._session_factory = MagicMock(return_value=_FakeSession())  # type: ignore[attr-defined]

    await mixin.collect_process_metrics()

    assert executed, "collect_process_metrics n'a exécuté aucune requête"
    assert any(PROCESS_METRICS_FRESH_SQL in sql for sql in executed), (
        "la lecture de process_metrics n'utilise pas le prédicat partagé ; SQL exécuté :\n"
        + "\n".join(executed)
    )


def _row(agent_name: str, pid: int, *, is_live: bool) -> tuple[Any, ...]:
    """Une ligne process_metrics telle que la rend le SELECT (is_live en dernier)."""
    return (
        agent_name,
        pid,
        None,  # started_at
        None,  # updated_at
        {"brain_search": {"calls": 3, "total_latency": 30.0}},  # tool_stats
        {},  # embedding_stats
        0,  # memory_rss_bytes
        is_live,
    )


@pytest.mark.asyncio
async def test_active_processes_counts_only_processes_that_still_refresh() -> None:
    """``active_processes`` est une affirmation de VIVACITÉ, pas de séjour en base.

    Élargir la fenêtre de lecture à la rétention sans distinguer les deux ferait
    compter un process mort pendant une heure. Mesuré sur la production le
    2026-08-10 : le pid 1082528 était dans la fenêtre d'une heure et absent de ``ps``.

    RED avant correctif : ``active_processes`` compte les pids de TOUTES les lignes
    rendues, donc 2 au lieu de 1.
    """
    from brain_v42.metrics.collector_db import _DbCollectorsMixin

    class _FakeResult:
        def all(self) -> list[Any]:
            return [
                _row("brain-v42", 1111, is_live=True),
                _row("codex", 2222, is_live=False),  # n'a plus rafraîchi depuis longtemps
            ]

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, statement: Any) -> _FakeResult:
            return _FakeResult()

    mixin = _DbCollectorsMixin.__new__(_DbCollectorsMixin)
    mixin._session_factory = MagicMock(return_value=_FakeSession())  # type: ignore[attr-defined]

    result = await mixin.collect_process_metrics()

    assert result["active_processes"] == 1, (
        "un process qui a cessé de rafraîchir sa ligne est compté comme actif : "
        f"active_processes={result['active_processes']}"
    )
    # …mais l'agent reste visible du panneau : c'est tout l'objet de l'élargissement.
    assert result["active_agents"] == 2, (
        "l'agent silencieux doit rester visible dans le panneau pendant la rétention"
    )
    assert set(result["by_agent"]) == {"brain-v42", "codex"}


def test_the_liveness_window_is_strictly_tighter_than_the_retention_window() -> None:
    """Deux fenêtres, deux questions. Les confondre est exactement le défaut d'origine."""
    from brain_v42.metrics.retention import (
        PROCESS_METRICS_LIVE_SECONDS,
        PROCESS_METRICS_RETENTION_SECONDS,
    )

    assert PROCESS_METRICS_LIVE_SECONDS < PROCESS_METRICS_RETENTION_SECONDS, (
        "la vivacité doit être plus étroite que le séjour, sinon un process mort "
        "reste 'actif' aussi longtemps que sa ligne survit"
    )
