import hashlib
import json
from dataclasses import dataclass
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
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
from backend.models import (
    AnalysisCandidate,
    AuditEvent,
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
    ManualRevisionRequest,
    OptimizationProposalOutput,
    ProductMetrics,
    TrustedOptimizationInput,
    TrustedProductSku,
)


@dataclass
class ManualReviewDomainError(Exception):
    code: str
    status_code: int


@dataclass(frozen=True)
class ManualRevisionResult:
    revision_id: str
    workflow_run_id: str
    created: bool


def compose_manual_output(
    request: ManualRevisionRequest,
    parent: OptimizationProposalOutput,
) -> OptimizationProposalOutput:
    values = request.model_dump(exclude={"parent_revision_id", "base_product_version"})
    values.update(
        parent.model_dump(include={"citations", "price_suggestions", "sku_suggestions"})
    )
    return OptimizationProposalOutput.model_validate(values)


_ALLOWED_ROLES = frozenset({UserRole.OPERATOR, UserRole.SUPERVISOR, UserRole.ADMIN})
_REVIEWABLE_VIOLATIONS = frozenset(
    {"OUTPUT_BUSINESS_LENGTH", "TITLE_LANGUAGE", "RESTRICTED_PHRASE"}
)
_EDITABLE_FIELDS = (
    "title",
    "selling_points",
    "description",
    "keywords",
    "attribute_completions",
)


def _canonical_json(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _request_hash(
    *, actor_id: str, proposal_id: str, request: ManualRevisionRequest
) -> str:
    return _sha256(
        {
            "action": "manual_revision",
            "actor_id": actor_id,
            "proposal_id": proposal_id,
            "parent_revision_id": request.parent_revision_id,
            "base_product_version": request.base_product_version,
            "request": request.model_dump(mode="json"),
        }
    )


def _changed_fields(
    request: ManualRevisionRequest, parent: OptimizationProposalOutput
) -> list[str]:
    return sorted(
        field for field in _EDITABLE_FIELDS if getattr(request, field) != getattr(parent, field)
    )


async def _authorized_context(
    session: AsyncSession, *, actor_id: str, proposal_id: str
) -> tuple[User, ProductProposal, Store]:
    actor = await session.scalar(
        select(User)
        .where(User.id == actor_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if actor is None or actor.status is not UserStatus.ACTIVE:
        raise ManualReviewDomainError("PROPOSAL_NOT_FOUND", 404)
    if actor.role not in _ALLOWED_ROLES:
        raise ManualReviewDomainError("PROPOSAL_ACTION_FORBIDDEN", 403)
    proposal_hint = await session.scalar(
        select(ProductProposal)
        .where(ProductProposal.id == proposal_id)
        .execution_options(populate_existing=True)
    )
    if proposal_hint is None:
        raise ManualReviewDomainError("PROPOSAL_NOT_FOUND", 404)
    store = await session.scalar(
        select(Store)
        .where(Store.id == proposal_hint.store_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    scope = await session.scalar(
        select(UserStoreScope)
        .where(
            UserStoreScope.user_id == actor.id,
            UserStoreScope.store_id == proposal_hint.store_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if store is None or not store.enabled or scope is None:
        raise ManualReviewDomainError("PROPOSAL_NOT_FOUND", 404)
    proposal = await session.scalar(
        select(ProductProposal)
        .where(
            ProductProposal.id == proposal_id,
            ProductProposal.store_id == store.id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if proposal is None:
        raise ManualReviewDomainError("PROPOSAL_NOT_FOUND", 404)
    original_run = await session.scalar(
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
    if (
        original_run is None
        or original_run.workflow_type is not WorkflowType.OPTIMIZATION
        or original_run.store_id != store.id
        or not isinstance(original_run.input, dict)
        or original_run.input.get("proposal_id") != proposal.id
        or original_run.input.get("product_id") != proposal.product_id
        or original_run.input.get("store_id") != store.id
        or product is None
        or product.store_id != store.id
    ):
        raise ManualReviewDomainError("PROPOSAL_NOT_FOUND", 404)
    return actor, proposal, store


async def _replay_result(
    session: AsyncSession,
    *,
    actor: User,
    proposal: ProductProposal,
    store: Store,
    request: ManualRevisionRequest,
    key_hash: str,
    request_hash: str,
) -> ManualRevisionResult | None:
    manual = await session.scalar(
        select(ManualReviewRun)
        .where(
            ManualReviewRun.proposal_id == proposal.id,
            ManualReviewRun.submitted_by == actor.id,
            ManualReviewRun.idempotency_key_hash == key_hash,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if manual is None:
        return None
    if manual.request_hash != request_hash:
        raise ManualReviewDomainError("IDEMPOTENCY_REPLAY_CONFLICT", 409)
    revision = await session.scalar(
        select(ProposalRevision)
        .where(
            ProposalRevision.id == manual.proposal_revision_id,
            ProposalRevision.proposal_id == proposal.id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    workflow = await session.scalar(
        select(WorkflowRun)
        .where(WorkflowRun.id == manual.workflow_run_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    parent = None
    if revision is not None and revision.parent_revision_id is not None:
        parent = await session.scalar(
            select(ProposalRevision)
            .where(
                ProposalRevision.id == revision.parent_revision_id,
                ProposalRevision.proposal_id == proposal.id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    audits = list(
        await session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.event_type == AuditEventType.MANUAL_REVISION_CREATED,
                AuditEvent.outcome == AuditOutcome.SUCCESS,
                AuditEvent.actor_id == actor.id,
                AuditEvent.proposal_id == proposal.id,
                AuditEvent.proposal_revision_id == manual.proposal_revision_id,
                AuditEvent.workflow_run_id == manual.workflow_run_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    )
    try:
        parent_output = (
            OptimizationProposalOutput.model_validate(parent.proposal_output)
            if parent is not None
            else None
        )
        output = (
            OptimizationProposalOutput.model_validate(revision.proposal_output)
            if revision is not None
            else None
        )
        expected_output = (
            compose_manual_output(request, parent_output)
            if parent_output is not None
            else None
        )
    except (TypeError, ValueError, ValidationError):
        raise ManualReviewDomainError("PROPOSAL_DATA_INCONSISTENT", 503) from None
    expected_input = {
        "manual_review_run_id": manual.id,
        "proposal_id": proposal.id,
        "proposal_revision_id": manual.proposal_revision_id,
        "parent_revision_id": request.parent_revision_id,
        "product_id": proposal.product_id,
        "store_id": store.id,
    }
    expected_details = {
        "from_status": audits[0].details.get("from_status") if len(audits) == 1 else None,
        "to_status": WorkflowStatus.PENDING_MANUAL.value,
        "revision_number": revision.revision_number if revision is not None else None,
        "origin": ProposalRevisionOrigin.MANUAL.value,
        "workflow_type": WorkflowType.MANUAL_REVIEW.value,
        "quality_status": WorkflowQuality.NORMAL.value,
        "current_step": "manual_review_pending",
        "changed_fields": _changed_fields(request, parent_output) if parent_output else None,
    }
    if (
        revision is None
        or parent is None
        or workflow is None
        or output != expected_output
        or revision.origin is not ProposalRevisionOrigin.MANUAL
        or revision.iteration is not None
        or revision.created_by != actor.id
        or revision.parent_revision_id != request.parent_revision_id
        or revision.revision_number != parent.revision_number + 1
        or revision.base_product_version != request.base_product_version
        or len(revision.trusted_fact_hash) != 64
        or revision.citations != parent.citations
        or workflow.workflow_type is not WorkflowType.MANUAL_REVIEW
        or workflow.store_id != store.id
        or workflow.created_by != actor.id
        or workflow.status
        not in {
            WorkflowStatus.ACCEPTED,
            WorkflowStatus.PROCESSING,
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
        }
        or workflow.input != expected_input
        or len(audits) != 1
        or audits[0].store_id != store.id
        or audits[0].actor_role not in _ALLOWED_ROLES
        or audits[0].error_code is not None
        or expected_details["from_status"]
        not in {WorkflowStatus.DRAFT_READY.value, WorkflowStatus.PENDING_MANUAL.value}
        or audits[0].details != expected_details
    ):
        raise ManualReviewDomainError("PROPOSAL_DATA_INCONSISTENT", 503)
    return ManualRevisionResult(revision.id, workflow.id, False)


async def _creation_context(
    session: AsyncSession,
    *,
    proposal: ProductProposal,
    request: ManualRevisionRequest,
) -> tuple[
    WorkflowRun,
    Product,
    ProposalRevision,
    OptimizationProposalOutput,
    list[CanonicalRuleCitation],
    TrustedOptimizationInput,
    OptimizationProposalOutput,
    int,
]:
    run = await session.scalar(
        select(WorkflowRun)
        .where(WorkflowRun.id == proposal.optimization_run_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if (
        run is None
        or run.workflow_type is not WorkflowType.OPTIMIZATION
        or run.store_id != proposal.store_id
        or not isinstance(run.input, dict)
        or run.input.get("proposal_id") != proposal.id
    ):
        raise ManualReviewDomainError("PROPOSAL_NOT_FOUND", 404)
    if run.status not in {WorkflowStatus.DRAFT_READY, WorkflowStatus.PENDING_MANUAL}:
        raise ManualReviewDomainError("PROPOSAL_EDIT_FORBIDDEN", 409)
    if proposal.active_manual_review_run_id is not None:
        raise ManualReviewDomainError("MANUAL_REVIEW_ACTIVE", 409)
    if proposal.current_revision_id != request.parent_revision_id:
        raise ManualReviewDomainError("PROPOSAL_EDIT_FORBIDDEN", 409)

    product = await session.scalar(
        select(Product)
        .where(Product.id == proposal.product_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if product is None or not product.enabled or product.store_id != proposal.store_id:
        raise ManualReviewDomainError("PROPOSAL_NOT_FOUND", 404)
    if (
        request.base_product_version != proposal.base_product_version
        or request.base_product_version != product.current_version
    ):
        raise ManualReviewDomainError("PRODUCT_VERSION_CONFLICT", 409)
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
        analysis is None
        or analysis.workflow_type is not WorkflowType.ANALYSIS
        or analysis.status is not WorkflowStatus.COMPLETED
        or analysis.current_step != "product_selected"
        or candidate is None
        or candidate.workflow_run_id != analysis.id
        or candidate.product_id != product.id
        or candidate.product_code != product.code
    ):
        raise ManualReviewDomainError("PROPOSAL_NOT_FOUND", 404)
    revisions = list(
        await session.scalars(
            select(ProposalRevision)
            .where(ProposalRevision.proposal_id == proposal.id)
            .order_by(ProposalRevision.revision_number, ProposalRevision.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    )
    if not revisions or revisions[-1].id != request.parent_revision_id:
        raise ManualReviewDomainError("PROPOSAL_DATA_INCONSISTENT", 503)
    parent = revisions[-1]
    review = await session.scalar(
        select(ComplianceReview)
        .where(ComplianceReview.proposal_revision_id == parent.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if (
        review is None
        or review.proposal_id != proposal.id
        or review.iteration != parent.iteration
        or review.citations != parent.citations
        or parent.base_product_version != request.base_product_version
    ):
        raise ManualReviewDomainError("PROPOSAL_DATA_INCONSISTENT", 503)
    skus = list(
        await session.scalars(
            select(ProductSku)
            .where(ProductSku.product_id == product.id)
            .order_by(ProductSku.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    )
    try:
        parent_output = OptimizationProposalOutput.model_validate(parent.proposal_output)
        citations = [CanonicalRuleCitation.model_validate(value) for value in parent.citations]
        metrics = ProductMetrics.model_validate(candidate.metrics)
        if metrics.product_id != product.id or metrics.product_code != product.code:
            raise ValueError("candidate mismatch")
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
            skus=[
                TrustedProductSku(
                    id=sku.id,
                    code=sku.code,
                    spec=sku.spec,
                    price=sku.price,
                    stock=sku.current_stock,
                )
                for sku in skus
            ],
            candidate_metrics=metrics,
            candidate_evidence=list(candidate.evidence),
            rag_quality="normal",
            canonical_rule_citations=citations,
        )
        output = compose_manual_output(request, parent_output)
    except (TypeError, ValueError, ValidationError):
        raise ManualReviewDomainError("MANUAL_REVISION_INVALID", 422) from None
    if not await _recheck_canonical_citations(session, product, citations):
        raise ManualReviewDomainError("TRUSTED_EVIDENCE_INVALID", 422)
    deterministic = validate_optimization_output(trusted, output)
    if any(
        violation.code not in _REVIEWABLE_VIOLATIONS
        for violation in deterministic.violations
    ):
        raise ManualReviewDomainError("TRUSTED_EVIDENCE_INVALID", 422)
    return (
        run,
        product,
        parent,
        parent_output,
        citations,
        trusted,
        output,
        parent.revision_number + 1,
    )


async def _create_first(
    session: AsyncSession,
    *,
    actor: User,
    proposal: ProductProposal,
    request: ManualRevisionRequest,
    key_hash: str,
    request_hash: str,
    request_id: str,
) -> ManualRevisionResult:
    (
        run,
        product,
        parent,
        parent_output,
        citations,
        trusted,
        output,
        revision_number,
    ) = await _creation_context(session, proposal=proposal, request=request)
    revision_id = str(uuid4())
    workflow_id = str(uuid4())
    manual_run_id = str(uuid4())
    revision = ProposalRevision(
        id=revision_id,
        proposal_id=proposal.id,
        iteration=None,
        revision_number=revision_number,
        origin=ProposalRevisionOrigin.MANUAL,
        created_by=actor.id,
        parent_revision_id=parent.id,
        base_product_version=product.current_version,
        trusted_fact_hash=_sha256(trusted),
        proposal_output=output.model_dump(mode="json"),
        citations=[citation.model_dump(mode="json") for citation in citations],
    )
    workflow = WorkflowRun(
        id=workflow_id,
        workflow_type=WorkflowType.MANUAL_REVIEW,
        store_id=proposal.store_id,
        created_by=actor.id,
        status=WorkflowStatus.ACCEPTED,
        quality_status=WorkflowQuality.NORMAL,
        current_step="accepted",
        input={
            "manual_review_run_id": manual_run_id,
            "proposal_id": proposal.id,
            "proposal_revision_id": revision_id,
            "parent_revision_id": parent.id,
            "product_id": product.id,
            "store_id": proposal.store_id,
        },
    )
    manual = ManualReviewRun(
        id=manual_run_id,
        workflow_run_id=workflow_id,
        proposal_id=proposal.id,
        proposal_revision_id=revision_id,
        submitted_by=actor.id,
        idempotency_key_hash=key_hash,
        request_hash=request_hash,
    )
    from_status = run.status
    session.add_all([revision, workflow])
    await session.flush()
    session.add(manual)
    await session.flush()
    add_audit_event(
        session,
        event_type=AuditEventType.MANUAL_REVISION_CREATED,
        outcome=AuditOutcome.SUCCESS,
        store_id=proposal.store_id,
        actor_id=actor.id,
        actor_role=actor.role,
        proposal_id=proposal.id,
        proposal_revision_id=revision_id,
        workflow_run_id=workflow_id,
        request_id=request_id,
        details={
            "from_status": from_status.value,
            "to_status": WorkflowStatus.PENDING_MANUAL.value,
            "revision_number": revision_number,
            "origin": ProposalRevisionOrigin.MANUAL.value,
            "workflow_type": WorkflowType.MANUAL_REVIEW.value,
            "quality_status": WorkflowQuality.NORMAL.value,
            "current_step": "manual_review_pending",
            "changed_fields": _changed_fields(request, parent_output),
        },
    )
    await session.flush()
    proposal.current_revision_id = revision_id
    proposal.active_manual_review_run_id = manual_run_id
    run.status = WorkflowStatus.PENDING_MANUAL
    run.quality_status = WorkflowQuality.NORMAL
    run.current_step = "manual_review_pending"
    run.error_code = None
    await session.commit()
    return ManualRevisionResult(revision_id, workflow_id, True)


async def create_manual_revision(
    session: AsyncSession,
    *,
    actor_id: str,
    proposal_id: str,
    request: ManualRevisionRequest,
    idempotency_key: str | None,
    request_id: str,
) -> ManualRevisionResult:
    if not isinstance(idempotency_key, str):
        raise ManualReviewDomainError("IDEMPOTENCY_KEY_INVALID", 400)
    normalized_key = idempotency_key.strip()
    if not 1 <= len(normalized_key) <= 128:
        raise ManualReviewDomainError("IDEMPOTENCY_KEY_INVALID", 400)
    key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    request_hash = _request_hash(
        actor_id=actor_id,
        proposal_id=proposal_id,
        request=request,
    )
    try:
        actor, proposal, store = await _authorized_context(
            session, actor_id=actor_id, proposal_id=proposal_id
        )
        replay = await _replay_result(
            session,
            actor=actor,
            proposal=proposal,
            store=store,
            request=request,
            key_hash=key_hash,
            request_hash=request_hash,
        )
        if replay is not None:
            await session.commit()
            return replay
        return await _create_first(
            session,
            actor=actor,
            proposal=proposal,
            request=request,
            key_hash=key_hash,
            request_hash=request_hash,
            request_id=request_id,
        )
    except IntegrityError:
        await session.rollback()
        try:
            actor, proposal, store = await _authorized_context(
                session, actor_id=actor_id, proposal_id=proposal_id
            )
            replay = await _replay_result(
                session,
                actor=actor,
                proposal=proposal,
                store=store,
                request=request,
                key_hash=key_hash,
                request_hash=request_hash,
            )
            if replay is not None:
                await session.commit()
                return replay
            await _creation_context(session, proposal=proposal, request=request)
            raise ManualReviewDomainError("PROPOSAL_DATA_INCONSISTENT", 503)
        except ManualReviewDomainError:
            await session.rollback()
            raise
        except SQLAlchemyError:
            await session.rollback()
            raise ManualReviewDomainError("PROPOSAL_DATA_INCONSISTENT", 503) from None
    except ManualReviewDomainError:
        await session.rollback()
        raise
    except (SQLAlchemyError, ValueError):
        await session.rollback()
        raise ManualReviewDomainError("PROPOSAL_DATA_INCONSISTENT", 503) from None
