import hashlib
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import store_visibility_predicate
from backend.common import (
    ApprovalActionType,
    ProposalRevisionOrigin,
    UserRole,
    UserStatus,
    WorkflowQuality,
    WorkflowStatus,
    WorkflowType,
)
from backend.models import (
    AnalysisCandidate,
    ApprovalAction,
    ComplianceReview,
    ManualReviewRun,
    PlatformDelivery,
    Product,
    ProductProposal,
    ProposalRevision,
    PublishRecord,
    Store,
    User,
    UserStoreScope,
    WorkflowRun,
)


@dataclass(frozen=True)
class ProposalDomainError(Exception):
    code: str
    status_code: int


@dataclass(frozen=True)
class ProductSelectionResult:
    proposal: ProductProposal
    optimization_run: WorkflowRun
    created: bool


@dataclass(frozen=True)
class ProposalReadResult:
    proposal: ProductProposal
    optimization_run: WorkflowRun
    current_revision: ProposalRevision | None
    current_review: ComplianceReview | None
    active_manual_review_run: ManualReviewRun | None
    active_manual_workflow: WorkflowRun | None
    submitted_revision: ProposalRevision | None
    latest_action: ApprovalAction | None
    publish_record: PublishRecord | None
    platform_delivery: PlatformDelivery | None


async def _selection_context(
    session: AsyncSession, *, actor_id: str, analysis_run_id: str
) -> WorkflowRun:
    user = await session.scalar(
        select(User)
        .where(User.id == actor_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if user is None or user.status is not UserStatus.ACTIVE:
        raise ProposalDomainError("ANALYSIS_SELECTION_NOT_FOUND", 404)
    if user.role is not UserRole.OPERATOR:
        raise ProposalDomainError("ANALYSIS_SELECTION_FORBIDDEN", 403)

    run = await session.scalar(
        select(WorkflowRun)
        .where(WorkflowRun.id == analysis_run_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if run is None:
        raise ProposalDomainError("ANALYSIS_SELECTION_NOT_FOUND", 404)

    store = await session.scalar(
        select(Store)
        .where(Store.id == run.store_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if store is None or not store.enabled:
        raise ProposalDomainError("ANALYSIS_SELECTION_NOT_FOUND", 404)

    scope = await session.scalar(
        select(UserStoreScope)
        .where(UserStoreScope.user_id == actor_id, UserStoreScope.store_id == store.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if scope is None:
        raise ProposalDomainError("ANALYSIS_SELECTION_NOT_FOUND", 404)
    return run


async def _optimization_for_proposal(
    session: AsyncSession,
    proposal: ProductProposal,
    *,
    lock: bool,
    error_code: Literal["ANALYSIS_SELECTION_UNAVAILABLE", "PROPOSAL_DATA_INCONSISTENT"] = "ANALYSIS_SELECTION_UNAVAILABLE",
) -> WorkflowRun:
    statement = (
        select(WorkflowRun)
        .where(WorkflowRun.id == proposal.optimization_run_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    run = await session.scalar(statement)
    if (
        run is None
        or run.workflow_type is not WorkflowType.OPTIMIZATION
        or not isinstance(run.input, dict)
        or run.input.get("proposal_id") != proposal.id
    ):
        raise ProposalDomainError(error_code, 503)
    return run


async def _replay_selection(
    session: AsyncSession,
    *,
    actor_id: str,
    analysis_run_id: str,
    candidate_id: str,
) -> ProductSelectionResult:
    await _selection_context(session, actor_id=actor_id, analysis_run_id=analysis_run_id)
    proposal = await session.scalar(
        select(ProductProposal)
        .where(ProductProposal.analysis_run_id == analysis_run_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if proposal is None:
        raise ProposalDomainError("ANALYSIS_SELECTION_UNAVAILABLE", 503)
    if proposal.analysis_candidate_id != candidate_id:
        raise ProposalDomainError("ANALYSIS_SELECTION_CONFLICT", 409)
    optimization_run = await _optimization_for_proposal(session, proposal, lock=True)
    await session.commit()
    return ProductSelectionResult(proposal=proposal, optimization_run=optimization_run, created=False)


async def select_product_for_optimization(
    session: AsyncSession,
    actor_id: str,
    analysis_run_id: str,
    candidate_id: str,
    idempotency_key: str | None,
) -> ProductSelectionResult:
    if (
        not isinstance(idempotency_key, str)
        or not idempotency_key.strip()
        or not 1 <= len(idempotency_key) <= 128
    ):
        raise ProposalDomainError("ANALYSIS_IDEMPOTENCY_KEY_INVALID", 400)
    key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()

    try:
        run = await _selection_context(session, actor_id=actor_id, analysis_run_id=analysis_run_id)
        proposal = await session.scalar(
            select(ProductProposal)
            .where(ProductProposal.analysis_run_id == run.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if proposal is not None:
            if proposal.analysis_candidate_id != candidate_id:
                raise ProposalDomainError("ANALYSIS_SELECTION_CONFLICT", 409)
            optimization_run = await _optimization_for_proposal(session, proposal, lock=True)
            await session.commit()
            return ProductSelectionResult(
                proposal=proposal, optimization_run=optimization_run, created=False
            )

        if run.workflow_type is not WorkflowType.ANALYSIS:
            raise ProposalDomainError("ANALYSIS_SELECTION_INVALID_TYPE", 409)
        if run.status is not WorkflowStatus.AWAITING_SELECTION:
            raise ProposalDomainError("ANALYSIS_SELECTION_NOT_READY", 409)

        candidate = await session.scalar(
            select(AnalysisCandidate)
            .where(
                AnalysisCandidate.id == candidate_id,
                AnalysisCandidate.workflow_run_id == run.id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if candidate is None:
            raise ProposalDomainError("ANALYSIS_SELECTION_NOT_FOUND", 404)
        product = await session.scalar(
            select(Product)
            .where(Product.id == candidate.product_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if product is None or product.store_id != run.store_id:
            raise ProposalDomainError("ANALYSIS_SELECTION_NOT_FOUND", 404)
        if product.id != candidate.product_id or product.code != candidate.product_code:
            raise ProposalDomainError("ANALYSIS_PRODUCT_MISMATCH", 409)

        proposal_id = str(uuid4())
        optimization_run = WorkflowRun(
            id=str(uuid4()),
            workflow_type=WorkflowType.OPTIMIZATION,
            store_id=run.store_id,
            created_by=actor_id,
            start_date=None,
            end_date=None,
            status=WorkflowStatus.ACCEPTED,
            quality_status=WorkflowQuality.NORMAL,
            input={
                "proposal_id": proposal_id,
                "source_analysis_run_id": run.id,
                "analysis_candidate_id": candidate.id,
                "product_id": product.id,
                "store_id": run.store_id,
            },
        )
        proposal = ProductProposal(
            id=proposal_id,
            analysis_run_id=run.id,
            analysis_candidate_id=candidate.id,
            optimization_run_id=optimization_run.id,
            store_id=run.store_id,
            product_id=product.id,
            base_product_version=product.current_version,
            selection_idempotency_hash=key_hash,
        )
        session.add(optimization_run)
        await session.flush()
        session.add(proposal)
        run.status = WorkflowStatus.COMPLETED
        run.current_step = "product_selected"
        await session.commit()
        return ProductSelectionResult(
            proposal=proposal, optimization_run=optimization_run, created=True
        )
    except IntegrityError:
        await session.rollback()
        try:
            return await _replay_selection(
                session,
                actor_id=actor_id,
                analysis_run_id=analysis_run_id,
                candidate_id=candidate_id,
            )
        except ProposalDomainError:
            await session.rollback()
            raise
        except SQLAlchemyError:
            await session.rollback()
            raise ProposalDomainError("ANALYSIS_SELECTION_UNAVAILABLE", 503) from None
    except ProposalDomainError:
        await session.rollback()
        raise
    except SQLAlchemyError:
        await session.rollback()
        raise ProposalDomainError("ANALYSIS_SELECTION_UNAVAILABLE", 503) from None


async def get_proposal_for_actor(
    session: AsyncSession, actor_id: str, proposal_id: str
) -> ProposalReadResult:
    try:
        user = await session.scalar(
            select(User)
            .where(User.id == actor_id)
            .execution_options(populate_existing=True)
        )
        if user is None or user.status is not UserStatus.ACTIVE or user.role not in {
            UserRole.OPERATOR,
            UserRole.SUPERVISOR,
            UserRole.ADMIN,
        }:
            raise ProposalDomainError("PROPOSAL_READ_FORBIDDEN", 403)

        proposal = await session.scalar(
            select(ProductProposal)
            .where(ProductProposal.id == proposal_id, store_visibility_predicate(user,ProductProposal.store_id))
            .execution_options(populate_existing=True)
        )
        if proposal is None:
            raise ProposalDomainError("PROPOSAL_NOT_FOUND", 404)
        store = await session.scalar(
            select(Store)
            .where(Store.id == proposal.store_id)
            .execution_options(populate_existing=True)
        )
        if store is None or not store.enabled:
            raise ProposalDomainError("PROPOSAL_NOT_FOUND", 404)
        optimization_run = await _optimization_for_proposal(
            session,
            proposal,
            lock=False,
            error_code="PROPOSAL_DATA_INCONSISTENT",
        )
        product = await session.scalar(
            select(Product)
            .where(Product.id == proposal.product_id)
            .execution_options(populate_existing=True)
        )
        if (
            product is None
            or product.store_id != store.id
            or optimization_run.store_id != store.id
            or optimization_run.input.get("product_id") != product.id
            or optimization_run.input.get("store_id") != store.id
        ):
            raise ProposalDomainError("PROPOSAL_DATA_INCONSISTENT", 503)

        revision = None
        review = None
        if proposal.current_revision_id is not None:
            revision = await session.scalar(
                select(ProposalRevision)
                .where(
                    ProposalRevision.id == proposal.current_revision_id,
                    ProposalRevision.proposal_id == proposal.id,
                )
                .execution_options(populate_existing=True)
            )
            if revision is None:
                raise ProposalDomainError("PROPOSAL_DATA_INCONSISTENT", 503)
            review = await session.scalar(
                select(ComplianceReview)
                .where(
                    ComplianceReview.proposal_revision_id == revision.id,
                    ComplianceReview.proposal_id == proposal.id,
                    ComplianceReview.iteration == revision.iteration,
                )
                .execution_options(populate_existing=True)
            )
            any_review = await session.scalar(
                select(ComplianceReview.id).where(ComplianceReview.proposal_revision_id == revision.id)
            )
            if review is None and any_review is not None:
                raise ProposalDomainError("PROPOSAL_DATA_INCONSISTENT", 503)

        active_manual_review_run = None
        active_manual_workflow = None
        if proposal.active_manual_review_run_id is not None:
            active_manual_review_run = await session.scalar(
                select(ManualReviewRun)
                .where(
                    ManualReviewRun.id == proposal.active_manual_review_run_id,
                    ManualReviewRun.proposal_id == proposal.id,
                )
                .execution_options(populate_existing=True)
            )
            if active_manual_review_run is not None:
                active_manual_workflow = await session.scalar(
                    select(WorkflowRun)
                    .where(WorkflowRun.id == active_manual_review_run.workflow_run_id)
                    .execution_options(populate_existing=True)
                )
            if (
                active_manual_review_run is None
                or active_manual_workflow is None
                or active_manual_workflow.workflow_type is not WorkflowType.MANUAL_REVIEW
                or active_manual_workflow.status
                not in {WorkflowStatus.ACCEPTED, WorkflowStatus.PROCESSING}
                or active_manual_workflow.store_id != store.id
                or active_manual_workflow.created_by != active_manual_review_run.submitted_by
                or not isinstance(active_manual_workflow.input, dict)
                or active_manual_workflow.input.get("manual_review_run_id")
                != active_manual_review_run.id
                or active_manual_workflow.input.get("proposal_id") != proposal.id
                or active_manual_workflow.input.get("proposal_revision_id")
                != active_manual_review_run.proposal_revision_id
                or active_manual_workflow.input.get("product_id") != product.id
                or active_manual_workflow.input.get("store_id") != store.id
                or revision is None
                or revision.id != active_manual_review_run.proposal_revision_id
                or revision.origin is not ProposalRevisionOrigin.MANUAL
                or revision.created_by != active_manual_review_run.submitted_by
            ):
                raise ProposalDomainError("PROPOSAL_DATA_INCONSISTENT", 503)

        submitted_revision = None
        if proposal.submitted_revision_id is not None:
            submitted_revision = await session.scalar(
                select(ProposalRevision)
                .where(
                    ProposalRevision.id == proposal.submitted_revision_id,
                    ProposalRevision.proposal_id == proposal.id,
                )
                .execution_options(populate_existing=True)
            )
            if submitted_revision is None:
                raise ProposalDomainError("PROPOSAL_DATA_INCONSISTENT", 503)

        latest_action = await session.scalar(
            select(ApprovalAction)
            .where(ApprovalAction.proposal_id == proposal.id)
            .order_by(ApprovalAction.created_at.desc(), ApprovalAction.id.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        if latest_action is not None:
            action_revision = await session.scalar(
                select(ProposalRevision.id).where(
                    ProposalRevision.id == latest_action.proposal_revision_id,
                    ProposalRevision.proposal_id == proposal.id,
                )
            )
            if latest_action.store_id != store.id or action_revision is None:
                raise ProposalDomainError("PROPOSAL_DATA_INCONSISTENT", 503)

        publish_record = await session.scalar(
            select(PublishRecord)
            .where(PublishRecord.proposal_id == proposal.id)
            .execution_options(populate_existing=True)
        )
        platform_delivery = None
        if publish_record is not None:
            publish_action = await session.scalar(
                select(ApprovalAction)
                .where(ApprovalAction.id == publish_record.approval_action_id)
                .execution_options(populate_existing=True)
            )
            snapshot_keys = {
                "title",
                "selling_points",
                "description",
                "search_keywords",
                "attributes",
                "current_version",
            }
            if (
                publish_record.store_id != store.id
                or publish_record.product_id != product.id
                or submitted_revision is None
                or publish_record.proposal_revision_id != submitted_revision.id
                or publish_action is None
                or publish_action.action is not ApprovalActionType.APPROVE
                or publish_action.proposal_id != proposal.id
                or publish_action.proposal_revision_id != publish_record.proposal_revision_id
                or publish_action.store_id != store.id
                or publish_action.actor_id != publish_record.approved_by
                or not isinstance(publish_record.before_snapshot, dict)
                or not isinstance(publish_record.after_snapshot, dict)
                or not set(publish_record.before_snapshot) <= snapshot_keys
                or not set(publish_record.after_snapshot) <= snapshot_keys
            ):
                raise ProposalDomainError("PROPOSAL_DATA_INCONSISTENT", 503)
            platform_delivery = await session.scalar(
                select(PlatformDelivery)
                .where(PlatformDelivery.publish_record_id == publish_record.id)
                .execution_options(populate_existing=True)
            )
            if platform_delivery is not None and (
                platform_delivery.publish_record_id != publish_record.id
                or platform_delivery.store_id != store.id
            ):
                raise ProposalDomainError("PROPOSAL_DATA_INCONSISTENT", 503)
        return ProposalReadResult(
            proposal=proposal,
            optimization_run=optimization_run,
            current_revision=revision,
            current_review=review,
            active_manual_review_run=active_manual_review_run,
            active_manual_workflow=active_manual_workflow,
            submitted_revision=submitted_revision,
            latest_action=latest_action,
            publish_record=publish_record,
            platform_delivery=platform_delivery,
        )
    except ProposalDomainError:
        await session.rollback()
        raise
    except SQLAlchemyError:
        await session.rollback()
        raise ProposalDomainError("PROPOSAL_READ_UNAVAILABLE", 503) from None
