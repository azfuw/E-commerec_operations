import pytest


async def test_worker_refuses_unknown_or_disabled_identity(session):
    from scripts.run_logistics_worker import patrol_once
    from backend.common import UserRole, UserStatus
    from backend.models import User

    with pytest.raises(ValueError, match="active"):
        await patrol_once(session, "missing")
    session.add(User(id="disabled-logistics-worker", username="disabled-logistics-worker",
                     password_hash="not-used", role=UserRole.SUPERVISOR, status=UserStatus.DISABLED))
    await session.flush()
    with pytest.raises(ValueError, match="active"):
        await patrol_once(session, "disabled-logistics-worker")


async def test_periodic_patrol_uses_current_identity_scope_without_duplicate_tasks(session):
    from scripts.run_logistics_demo import seed_demo_identity
    from scripts.run_logistics_worker import patrol_once
    from backend.logistics_seed import seed_logistics_demo

    await seed_demo_identity(session)
    await seed_logistics_demo(session)
    first = await patrol_once(session, "warehouse")
    second = await patrol_once(session, "warehouse")
    assert first["scanned_shipments"] == 12
    assert first["created_exceptions"] == second["created_exceptions"] == 0
