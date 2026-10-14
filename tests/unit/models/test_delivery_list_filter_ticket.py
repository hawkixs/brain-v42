"""Ticket filtering is validated and binds continuation cursors to one ticket."""

from uuid import uuid4

from brain_v42.models.delivery import DeliveryListFilters
from brain_v42.models.delivery_hashes import canonical_digest
from brain_v42.repositories.delivery_cursor import list_scope


def test_ticket_id_is_validated():
    first = uuid4()
    assert DeliveryListFilters(ticket_id=str(first)).ticket_id == first


def test_ticket_id_scopes_the_cursor():
    first, second = uuid4(), uuid4()
    assert list_scope("brain-v42", DeliveryListFilters(ticket_id=first)) != list_scope(
        "brain-v42", DeliveryListFilters(ticket_id=second)
    )
    assert list_scope("brain-v42", DeliveryListFilters()) != list_scope(
        "brain-v42", DeliveryListFilters(ticket_id=first)
    )


def test_scope_without_ticket_preserves_existing_cursor_digest():
    assert list_scope("brain-v42", DeliveryListFilters()) == canonical_digest(
        {
            "operation": "delivery_list_cursor",
            "project": "brain-v42",
            "filters": {"work": None, "blocker": None, "stage": None},
        },
        domain="request",
    )
