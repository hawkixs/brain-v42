"""Observer ownership uses the namespace reserved by the delivery repository."""


def test_observer_lock_uses_the_shared_delivery_namespace():
    from brain_v42.delivery_observer.ownership import ObserverOwnership
    from brain_v42.repositories.pg_delivery import DELIVERY_OBSERVER_LOCK

    assert ObserverOwnership.LOCK_KEY == DELIVERY_OBSERVER_LOCK
