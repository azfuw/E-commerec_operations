from datetime import UTC, date, datetime, timedelta

from sqlalchemy import update

from backend.common import UserRole, WorkflowQuality, WorkflowStatus, WorkflowType
from backend.models import Store, User, WorkflowRun
from backend.workflow_leases import (
    commit_owned_workflow_update,
    owned_workflow_lease,
    workflow_lease_expiry,
)


async def _add_live_typed_runs(session, owner: str) -> tuple[str, str]:
    store = Store(id="store-1", name="旗舰店", code="flagship")
    user = User(id="user-1", username="operator", password_hash="hash", role=UserRole.OPERATOR)
    lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
    analysis = WorkflowRun(
        id="analysis-1",
        workflow_type=WorkflowType.ANALYSIS,
        store_id=store.id,
        created_by=user.id,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2),
        status=WorkflowStatus.PROCESSING,
        quality_status=WorkflowQuality.NORMAL,
        lease_owner=owner,
        lease_expires_at=lease_expires_at,
    )
    optimization = WorkflowRun(
        id="optimization-1",
        workflow_type=WorkflowType.OPTIMIZATION,
        store_id=store.id,
        created_by=user.id,
        start_date=None,
        end_date=None,
        status=WorkflowStatus.PROCESSING,
        quality_status=WorkflowQuality.NORMAL,
        lease_owner=owner,
        lease_expires_at=lease_expires_at,
    )
    session.add_all([store, user, analysis, optimization])
    analysis_id, optimization_id = analysis.id, optimization.id
    await session.commit()
    return analysis_id, optimization_id


async def test_sqlite_lease_expiry_uses_datetime_not_postgres_interval(session) -> None:
    assert session.bind is not None
    rendered = str(
        workflow_lease_expiry(session, 60).compile(
            dialect=session.bind.dialect, compile_kwargs={"literal_binds": True}
        )
    ).lower()

    assert "datetime" in rendered
    assert "make_interval" not in rendered


async def test_owned_update_commits_for_matching_owner_and_rolls_back_for_stale_owner(session) -> None:
    analysis_id, _ = await _add_live_typed_runs(session, owner="worker-a")
    matched = update(WorkflowRun).where(
        *owned_workflow_lease(analysis_id, WorkflowType.ANALYSIS, "worker-a")
    ).values(current_step="matched")
    assert await commit_owned_workflow_update(session, matched) is True

    stale = update(WorkflowRun).where(
        *owned_workflow_lease(analysis_id, WorkflowType.ANALYSIS, "worker-b")
    ).values(current_step="must-not-write")
    assert await commit_owned_workflow_update(session, stale) is False

    refreshed = await session.get(WorkflowRun, analysis_id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.current_step == "matched"


async def test_owned_analysis_predicate_never_updates_same_owner_optimization_row(session) -> None:
    analysis_id, optimization_id = await _add_live_typed_runs(session, owner="worker-a")
    statement = update(WorkflowRun).where(
        *owned_workflow_lease(analysis_id, WorkflowType.ANALYSIS, "worker-a")
    ).values(current_step="analysis-next")
    assert await commit_owned_workflow_update(session, statement) is True

    stale_statement = update(WorkflowRun).where(
        *owned_workflow_lease(optimization_id, WorkflowType.ANALYSIS, "worker-a")
    ).values(current_step="must-not-write")
    assert await commit_owned_workflow_update(session, stale_statement) is False

    refreshed = await session.get(WorkflowRun, optimization_id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.current_step is None


async def test_manual_renewal_requires_manual_type_current_owner_and_live_lease(session) -> None:
    from backend.manual_review_runs import renew_manual_review_lease
    from tests.test_manual_review_worker import _seed_manual_chain

    chain = await _seed_manual_chain(session)
    analysis_id, optimization_id = chain["analysis"].id, chain["optimization"].id
    manual_id = chain["manual_workflow"].id

    assert await renew_manual_review_lease(
        session,
        workflow_run_id=manual_id,
        lease_owner="worker-a",
        lease_seconds=60,
    ) is True
    assert await renew_manual_review_lease(
        session,
        workflow_run_id=analysis_id,
        lease_owner="worker-a",
        lease_seconds=60,
    ) is False
    assert await renew_manual_review_lease(
        session,
        workflow_run_id=optimization_id,
        lease_owner="worker-a",
        lease_seconds=60,
    ) is False
    assert await renew_manual_review_lease(
        session,
        workflow_run_id=manual_id,
        lease_owner="worker-b",
        lease_seconds=60,
    ) is False
    await session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == manual_id)
        .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()
    assert await renew_manual_review_lease(
        session,
        workflow_run_id=manual_id,
        lease_owner="worker-a",
        lease_seconds=60,
    ) is False
