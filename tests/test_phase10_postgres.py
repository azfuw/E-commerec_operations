import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from backend.database import engine


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="explicit PostgreSQL integration opt-in required",
)


def _migrations():
    modules = []
    for path in sorted(Path("alembic/versions").glob("000[1-7]*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        modules.append(module)
    return modules


async def _reject(connection, statement: str, parameters: dict[str, object] | None = None) -> None:
    savepoint = await connection.begin_nested()
    with pytest.raises(IntegrityError):
        await connection.execute(sa.text(statement), parameters or {})
    if savepoint.is_active:
        await savepoint.rollback()


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.postgres_integration
async def test_phase10_migration_enforces_delivery_and_webhook_constraints() -> None:
    migrations = _migrations()
    migration = migrations[-1]
    schema = f"phase10_schema_{uuid4().hex}"
    quoted_schema = f'"{schema}"'
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(sa.text(f"CREATE SCHEMA {quoted_schema}"))
            await connection.execute(sa.text(f"SET LOCAL search_path TO {quoted_schema}"))
            def run_migration(sync, action):
                with Operations.context(MigrationContext.configure(sync)):
                    action()

            for previous in migrations[:-1]:
                await connection.run_sync(run_migration, previous.upgrade)
            await connection.run_sync(run_migration, migration.upgrade)

            tables = set(await connection.scalars(sa.text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
            )))
            constraints = {
                (row.table_name, row.contype, row.conname)
                for row in (await connection.execute(sa.text(
                    "SELECT rel.relname AS table_name, con.contype::text AS contype, con.conname "
                    "FROM pg_constraint con JOIN pg_class rel ON rel.oid = con.conrelid "
                    "JOIN pg_namespace ns ON ns.oid = rel.relnamespace WHERE ns.nspname = current_schema()"
                )))
            }
            indexes = set(await connection.scalars(sa.text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() "
                "AND tablename = 'platform_deliveries'"
            )))
            assert {"platform_deliveries", "platform_webhook_receipts"} <= tables
            assert {name for table, kind, name in constraints if table == "platform_deliveries" and kind == "f"} == {
                "fk_platform_deliveries_publish_record_id",
                "fk_platform_deliveries_store_id",
            }
            assert ("platform_webhook_receipts", "f", "fk_platform_webhook_receipts_platform_delivery_id") in constraints
            assert ("platform_deliveries", "u", "uq_platform_deliveries_publish_record_id") in constraints
            assert ("platform_webhook_receipts", "u", "uq_platform_webhook_receipts_event_id") in constraints
            assert {"ix_platform_deliveries_claim", "ix_platform_deliveries_store_created"} <= indexes
            await connection.execute(sa.text(
                "ALTER TABLE platform_deliveries "
                "DROP CONSTRAINT fk_platform_deliveries_publish_record_id, "
                "DROP CONSTRAINT fk_platform_deliveries_store_id"
            ))

            base = (
                "INSERT INTO platform_deliveries "
                "(id,publish_record_id,store_id,provider,status,attempt_count,created_at,updated_at{columns}) "
                "VALUES (:id,'publish','store','contract_simulator',:status,:attempt,now(),now(){values})"
            )
            invalid = [
                ({"id": "bad-status", "status": "unknown", "attempt": 0}, "", ""),
                ({"id": "bad-attempt", "status": "pending", "attempt": 4}, "", ""),
                ({"id": "missing-lease", "status": "processing", "attempt": 1}, "", ""),
                ({"id": "partial-lease", "status": "processing", "attempt": 1}, ",lease_owner", ",'owner'"),
                ({"id": "bad-success", "status": "succeeded", "attempt": 1}, ",completed_at", ",now()"),
            ]
            for parameters, columns, values in invalid:
                await _reject(connection, base.format(columns=columns, values=values), parameters)

            await connection.execute(sa.text(
                "INSERT INTO platform_deliveries "
                "(id,publish_record_id,store_id,provider,status,attempt_count,error_code,created_at,updated_at,completed_at) "
                "VALUES ('failed','failed-publish','store','contract_simulator','failed',3,"
                "'PLATFORM_DELIVERY_FAILED',now(),now(),now())"
            ))
            await connection.execute(sa.text("DELETE FROM platform_deliveries WHERE id = 'failed'"))

            await connection.execute(
                sa.text(base.format(columns="", values="")),
                {"id": "delivery", "status": "pending", "attempt": 0},
            )
            await _reject(
                connection,
                base.format(columns="", values=""),
                {"id": "duplicate-publish", "status": "pending", "attempt": 0},
            )
            receipt = (
                "INSERT INTO platform_webhook_receipts "
                "(id,platform_delivery_id,event_id,event_type,payload_digest,received_at) "
                "VALUES (:id,'delivery','event','publish.confirmed',:digest,now())"
            )
            await _reject(
                connection,
                receipt,
                {"id": "invalid-digest", "digest": "g" * 64},
            )
            await connection.execute(sa.text(receipt), {"id": "receipt", "digest": "a" * 64})
            await _reject(connection, receipt, {"id": "duplicate-event", "digest": "b" * 64})

            await connection.execute(sa.text("DELETE FROM platform_webhook_receipts"))
            await connection.execute(sa.text("DELETE FROM platform_deliveries"))
            await connection.run_sync(run_migration, migration.downgrade)
            assert await connection.scalar(sa.text(
                "SELECT to_regclass(current_schema() || '.platform_deliveries')"
            )) is None
            await _reject(
                connection,
                "INSERT INTO audit_events (id,event_type,outcome,details,created_at) VALUES "
                "('downgraded-event','platform_delivery_enqueued','success','{}',now())",
            )
            await _reject(
                connection,
                "INSERT INTO audit_events (id,event_type,outcome,details,created_at,resource_type,resource_id) VALUES "
                "('downgraded-resource','authorization_denied','denied','{}',now(),'platform_delivery','delivery')",
            )

            await connection.run_sync(run_migration, migration.upgrade)
            await connection.execute(sa.text(
                "ALTER TABLE platform_deliveries "
                "DROP CONSTRAINT fk_platform_deliveries_publish_record_id, "
                "DROP CONSTRAINT fk_platform_deliveries_store_id"
            ))
            await connection.execute(
                sa.text(base.format(columns="", values="")),
                {"id": "fact", "status": "pending", "attempt": 0},
            )
            with pytest.raises(RuntimeError, match="cannot downgrade phase-ten production validation with facts"):
                await connection.run_sync(run_migration, migration.downgrade)
            await connection.execute(sa.text("DELETE FROM platform_deliveries"))
            await connection.execute(
                sa.text(
                    "INSERT INTO audit_events (id,event_type,outcome,details,created_at) VALUES "
                    "('phase10-audit','platform_delivery_enqueued','success','{}',now())"
                )
            )
            with pytest.raises(RuntimeError, match="cannot downgrade phase-ten production validation with facts"):
                await connection.run_sync(run_migration, migration.downgrade)
        finally:
            if transaction.is_active:
                await transaction.rollback()
