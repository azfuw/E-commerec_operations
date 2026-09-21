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


async def test_worker_denies_operations_identity_without_creating_a_run(session):
    from fastapi import HTTPException
    from sqlalchemy import func, select
    from backend.common import UserRole
    from backend.logistics_models import AgentRun
    from backend.models import User
    from scripts.run_logistics_worker import patrol_once

    session.add(User(id="operations-worker", username="operations-worker", password_hash="unused", role=UserRole.SUPERVISOR))
    await session.commit()
    with pytest.raises(HTTPException) as denied:
        await patrol_once(session, "operations-worker")
    assert denied.value.status_code == 403
    assert await session.scalar(select(func.count(AgentRun.id))) == 0


async def test_worker_reloads_department_before_next_patrol(session):
    from fastapi import HTTPException
    from sqlalchemy import func, select, text
    from backend.logistics_models import AgentRun
    from backend.models import User
    from scripts.run_logistics_demo import seed_demo_identity
    from scripts.run_logistics_worker import patrol_once

    await seed_demo_identity(session)
    await session.commit()
    actor = await session.scalar(select(User).where(User.username == "logistics"))
    await patrol_once(session, actor.username)
    before = await session.scalar(select(func.count(AgentRun.id)))
    # Bypass the identity map, like an administrator using a different session.
    await session.execute(text("UPDATE users SET department='operations' WHERE id=:id"), {"id": actor.id})
    await session.commit()
    assert actor.department == "logistics"
    with pytest.raises(HTTPException) as denied:
        await patrol_once(session, actor.username)
    assert denied.value.status_code == 403
    assert await session.scalar(select(func.count(AgentRun.id))) == before
