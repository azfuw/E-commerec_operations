"""Local, persistent logistics demo. Never connects to a production database."""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import secrets
import sys
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEMO_PASSWORD = "Logistics!2026"


def configure_demo(data_dir: Path) -> Path:
    data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    secret_file = data_dir / "session.key"
    try:
        with secret_file.open("x", encoding="ascii") as target:
            target.write(secrets.token_urlsafe(48))
    except FileExistsError:
        pass
    database = data_dir / "logistics.db"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{database.as_posix()}"
    os.environ["JWT_SECRET_KEY"] = secret_file.read_text(encoding="ascii").strip()
    os.environ["APP_ENV"] = "logistics_demo"
    return database


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="启动本地物流演示工作台（仅监听 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "logistics-demo")
    parser.add_argument("--seed-only", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    return args


def demo_id(name: str) -> str:
    return str(uuid5(NAMESPACE_URL, "zhiliu-logistics-demo:" + name))


def upgrade_demo_departments(connection) -> None:
    """Upgrade the local SQLite schema once, without resetting account settings."""
    from sqlalchemy import inspect, text

    schema = inspect(connection)
    if not schema.has_table("users") or "department" in {column["name"] for column in schema.get_columns("users")}:
        return
    connection.exec_driver_sql(
        "ALTER TABLE users ADD COLUMN department VARCHAR(10) NOT NULL DEFAULT 'operations' "
        "CONSTRAINT user_department CHECK (department IN ('operations', 'logistics'))"
    )
    # Only identities created by this demo belong to its logistics department.
    connection.execute(text("UPDATE users SET department='logistics' WHERE id=:id AND username=:username"), [
        {"id": demo_id("user:" + username), "username": username} for username in ("logistics", "warehouse")
    ])


async def seed_demo_identity(session) -> None:
    from sqlalchemy import select
    from backend.auth import hash_password
    from backend.common import OrderStatus, UserDepartment, UserRole
    from backend.models import Order, Store, User, UserStoreScope

    now = datetime.now(UTC)
    stores = [("flagship", "拾物旗舰店"), ("digital", "拾物数码店"), ("home", "拾物家居店")]
    for code, name in stores:
        if not await session.get(Store, demo_id("store:" + code)):
            session.add(Store(id=demo_id("store:" + code), code="logistics-" + code, name=name))
    await session.flush()
    for username, role, department, scope in [
        ("logistics", UserRole.SUPERVISOR, UserDepartment.LOGISTICS, stores),
        ("warehouse", UserRole.OPERATOR, UserDepartment.LOGISTICS, stores[:1]),
        ("operations", UserRole.SUPERVISOR, UserDepartment.OPERATIONS, stores),
        ("admin", UserRole.ADMIN, UserDepartment.OPERATIONS, []),
    ]:
        actor = await session.scalar(select(User).where(User.username == username))
        if actor is None:
            actor = User(id=demo_id("user:" + username), username=username,
                         password_hash=hash_password(DEMO_PASSWORD), role=role, department=department)
            session.add(actor)
            await session.flush()
            for code, _ in scope:
                session.add(UserStoreScope(user_id=actor.id, store_id=demo_id("store:" + code)))
    for code, _ in stores:
        for index in range(20):
            order_id = f"EC2026{code[0].upper()}{index + 1:06d}"
            if await session.get(Order, order_id) is None:
                session.add(Order(id=order_id, store_id=demo_id("store:" + code),
                                  ordered_at=now - timedelta(days=14, minutes=index),
                                  status=OrderStatus.PAID, total_amount=Decimal("199.00")))
    await session.flush()


async def initialize_demo() -> None:
    from sqlalchemy import event
    from backend.database import async_session_factory, engine
    from backend.models import Base
    from backend.logistics_seed import seed_logistics_demo

    @event.listens_for(engine.sync_engine, "connect")
    def sqlite_options(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")

    async with engine.begin() as connection:
        await connection.run_sync(upgrade_demo_departments)
        await connection.run_sync(Base.metadata.create_all)
    async with async_session_factory() as session:
        await seed_demo_identity(session)
        await seed_logistics_demo(session)
        await session.commit()
    await engine.dispose()


def create_demo_app():
    from backend.main import create_app
    from scripts.run_logistics_worker import run_forever

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(run_forever("logistics", interval=60))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = create_app()
    app.router.lifespan_context = lifespan
    return app


def main(argv=None) -> None:
    args = parse_args(argv)
    configure_demo(args.data_dir)
    asyncio.run(initialize_demo())
    if args.seed_only:
        print("Logistics demo database is ready; existing data retained.")
        return
    if not (ROOT / "frontend" / "dist" / "index.html").exists():
        raise SystemExit("请先运行 npm --prefix frontend run build 或 start-logistics.ps1")
    import uvicorn
    print(f"物流工作台：http://127.0.0.1:{args.port}/app/logistics")
    print(f"本地演示账号：logistics / {DEMO_PASSWORD}")
    uvicorn.run(create_demo_app(), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
