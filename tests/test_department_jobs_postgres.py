"""Real permission-lock checks in disposable PostgreSQL schemas."""

import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.common import KnowledgeVersionStatus, UserDepartment, UserRole, utc_now
from backend.config import get_settings
from backend.knowledge_runs import _knowledge_actor, disable_knowledge_document, renew_knowledge_lease
from backend.models import Base, KnowledgeDocument, KnowledgeDocumentVersion, User


pytestmark = pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="explicit PostgreSQL integration opt-in required")


@pytest_asyncio.fixture
async def knowledge_permission_factory():
    schema = "department_locks_" + uuid4().hex
    control = create_async_engine(get_settings().database_url)
    engine = create_async_engine(get_settings().database_url, connect_args={"server_settings": {
        "search_path": schema, "lock_timeout": "5000", "statement_timeout": "8000",
    }})
    try:
        async with control.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(User(id="knowledge-admin", username="knowledge-admin", password_hash="unused", role=UserRole.ADMIN))
            await session.flush()
            session.add(KnowledgeDocument(id="document", name="Permission test", category="test", created_by="knowledge-admin"))
            await session.flush()
            session.add(KnowledgeDocumentVersion(id="version", document_id="document", version_number=1,
                sha256="a" * 64, original_filename="test.md", mime_type="text/markdown", storage_path="unused",
                status=KnowledgeVersionStatus.PROCESSING, attempt_count=1, lease_owner="worker",
                lease_expires_at=utc_now() + timedelta(minutes=5)))
            await session.commit()
        yield engine, factory
    finally:
        await engine.dispose()
        async with control.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await control.dispose()


async def test_knowledge_renewal_and_disable_share_authorization_locks_without_deadlock(knowledge_permission_factory):
    engine, factory = knowledge_permission_factory
    version_locked, disabling_authorized = asyncio.Event(), asyncio.Event()

    class CoordinatedSession(AsyncSession):
        async def scalar(self, statement, *args, **kwargs):
            result = await super().scalar(statement, *args, **kwargs)
            entity = statement.column_descriptions[0]["entity"]
            if statement._for_update_arg is not None:
                if self.info["operation"] == "renew" and entity is KnowledgeDocumentVersion:
                    version_locked.set()
                    await disabling_authorized.wait()
                elif self.info["operation"] == "disable" and entity is User:
                    disabling_authorized.set()
            return result

    async def renew():
        async with CoordinatedSession(engine, expire_on_commit=False, info={"operation": "renew"}) as session:
            return await renew_knowledge_lease(session, version_id="version", lease_owner="worker", lease_seconds=60)

    async def disable():
        await version_locked.wait()
        async with CoordinatedSession(engine, expire_on_commit=False, info={"operation": "disable"}) as session:
            return await disable_knowledge_document(session, document_id="document", actor_id="knowledge-admin")

    results = await asyncio.wait_for(asyncio.gather(renew(), disable(), return_exceptions=True), timeout=10)
    assert results == [True, True], results
    async with factory() as session:
        assert (await session.get(KnowledgeDocument, "document")).enabled is False
        assert (await session.get(KnowledgeDocumentVersion, "version")).status is KnowledgeVersionStatus.DISABLED


async def test_knowledge_authorization_share_lock_blocks_permission_updates_until_release(knowledge_permission_factory):
    _, factory = knowledge_permission_factory
    async with factory() as reader, factory() as writer:
        await _knowledge_actor(reader, "knowledge-admin")
        await writer.execute(text("SET LOCAL lock_timeout = '150ms'"))
        change = update(User).where(User.id == "knowledge-admin").values(role=UserRole.SUPERVISOR, department=UserDepartment.LOGISTICS)
        with pytest.raises(DBAPIError) as error:
            await writer.execute(change)
        assert error.value.orig.sqlstate == "55P03"
        await writer.rollback()
        await reader.commit()
        await writer.execute(change)
        await writer.commit()
        assert await writer.scalar(select(User.department).where(User.id == "knowledge-admin")) is UserDepartment.LOGISTICS
