"""`docs/contracts/delivery_attestations.json` is the attestation API as DATA.

Ticket 04bc1f4a, red-rail request 4: the consumer must never import `brain_v42`,
so it freezes this file at a git tag in a boundary test. Everything in it is
therefore derived from, or recomputed against, the code here — a contract that
drifted from its implementation would be worse than none.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = ROOT / "docs" / "contracts" / "delivery_attestations.json"
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
_ERROR_LITERAL = re.compile(r'DeliveryError\(\s*"([a-z_]+)"')


def _function_sources(path: Path, names: set[str]) -> str:
    """The source of every function or method named in `names`, wherever it nests."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    segments = [
        ast.get_source_segment(text, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in names
    ]
    assert len(segments) == len(names), (path.name, names, len(segments))
    return "\n".join(segments)


def _emitted_codes() -> set[str]:
    """Every literal code the attestation surface can raise, by census of its emitters."""
    sources = [
        (ROOT / "src/brain_v42/repositories/pg_delivery_attestations.py").read_text(
            encoding="utf-8"
        ),
        _function_sources(
            ROOT / "src/brain_v42/services/delivery_service.py",
            {"attest", "list_attestations"},
        ),
        _function_sources(
            ROOT / "src/brain_v42/mcp/tools/delivery_tools.py",
            {"brain_delivery_attest", "brain_delivery_attestation_list"},
        ),
        _function_sources(
            ROOT / "src/brain_v42/models/delivery.py",
            {"validate_attestation_form", "validate_attestation_kind", "parse_window_bound"},
        ),
    ]
    return {code for source in sources for code in _ERROR_LITERAL.findall(source)}


def test_contract_version_and_tool_versions() -> None:
    assert CONTRACT["contract_version"] == 1
    assert CONTRACT["tools"] == {
        "brain_delivery_attest": "1.0",
        "brain_delivery_attestation_list": "1.0",
    }


def test_kind_rules_are_the_code_constants() -> None:
    from brain_v42.models.delivery import (
        ATTESTATION_KIND_PATTERN,
        DOCUMENTED_ATTESTATION_KINDS,
        RESERVED_ATTESTATION_KINDS,
    )

    assert CONTRACT["kind"]["pattern"] == ATTESTATION_KIND_PATTERN
    assert CONTRACT["kind"]["documented"] == list(DOCUMENTED_ATTESTATION_KINDS)
    assert CONTRACT["kind"]["reserved"] == list(RESERVED_ATTESTATION_KINDS)
    assert CONTRACT["kind"]["enforced_vocabulary"] is False


def test_payload_bounds_are_the_code_constants() -> None:
    from brain_v42.models.delivery import (
        MAX_ATTESTATION_PAYLOAD_BYTES,
        MAX_ATTESTATION_PAYLOAD_DEPTH,
    )

    assert CONTRACT["payload"]["max_canonical_bytes"] == MAX_ATTESTATION_PAYLOAD_BYTES
    assert CONTRACT["payload"]["max_depth"] == MAX_ATTESTATION_PAYLOAD_DEPTH
    assert CONTRACT["payload"]["object_only"] is True
    assert CONTRACT["payload"]["floats"] == "refused"
    assert CONTRACT["payload"]["unicode_surrogates"] == "refused"


def test_digest_recipe_and_every_vector_recompute() -> None:
    from brain_v42.models.delivery_hashes import canonical_digest

    digest = CONTRACT["digest"]
    assert digest["algorithm"] == "sha256"
    assert digest["domain_prefix"] == "brain-delivery-attestation:v1\n"
    assert digest["canonical_json"] == {
        "sort_keys": True,
        "separators": [",", ":"],
        "ensure_ascii": False,
        "allow_nan": False,
        "encoding": "utf-8",
    }
    assert digest["format"] == "64 lowercase hexadecimal characters, no prefix"
    assert len(digest["vectors"]) >= 4
    for vector in digest["vectors"]:
        assert re.fullmatch(r"[0-9a-f]{64}", vector["digest"])
        assert canonical_digest(vector["payload"], domain="attestation") == vector["digest"]


def test_the_negative_example_is_refused_with_its_published_code() -> None:
    from brain_v42.models.delivery import DeliveryError, validate_attestation_form

    negative = CONTRACT["digest"]["negative_example"]
    with pytest.raises(DeliveryError) as excinfo:
        validate_attestation_form(
            kind="gate_passed",
            payload=negative["payload"],
            emitted_at=datetime(2026, 9, 16, 10, tzinfo=UTC),
        )
    assert excinfo.value.code == negative["error_code"] == "invalid_payload"


def test_error_codes_are_exactly_the_emitted_ones() -> None:
    from brain_v42.models.delivery import DELIVERY_ATTESTATION_ERROR_CODES

    published = CONTRACT["error_codes"]["tool"]
    assert len(published) == len(set(published))
    assert set(published) == set(DELIVERY_ATTESTATION_ERROR_CODES) == _emitted_codes()
    assert CONTRACT["error_codes"]["transport"] == ["invalid_arguments", "delivery_unavailable"]


def test_identity_replay_and_list_rules() -> None:
    from brain_v42.models.delivery import DeliveryAttestation

    assert CONTRACT["record_fields"] == list(DeliveryAttestation.model_fields)
    assert CONTRACT["uniqueness"] == ["ticket_id", "issuer_project", "idempotency_key"]
    assert CONTRACT["replay_equality"] == [
        "kind",
        "digest",
        "issuer_identity",
        "contract_revision",
        "emitted_at",
    ]
    assert CONTRACT["timestamps"]["emitted_at"]["timezone"] == "required"
    assert CONTRACT["timestamps"]["window"]["violation"] == "invalid_window"
    assert CONTRACT["contract_revision"]["max"] == 2**31 - 1
    assert CONTRACT["list"]["order"] == ["emitted_at DESC", "id DESC"]
    assert CONTRACT["list"]["limit"] == [1, 100]
    assert set(CONTRACT["list"]["scopes"]) == {"ticket", "issuer_project"}
