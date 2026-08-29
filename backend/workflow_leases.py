from datetime import datetime

from sqlalchemy import Update, func, literal, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from backend.common import WorkflowStatus, WorkflowType
from backend.models import WorkflowRun


def workflow_lease_expiry(
    session: AsyncSession, lease_seconds: int
) -> ColumnElement[datetime]:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        return func.now() + text("make_interval(secs => :lease_seconds)").bindparams(
            lease_seconds=lease_seconds
        )
    return func.datetime(func.now(), literal(f"+{lease_seconds} seconds"))


def owned_workflow_lease(
    workflow_run_id: str, workflow_type: WorkflowType, lease_owner: str
) -> tuple[ColumnElement[bool], ...]:
    return (
        WorkflowRun.id == workflow_run_id,
        WorkflowRun.workflow_type == workflow_type,
        WorkflowRun.status == WorkflowStatus.PROCESSING,
        WorkflowRun.lease_owner == lease_owner,
        WorkflowRun.lease_expires_at > func.now(),
    )


async def commit_owned_workflow_update(session: AsyncSession, statement: Update) -> bool:
    result = await session.execute(statement)
    if result.rowcount > 0:
        await session.commit()
        return True
    await session.rollback()
    return False
