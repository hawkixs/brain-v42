"""Tests for RelationInput Pydantic model."""

import pytest
from pydantic import ValidationError

from brain_v42.models.relation import RelationInput


class TestRelationInput:
    def test_valid_relation(self):
        r = RelationInput(id="550e8400-e29b-41d4-a716-446655440000", type="MOTIVATED_BY")
        assert r.type == "MOTIVATED_BY"

    def test_all_valid_types(self):
        for t in ["MOTIVATED_BY", "IMPLEMENTS", "DOCUMENTS", "USES", "RELATED_TO"]:
            r = RelationInput(id="550e8400-e29b-41d4-a716-446655440000", type=t)
            assert r.type == t

    def test_invalid_type_rejected(self):
        with pytest.raises(ValidationError):
            RelationInput(id="550e8400-e29b-41d4-a716-446655440000", type="INVALID")

    def test_invalid_uuid_rejected(self):
        with pytest.raises(ValidationError):
            RelationInput(id="not-a-uuid", type="MOTIVATED_BY")
