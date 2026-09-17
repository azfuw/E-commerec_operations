import pytest


def test_demo_configuration_is_local_persistent_and_keeps_secrets_out_of_output(tmp_path, monkeypatch):
    from scripts.run_logistics_demo import configure_demo

    # Register all mutated keys even when absent, so pytest restores the environment.
    for name in ("DATABASE_URL", "JWT_SECRET_KEY", "APP_ENV"):
        monkeypatch.setenv(name, "before-demo-configuration")
    first = configure_demo(tmp_path)
    import os

    key = os.environ["JWT_SECRET_KEY"]
    assert first == tmp_path / "logistics.db"
    assert os.environ["DATABASE_URL"] == f"sqlite+aiosqlite:///{first.as_posix()}"
    assert len(key) >= 32
    assert configure_demo(tmp_path) == first
    assert os.environ["JWT_SECRET_KEY"] == key


def test_demo_cli_rejects_out_of_range_ports():
    from scripts.run_logistics_demo import parse_args

    assert parse_args([]).port == 8010
    with pytest.raises(SystemExit):
        parse_args(["--port", "65536"])


async def test_demo_identity_seed_is_repeatable_and_store_scoped(session):
    from scripts.run_logistics_demo import seed_demo_identity
    from backend.models import Order, Store, User, UserStoreScope
    from sqlalchemy import func, select

    await seed_demo_identity(session)
    await session.commit()
    await seed_demo_identity(session)
    await session.commit()
    assert await session.scalar(select(func.count(Store.id))) == 3
    assert await session.scalar(select(func.count(Order.id))) == 60
    manager = await session.scalar(select(User).where(User.username == "logistics"))
    assert manager is not None and manager.role.value == "supervisor"
    assert await session.scalar(select(func.count(UserStoreScope.store_id)).where(UserStoreScope.user_id == manager.id)) == 3
