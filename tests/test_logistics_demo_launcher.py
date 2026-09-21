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
    assert getattr(manager, "department", None) == "logistics"
    operations = await session.scalar(select(User).where(User.username == "operations"))
    admin = await session.scalar(select(User).where(User.username == "admin"))
    assert operations is not None and operations.department == "operations"
    assert admin is not None and admin.role.value == "admin"
    assert await session.scalar(select(func.count(UserStoreScope.store_id)).where(UserStoreScope.user_id == manager.id)) == 3


def test_previous_demo_upgrade_preserves_accounts_records_and_later_department_edits(tmp_path):
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path
    from scripts.run_logistics_demo import demo_id

    database = tmp_path / "logistics.db"
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TABLE users (
            id VARCHAR(36) PRIMARY KEY, username VARCHAR(64) NOT NULL UNIQUE,
            password_hash VARCHAR(255) NOT NULL, role VARCHAR(10) NOT NULL,
            status VARCHAR(8) NOT NULL, created_at DATETIME NOT NULL)""")
        connection.executemany("INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)", [
            (demo_id("user:logistics"), "logistics", "preserved-password", "supervisor", "disabled", "2026-09-01 00:00:00"),
            ("existing-operations", "existing-operations", "existing-password", "operator", "active", "2026-09-01 00:00:00"),
            ("unrelated-warehouse", "warehouse", "unrelated-password", "operator", "active", "2026-09-01 00:00:00"),
        ])

    def start():
        result = subprocess.run(
            [sys.executable, "-m", "scripts.run_logistics_demo", "--seed-only", "--data-dir", str(tmp_path)],
            cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr

    start()
    with sqlite3.connect(database) as connection:
        assert "department" in {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        assert connection.execute("SELECT department, password_hash, status FROM users WHERE username='logistics'").fetchone() == ("logistics", "preserved-password", "disabled")
        assert connection.execute("SELECT department FROM users WHERE id='existing-operations'").fetchone() == ("operations",)
        assert connection.execute("SELECT department FROM users WHERE id='unrelated-warehouse'").fetchone() == ("operations",)
        before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("logistics_shipments", "logistics_returns", "logistics_events")}
        assert all(before.values())
        connection.execute("UPDATE users SET department='operations' WHERE username='logistics'")
    start()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT department, password_hash, status FROM users WHERE username='logistics'").fetchone() == ("operations", "preserved-password", "disabled")
        after = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
        assert after == before
