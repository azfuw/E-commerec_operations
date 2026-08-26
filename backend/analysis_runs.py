from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common import WorkflowQuality, WorkflowStatus
from backend.models import AnalysisCandidate, WorkflowRun


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
