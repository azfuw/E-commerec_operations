import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.database import engine
from backend.platform_delivery_runs import (
    claim_next_platform_delivery,
    complete_platform_delivery,
)
from backend.platform_webhooks import receive_platform_webhook


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


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.postgres_integration
async def test_webhook_exact_duplicate_unique_race_writes_once() -> None:
    schema = f"phase10_webhook_{uuid4().hex}"
    quoted_schema = f'"{schema}"'
    ids = {name: f"{name}-{uuid4().hex[:12]}" for name in (
        "user", "store", "product", "analysis", "optimization", "candidate",
        "proposal", "revision", "action", "publish", "delivery",
    )}
    secret = "postgres-webhook-secret"
    timestamp = str(int(datetime.now(UTC).timestamp()))
    body = json.dumps(
        {
            "event_type": "publish.confirmed",
            "delivery_id": ids["delivery"],
            "external_operation_id": "operation-1",
        },
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(
        secret.encode(),
        timestamp.encode() + b".event-race." + body,
        hashlib.sha256,
    ).hexdigest()

    try:
        async with engine.begin() as connection:
            await connection.execute(sa.text(f"CREATE SCHEMA {quoted_schema}"))
            await connection.execute(sa.text(f"SET LOCAL search_path TO {quoted_schema}"))

            def run_migration(sync, action):
                with Operations.context(MigrationContext.configure(sync)):
                    action()

            for migration in _migrations():
                await connection.run_sync(run_migration, migration.upgrade)

        scoped_engine = create_async_engine(
            engine.url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": schema}},
        )
        factory = async_sessionmaker(scoped_engine, expire_on_commit=False)
        try:
            async with factory() as session:
                statements = [
                    (
                        "INSERT INTO users (id,username,password_hash,role,status,created_at) "
                        "VALUES (:user,:user,'test','supervisor','active',now())",
                        ids,
                    ),
                    (
                        "INSERT INTO stores (id,name,code,enabled,created_at) "
                        "VALUES (:store,'Webhook',:store,true,now())",
                        ids,
                    ),
                    (
                        "INSERT INTO products (id,store_id,code,title,category,brand,selling_points,"
                        "description,search_keywords,attributes,current_version,enabled) VALUES "
                        "(:product,:store,:product,'Webhook','test','', '[]','', '[]','{}',2,true)",
                        ids,
                    ),
                    (
                        "INSERT INTO workflow_runs (id,workflow_type,store_id,created_by,start_date,end_date,"
                        "status,quality_status,attempt_count,input,output,quality,created_at,updated_at) VALUES "
                        "(:analysis,'analysis',:store,:user,CURRENT_DATE,CURRENT_DATE,'completed','normal',0,"
                        "'{}','{}','{}',now(),now()), "
                        "(:optimization,'optimization',:store,:user,NULL,NULL,'completed','normal',0,"
                        "'{}','{}','{}',now(),now())",
                        ids,
                    ),
                    (
                        "INSERT INTO analysis_candidates (id,workflow_run_id,product_id,rank,product_code,"
                        "anomaly_types,metrics,business_impact,evidence,impact_explanation,reason,"
                        "recommended_action,confidence,created_at,updated_at) VALUES "
                        "(:candidate,:analysis,:product,1,:product,'[]','{}',0,'[]','test','test','test',1,now(),now())",
                        ids,
                    ),
                    (
                        "INSERT INTO product_proposals (id,analysis_run_id,analysis_candidate_id,optimization_run_id,"
                        "store_id,product_id,base_product_version,selection_idempotency_hash,created_at,updated_at) "
                        "VALUES (:proposal,:analysis,:candidate,:optimization,:store,:product,1,:hash,now(),now())",
                        {**ids, "hash": "a" * 64},
                    ),
                    (
                        "INSERT INTO proposal_revisions (id,proposal_id,iteration,revision_number,origin,created_by,"
                        "base_product_version,trusted_fact_hash,proposal_output,citations,created_at) VALUES "
                        "(:revision,:proposal,0,1,'agent',:user,1,:hash,'{}','[]',now())",
                        {**ids, "hash": "b" * 64},
                    ),
                    (
                        "INSERT INTO approval_actions (id,proposal_id,proposal_revision_id,store_id,actor_id,actor_role,"
                        "action,comment,idempotency_key_hash,request_hash,created_at) VALUES "
                        "(:action,:proposal,:revision,:store,:user,'supervisor','approve',NULL,:key_hash,:request_hash,now())",
                        {**ids, "key_hash": "c" * 64, "request_hash": "d" * 64},
                    ),
                    (
                        "INSERT INTO publish_records (id,proposal_id,proposal_revision_id,product_id,store_id,approved_by,"
                        "approval_action_id,publish_idempotency_hash,before_snapshot,after_snapshot,base_product_version,"
                        "published_product_version,published_at) VALUES "
                        "(:publish,:proposal,:revision,:product,:store,:user,:action,:hash,'{}','{}',1,2,now())",
                        {**ids, "hash": "e" * 64},
                    ),
                    (
                        "INSERT INTO platform_deliveries (id,publish_record_id,store_id,provider,status,attempt_count,"
                        "external_operation_id,created_at,updated_at,completed_at) VALUES "
                        "(:delivery,:publish,:store,'contract_simulator','succeeded',1,'operation-1',now(),now(),now())",
                        ids,
                    ),
                ]
                for statement, parameters in statements:
                    await session.execute(sa.text(statement), parameters)
                await session.commit()

            barrier = asyncio.Barrier(2)

            async def receive() -> bool:
                async with factory() as session:
                    commit = session.commit

                    async def synchronized_commit() -> None:
                        await barrier.wait()
                        await commit()

                    session.commit = synchronized_commit
                    return await receive_platform_webhook(
                        session,
                        body=body,
                        event_id="event-race",
                        timestamp=timestamp,
                        signature=signature,
                        secret=secret,
                    )

            assert sorted(await asyncio.gather(receive(), receive())) == [False, True]
            async with factory() as session:
                assert await session.scalar(sa.text(
                    "SELECT count(*) FROM platform_webhook_receipts WHERE event_id='event-race'"
                )) == 1
                assert await session.scalar(sa.text(
                    "SELECT count(*) FROM audit_events WHERE event_type='platform_webhook_received'"
                )) == 1
        finally:
            await scoped_engine.dispose()
    finally:
        async with engine.begin() as connection:
            await connection.execute(sa.text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE"))


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.postgres_integration
@pytest.mark.parametrize("successive_versions", [False, True])
async def test_platform_delivery_claim_skip_locked_reclaim_and_stale_owner_guard(successive_versions) -> None:
    schema = f"phase10_delivery_{uuid4().hex}"
    quoted_schema = f'"{schema}"'
    ids = {
        name: f"{name}-{uuid4().hex[:12]}"
        for name in (
            "user",
            "store",
            "product",
            "analysis",
            "optimization",
            "candidate",
            "proposal",
            "revision",
            "action",
            "publish",
            "delivery",
        )
    }
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.text(f"CREATE SCHEMA {quoted_schema}"))
            await connection.execute(sa.text(f"SET LOCAL search_path TO {quoted_schema}"))

            def run_migration(sync, action):
                with Operations.context(MigrationContext.configure(sync)):
                    action()

            for migration in _migrations():
                await connection.run_sync(run_migration, migration.upgrade)

        scoped_engine = create_async_engine(
            engine.url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": schema}},
        )
        factory = async_sessionmaker(scoped_engine, expire_on_commit=False)
        try:
            async with factory() as session:
                statements = [
                    (
                        "INSERT INTO users (id,username,password_hash,role,status,created_at) "
                        "VALUES (:user,:user,'test','supervisor','active',now())",
                        ids,
                    ),
                    (
                        "INSERT INTO stores (id,name,code,enabled,created_at) "
                        "VALUES (:store,'Delivery',:store,true,now())",
                        ids,
                    ),
                    (
                        "INSERT INTO products (id,store_id,code,title,category,brand,selling_points,"
                        "description,search_keywords,attributes,current_version,enabled) VALUES "
                        "(:product,:store,:product,'Delivery','test','', '[]','', '[]','{}',2,true)",
                        ids,
                    ),
                    (
                        "INSERT INTO workflow_runs (id,workflow_type,store_id,created_by,start_date,end_date,"
                        "status,quality_status,attempt_count,input,output,quality,created_at,updated_at) VALUES "
                        "(:analysis,'analysis',:store,:user,CURRENT_DATE,CURRENT_DATE,'completed','normal',0,"
                        "'{}','{}','{}',now(),now()), "
                        "(:optimization,'optimization',:store,:user,NULL,NULL,'completed','normal',0,"
                        "'{}','{}','{}',now(),now())",
                        ids,
                    ),
                    (
                        "INSERT INTO analysis_candidates (id,workflow_run_id,product_id,rank,product_code,"
                        "anomaly_types,metrics,business_impact,evidence,impact_explanation,reason,"
                        "recommended_action,confidence,created_at,updated_at) VALUES "
                        "(:candidate,:analysis,:product,1,:product,'[]','{}',0,'[]','test','test','test',1,now(),now())",
                        ids,
                    ),
                    (
                        "INSERT INTO product_proposals (id,analysis_run_id,analysis_candidate_id,optimization_run_id,"
                        "store_id,product_id,base_product_version,selection_idempotency_hash,created_at,updated_at) "
                        "VALUES (:proposal,:analysis,:candidate,:optimization,:store,:product,:base_version,:hash,now(),now())",
                        {**ids, "hash": "a" * 64},
                    ),
                    (
                        "INSERT INTO proposal_revisions (id,proposal_id,iteration,revision_number,origin,created_by,"
                        "base_product_version,trusted_fact_hash,proposal_output,citations,created_at) VALUES "
                        "(:revision,:proposal,0,1,'agent',:user,:base_version,:hash,'{}','[]',now())",
                        {**ids, "hash": "b" * 64},
                    ),
                    (
                        "INSERT INTO approval_actions (id,proposal_id,proposal_revision_id,store_id,actor_id,actor_role,"
                        "action,comment,idempotency_key_hash,request_hash,created_at) VALUES "
                        "(:action,:proposal,:revision,:store,:user,'supervisor','approve',NULL,:key_hash,:request_hash,now())",
                        {**ids, "key_hash": "c" * 64, "request_hash": "d" * 64},
                    ),
                    (
                        "INSERT INTO publish_records (id,proposal_id,proposal_revision_id,product_id,store_id,approved_by,"
                        "approval_action_id,publish_idempotency_hash,before_snapshot,after_snapshot,base_product_version,"
                        "published_product_version,published_at) VALUES "
                        "(:publish,:proposal,:revision,:product,:store,:user,:action,:hash,'{}','{}',:base_version,:published_version,now())",
                        {**ids, "hash": "e" * 64},
                    ),
                    (
                        "INSERT INTO platform_deliveries (id,publish_record_id,store_id,provider,status,attempt_count,"
                        "created_at,updated_at) VALUES "
                        "(:delivery,:publish,:store,'contract_simulator','pending',0,now(),now())",
                        ids,
                    ),
                ]
                for statement, parameters in statements:
                    await session.execute(sa.text(statement), {**parameters, "base_version": 1, "published_version": 2})
                await session.commit()

            newer_ids = {name: f"{name}-{uuid4().hex[:12]}" for name in ids}
            newer_ids.update({key: ids[key] for key in ("user", "store", "product")})
            other_ids = {name: f"{name}-{uuid4().hex[:12]}" for name in ids}
            other_ids.update({key: ids[key] for key in ("user", "store")})
            if successive_versions:
                async with factory() as session:
                    # Real FK chains for the next version and an unrelated product in the same store.
                    for record_ids, start, version in ((newer_ids, 3, 3), (other_ids, 2, 2)):
                        for statement, parameters in statements[start:]:
                            parameters = {**parameters, **record_ids}
                            for key in ("hash", "key_hash", "request_hash"):
                                if key in parameters:
                                    parameters[key] = hashlib.sha256((key + record_ids["publish"]).encode()).hexdigest()
                            await session.execute(sa.text(statement), {**parameters, "base_version": version - 1, "published_version": version})
                    await session.execute(sa.text(
                        "UPDATE products SET current_version=3 WHERE id=:product"
                    ), ids)
                    await session.commit()

            locked, release = asyncio.Event(), asyncio.Event()

            async def claim_first():
                async with factory() as session:
                    commit = session.commit

                    async def hold_claim_lock():
                        locked.set()
                        await release.wait()
                        await commit()

                    session.commit = hold_claim_lock
                    return await claim_next_platform_delivery(session, lease_owner="worker-a", lease_seconds=60)

            first = asyncio.create_task(claim_first())
            try:
                await asyncio.wait_for(locked.wait(), timeout=10)
                async with factory() as session:
                    second = await asyncio.wait_for(claim_next_platform_delivery(
                        session, lease_owner="worker-b", lease_seconds=60,
                    ), timeout=10)
            finally:
                release.set()
                first_claim = await first
            assert first_claim is not None and first_claim.id == ids["delivery"]
            if successive_versions:
                assert second is not None and second.id == other_ids["delivery"]
            else:
                assert second is None
            first_owner = first_claim.lease_owner
            assert first_owner == "worker-a"
            async with factory() as session:
                assert await claim_next_platform_delivery(session, lease_owner="blocked", lease_seconds=60) is None

            async with factory() as session:
                await session.execute(
                    sa.text(
                        "UPDATE platform_deliveries SET lease_expires_at=now()-interval '1 second' "
                        "WHERE id=:delivery"
                    ),
                    ids,
                )
                await session.commit()
            async with factory() as session:
                reclaimed = await claim_next_platform_delivery(
                    session, lease_owner="worker-reclaimed", lease_seconds=60
                )
                assert reclaimed is not None and reclaimed.id == ids["delivery"]
            async with factory() as session:
                assert not await complete_platform_delivery(
                    session,
                    delivery_id=ids["delivery"],
                    lease_owner=first_owner,
                    external_operation_id="operation-stale",
                )
            async with factory() as session:
                assert await complete_platform_delivery(
                    session,
                    delivery_id=ids["delivery"],
                    lease_owner="worker-reclaimed",
                    external_operation_id="operation-current",
                )
            async with factory() as session:
                row = (
                    await session.execute(
                        sa.text(
                            "SELECT status,attempt_count,lease_owner,external_operation_id "
                            "FROM platform_deliveries WHERE id=:delivery"
                        ),
                        ids,
                    )
                ).one()
                assert tuple(row) == (
                    "succeeded",
                    2,
                    None,
                    "operation-current",
                )
            if successive_versions:
                async with factory() as session:
                    released = await claim_next_platform_delivery(session, lease_owner="newer", lease_seconds=60)
                    assert released is not None and released.id == newer_ids["delivery"]
        finally:
            await scoped_engine.dispose()
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE")
            )
