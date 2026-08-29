import hashlib
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common import UserRole, UserStatus, WorkflowQuality, WorkflowStatus, WorkflowType
from backend.models import (
    AnalysisCandidate,
    ComplianceReview,
    Product,
    ProductProposal,
    ProposalRevision,
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
            .where(ProductProposal.id == proposal_id)
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
        scope = await session.scalar(
            select(UserStoreScope)
            .where(UserStoreScope.user_id == actor_id, UserStoreScope.store_id == store.id)
            .execution_options(populate_existing=True)
        )
        if scope is None:
            raise ProposalDomainError("PROPOSAL_NOT_FOUND", 404)

        optimization_run = await _optimization_for_proposal(
            session,
            proposal,
            lock=False,
            error_code="PROPOSAL_DATA_INCONSISTENT",
        )

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
        return ProposalReadResult(
            proposal=proposal,
            optimization_run=optimization_run,
            current_revision=revision,
            current_review=review,
        )
    except ProposalDomainError:
        await session.rollback()
        raise
    except SQLAlchemyError:
        await session.rollback()
        raise ProposalDomainError("PROPOSAL_READ_UNAVAILABLE", 503) from None
