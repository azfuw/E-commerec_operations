from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from backend.auth import has_department_access, store_visibility_predicate
from backend.common import (
    UserDepartment,
    UserRole,
    WorkflowQuality,
    WorkflowStatus,
    WorkflowType,
)
from backend.models import (
    ManualReviewRun,
    ProductProposal,
    Store,
    User,
    UserStoreScope,
    WorkflowRun,
)

WorkbenchKind = Literal["analysis", "proposal"]
WorkbenchAction = Literal[
    "wait",
    "select_product",
    "edit_proposal",
    "submit_proposal",
    "review_approval",
    "view_result",
    "resolve_failure",
]


@dataclass
class WorkbenchDomainError(Exception):
    code: str
    status_code: int


@dataclass(frozen=True)
class WorkbenchReadRow:
    id: str
    kind: WorkbenchKind
    store_id: str
    product_id: str | None
    analysis_run_id: str
    proposal_id: str | None
    workflow_run_id: str
    workflow_type: WorkflowType
    status: WorkflowStatus
    quality_status: WorkflowQuality
    current_step: str | None
    action_required: WorkbenchAction
    requires_current_user_action: bool
    created_by: str
    updated_at: datetime


def _task_action(
    role: UserRole,
    kind: WorkbenchKind,
    status: WorkflowStatus,
    has_active_manual: bool,
) -> tuple[WorkbenchAction, bool]:
    if kind == "proposal" and has_active_manual:
        return "wait", False
    if kind == "analysis":
        if status is WorkflowStatus.AWAITING_SELECTION:
            return ("select_product", True) if role is UserRole.OPERATOR else ("view_result", False)
        if status is WorkflowStatus.COMPLETED:
            return "view_result", False
        if status is WorkflowStatus.FAILED:
            return "resolve_failure", False
        return "wait", False
    if status is WorkflowStatus.DRAFT_READY:
        return "submit_proposal", True
    if status is WorkflowStatus.PENDING_MANUAL:
        return "edit_proposal", True
    if status is WorkflowStatus.PENDING_APPROVAL:
        if role in {UserRole.SUPERVISOR, UserRole.ADMIN}:
            return "review_approval", True
        return "wait", False
    if status in {WorkflowStatus.COMPLETED, WorkflowStatus.REJECTED}:
        return "view_result", False
    if status is WorkflowStatus.FAILED:
        return "resolve_failure", False
    return "wait", False


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def list_workbench_tasks(
    session: AsyncSession,
    *,
    actor_id: str,
    page: int,
    page_size: int,
    store_id: str | None,
    kind: WorkbenchKind | None,
    status: WorkflowStatus | None,
) -> tuple[list[WorkbenchReadRow], int]:
    try:
        actor = await session.scalar(
            select(User)
            .where(User.id == actor_id)
            .execution_options(populate_existing=True)
        )
        if not has_department_access(actor, UserDepartment.OPERATIONS):
            raise WorkbenchDomainError("WORKBENCH_FORBIDDEN", 403)

        authorized_store_ids = set(
            (
                await session.scalars(
                    select(Store.id)
                    .where(store_visibility_predicate(actor,Store.id))
                    .where(Store.enabled.is_(True))
                )
            ).all()
        )
        if store_id is not None:
            if store_id not in authorized_store_ids:
                raise WorkbenchDomainError("WORKBENCH_STORE_NOT_FOUND", 404)
            authorized_store_ids = {store_id}
        if not authorized_store_ids:
            return [], 0

        optimization_workflow = aliased(WorkflowRun)
        manual_workflow = aliased(WorkflowRun)
        proposal_rows = (
            await session.execute(
                select(
                    ProductProposal,
                    optimization_workflow,
                    ManualReviewRun,
                    manual_workflow,
                )
                .join(
                    optimization_workflow,
                    and_(
                        optimization_workflow.id == ProductProposal.optimization_run_id,
                        optimization_workflow.workflow_type == WorkflowType.OPTIMIZATION,
                        optimization_workflow.store_id == ProductProposal.store_id,
                    ),
                )
                .outerjoin(
                    ManualReviewRun,
                    and_(
                        ManualReviewRun.id == ProductProposal.active_manual_review_run_id,
                        ManualReviewRun.proposal_id == ProductProposal.id,
                    ),
                )
                .outerjoin(
                    manual_workflow,
                    and_(
                        manual_workflow.id == ManualReviewRun.workflow_run_id,
                        manual_workflow.workflow_type == WorkflowType.MANUAL_REVIEW,
                        manual_workflow.store_id == ProductProposal.store_id,
                    ),
                )
                .where(ProductProposal.store_id.in_(authorized_store_ids))
            )
        ).all()

        rows: list[WorkbenchReadRow] = []
        represented_analysis_ids: set[str] = set()
        for proposal, optimization, manual_run, manual in proposal_rows:
            represented_analysis_ids.add(proposal.analysis_run_id)
            has_active_manual = manual_run is not None and manual is not None
            current = manual if has_active_manual else optimization
            action, required = _task_action(
                actor.role, "proposal", current.status, has_active_manual
            )
            rows.append(
                WorkbenchReadRow(
                    id=proposal.id,
                    kind="proposal",
                    store_id=proposal.store_id,
                    product_id=proposal.product_id,
                    analysis_run_id=proposal.analysis_run_id,
                    proposal_id=proposal.id,
                    workflow_run_id=current.id,
                    workflow_type=current.workflow_type,
                    status=current.status,
                    quality_status=current.quality_status,
                    current_step=current.current_step,
                    action_required=action,
                    requires_current_user_action=required,
                    created_by=current.created_by,
                    updated_at=current.updated_at,
                )
            )

        analysis_runs = (
            await session.scalars(
                select(WorkflowRun).where(
                    WorkflowRun.workflow_type == WorkflowType.ANALYSIS,
                    WorkflowRun.store_id.in_(authorized_store_ids),
                )
            )
        ).all()
        for run in analysis_runs:
            if run.id in represented_analysis_ids:
                continue
            action, required = _task_action(actor.role, "analysis", run.status, False)
            rows.append(
                WorkbenchReadRow(
                    id=run.id,
                    kind="analysis",
                    store_id=run.store_id,
                    product_id=None,
                    analysis_run_id=run.id,
                    proposal_id=None,
                    workflow_run_id=run.id,
                    workflow_type=run.workflow_type,
                    status=run.status,
                    quality_status=run.quality_status,
                    current_step=run.current_step,
                    action_required=action,
                    requires_current_user_action=required,
                    created_by=run.created_by,
                    updated_at=run.updated_at,
                )
            )

        # ponytail: local single-company task volumes are merged in memory;
        # replace with a SQL UNION only when measured task volume makes this slow.
        rows.sort(key=lambda row: row.id)
        rows.sort(key=lambda row: _utc(row.updated_at), reverse=True)
        rows.sort(key=lambda row: not row.requires_current_user_action)
        if kind is not None:
            rows = [row for row in rows if row.kind == kind]
        if status is not None:
            rows = [row for row in rows if row.status is status]
        total = len(rows)
        start = (page - 1) * page_size
        return rows[start : start + page_size], total
    except WorkbenchDomainError:
        raise
    except SQLAlchemyError:
        await session.rollback()
        raise WorkbenchDomainError("WORKBENCH_READ_FAILED", 503) from None
