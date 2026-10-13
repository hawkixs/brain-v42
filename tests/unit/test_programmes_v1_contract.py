"""The programmes/v1 contract files agree with each other (tickets 8a063256, 24c1f172).

red-monitor copies the sample blocks into its own fixtures and red-cockpit pins
those copies: a sample that drifts from the declared shape would ship the drift
to both consumers. The publisher itself is pinned against the same shape by the
sidecar block's own tests.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

CONTRACTS = Path(__file__).resolve().parents[2] / "docs" / "contracts"
SHAPE = json.loads((CONTRACTS / "programmes_v1_block.json").read_text())
SAMPLES = sorted((CONTRACTS / "programmes_v1").glob("*.json"))
RESERVED = {"served_at", "source_age_s", "source", "available", "reason", "payload"}
KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def conforms(value: Any, shape: Any, objects: dict[str, Any], path: str = "$") -> None:
    """Exact key sets, declared types, and the declared shape of each nullable object."""
    if isinstance(shape, dict):
        assert isinstance(value, dict), path
        assert set(value) == set(shape), f"{path}: {sorted(set(value) ^ set(shape))}"
        for key, inner in shape.items():
            conforms(value[key], inner, objects, f"{path}.{key}")
    elif isinstance(shape, list):
        assert isinstance(value, list), path
        for index, item in enumerate(value):
            conforms(item, shape[0], objects, f"{path}[{index}]")
    else:
        assert _kind(value) in shape.split("|"), f"{path}: {_kind(value)} not in {shape}"
        name = path.rsplit(".", 1)[-1]
        if isinstance(value, dict) and name in objects:
            conforms(value, objects[name], objects, path)


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    assert parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0, value
    return parsed


def check_semantics(block: dict[str, Any]) -> None:
    """The rules the shape file cannot say: vocabularies, keys, limits, graph, time."""
    enums = SHAPE["enums"]
    assert block["version"] == "programmes/v1"
    assert not RESERVED & set(block)
    generated_at = _instant(block["generated_at"])
    programme_keys = [p["key"] for p in block["programmes"]]
    assert len(programme_keys) == len(set(programme_keys))
    for programme in block["programmes"]:
        assert KEY.match(programme["key"]) and len(programme["key"]) <= 64
        assert programme["state"] in enums["programme.state"]
        assert 0 < len(programme["title"]) <= 120
        gate_keys = [g["key"] for g in programme["gates"]]
        assert len(gate_keys) == len(set(gate_keys)) and len(gate_keys) <= 32
        for gate in programme["gates"]:
            assert KEY.match(gate["key"]) and len(gate["key"]) <= 32
            assert gate["state"] in enums["gate.state"]
            assert 1 <= gate["weight"] <= 100
            assert gate["key"] not in gate["depends_on"]
            assert set(gate["depends_on"]) <= set(gate_keys)
            assert len(gate["depends_on"]) <= 12
            if gate["note"] is not None:
                assert 0 < len(gate["note"]) <= 200
            if gate["waiting_on_operator"] is not None:
                assert 0 < len(gate["waiting_on_operator"]["ask"]) <= 200
                assert _instant(gate["waiting_on_operator"]["since"]) <= generated_at
            if gate["progress"] is not None:
                assert 0 <= gate["progress"]["done"] <= gate["progress"]["total"]
            assert _instant(gate["updated_at"]) <= generated_at
        change = programme["last_change"]
        if change is not None:
            assert change["gate"] in gate_keys
            assert change["from"] != change["to"]
            assert {change["from"], change["to"]} <= set(enums["gate.state"])
            assert _instant(change["at"]) <= generated_at
        assert len(json.dumps(programme).encode()) <= 16 * 1024


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda p: p.name)
def test_sample_conforms_to_the_declared_shape(sample: Path) -> None:
    block = json.loads(sample.read_text())
    conforms(block, SHAPE["shape"], SHAPE["objects"])
    check_semantics(block)


def test_the_samples_cover_every_case_the_consumers_pin() -> None:
    names = {sample.name for sample in SAMPLES}
    assert {"full.json", "brain_move_g1_g12.json", "empty.json"} <= names
    assert json.loads((CONTRACTS / "programmes_v1" / "empty.json").read_text())["programmes"] == []

    full = json.loads((CONTRACTS / "programmes_v1" / "full.json").read_text())
    states = {p["state"] for p in full["programmes"]}
    gates = [g for p in full["programmes"] for g in p["gates"]]
    assert states == set(SHAPE["enums"]["programme.state"])
    assert {p["last_change"] is None for p in full["programmes"]} == {True, False}
    for nullable in ("waiting_on_operator", "progress", "note"):
        assert {g[nullable] is None for g in gates} == {True, False}, nullable


def test_brain_move_sample_carries_the_structure_the_coordination_confirmed() -> None:
    block = json.loads((CONTRACTS / "programmes_v1" / "brain_move_g1_g12.json").read_text())
    (move,) = block["programmes"]
    gates = {g["key"]: g for g in move["gates"]}
    assert list(gates) == [f"G{n}" for n in range(1, 13)]
    expected = {
        "G2": ["G1"],
        "G3": ["G2"],
        "G4": ["G3"],
        "G5": ["G4"],
        "G7": ["G6"],
        "G10": ["G5", "G7", "G8", "G9"],
        "G11": ["G4", "G5", "G7", "G8", "G9", "G10"],
        "G12": ["G11"],
    }
    assert {k: g["depends_on"] for k, g in gates.items() if g["depends_on"]} == expected
    assert gates["G7"]["weight"] == 4


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.pop("generated_at"),
        lambda b: b.update(source="red-base"),
        lambda b: b["programmes"][0]["gates"][0].pop("note"),
        lambda b: b["programmes"][0]["gates"][0].update(depends_on=None),
        lambda b: b["programmes"][0]["gates"][0].update(weight=0),
        lambda b: b["programmes"][0]["gates"][0].update(state="mostly_done"),
        lambda b: b["programmes"][0]["gates"][1].update(depends_on=["G2"]),
        lambda b: b["programmes"][0]["gates"][6]["waiting_on_operator"].pop("since"),
    ],
)
def test_the_checks_reject_a_drifted_block(mutate: Any) -> None:
    block = json.loads((CONTRACTS / "programmes_v1" / "brain_move_g1_g12.json").read_text())
    mutate(block)
    with pytest.raises(AssertionError):
        conforms(block, SHAPE["shape"], SHAPE["objects"])
        check_semantics(block)
