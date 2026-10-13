"""The programmes/v1 contract files agree with each other (tickets 8a063256, 24c1f172).

red-monitor copies the sample blocks into its own fixtures and red-cockpit pins
those copies: a sample that drifts from the declared shape would ship the drift
to both consumers. The publisher itself is pinned against the same shape by the
sidecar block's own tests.
"""

import copy
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from brain_v42.models.project_key import canonicalize_project_key

CONTRACTS = Path(__file__).resolve().parents[2] / "docs" / "contracts"
SHAPE = json.loads((CONTRACTS / "programmes_v1_block.json").read_text())
SAMPLES = sorted((CONTRACTS / "programmes_v1").glob("*.json"))
RESERVED = {"served_at", "source_age_s", "source", "available", "reason", "payload"}
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


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


#: RFC 3339 as brain writes it (contract section 4): UTC, offset `+00:00`, and no
#: fractional digits or exactly six. `datetime.fromisoformat` alone would also accept
#: the basic format and a space separator.
RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{6})?\+00:00$")
#: What contract section 2 forbids in a declared text: C0 and C1 controls (line breaks
#: included), DEL, zero-width characters, the Unicode line and paragraph separators
#: and bidirectional overrides.
FORBIDDEN_TEXT = re.compile(
    "[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]"
)
MAX_LIVE, MAX_DONE = 8, 4


def _instant(value: str) -> datetime:
    assert RFC3339_UTC.match(value), value
    return datetime.fromisoformat(value)


def _line(value: str, limit: int) -> None:
    assert 0 < len(value) <= limit and value.strip() == value, value
    assert not FORBIDDEN_TEXT.search(value), value


def _project(value: str) -> None:
    """A published identity is a canonical brain project key (`models/project_key.py`)."""
    try:
        canonical = canonicalize_project_key(value)
    except ValueError as refused:
        raise AssertionError(value) from refused
    assert len(value) <= 50 and canonical == value, value


def _acyclic(gates: dict[str, list[str]]) -> None:
    state: dict[str, int] = {}

    def visit(key: str) -> None:
        assert state.get(key) != 1, f"dependency cycle through {key}"
        if state.get(key) == 2:
            return
        state[key] = 1
        for dependency in gates[key]:
            visit(dependency)
        state[key] = 2

    for key in gates:
        visit(key)


def check_semantics(block: dict[str, Any]) -> None:
    """The rules the shape file cannot say: vocabularies, keys, limits, graph, time."""
    enums = SHAPE["enums"]
    assert block["version"] == "programmes/v1"
    assert not RESERVED & set(block)
    generated_at = _instant(block["generated_at"])
    programme_keys = [p["key"] for p in block["programmes"]]
    assert len(programme_keys) == len(set(programme_keys))
    states = [p["state"] for p in block["programmes"]]
    assert sum(state != "done" for state in states) <= MAX_LIVE
    assert states.count("done") <= MAX_DONE
    for programme in block["programmes"]:
        assert KEY.fullmatch(programme["key"]) and len(programme["key"]) <= 64
        assert programme["state"] in enums["programme.state"]
        _line(programme["title"], 120)
        _project(programme["coordinator"])
        gates = {g["key"]: g for g in programme["gates"]}
        assert len(gates) == len(programme["gates"]) and len(gates) <= 32
        for gate in programme["gates"]:
            assert KEY.fullmatch(gate["key"]) and len(gate["key"]) <= 32
            _line(gate["title"], 120)
            _project(gate["owner"])
            _project(gate["updated_by"])
            assert gate["state"] in enums["gate.state"]
            assert 1 <= gate["weight"] <= 100
            assert gate["key"] not in gate["depends_on"]
            assert set(gate["depends_on"]) <= set(gates)
            assert len(gate["depends_on"]) == len(set(gate["depends_on"])) <= 12
            updated_at = _instant(gate["updated_at"])
            assert updated_at <= generated_at
            if gate["note"] is not None:
                _line(gate["note"], 200)
            request = gate["waiting_on_operator"]
            if request is not None:
                _line(request["ask"], 200)
                assert _instant(request["since"]) <= updated_at
            progress = gate["progress"]
            if progress is not None:
                assert 1 <= progress["total"] <= 100_000
                assert 0 <= progress["done"] <= progress["total"]
                _line(progress["unit"], 24)
        _acyclic({key: gate["depends_on"] for key, gate in gates.items()})
        change = programme["last_change"]
        if change is not None:
            gate = gates[change["gate"]]
            assert change["from"] != change["to"]
            assert {change["from"], change["to"]} <= set(enums["gate.state"])
            # The programme's latest transition is its gate's latest one: the gate
            # still holds the state it moved to, and was written at or after it.
            assert gate["state"] == change["to"]
            assert _instant(change["at"]) <= _instant(gate["updated_at"])
            _project(change["by"])
        assert len(json.dumps(programme).encode()) <= 16 * 1024


def _prose_example() -> dict[str, Any]:
    text = (CONTRACTS / "programmes_v1.md").read_text()
    blocks = re.findall(r"```json\n(.*?)\n```", text, flags=re.DOTALL)
    assert len(blocks) == 1
    return json.loads(blocks[0])


def test_the_prose_example_obeys_its_own_contract() -> None:
    block = _prose_example()
    conforms(block, SHAPE["shape"], SHAPE["objects"])
    check_semantics(block)


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
        # Contract review round 1: every rule below was stated and not checked.
        lambda b: b["programmes"][0]["gates"][0].update(depends_on=["G2"]),
        lambda b: b["programmes"][0]["gates"][0].update(title="t" * 121),
        lambda b: b["programmes"][0]["gates"][0].update(note="two\nlines"),
        lambda b: b["programmes"][0]["gates"][0].update(note="nul\u0000here"),
        lambda b: b["programmes"][0]["gates"][0].update(note="bidi\u202eflip"),
        lambda b: b["programmes"][0]["gates"][6]["waiting_on_operator"].update(ask="a\nb"),
        lambda b: b["programmes"][0]["gates"][6]["progress"].update(total=0, done=0),
        lambda b: b["programmes"][0]["gates"][6]["progress"].update(total=100_001),
        lambda b: b["programmes"][0]["gates"][6]["progress"].update(unit=""),
        lambda b: b["programmes"][0]["gates"][6]["progress"].update(unit="u" * 25),
        lambda b: b["programmes"].extend(
            {**copy.deepcopy(b["programmes"][0]), "key": f"p{n}"} for n in range(8)
        ),
        lambda b: b["programmes"].extend(
            {**copy.deepcopy(b["programmes"][0]), "key": f"d{n}", "state": "done"} for n in range(5)
        ),
        lambda b: b["programmes"][0]["last_change"].update(to="done", **{"from": "todo"}),
        lambda b: b["programmes"][0]["last_change"].update(at="2026-10-06T16:39:00+00:00"),
        lambda b: b["programmes"][0]["gates"][6]["waiting_on_operator"].update(
            since="2026-10-06T16:31:00+00:00"
        ),
        lambda b: b.update(generated_at="20261006T164000+00:00"),
        lambda b: b.update(generated_at="2026-10-06 16:40:00+00:00"),
        lambda b: b.update(generated_at="2026-10-06T16:40:00Z"),
        lambda b: b["programmes"][0]["gates"][0].update(updated_at="2026-10-06T12:00:00.5+00:00"),
        # Contract review round 2.
        lambda b: b["programmes"][0]["gates"][0].update(title="first\u2028second"),
        lambda b: b["programmes"][0]["gates"][0].update(note="first\u2029second"),
        lambda b: b["programmes"][0].update(key="brain-move-red-base\n"),
        lambda b: b["programmes"][0]["gates"][0].update(key="G1\n"),
        lambda b: b["programmes"][0].update(coordinator="Brain_V42"),
        lambda b: b["programmes"][0]["gates"][0].update(owner="brain-v42\n"),
        lambda b: b["programmes"][0]["gates"][0].update(updated_by="x" * 51),
    ],
)
def test_the_checks_reject_a_drifted_block(mutate: Any) -> None:
    block = json.loads((CONTRACTS / "programmes_v1" / "brain_move_g1_g12.json").read_text())
    mutate(block)
    with pytest.raises(AssertionError):
        conforms(block, SHAPE["shape"], SHAPE["objects"])
        check_semantics(block)
