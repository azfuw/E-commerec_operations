import os
import importlib.util
from pathlib import Path
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from backend.config import get_settings
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from backend.audit_events import add_audit_event
from backend.common import (
    AuditEventType,
    AuditOutcome,
    EvaluationAgentType,
    EvaluationRunStatus,
    UserRole,
)
from backend.database import async_session_factory
from backend.models import (
    AuditEvent,
    EvaluationCase,
    EvaluationResult,
    EvaluationRun,
    Store,
    User,
    UserStoreScope,
)


@pytest.mark.asyncio(loop_scope='module')
async def test_postgres_audit_admin_global_and_scoped_disabled_history():
    from backend.audit_events import list_audit_events,AuditEventFilters
    suffix=uuid4().hex[:12]
    admin,supervisor,store=[f'p9-{kind}-{suffix}' for kind in ('a','s','st')]
    try:
      async with async_session_factory() as writer:
        writer.add_all([User(id=admin,username=admin,password_hash='test',role=UserRole.ADMIN),
            User(id=supervisor,username=supervisor,password_hash='test',role=UserRole.SUPERVISOR),Store(id=store,name='History',code=store,enabled=False)])
        await writer.flush();writer.add(UserStoreScope(user_id=supervisor,store_id=store))
        for value in (None,store):
            add_audit_event(writer,event_type=AuditEventType.ADMIN_STORE_UPDATED,outcome=AuditOutcome.SUCCESS,
                store_id=value,actor_id=admin,resource_type='store',resource_id=store,details={'store_enabled':False})
        await writer.commit()
      async with async_session_factory() as reader:
        rows,total=await list_audit_events(reader,actor_id=admin,filters=AuditEventFilters(actor_id=admin))
        assert total==2 and {r.store_id for r in rows}=={None,store}
        rows,total=await list_audit_events(reader,actor_id=supervisor,filters=AuditEventFilters(actor_id=admin))
        assert total==1 and rows[0].store_id==store
    finally:
      async with async_session_factory() as cleanup:
        await cleanup.execute(delete(AuditEvent).where(AuditEvent.actor_id==admin))
        await cleanup.execute(delete(UserStoreScope).where(UserStoreScope.user_id==supervisor))
        await cleanup.execute(delete(Store).where(Store.id==store))
        await cleanup.execute(delete(User).where(User.id.in_([admin,supervisor])))
        await cleanup.commit()


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="explicit PostgreSQL integration opt-in required",
)


@pytest.mark.asyncio(loop_scope='module')
async def test_postgres_evaluation_atomic_complete_and_failed_header(monkeypatch):
    from backend.agent_evaluations import persist_evaluation_run
    from test_agent_evaluations import arguments
    suffix = uuid4().hex[:12]
    ids = {key:f'p9-{key}-{suffix}' for key in ('admin','store','case')}
    try:
      async with async_session_factory() as session:
        session.add_all([User(id=ids['admin'],username=ids['admin'],password_hash='test',role=UserRole.ADMIN), Store(id=ids['store'],code=ids['store'],name='Test')])
        session.add(EvaluationCase(id=ids['case'],agent_type=EvaluationAgentType.ANALYSIS,case_key=ids['case'],case_version=1,fixture={'ids':['p']},expected={'valid':True}))
        await session.commit()
        args = arguments(); args.update(actor_id=ids['admin'],store_id=ids['store'])
        from backend.agent_evaluations import EvaluationResultInput
        cases = list(await session.scalars(select(EvaluationCase).where(EvaluationCase.enabled.is_(True),EvaluationCase.agent_type == EvaluationAgentType.ANALYSIS)))
        args['results'] = [EvaluationResultInput(c.id,'passed',args['results'][0].metrics,'EVALUATION_PASSED',1.0) for c in cases]
        complete = await persist_evaluation_run(session,**args)
        assert complete.status is EvaluationRunStatus.COMPLETED
        assert await session.scalar(select(func.count(EvaluationResult.id)).where(EvaluationResult.evaluation_run_id == complete.id)) == len(cases)
        original = session.flush
        failed_once = False
        async def fail_first(*a,**kw):
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                raise IntegrityError('INSERT',{},Exception('private failure'))
            await original(*a,**kw)
        monkeypatch.setattr(session,'flush',fail_first)
        failed = await persist_evaluation_run(session,**args)
        assert failed.status is EvaluationRunStatus.FAILED
        assert await session.scalar(select(func.count(EvaluationResult.id)).where(EvaluationResult.evaluation_run_id == failed.id)) == 0
        assert await session.scalar(select(func.count(AuditEvent.id)).where(AuditEvent.actor_id == ids['admin'])) == 2
    finally:
      async with async_session_factory() as session:
        await session.execute(delete(AuditEvent).where(AuditEvent.actor_id == ids['admin']))
        await session.execute(delete(EvaluationResult).where(EvaluationResult.evaluation_run_id.in_(select(EvaluationRun.id).where(EvaluationRun.created_by == ids['admin']))))
        await session.execute(delete(EvaluationRun).where(EvaluationRun.created_by == ids['admin']))
        await session.execute(delete(EvaluationCase).where(EvaluationCase.id == ids['case']))
        await session.execute(delete(Store).where(Store.id == ids['store']))
        await session.execute(delete(User).where(User.id == ids['admin']))
        await session.commit()


def test_postgres_migration_roundtrip_and_fact_guard() -> None:
    engine = sa.create_engine(get_settings().database_url.replace('+asyncpg', '+psycopg'))
    try:
      with engine.connect() as connection:
        transaction = connection.begin()
        try:
            schema = 'phase9_test_' + uuid4().hex
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
            migrations = []
            for path in sorted(Path('alembic/versions').glob('000[1-6]*.py')):
                spec = importlib.util.spec_from_file_location(path.stem, path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                migrations.append(module)
            with Operations.context(MigrationContext.configure(connection)):
                for migration in migrations[:-1]:
                    migration.upgrade()
                connection.execute(sa.text("INSERT INTO stores (id,name,code,enabled,created_at) VALUES ('old-store','old','old',true,now())"))
                connection.execute(sa.text("INSERT INTO audit_events (id,event_type,outcome,store_id,details,created_at) VALUES ('old-audit','authorization_denied','denied','old-store','{}',now())"))
                migration = migrations[-1]
                migration.upgrade()
                assert connection.scalar(sa.text("SELECT store_id FROM audit_events WHERE id='old-audit'")) == 'old-store'
                inspector = sa.inspect(connection)
                assert {'evaluation_cases','evaluation_runs','evaluation_results'} <= set(inspector.get_table_names())
                assert len(inspector.get_foreign_keys('evaluation_results')) == 2
                assert {'ix_evaluation_runs_agent_created','ix_evaluation_runs_store_created','ix_evaluation_runs_status_created'} <= {x['name'] for x in inspector.get_indexes('evaluation_runs')}
                migration.downgrade()
                assert 'evaluation_runs' not in sa.inspect(connection).get_table_names()
                migration.upgrade()
                for values in ["'user',NULL", "NULL,'some-id'", "'invalid','some-id'"]:
                    with pytest.raises(IntegrityError):
                      with connection.begin_nested():
                        connection.execute(sa.text(f"INSERT INTO audit_events (id,event_type,outcome,details,resource_type,resource_id,created_at) VALUES ('bad','admin_user_updated','success','{{}}',{values},now())"))
                with connection.begin_nested() as savepoint:
                    connection.execute(sa.text("INSERT INTO evaluation_cases (id,agent_type,case_key,case_version,fixture,expected,enabled,created_at,updated_at) VALUES ('case','analysis','case',1,:fixture,:expected,true,now(),now())"), {'fixture':'{"synthetic":true}', 'expected':'{"valid":true}'})
                    with pytest.raises(RuntimeError, match='cannot downgrade phase-nine management console with facts'):
                        migration.downgrade()
                    savepoint.rollback()
                for event_type in ['knowledge_document_created','knowledge_version_created','knowledge_document_disabled','evaluation_run_persisted','admin_user_updated','admin_user_scopes_replaced','admin_store_updated']:
                  with connection.begin_nested() as savepoint:
                    connection.execute(sa.text("INSERT INTO audit_events (id,event_type,outcome,store_id,details,created_at) VALUES ('new',:kind,'success','old-store','{}',now())"), {'kind':event_type})
                    with pytest.raises(RuntimeError, match='cannot downgrade phase-nine management console with facts'):
                        migration.downgrade()
                    savepoint.rollback()
        finally:
            transaction.rollback()
    finally:
        engine.dispose()


@pytest.mark.asyncio(loop_scope='module')
async def test_postgres_evaluation_facts_enforce_fk_unique_and_safe_audit() -> None:
    suffix = uuid4().hex[:16]
    ids = {
        "user": f"phase9-user-{suffix}",
        "store": f"phase9-store-{suffix}",
        "case": f"phase9-case-{suffix}",
        "run": f"phase9-run-{suffix}",
        "result": f"phase9-result-{suffix}",
        "audit": f"phase9-audit-{suffix}",
    }
    started = datetime.now(UTC)
    try:
        async with async_session_factory() as session:
            session.add_all(
                [
                    User(
                        id=ids["user"],
                        username=f"phase9-{suffix[:20]}",
                        password_hash="phase-nine-postgres",
                        role=UserRole.ADMIN,
                    ),
                    Store(
                        id=ids["store"],
                        name="阶段九测试店铺",
                        code=f"p9-{suffix[:16]}",
                    ),
                ]
            )
            await session.flush()
            session.add(
                EvaluationCase(
                    id=ids["case"],
                    agent_type=EvaluationAgentType.ANALYSIS,
                    case_key="analysis-minimal",
                    case_version=1,
                    fixture={"product_id": "synthetic-product"},
                    expected={"candidate_set_valid": True},
                )
            )
            session.add(
                EvaluationRun(
                    id=ids["run"],
                    agent_type=EvaluationAgentType.ANALYSIS,
                    store_id=ids["store"],
                    suite_version="phase9-v1",
                    runner_version="phase9-v1",
                    dataset_version="phase9-v1",
                    execution_mode="offline_fixture",
                    status=EvaluationRunStatus.COMPLETED,
                    started_at=started,
                    completed_at=started,
                    summary={
                        "total_cases": 1,
                        "passed_cases": 1,
                        "failed_cases": 0,
                        "average_latency_ms": 1.0,
                    },
                    created_by=ids["user"],
                )
            )
            await session.flush()
            session.add(
                EvaluationResult(
                    id=ids["result"],
                    evaluation_run_id=ids["run"],
                    evaluation_case_id=ids["case"],
                    agent_type=EvaluationAgentType.ANALYSIS,
                    outcome="passed",
                    metrics={
                        "candidate_set_valid": True,
                        "rank_order_valid": True,
                        "latency_ms": 1.0,
                    },
                    result_code="EVALUATION_PASSED",
                    latency_ms=1.0,
                )
            )
            add_audit_event(
                session,
                event_type=AuditEventType.EVALUATION_RUN_PERSISTED,
                outcome=AuditOutcome.SUCCESS,
                store_id=ids["store"],
                actor_id=ids["user"],
                actor_role=UserRole.ADMIN,
                resource_type="evaluation_run",
                resource_id=ids["run"],
                details={
                    "evaluation_agent_type": "analysis",
                    "evaluation_status": "completed",
                    "case_count": 1,
                },
            ).id = ids["audit"]
            await session.commit()

        async with async_session_factory() as session:
            with pytest.raises(IntegrityError):
              async with session.begin_nested():
                session.add(
                    EvaluationResult(
                        id=f"duplicate-{suffix}",
                        evaluation_run_id=ids["run"],
                        evaluation_case_id=ids["case"],
                        agent_type=EvaluationAgentType.ANALYSIS,
                        outcome="passed",
                        metrics={
                            "candidate_set_valid": True,
                            "rank_order_valid": True,
                            "latency_ms": 1.0,
                        },
                        result_code="EVALUATION_PASSED",
                        latency_ms=1.0,
                    )
                )
                await session.flush()
            assert await session.scalar(
                select(func.count(EvaluationResult.id)).where(
                    EvaluationResult.evaluation_run_id == ids["run"]
                )
            ) == 1
    finally:
        async with async_session_factory() as session:
            await session.execute(delete(AuditEvent).where(AuditEvent.id == ids["audit"]))
            await session.execute(
                delete(EvaluationResult).where(
                    EvaluationResult.evaluation_run_id == ids["run"]
                )
            )
            await session.execute(delete(EvaluationRun).where(EvaluationRun.id == ids["run"]))
            await session.execute(delete(EvaluationCase).where(EvaluationCase.id == ids["case"]))
            await session.execute(delete(Store).where(Store.id == ids["store"]))
            await session.execute(delete(User).where(User.id == ids["user"]))
            await session.commit()
