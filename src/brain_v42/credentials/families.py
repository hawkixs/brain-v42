"""Separate stored permissions from temporary administrative elevation."""

STORABLE_FAMILIES = frozenset({"read", "write", "delivery", "telemetry", "elevate"})
ELEVATION_FAMILY = "admin"
