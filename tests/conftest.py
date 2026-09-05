import os
from collections.abc import AsyncIterator

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-at-least-32-characters")

import pytest_asyncio
import pytest
from sqlalchemy import event
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base


@pytest.fixture(scope='session', autouse=True)
def postgres_test_pool():
    if os.getenv('RUN_POSTGRES_INTEGRATION') != '1':
        yield
        return
    from backend.database import engine
    # Test modules own different event loops; asyncpg connections cannot cross them.
    original = engine.sync_engine.pool
    isolated = create_async_engine(engine.url, poolclass=NullPool)
    engine.sync_engine.pool = isolated.sync_engine.pool
    try:
        yield
    finally:
        engine.sync_engine.pool = original


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as test_session:
        try:
            yield test_session
        finally:
            await test_session.rollback()

    await engine.dispose()
