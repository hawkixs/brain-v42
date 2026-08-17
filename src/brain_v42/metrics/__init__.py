"""brain_v42.metrics package init.

Wires the historical ``CodexConversationRegistry`` name (and its
registry-only constants) onto ``codex_telemetry`` from here rather than
from inside ``codex_telemetry`` itself.

``client_activity`` imports its decoder helpers (``_decode``,
``_ProjectedRecord``, ``_COMPLETION_EVENTS``) from ``codex_telemetry``.
An alias import back into ``codex_telemetry`` would therefore be a real
circular import — not just an import-order nuisance — whenever
``client_activity`` is the first of the two modules touched (e.g. `from
brain_v42.metrics.client_activity import ClientActivityRegistry` before
`codex_telemetry` is ever imported, exactly what
``tests/unit/test_client_activity.py`` does). Assigning the alias here, in
the parent package's ``__init__``, breaks the cycle: package
initialization always completes — and this module always finishes running
— before either submodule's body can be imported.
"""

from __future__ import annotations

from brain_v42.metrics import codex_telemetry
from brain_v42.metrics.client_activity import (
    ACTIVITY_TTL_SECONDS,
    FINGERPRINT_TTL_SECONDS,
    MAX_ACTIVE_CONVERSATIONS,
    MAX_FINGERPRINTS,
    ClientActivityRegistry,
)

# Nom historique : le registre n'est plus spécifique à Codex depuis qu'il
# fusionne les sources, mais l'ancien nom (et les anciennes constantes)
# restent importés depuis `codex_telemetry` par les tests existants.
# mypy ne peut pas vérifier statiquement une assignation d'attribut de
# module ajoutée dynamiquement — c'est le prix de cette porte de sortie
# anti-cycle, assumé et isolé ici plutôt que masqué plus largement.
codex_telemetry.CodexConversationRegistry = ClientActivityRegistry  # type: ignore[attr-defined]
codex_telemetry.MAX_ACTIVE_CONVERSATIONS = MAX_ACTIVE_CONVERSATIONS  # type: ignore[attr-defined]
codex_telemetry.ACTIVITY_TTL_SECONDS = ACTIVITY_TTL_SECONDS  # type: ignore[attr-defined]
codex_telemetry.MAX_FINGERPRINTS = MAX_FINGERPRINTS  # type: ignore[attr-defined]
codex_telemetry.FINGERPRINT_TTL_SECONDS = FINGERPRINT_TTL_SECONDS  # type: ignore[attr-defined]

__all__ = ["ClientActivityRegistry", "codex_telemetry"]
