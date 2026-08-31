from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.audit_events import add_audit_event
from backend.common import (
    AuditEventType,
    AuditOutcome,
    ProposalRevisionOrigin,
    UserRole,
    UserStatus,
    WorkflowQuality,
    WorkflowStatus,
    WorkflowType,
)
from backend.manual_reviews import _sha256
from backend.models import (
    AnalysisCandidate,
    ComplianceReview,
    ManualReviewRun,
    Product,
    ProductProposal,
    ProductSku,
    ProposalRevision,
    Store,
    User,
    UserStoreScope,
    WorkflowRun,
)
from backend.optimization_runs import _recheck_canonical_citations
from backend.optimization_validation import validate_optimization_output
from backend.schemas import (
    CanonicalRuleCitation,
    OptimizationProposalOutput,
    ProductMetrics,
    TrustedOptimizationInput,
    TrustedProductSku,
)
from backend.workflow_leases import (
    commit_owned_workflow_update,
    owned_workflow_lease,
    workflow_lease_expiry,
)


ManualReviewFailureCode = Literal[
    "MANUAL_REVIEW_CONTEXT_NOT_FOUND",
    "MANUAL_REVIEW_CONTEXT_INCONSISTENT",
    "MANUAL_REVIEW_AUTHORIZATION_CHANGED",
    "PRODUCT_VERSION_CONFLICT",
    "MANUAL_REVIEW_FACT_ERROR",
    "MANUAL_REVIEW_DATABASE_ERROR",
    "MANUAL_REVIEW_CHECKPOINT_ERROR",
    "MANUAL_REVIEW_REPLAY_CONFLICT",
]

_ALLOWED_ROLES = frozenset({UserRole.OPERATOR, UserRole.SUPERVISOR, UserRole.ADMIN})
_REVIEWABLE_VIOLATIONS = frozenset(
    {"OUTPUT_BUSINESS_LENGTH", "TITLE_LANGUAGE", "RESTRICTED_PHRASE"}
)


@dataclass(frozen=True)
class ManualReviewClaim:
    workflow_run_id: str
    manual_review_run_id: str
    lease_owner: str
    attempt_count: int


@dataclass(frozen=True)
class OwnedManualReviewContext:
    workflow_run_id: str
    manual_review_run_id: str
    proposal_id: str
    proposal_revision_id: str
    revision_number: int
    store_id: str
    product_id: str
    submitted_by: str
    base_product_version: int
    proposal_output: OptimizationProposalOutput
    canonical_citations: tuple[CanonicalRuleCitation, ...]
    title: str
    category: str
    brand: str
    selling_points: tuple[str, ...]
    description: str
    search_keywords: tuple[str, ...]
    attributes: dict[str, str]
    skus: tuple[TrustedProductSku, ...]
    candidate_metrics: ProductMetrics
    candidate_evidence: tuple[str, ...]
    current_review_id: str | None


@dataclass(frozen=True)
class OwnedManualReviewContextResult:
    disposition: Literal["ready", "failed", "lease_lost"]
    context: OwnedManualReviewContext | None
    error_code: ManualReviewFailureCode | None


def _audit_details(
    from_status: WorkflowStatus,
    to_status: WorkflowStatus,
    quality_status: WorkflowQuality,
    current_step: str,
) -> dict[str, object]:
    return {
        "from_status": from_status.value,
        "to_status": to_status.value,
        "workflow_type": WorkflowType.MANUAL_REVIEW.value,
        "quality_status": quality_status.value,
        "current_step": current_step,
    }


async def _exhaust_attempts(session: AsyncSession) -> None:
    exhausted = list(
        await session.scalars(
            select(WorkflowRun)
            .where(
                WorkflowRun.workflow_type == WorkflowType.MANUAL_REVIEW,
                WorkflowRun.status == WorkflowStatus.PROCESSING,
                WorkflowRun.lease_expires_at < func.now(),
                WorkflowRun.attempt_count >= 3,
            )
            .order_by(WorkflowRun.created_at, WorkflowRun.id)
            .with_for_update(skip_locked=True)
        )
    )
    for run in exhausted:
        manual = await session.scalar(
            select(ManualReviewRun)
            .where(ManualReviewRun.workflow_run_id == run.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if manual is None:
            raise ValueError("exhausted manual review run is missing")
        proposal = await session.scalar(
            select(ProductProposal)
            .where(ProductProposal.id == manual.proposal_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if proposal is None:
            raise ValueError("exhausted proposal is missing")
        original = await session.scalar(
            select(WorkflowRun)
            .where(WorkflowRun.id == proposal.optimization_run_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if (
            manual.proposal_revision_id != proposal.current_revision_id
            or proposal.active_manual_review_run_id != manual.id
            or run.store_id != proposal.store_id
            or original is None
            or original.workflow_type is not WorkflowType.OPTIMIZATION
            or original.store_id != proposal.store_id
            or not isinstance(original.input, dict)
            or original.input.get("proposal_id") != proposal.id
            or original.input.get("product_id") != proposal.product_id
            or original.input.get("store_id") != proposal.store_id
        ):
            raise ValueError("exhausted manual review chain is inconsistent")
        run.status = WorkflowStatus.FAILED
        run.quality_status = WorkflowQuality.DEGRADED
        run.lease_owner = None
        run.lease_expires_at = None
        run.current_step = "failed"
        run.error_code = "LEASE_ATTEMPTS_EXHAUSTED"
        original.status = WorkflowStatus.FAILED
        original.quality_status = WorkflowQuality.DEGRADED
        original.lease_owner = None
        original.lease_expires_at = None
        original.current_step = "manual_review_failed"
        original.error_code = "LEASE_ATTEMPTS_EXHAUSTED"
        proposal.active_manual_review_run_id = None
        add_audit_event(
            session,
            event_type=AuditEventType.MANUAL_REVIEW_FAILED,
            outcome=AuditOutcome.FAILED,
            store_id=proposal.store_id,
            proposal_id=proposal.id,
            proposal_revision_id=manual.proposal_revision_id,
            workflow_run_id=run.id,
            error_code="LEASE_ATTEMPTS_EXHAUSTED",
            details=_audit_details(
                WorkflowStatus.PROCESSING,
                WorkflowStatus.FAILED,
                WorkflowQuality.DEGRADED,
                "failed",
            ),
        )


async def claim_next_manual_review_run(
    session: AsyncSession, *, lease_owner: str, lease_seconds: int
) -> ManualReviewClaim | None:
    try:
        await _exhaust_attempts(session)
        eligible = or_(
            WorkflowRun.status == WorkflowStatus.ACCEPTED,
            and_(
                WorkflowRun.status == WorkflowStatus.PROCESSING,
                WorkflowRun.lease_expires_at < func.now(),
            ),
        )
        row = (
            await session.execute(
                select(WorkflowRun, ManualReviewRun)
                .join(ManualReviewRun, ManualReviewRun.workflow_run_id == WorkflowRun.id)
                .where(
                    WorkflowRun.workflow_type == WorkflowType.MANUAL_REVIEW,
                    eligible,
                    WorkflowRun.attempt_count < 3,
                )
                .order_by(WorkflowRun.created_at, WorkflowRun.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).first()
        if row is None:
            await session.commit()
            return None
        run, manual = row
        from_status = run.status
        run.status = WorkflowStatus.PROCESSING
        run.lease_owner = lease_owner
        run.lease_expires_at = workflow_lease_expiry(session, lease_seconds)
        run.attempt_count += 1
        run.current_step = "claimed"
        run.error_code = None
        add_audit_event(
            session,
            event_type=AuditEventType.MANUAL_REVIEW_CLAIMED,
            outcome=AuditOutcome.SUCCESS,
            store_id=run.store_id,
            proposal_id=manual.proposal_id,
            proposal_revision_id=manual.proposal_revision_id,
            workflow_run_id=run.id,
            details=_audit_details(
                from_status,
                WorkflowStatus.PROCESSING,
                run.quality_status,
                "claimed",
            ),
        )
        await session.commit()
        return ManualReviewClaim(run.id, manual.id, lease_owner, run.attempt_count)
    except (SQLAlchemyError, ValueError):
        await session.rollback()
        raise


async def renew_manual_review_lease(
    session: AsyncSession,
    *,
    workflow_run_id: str,
    lease_owner: str,
    lease_seconds: int,
) -> bool:
    return await commit_owned_workflow_update(
        session,
        update(WorkflowRun)
        .where(
            *owned_workflow_lease(
                workflow_run_id, WorkflowType.MANUAL_REVIEW, lease_owner
            )
        )
        .values(lease_expires_at=workflow_lease_expiry(session, lease_seconds)),
    )


async def _fail_context(
    session: AsyncSession,
    *,
    run: WorkflowRun,
    error_code: ManualReviewFailureCode,
    manual: ManualReviewRun | None = None,
    proposal: ProductProposal | None = None,
    original: WorkflowRun | None = None,
) -> OwnedManualReviewContextResult:
    from_status = run.status
    run.status = WorkflowStatus.FAILED
    run.quality_status = WorkflowQuality.DEGRADED
    run.lease_owner = None
    run.lease_expires_at = None
    run.current_step = "failed"
    run.error_code = error_code
    exact_active_chain = (
        manual is not None
        and proposal is not None
        and manual.workflow_run_id == run.id
        and manual.proposal_id == proposal.id
        and proposal.active_manual_review_run_id == manual.id
        and proposal.store_id == run.store_id
    )
    exact_original_chain = (
        exact_active_chain
        and original is not None
        and proposal.optimization_run_id == original.id
        and original.workflow_type is WorkflowType.OPTIMIZATION
        and original.store_id == proposal.store_id
        and isinstance(original.input, dict)
        and original.input.get("proposal_id") == proposal.id
        and original.input.get("product_id") == proposal.product_id
        and original.input.get("store_id") == proposal.store_id
    )
    if exact_original_chain:
        original.status = WorkflowStatus.FAILED
        original.quality_status = WorkflowQuality.DEGRADED
        original.lease_owner = None
        original.lease_expires_at = None
        original.current_step = "manual_review_failed"
        original.error_code = error_code
    if exact_active_chain:
        proposal.active_manual_review_run_id = None
    add_audit_event(
        session,
        event_type=AuditEventType.MANUAL_REVIEW_FAILED,
        outcome=AuditOutcome.FAILED,
        store_id=run.store_id,
        proposal_id=manual.proposal_id if manual is not None else None,
        proposal_revision_id=(
            manual.proposal_revision_id if manual is not None else None
        ),
        workflow_run_id=run.id,
        error_code=error_code,
        details=_audit_details(
            from_status,
            WorkflowStatus.FAILED,
            WorkflowQuality.DEGRADED,
            "failed",
        ),
    )
    await session.commit()
    return OwnedManualReviewContextResult("failed", None, error_code)


async def load_owned_manual_review_context(
    session: AsyncSession, *, workflow_run_id: str, lease_owner: str
) -> OwnedManualReviewContextResult:
    try:
        run = await session.scalar(
            select(WorkflowRun)
            .where(
                *owned_workflow_lease(
                    workflow_run_id, WorkflowType.MANUAL_REVIEW, lease_owner
                )
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if run is None:
            await session.rollback()
            return OwnedManualReviewContextResult("lease_lost", None, None)
        manual_hint = await session.scalar(
            select(ManualReviewRun)
            .where(ManualReviewRun.workflow_run_id == run.id)
            .execution_options(populate_existing=True)
        )
        if manual_hint is None:
            return await _fail_context(
                session,
                run=run,
                error_code="MANUAL_REVIEW_CONTEXT_NOT_FOUND",
            )
        manual_hint_values = (
            manual_hint.id,
            manual_hint.workflow_run_id,
            manual_hint.proposal_id,
            manual_hint.proposal_revision_id,
            manual_hint.submitted_by,
        )
        proposal_hint = await session.scalar(
            select(ProductProposal)
            .where(ProductProposal.id == manual_hint.proposal_id)
            .execution_options(populate_existing=True)
        )
        if proposal_hint is None:
            return await _fail_context(
                session,
                run=run,
                manual=manual_hint,
                error_code="MANUAL_REVIEW_CONTEXT_NOT_FOUND",
            )
        proposal_hint_values = (proposal_hint.id, proposal_hint.store_id)
        actor = await session.scalar(
            select(User)
            .where(User.id == manual_hint_values[4])
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        store = await session.scalar(
            select(Store)
            .where(Store.id == proposal_hint_values[1])
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        scope = await session.scalar(
            select(UserStoreScope)
            .where(
                UserStoreScope.user_id == manual_hint_values[4],
                UserStoreScope.store_id == proposal_hint_values[1],
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        proposal = await session.scalar(
            select(ProductProposal)
            .where(ProductProposal.id == proposal_hint_values[0])
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if proposal is None:
            return await _fail_context(
                session,
                run=run,
                manual=manual_hint,
                error_code="MANUAL_REVIEW_CONTEXT_NOT_FOUND",
            )
        original = await session.scalar(
            select(WorkflowRun)
            .where(WorkflowRun.id == proposal.optimization_run_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        product = await session.scalar(
            select(Product)
            .where(Product.id == proposal.product_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        manual = await session.scalar(
            select(ManualReviewRun)
            .where(ManualReviewRun.id == manual_hint_values[0])
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if manual is None:
            return await _fail_context(
                session,
                run=run,
                proposal=proposal,
                original=original,
                error_code="MANUAL_REVIEW_CONTEXT_NOT_FOUND",
            )
        revision = await session.scalar(
            select(ProposalRevision)
            .where(ProposalRevision.id == manual.proposal_revision_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        parent = (
            await session.scalar(
                select(ProposalRevision)
                .where(ProposalRevision.id == revision.parent_revision_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if revision is not None and revision.parent_revision_id is not None
            else None
        )
        analysis = await session.scalar(
            select(WorkflowRun)
            .where(WorkflowRun.id == proposal.analysis_run_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        candidate = await session.scalar(
            select(AnalysisCandidate)
            .where(AnalysisCandidate.id == proposal.analysis_candidate_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if (
            original is None
            or revision is None
            or parent is None
            or product is None
            or analysis is None
            or candidate is None
            or store is None
            or actor is None
        ):
            return await _fail_context(
                session,
                run=run,
                manual=manual,
                proposal=proposal,
                original=original,
                error_code="MANUAL_REVIEW_CONTEXT_NOT_FOUND",
            )
        expected_manual_input = {
            "manual_review_run_id": manual.id,
            "proposal_id": proposal.id,
            "proposal_revision_id": revision.id,
            "parent_revision_id": parent.id,
            "product_id": product.id,
            "store_id": store.id,
        }
        if (
            (
                manual.id,
                manual.workflow_run_id,
                manual.proposal_id,
                manual.proposal_revision_id,
                manual.submitted_by,
            )
            != manual_hint_values
            or (proposal.id, proposal.store_id) != proposal_hint_values
            or manual.proposal_revision_id != revision.id
            or proposal.current_revision_id != revision.id
            or proposal.active_manual_review_run_id != manual.id
            or run.created_by != manual.submitted_by
            or run.store_id != proposal.store_id
            or run.input != expected_manual_input
            or original.workflow_type is not WorkflowType.OPTIMIZATION
            or original.store_id != proposal.store_id
            or not isinstance(original.input, dict)
            or original.input.get("proposal_id") != proposal.id
            or original.input.get("product_id") != product.id
            or original.input.get("store_id") != store.id
            or revision.proposal_id != proposal.id
            or revision.origin is not ProposalRevisionOrigin.MANUAL
            or revision.iteration is not None
            or revision.created_by != manual.submitted_by
            or revision.parent_revision_id != parent.id
            or parent.proposal_id != proposal.id
            or revision.revision_number != parent.revision_number + 1
            or product.store_id != proposal.store_id
            or analysis.workflow_type is not WorkflowType.ANALYSIS
            or analysis.store_id != proposal.store_id
            or analysis.status is not WorkflowStatus.COMPLETED
            or analysis.current_step != "product_selected"
            or candidate.workflow_run_id != analysis.id
            or candidate.product_id != product.id
            or candidate.product_code != product.code
        ):
            return await _fail_context(
                session,
                run=run,
                manual=manual,
                proposal=proposal,
                original=original,
                error_code="MANUAL_REVIEW_CONTEXT_INCONSISTENT",
            )
        if (
            actor.status is not UserStatus.ACTIVE
            or actor.role not in _ALLOWED_ROLES
            or not store.enabled
            or scope is None
        ):
            return await _fail_context(
                session,
                run=run,
                manual=manual,
                proposal=proposal,
                original=original,
                error_code="MANUAL_REVIEW_AUTHORIZATION_CHANGED",
            )
        if (
            proposal.base_product_version != product.current_version
            or revision.base_product_version != product.current_version
        ):
            return await _fail_context(
                session,
                run=run,
                manual=manual,
                proposal=proposal,
                original=original,
                error_code="PRODUCT_VERSION_CONFLICT",
            )
        if not product.enabled:
            return await _fail_context(
                session,
                run=run,
                manual=manual,
                proposal=proposal,
                original=original,
                error_code="MANUAL_REVIEW_FACT_ERROR",
            )
        skus = list(
            await session.scalars(
                select(ProductSku)
                .where(ProductSku.product_id == product.id)
                .order_by(ProductSku.id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        )
        if not skus or any(sku.product_id != product.id for sku in skus):
            return await _fail_context(
                session,
                run=run,
                manual=manual,
                proposal=proposal,
                original=original,
                error_code="MANUAL_REVIEW_CONTEXT_INCONSISTENT",
            )
        review = await session.scalar(
            select(ComplianceReview)
            .where(ComplianceReview.proposal_revision_id == revision.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        try:
            output = OptimizationProposalOutput.model_validate(revision.proposal_output)
            parent_output = OptimizationProposalOutput.model_validate(parent.proposal_output)
            citations = tuple(
                CanonicalRuleCitation.model_validate(value) for value in revision.citations
            )
            metrics = ProductMetrics.model_validate(candidate.metrics)
            typed_skus = tuple(
                TrustedProductSku(
                    id=sku.id,
                    code=sku.code,
                    spec=sku.spec,
                    price=sku.price,
                    stock=sku.current_stock,
                )
                for sku in skus
            )
        except (TypeError, ValueError, ValidationError):
            return await _fail_context(
                session,
                run=run,
                manual=manual,
                proposal=proposal,
                original=original,
                error_code="MANUAL_REVIEW_CONTEXT_INCONSISTENT",
            )
        citation_ids = tuple(citation.chunk_id for citation in citations)
        if (
            len(citation_ids) != len(set(citation_ids))
            or any(not citation.active or not citation.applicable for citation in citations)
            or revision.citations != parent.citations
            or output.price_suggestions != parent_output.price_suggestions
            or output.sku_suggestions != parent_output.sku_suggestions
            or output.citations != parent_output.citations
            or metrics.product_id != product.id
            or metrics.product_code != product.code
            or (
                review is not None
                and (
                    review.proposal_id != proposal.id
                    or review.iteration is not None
                    or review.citations != revision.citations
                )
            )
        ):
            return await _fail_context(
                session,
                run=run,
                manual=manual,
                proposal=proposal,
                original=original,
                error_code="MANUAL_REVIEW_CONTEXT_INCONSISTENT",
            )
        try:
            trusted = TrustedOptimizationInput(
                store_id=proposal.store_id,
                product_id=product.id,
                base_product_version=product.current_version,
                title=product.title,
                category=product.category,
                brand=product.brand,
                selling_points=list(product.selling_points),
                description=product.description,
                search_keywords=list(product.search_keywords),
                attributes=dict(product.attributes),
                skus=list(typed_skus),
                candidate_metrics=metrics,
                candidate_evidence=list(candidate.evidence),
                rag_quality="normal",
                canonical_rule_citations=list(citations),
            )
        except (TypeError, ValueError, ValidationError):
            return await _fail_context(
                session,
                run=run,
                manual=manual,
                proposal=proposal,
                original=original,
                error_code="MANUAL_REVIEW_FACT_ERROR",
            )
        if revision.trusted_fact_hash != _sha256(trusted):
            return await _fail_context(
                session,
                run=run,
                manual=manual,
                proposal=proposal,
                original=original,
                error_code="MANUAL_REVIEW_FACT_ERROR",
            )
        if not await _recheck_canonical_citations(session, product, citations):
            return await _fail_context(
                session,
                run=run,
                manual=manual,
                proposal=proposal,
                original=original,
                error_code="MANUAL_REVIEW_FACT_ERROR",
            )
        deterministic = validate_optimization_output(trusted, output)
        if any(
            violation.code not in _REVIEWABLE_VIOLATIONS
            for violation in deterministic.violations
        ):
            return await _fail_context(
                session,
                run=run,
                manual=manual,
                proposal=proposal,
                original=original,
                error_code="MANUAL_REVIEW_FACT_ERROR",
            )
        context = OwnedManualReviewContext(
            workflow_run_id=run.id,
            manual_review_run_id=manual.id,
            proposal_id=proposal.id,
            proposal_revision_id=revision.id,
            revision_number=revision.revision_number,
            store_id=proposal.store_id,
            product_id=product.id,
            submitted_by=manual.submitted_by,
            base_product_version=proposal.base_product_version,
            proposal_output=output,
            canonical_citations=citations,
            title=product.title,
            category=product.category,
            brand=product.brand,
            selling_points=tuple(product.selling_points),
            description=product.description,
            search_keywords=tuple(product.search_keywords),
            attributes=dict(product.attributes),
            skus=typed_skus,
            candidate_metrics=metrics,
            candidate_evidence=tuple(candidate.evidence),
            current_review_id=review.id if review is not None else None,
        )
        await session.commit()
        return OwnedManualReviewContextResult("ready", context, None)
    except SQLAlchemyError:
        await session.rollback()
        return OwnedManualReviewContextResult(
            "failed", None, "MANUAL_REVIEW_DATABASE_ERROR"
        )
