from datetime import date
from uuid import uuid4

from sqlalchemy import and_, func, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.analysis_agent import AgentCallRecord
from backend.common import WorkflowQuality, WorkflowStatus
from backend.models import AgentCall, AnalysisCandidate, WorkflowRun
from backend.schemas import AnalysisCandidateView


def _lease_expiry(session: AsyncSession, lease_seconds: int):
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        return func.now() + text("make_interval(secs => :lease_seconds)").bindparams(
            lease_seconds=lease_seconds
        )
    return func.datetime(func.now(), literal(f"+{lease_seconds} seconds"))


def _owned_lease(workflow_run_id: str, lease_owner: str):
    return (
        WorkflowRun.id == workflow_run_id,
        WorkflowRun.status == WorkflowStatus.PROCESSING,
        WorkflowRun.lease_owner == lease_owner,
        WorkflowRun.lease_expires_at > func.now(),
    )


async def _commit_owned_update(session: AsyncSession, statement) -> bool:
    result = await session.execute(statement)
    if result.rowcount:
        await session.commit()
        return True
    await session.rollback()
    return False


async def create_analysis_run(
    session: AsyncSession,
    *,
    store_id: str,
    created_by: str,
    start_date: date,
    end_date: date,
) -> WorkflowRun:
    run = WorkflowRun(
        workflow_type="analysis",
        store_id=store_id,
        created_by=created_by,
        start_date=start_date,
        end_date=end_date,
        status=WorkflowStatus.ACCEPTED,
        quality_status=WorkflowQuality.NORMAL,
        input={
            "store_id": store_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
    )
    session.add(run)
    await session.flush()
    return run


async def get_workflow_run(
    session: AsyncSession, workflow_run_id: str
) -> WorkflowRun | None:
    return await session.get(WorkflowRun, workflow_run_id)


async def list_analysis_candidates(
    session: AsyncSession, workflow_run_id: str
) -> list[AnalysisCandidate]:
    return (
        await session.scalars(
            select(AnalysisCandidate)
            .where(AnalysisCandidate.workflow_run_id == workflow_run_id)
            .order_by(AnalysisCandidate.rank)
        )
    ).all()


async def claim_next_analysis_run(
    session: AsyncSession, *, lease_owner: str, lease_seconds: int
) -> WorkflowRun | None:
    await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.status == WorkflowStatus.PROCESSING,
            WorkflowRun.lease_expires_at < func.now(),
            WorkflowRun.attempt_count >= 3,
        )
        .values(
            status=WorkflowStatus.FAILED,
            lease_owner=None,
            lease_expires_at=None,
            current_step="failed",
            error_code="LEASE_ATTEMPTS_EXHAUSTED",
        )
    )
    eligible = or_(
        WorkflowRun.status == WorkflowStatus.ACCEPTED,
        and_(
            WorkflowRun.status == WorkflowStatus.PROCESSING,
            WorkflowRun.lease_expires_at < func.now(),
        ),
    )
    run = await session.scalar(
        select(WorkflowRun)
        .where(eligible, WorkflowRun.attempt_count < 3)
        .order_by(WorkflowRun.created_at, WorkflowRun.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if run is None:
        await session.commit()
        return None
    run.status = WorkflowStatus.PROCESSING
    run.lease_owner = lease_owner
    run.lease_expires_at = _lease_expiry(session, lease_seconds)
    run.attempt_count += 1
    run.current_step = "claimed"
    run.error_code = None
    await session.commit()
    return run


async def renew_analysis_lease(
    session: AsyncSession, *, workflow_run_id: str, lease_owner: str, lease_seconds: int
) -> bool:
    return await _commit_owned_update(
        session,
        update(WorkflowRun)
        .where(*_owned_lease(workflow_run_id, lease_owner))
        .values(lease_expires_at=_lease_expiry(session, lease_seconds)),
    )


async def update_analysis_step(
    session: AsyncSession, *, workflow_run_id: str, lease_owner: str, current_step: str
) -> bool:
    return await _commit_owned_update(
        session,
        update(WorkflowRun)
        .where(*_owned_lease(workflow_run_id, lease_owner))
        .values(current_step=current_step),
    )


def _insert_for(session: AsyncSession, model):
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        return postgresql_insert(model)
    return sqlite_insert(model)


async def persist_analysis_completion(
    session: AsyncSession,
    *,
    workflow_run_id: str,
    lease_owner: str,
    candidates: list[AnalysisCandidateView],
    calls: list[AgentCallRecord],
    quality_status: WorkflowQuality,
    quality: dict[str, object],
) -> bool:
    run = await session.scalar(
        select(WorkflowRun)
        .where(*_owned_lease(workflow_run_id, lease_owner))
        .with_for_update()
    )
    if run is None:
        await session.rollback()
        return False

    for candidate in candidates:
        values = {
            "id": str(uuid4()),
            "workflow_run_id": workflow_run_id,
            "product_id": candidate.product_id,
            "rank": candidate.rank,
            "product_code": candidate.product_code,
            "anomaly_types": candidate.anomaly_types,
            "metrics": candidate.metrics.model_dump(mode="json"),
            "business_impact": candidate.business_impact,
            "evidence": candidate.evidence,
            "impact_explanation": candidate.impact_explanation,
            "reason": candidate.reason,
            "recommended_action": candidate.recommended_action,
            "confidence": candidate.confidence,
        }
        insert = _insert_for(session, AnalysisCandidate).values(values)
        await session.execute(
            insert.on_conflict_do_update(
                index_elements=["workflow_run_id", "product_id"],
                set_={key: insert.excluded[key] for key in values if key not in {"id", "workflow_run_id"}},
            )
        )

    for call in calls:
        values = {
            "id": str(uuid4()),
            "workflow_run_id": workflow_run_id,
            "node_name": call.node_name,
            "call_type": call.call_type,
            "attempt": call.attempt,
            "model": call.model,
            "prompt_version": call.prompt_version,
            "status": call.status,
            "input_hash": call.input_hash,
            "prompt_tokens": call.prompt_tokens,
            "completion_tokens": call.completion_tokens,
            "total_tokens": call.total_tokens,
            "duration_ms": call.duration_ms,
            "estimated_cost": call.estimated_cost,
            "error_code": call.error_code,
        }
        insert = _insert_for(session, AgentCall).values(values)
        await session.execute(
            insert.on_conflict_do_update(
                index_elements=["workflow_run_id", "node_name", "call_type", "attempt"],
                set_={key: insert.excluded[key] for key in values if key not in {"id", "workflow_run_id"}},
            )
        )

    run.status = WorkflowStatus.AWAITING_SELECTION
    run.quality_status = quality_status
    run.lease_owner = None
    run.lease_expires_at = None
    run.current_step = "persist_results"
    run.output = {"candidate_count": len(candidates)}
    run.quality = quality
    await session.commit()
    return True


async def fail_analysis_run(
    session: AsyncSession, *, workflow_run_id: str, lease_owner: str, error_code: str
) -> bool:
    if error_code not in {
        "FACT_COLLECTION_ERROR",
        "INPUT_ERROR",
        "DATABASE_ERROR",
        "CHECKPOINT_ERROR",
    }:
        return False
    return await _commit_owned_update(
        session,
        update(WorkflowRun)
        .where(*_owned_lease(workflow_run_id, lease_owner))
        .values(
            status=WorkflowStatus.FAILED,
            lease_owner=None,
            lease_expires_at=None,
            current_step="failed",
            error_code=error_code,
        ),
    )
