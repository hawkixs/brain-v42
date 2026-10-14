"""Version the reliability order and the vocabulary used by stored evidence."""

from enum import StrEnum
from types import MappingProxyType

RANKS_VERSION = "v1"

RANKS = MappingProxyType(
    {
        "structure": 7,
        "measured": 6,
        "config": 5,
        "code": 4,
        "pr": 3,
        "ticket": 3,
        "decision": 3,
        "propagation": 2,
        "decay": 1,
    }
)


class State(StrEnum):
    VALID = "valid"
    CONTESTED = "contested"
    OBSOLETE = "obsolete"
    REFUTED = "refuted"
    UNVERIFIABLE = "unverifiable"


class Polarity(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    ENDS = "ends"
    NONE = "none"


class Centrality(StrEnum):
    CORE = "core"
    INCIDENTAL = "incidental"


class Temporal(StrEnum):
    TRANSIENT = "transient"
    DURABLE = "durable"


class ValidFromPrecision(StrEnum):
    EXACT = "exact"
    FIRST_OBSERVED = "first_observed"
    UNKNOWN = "unknown"
