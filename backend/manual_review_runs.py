import json
from dataclasses import asdict, dataclass
from typing import Literal, Sequence
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.audit_events import add_audit_event
from backend.common import (
    AgentCallType,
    AuditEventType,
    AuditOutcome,
    ComplianceRiskLevel,
    ProposalRevisionOrigin,
    UserRole,
    UserStatus,
    WorkflowQuality,
    WorkflowStatus,
    WorkflowType,
)
from backend.compliance_agent import (
    ComplianceAgentCallRecord,
    ComplianceAgentResponse,
    validate_compliance_response,
)
from backend.manual_reviews import _sha256
from backend.models import (
    AgentCall,
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
from backend.optimization_validation import (
    DeterministicComplianceResult,
    DeterministicViolation,
    validate_optimization_output,
)
from backend.schemas import (
    CanonicalRuleCitation,
    OptimizationProposalOutput,
    ProductMetrics,
    TrustedOptimizationInput,
    TrustedProductSku,
    ValidatedRequiredChange,
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
_MANUAL_FAILURE_CODES = frozenset(
    {
        "MANUAL_REVIEW_CONTEXT_NOT_FOUND",
        "MANUAL_REVIEW_CONTEXT_INCONSISTENT",
        "MANUAL_REVIEW_AUTHORIZATION_CHANGED",
        "PRODUCT_VERSION_CONFLICT",
        "MANUAL_REVIEW_FACT_ERROR",
        "MANUAL_REVIEW_DATABASE_ERROR",
        "MANUAL_REVIEW_CHECKPOINT_ERROR",
        "MANUAL_REVIEW_REPLAY_CONFLICT",
    }
)
_DEPENDENCY_CODES = frozenset(
    {
        "DEEPSEEK_KEY_MISSING",
        "DEEPSEEK_TIMEOUT",
        "DEEPSEEK_TRANSPORT",
        "DEEPSEEK_RATE_LIMIT",
        "DEEPSEEK_SERVER_ERROR",
        "DEEPSEEK_UNAUTHORIZED",
        "DEEPSEEK_FORBIDDEN",
        "DEEPSEEK_HTTP_ERROR",
        "DEEPSEEK_SCHEMA_INVALID",
        "KNOWLEDGE_MODEL_UNAVAILABLE",
        "KNOWLEDGE_DEPENDENCY_TIMEOUT",
        "KNOWLEDGE_DEPENDENCY_ERROR",
        "KNOWLEDGE_ZERO_HIT",
        "KNOWLEDGE_LOW_CONFIDENCE",
        "COMPLIANCE_AGENT_DEGRADED",
    }
)
_CALL_FIELDS = (
    "node_name",
    "call_type",
    "iteration",
    "attempt",
    "model",
    "prompt_version",
    "status",
    "input_hash",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "duration_ms",
    "estimated_cost",
    "error_code",
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


@dataclass(frozen=True)
class ManualReviewPersistenceResult:
    disposition: Literal["created", "replayed", "failed", "lease_lost"]
    review_id: str | None
    passed: bool | None
    quality_status: WorkflowQuality | None
    error_code: str | None


@dataclass(frozen=True)
class ManualReviewTerminalResult:
    disposition: Literal["draft_ready", "pending_manual", "failed", "lease_lost"]
    error_code: str | None


class _ImmutableManualReviewConflict(Exception):
    def __init__(self, error: IntegrityError) -> None:
        self.error = error


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


async def _load_owned_manual_review_context(
    session: AsyncSession,
    *,
    workflow_run_id: str,
    lease_owner: str,
    commit_ready: bool,
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
                and (review.proposal_id != proposal.id or review.iteration is not None)
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
        if commit_ready:
            await session.commit()
        return OwnedManualReviewContextResult("ready", context, None)
    except SQLAlchemyError:
        await session.rollback()
        return OwnedManualReviewContextResult(
            "failed", None, "MANUAL_REVIEW_DATABASE_ERROR"
        )


async def load_owned_manual_review_context(
    session: AsyncSession, *, workflow_run_id: str, lease_owner: str
) -> OwnedManualReviewContextResult:
    return await _load_owned_manual_review_context(
        session,
        workflow_run_id=workflow_run_id,
        lease_owner=lease_owner,
        commit_ready=True,
    )


def _json(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _citation_values(
    citations: Sequence[CanonicalRuleCitation],
) -> list[dict[str, object]] | None:
    values = list(citations)
    ids = [citation.chunk_id for citation in values]
    if len(ids) != len(set(ids)) or any(
        not citation.active or not citation.applicable for citation in values
    ):
        return None
    return [citation.model_dump(mode="json") for citation in values]


def _trusted_from_context(
    context: OwnedManualReviewContext,
    citations: Sequence[CanonicalRuleCitation],
    *,
    rag_quality: Literal["normal", "zero_hit", "low_confidence"] = "normal",
) -> TrustedOptimizationInput:
    return TrustedOptimizationInput(
        store_id=context.store_id,
        product_id=context.product_id,
        base_product_version=context.base_product_version,
        title=context.title,
        category=context.category,
        brand=context.brand,
        selling_points=list(context.selling_points),
        description=context.description,
        search_keywords=list(context.search_keywords),
        attributes=context.attributes,
        skus=list(context.skus),
        candidate_metrics=context.candidate_metrics,
        candidate_evidence=list(context.candidate_evidence),
        rag_quality=rag_quality,
        canonical_rule_citations=list(citations),
    )


def _trusted_matches(
    context: OwnedManualReviewContext, trusted: TrustedOptimizationInput
) -> bool:
    return isinstance(trusted, TrustedOptimizationInput) and (
        trusted.store_id == context.store_id
        and trusted.product_id == context.product_id
        and trusted.base_product_version == context.base_product_version
        and trusted.title == context.title
        and trusted.category == context.category
        and trusted.brand == context.brand
        and tuple(trusted.selling_points) == context.selling_points
        and trusted.description == context.description
        and tuple(trusted.search_keywords) == context.search_keywords
        and trusted.attributes == context.attributes
        and sorted(trusted.skus, key=lambda sku: sku.id)
        == sorted(context.skus, key=lambda sku: sku.id)
        and trusted.candidate_metrics == context.candidate_metrics
        and tuple(trusted.candidate_evidence) == context.candidate_evidence
    )


def _deterministic_json(
    deterministic: DeterministicComplianceResult,
) -> dict[str, object]:
    return {
        "passed": deterministic.passed,
        "violations": [asdict(violation) for violation in deterministic.violations],
        "canonical_citations": [
            citation.model_dump(mode="json")
            for citation in deterministic.canonical_citations
        ],
    }


def _required_changes(
    deterministic: DeterministicComplianceResult,
    response: ComplianceAgentResponse | None,
) -> list[ValidatedRequiredChange] | None:
    pairs = [(violation.code, violation.field) for violation in deterministic.violations]
    if len(pairs) != len(set(pairs)) or any(
        not isinstance(violation, DeterministicViolation)
        for violation in deterministic.violations
    ):
        return None
    try:
        changes = [
            ValidatedRequiredChange(
                source_track="deterministic",
                source_violation_code=violation.code,
                field=violation.field,
                instruction=violation.message_zh,
                citation_chunk_ids=[],
            )
            for violation in deterministic.violations
        ]
        if response is not None:
            changes.extend(
                ValidatedRequiredChange.model_validate(
                    change.model_dump(mode="json")
                )
                for change in response.required_changes
            )
    except (TypeError, ValueError, ValidationError):
        return None
    keys = [
        (change.source_track, change.source_violation_code, change.field)
        for change in changes
    ]
    return changes if len(keys) == len(set(keys)) else None


def _call_values(call: ComplianceAgentCallRecord | AgentCall) -> dict[str, object]:
    return {field: getattr(call, field) for field in _CALL_FIELDS}


def _calls_valid(calls: Sequence[ComplianceAgentCallRecord]) -> bool:
    keys: set[tuple[object, ...]] = set()
    seen_primary = False
    for call in calls:
        if not isinstance(call, ComplianceAgentCallRecord):
            return False
        expected = {
            "call_product_compliance_agent": AgentCallType.PRIMARY,
            "repair_product_compliance_schema": AgentCallType.SCHEMA_REPAIR,
        }.get(call.node_name)
        key = (call.node_name, call.call_type, call.iteration, call.attempt)
        if (
            expected is None
            or call.call_type is not expected
            or call.iteration != 0
            or call.attempt < 0
            or key in keys
        ):
            return False
        keys.add(key)
        seen_primary = seen_primary or call.call_type is AgentCallType.PRIMARY
    return seen_primary or not any(
        call.call_type is AgentCallType.SCHEMA_REPAIR for call in calls
    )


def _calls_exact(
    actual: Sequence[AgentCall], expected: Sequence[ComplianceAgentCallRecord]
) -> bool:
    return sorted((_json(_call_values(call)) for call in actual)) == sorted(
        _json(_call_values(call)) for call in expected
    )


async def _terminal_rows(
    session: AsyncSession,
    context: OwnedManualReviewContext,
    lease_owner: str,
) -> tuple[
    WorkflowRun,
    ManualReviewRun,
    ProductProposal,
    WorkflowRun,
    ProposalRevision,
    Product,
] | None:
    run = await session.scalar(
        select(WorkflowRun)
        .where(
            *owned_workflow_lease(
                context.workflow_run_id, WorkflowType.MANUAL_REVIEW, lease_owner
            )
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    manual = await session.scalar(
        select(ManualReviewRun)
        .where(ManualReviewRun.id == context.manual_review_run_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    proposal = await session.scalar(
        select(ProductProposal)
        .where(ProductProposal.id == context.proposal_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    original = (
        await session.scalar(
            select(WorkflowRun)
            .where(WorkflowRun.id == proposal.optimization_run_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if proposal is not None
        else None
    )
    revision = await session.scalar(
        select(ProposalRevision)
        .where(ProposalRevision.id == context.proposal_revision_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    product = await session.scalar(
        select(Product)
        .where(Product.id == context.product_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if (
        run is None
        or manual is None
        or proposal is None
        or original is None
        or revision is None
        or product is None
        or manual.workflow_run_id != run.id
        or manual.proposal_id != proposal.id
        or manual.proposal_revision_id != revision.id
        or proposal.current_revision_id != revision.id
        or proposal.active_manual_review_run_id != manual.id
        or proposal.optimization_run_id != original.id
        or original.workflow_type is not WorkflowType.OPTIMIZATION
        or original.store_id != proposal.store_id
        or not isinstance(original.input, dict)
        or original.input.get("proposal_id") != proposal.id
        or original.input.get("product_id") != product.id
        or original.input.get("store_id") != proposal.store_id
        or revision.proposal_id != proposal.id
        or product.store_id != proposal.store_id
        or product.current_version != context.base_product_version
    ):
        return None
    return run, manual, proposal, original, revision, product


async def _fail_ready_context(
    session: AsyncSession,
    *,
    context: OwnedManualReviewContext,
    lease_owner: str,
    error_code: ManualReviewFailureCode,
) -> ManualReviewTerminalResult:
    rows = await _terminal_rows(session, context, lease_owner)
    if rows is None:
        await session.rollback()
        return ManualReviewTerminalResult(
            "failed", "MANUAL_REVIEW_CONTEXT_INCONSISTENT"
        )
    run, manual, proposal, original, _, _ = rows
    await _fail_context(
        session,
        run=run,
        manual=manual,
        proposal=proposal,
        original=original,
        error_code=error_code,
    )
    return ManualReviewTerminalResult("failed", error_code)


async def _decode_stored_review(
    session: AsyncSession,
    context: OwnedManualReviewContext,
    review: ComplianceReview,
    product: Product,
) -> tuple[
    bool,
    WorkflowQuality,
    str | None,
    ComplianceRiskLevel,
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, object],
] | None:
    try:
        citations = tuple(
            CanonicalRuleCitation.model_validate(value) for value in review.citations
        )
    except (TypeError, ValueError, ValidationError):
        return None
    citation_values = _citation_values(citations)
    if citation_values is None or not await _recheck_canonical_citations(
        session, product, citations
    ):
        return None
    rag_quality: Literal["normal", "zero_hit", "low_confidence"] = "normal"
    if review.error_code == "KNOWLEDGE_ZERO_HIT":
        rag_quality = "zero_hit"
    elif review.error_code == "KNOWLEDGE_LOW_CONFIDENCE":
        rag_quality = "low_confidence"
    trusted = _trusted_from_context(context, citations, rag_quality=rag_quality)
    deterministic = validate_optimization_output(trusted, context.proposal_output)
    if review.error_code in _DEPENDENCY_CODES and review.semantic_review == {
        "status": "unavailable",
        "error_code": review.error_code,
    }:
        response = None
        passed = False
        quality = WorkflowQuality.DEGRADED
        error_code = review.error_code
        risk_level = ComplianceRiskLevel.HIGH
        semantic_json = dict(review.semantic_review)
    else:
        try:
            response = ComplianceAgentResponse.model_validate(review.semantic_review)
            validate_compliance_response(trusted, response)
        except (TypeError, ValueError, ValidationError):
            return None
        passed = deterministic.passed and response.passed and not response.degraded
        quality = (
            WorkflowQuality.DEGRADED
            if response.degraded
            else WorkflowQuality.NORMAL
        )
        error_code = "COMPLIANCE_AGENT_DEGRADED" if response.degraded else None
        risk_level = response.risk_level
        semantic_json = response.model_dump(mode="json")
    changes = _required_changes(deterministic, response)
    if changes is None:
        return None
    changes_json = [change.model_dump(mode="json") for change in changes]
    deterministic_json = _deterministic_json(deterministic)
    if (
        review.proposal_id != context.proposal_id
        or review.proposal_revision_id != context.proposal_revision_id
        or review.iteration is not None
        or review.passed != passed
        or review.risk_level is not risk_level
        or review.quality_status is not quality
        or review.error_code != error_code
        or _json(review.deterministic_checks) != _json(deterministic_json)
        or _json(review.semantic_review) != _json(semantic_json)
        or _json(review.required_changes) != _json(changes_json)
        or _json(review.citations) != _json(citation_values)
    ):
        return None
    return (
        passed,
        quality,
        error_code,
        risk_level,
        deterministic_json,
        semantic_json,
        changes_json,
        citation_values,
    )


async def _persist_manual_compliance_review(
    session: AsyncSession,
    *,
    workflow_run_id: str,
    lease_owner: str,
    trusted: TrustedOptimizationInput,
    deterministic: DeterministicComplianceResult,
    response: ComplianceAgentResponse | None,
    calls: Sequence[ComplianceAgentCallRecord],
    error_code: str | None,
    integrity_error: IntegrityError | None,
) -> ManualReviewPersistenceResult:
    loaded = await _load_owned_manual_review_context(
        session,
        workflow_run_id=workflow_run_id,
        lease_owner=lease_owner,
        commit_ready=False,
    )
    if loaded.disposition != "ready" or loaded.context is None:
        return ManualReviewPersistenceResult(
            loaded.disposition, None, None, None, loaded.error_code
        )
    context = loaded.context
    rows = await _terminal_rows(session, context, lease_owner)
    if rows is None:
        failed = await _fail_ready_context(
            session,
            context=context,
            lease_owner=lease_owner,
            error_code="MANUAL_REVIEW_CONTEXT_INCONSISTENT",
        )
        return ManualReviewPersistenceResult(
            failed.disposition, None, None, None, failed.error_code
        )
    _, _, _, _, _, product = rows
    citations = _citation_values(trusted.canonical_rule_citations)
    recalculated = (
        validate_optimization_output(trusted, context.proposal_output)
        if _trusted_matches(context, trusted)
        else None
    )
    changes = (
        _required_changes(recalculated, response) if recalculated is not None else None
    )
    valid_response = False
    if response is None:
        valid_response = error_code in _DEPENDENCY_CODES
        passed = False
        quality = WorkflowQuality.DEGRADED
        stored_error = error_code
        risk_level = ComplianceRiskLevel.HIGH
        semantic_json = {"status": "unavailable", "error_code": error_code}
    else:
        try:
            validate_compliance_response(trusted, response)
            valid_response = error_code is None
        except (TypeError, ValueError):
            valid_response = False
        passed = bool(
            recalculated is not None
            and recalculated.passed
            and response.passed
            and not response.degraded
        )
        quality = (
            WorkflowQuality.DEGRADED
            if response.degraded
            else WorkflowQuality.NORMAL
        )
        stored_error = "COMPLIANCE_AGENT_DEGRADED" if response.degraded else None
        risk_level = response.risk_level
        semantic_json = response.model_dump(mode="json")
    if (
        recalculated is None
        or deterministic != recalculated
        or citations is None
        or changes is None
        or not valid_response
        or not _calls_valid(calls)
        or not await _recheck_canonical_citations(
            session, product, trusted.canonical_rule_citations
        )
    ):
        failed = await _fail_ready_context(
            session,
            context=context,
            lease_owner=lease_owner,
            error_code="MANUAL_REVIEW_FACT_ERROR",
        )
        return ManualReviewPersistenceResult(
            failed.disposition, None, None, None, failed.error_code
        )
    deterministic_json = _deterministic_json(deterministic)
    changes_json = [change.model_dump(mode="json") for change in changes]
    existing_reviews = list(
        await session.scalars(
            select(ComplianceReview)
            .where(
                or_(
                    ComplianceReview.proposal_revision_id
                    == context.proposal_revision_id,
                    and_(
                        ComplianceReview.proposal_id == context.proposal_id,
                        ComplianceReview.iteration.is_(None),
                    ),
                )
            )
            .order_by(ComplianceReview.created_at, ComplianceReview.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    )
    existing_calls = list(
        await session.scalars(
            select(AgentCall)
            .where(AgentCall.workflow_run_id == context.workflow_run_id)
            .order_by(
                AgentCall.node_name,
                AgentCall.call_type,
                AgentCall.iteration,
                AgentCall.attempt,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    )
    if existing_reviews or existing_calls:
        exact = False
        if len(existing_reviews) == 1:
            decoded = await _decode_stored_review(
                session, context, existing_reviews[0], product
            )
            exact = bool(
                decoded is not None
                and decoded[0] == passed
                and decoded[1] is quality
                and decoded[2] == stored_error
                and decoded[3] is risk_level
                and _json(decoded[4]) == _json(deterministic_json)
                and _json(decoded[5]) == _json(semantic_json)
                and _json(decoded[6]) == _json(changes_json)
                and _json(decoded[7]) == _json(citations)
                and _calls_exact(existing_calls, calls)
            )
        if exact:
            await session.commit()
            return ManualReviewPersistenceResult(
                "replayed",
                existing_reviews[0].id,
                passed,
                quality,
                stored_error,
            )
        if integrity_error is not None:
            raise integrity_error
        failed = await _fail_ready_context(
            session,
            context=context,
            lease_owner=lease_owner,
            error_code="MANUAL_REVIEW_REPLAY_CONFLICT",
        )
        return ManualReviewPersistenceResult(
            failed.disposition, None, None, None, failed.error_code
        )
    if integrity_error is not None:
        raise integrity_error
    for call in calls:
        session.add(
            AgentCall(
                id=str(uuid4()),
                workflow_run_id=context.workflow_run_id,
                **_call_values(call),
            )
        )
    review = ComplianceReview(
        id=str(uuid4()),
        proposal_id=context.proposal_id,
        proposal_revision_id=context.proposal_revision_id,
        iteration=None,
        deterministic_checks=deterministic_json,
        semantic_review=semantic_json,
        passed=passed,
        risk_level=risk_level,
        required_changes=changes_json,
        citations=citations,
        error_code=stored_error,
        quality_status=quality,
    )
    session.add(review)
    try:
        await session.flush()
    except IntegrityError as error:
        await session.rollback()
        raise _ImmutableManualReviewConflict(error) from error
    await session.commit()
    return ManualReviewPersistenceResult(
        "created", review.id, passed, quality, stored_error
    )


async def persist_manual_compliance_review(
    session: AsyncSession,
    *,
    workflow_run_id: str,
    lease_owner: str,
    trusted: TrustedOptimizationInput,
    deterministic: DeterministicComplianceResult,
    response: ComplianceAgentResponse | None,
    calls: Sequence[ComplianceAgentCallRecord],
    error_code: str | None,
) -> ManualReviewPersistenceResult:
    try:
        return await _persist_manual_compliance_review(
            session,
            workflow_run_id=workflow_run_id,
            lease_owner=lease_owner,
            trusted=trusted,
            deterministic=deterministic,
            response=response,
            calls=calls,
            error_code=error_code,
            integrity_error=None,
        )
    except _ImmutableManualReviewConflict as conflict:
        return await _persist_manual_compliance_review(
            session,
            workflow_run_id=workflow_run_id,
            lease_owner=lease_owner,
            trusted=trusted,
            deterministic=deterministic,
            response=response,
            calls=calls,
            error_code=error_code,
            integrity_error=conflict.error,
        )


async def finalize_manual_review(
    session: AsyncSession, *, workflow_run_id: str, lease_owner: str
) -> ManualReviewTerminalResult:
    loaded = await _load_owned_manual_review_context(
        session,
        workflow_run_id=workflow_run_id,
        lease_owner=lease_owner,
        commit_ready=False,
    )
    if loaded.disposition != "ready" or loaded.context is None:
        return ManualReviewTerminalResult(loaded.disposition, loaded.error_code)
    context = loaded.context
    rows = await _terminal_rows(session, context, lease_owner)
    if rows is None:
        return await _fail_ready_context(
            session,
            context=context,
            lease_owner=lease_owner,
            error_code="MANUAL_REVIEW_CONTEXT_INCONSISTENT",
        )
    run, manual, proposal, original, revision, product = rows
    review = await session.scalar(
        select(ComplianceReview)
        .where(ComplianceReview.proposal_revision_id == revision.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    decoded = (
        await _decode_stored_review(session, context, review, product)
        if review is not None
        else None
    )
    if decoded is None:
        return await _fail_ready_context(
            session,
            context=context,
            lease_owner=lease_owner,
            error_code="MANUAL_REVIEW_REPLAY_CONFLICT",
        )
    passed, quality, error_code, risk_level, _, _, _, _ = decoded
    from_status = run.status
    run.status = WorkflowStatus.COMPLETED
    run.quality_status = quality
    run.current_step = "completed"
    run.error_code = error_code
    run.lease_owner = None
    run.lease_expires_at = None
    original.lease_owner = None
    original.lease_expires_at = None
    if passed:
        disposition: Literal["draft_ready", "pending_manual"] = "draft_ready"
        original.status = WorkflowStatus.DRAFT_READY
        original.quality_status = WorkflowQuality.NORMAL
        original.current_step = "manual_review_passed"
        original.error_code = None
    elif quality is WorkflowQuality.DEGRADED:
        disposition = "pending_manual"
        original.status = WorkflowStatus.PENDING_MANUAL
        original.quality_status = WorkflowQuality.DEGRADED
        original.current_step = "manual_review_degraded"
        original.error_code = error_code
    else:
        disposition = "pending_manual"
        original.status = WorkflowStatus.PENDING_MANUAL
        original.quality_status = WorkflowQuality.NORMAL
        original.current_step = "manual_review_changes_required"
        original.error_code = None
    proposal.active_manual_review_run_id = None
    add_audit_event(
        session,
        event_type=AuditEventType.MANUAL_REVIEW_COMPLETED,
        outcome=AuditOutcome.SUCCESS,
        store_id=proposal.store_id,
        actor_id=manual.submitted_by,
        proposal_id=proposal.id,
        proposal_revision_id=revision.id,
        workflow_run_id=run.id,
        error_code=error_code,
        details={
            **_audit_details(
                from_status,
                WorkflowStatus.COMPLETED,
                quality,
                "completed",
            ),
            "review_passed": passed,
            "risk_level": risk_level.value,
        },
    )
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise
    await session.commit()
    return ManualReviewTerminalResult(disposition, error_code)


async def fail_manual_review_run(
    session: AsyncSession,
    *,
    workflow_run_id: str,
    lease_owner: str,
    error_code: str,
) -> ManualReviewTerminalResult:
    if error_code not in _MANUAL_FAILURE_CODES:
        raise ValueError("unsupported manual review failure code")
    loaded = await _load_owned_manual_review_context(
        session,
        workflow_run_id=workflow_run_id,
        lease_owner=lease_owner,
        commit_ready=False,
    )
    if loaded.disposition != "ready" or loaded.context is None:
        return ManualReviewTerminalResult(loaded.disposition, loaded.error_code)
    return await _fail_ready_context(
        session,
        context=loaded.context,
        lease_owner=lease_owner,
        error_code=error_code,
    )
