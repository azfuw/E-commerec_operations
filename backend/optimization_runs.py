import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal, Sequence
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common import (
    AgentCallType,
    ComplianceRiskLevel,
    KnowledgeVersionStatus,
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
from backend.models import (
    AgentCall,
    AnalysisCandidate,
    ComplianceReview,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    Product,
    ProductProposal,
    ProductSku,
    ProposalRevision,
    Store,
    User,
    UserStoreScope,
    WorkflowRun,
)
from backend.optimization_agent import OptimizationAgentCallRecord
from backend.optimization_validation import DeterministicComplianceResult, DeterministicViolation
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


PendingManualErrorCode = Literal[
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
    "OPTIMIZATION_ITERATION_LIMIT",
]
OptimizationFailureCode = Literal[
    "OPTIMIZATION_CONTEXT_NOT_FOUND",
    "OPTIMIZATION_CONTEXT_INCONSISTENT",
    "OPTIMIZATION_AUTHORIZATION_CHANGED",
    "PRODUCT_VERSION_CONFLICT",
    "OPTIMIZATION_FACT_ERROR",
    "OPTIMIZATION_DATABASE_ERROR",
    "OPTIMIZATION_CHECKPOINT_ERROR",
    "OPTIMIZATION_REPLAY_CONFLICT",
]

_PENDING_CODES = {
    "DEEPSEEK_KEY_MISSING", "DEEPSEEK_TIMEOUT", "DEEPSEEK_TRANSPORT",
    "DEEPSEEK_RATE_LIMIT", "DEEPSEEK_SERVER_ERROR", "DEEPSEEK_UNAUTHORIZED",
    "DEEPSEEK_FORBIDDEN", "DEEPSEEK_HTTP_ERROR", "DEEPSEEK_SCHEMA_INVALID",
    "KNOWLEDGE_MODEL_UNAVAILABLE", "KNOWLEDGE_DEPENDENCY_TIMEOUT",
    "KNOWLEDGE_DEPENDENCY_ERROR", "KNOWLEDGE_ZERO_HIT", "KNOWLEDGE_LOW_CONFIDENCE",
    "COMPLIANCE_AGENT_DEGRADED", "OPTIMIZATION_ITERATION_LIMIT",
}
_FAILURE_CODES = {
    "OPTIMIZATION_CONTEXT_NOT_FOUND", "OPTIMIZATION_CONTEXT_INCONSISTENT",
    "OPTIMIZATION_AUTHORIZATION_CHANGED", "PRODUCT_VERSION_CONFLICT",
    "OPTIMIZATION_FACT_ERROR", "OPTIMIZATION_DATABASE_ERROR",
    "OPTIMIZATION_CHECKPOINT_ERROR", "OPTIMIZATION_REPLAY_CONFLICT",
}
_FAILURE_REVIEW_CODES = _PENDING_CODES - {
    "COMPLIANCE_AGENT_DEGRADED", "OPTIMIZATION_ITERATION_LIMIT"
}
_OPTIMIZATION_NODES = {
    "call_product_optimization_agent", "repair_product_optimization_schema"
}
_COMPLIANCE_NODES = {
    "call_product_compliance_agent", "repair_product_compliance_schema"
}
_AUDIT_KEYS = (
    "node_name", "call_type", "iteration", "attempt", "model", "prompt_version", "status",
    "input_hash", "prompt_tokens", "completion_tokens", "total_tokens", "duration_ms",
    "estimated_cost", "error_code",
)


@dataclass(frozen=True)
class OptimizationClaim:
    workflow_run_id: str
    lease_owner: str
    attempt_count: int


@dataclass(frozen=True)
class OwnedOptimizationContext:
    workflow_run_id: str
    proposal_id: str
    analysis_run_id: str
    analysis_candidate_id: str
    store_id: str
    product_id: str
    created_by: str
    base_product_version: int
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
    current_revision_id: str | None
    current_revision_iteration: int | None
    current_proposal_output: OptimizationProposalOutput | None
    current_canonical_citations: tuple[CanonicalRuleCitation, ...]
    current_review_id: str | None
    current_review_passed: bool | None
    current_review_quality_status: WorkflowQuality | None
    current_review_error_code: str | None
    current_required_changes: tuple[ValidatedRequiredChange, ...]


@dataclass(frozen=True)
class OwnedOptimizationContextResult:
    disposition: Literal["ready", "failed", "lease_lost"]
    context: OwnedOptimizationContext | None
    error_code: OptimizationFailureCode | None


@dataclass(frozen=True)
class RevisionPersistenceResult:
    disposition: Literal["created", "replayed", "failed", "lease_lost"]
    revision_id: str | None
    error_code: OptimizationFailureCode | None


@dataclass(frozen=True)
class ReviewPersistenceResult:
    disposition: Literal["created", "replayed", "failed", "lease_lost"]
    review_id: str | None
    passed: bool | None
    error_code: OptimizationFailureCode | None


@dataclass(frozen=True)
class OptimizationTerminalResult:
    disposition: Literal["draft_ready", "pending_manual", "failed", "lease_lost"]
    error_code: str | None


@dataclass
class _Locked:
    run: WorkflowRun
    proposal: ProductProposal
    product: Product
    context: OwnedOptimizationContext


class _ImmutableWriteConflict(Exception):
    def __init__(self, error: IntegrityError) -> None:
        self.error = error


def _json(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str
    )


def _models(values: Sequence[object]) -> list[object]:
    return [value.model_dump(mode="json") if hasattr(value, "model_dump") else value for value in values]


async def claim_next_optimization_run(
    session: AsyncSession, *, lease_owner: str, lease_seconds: int
) -> OptimizationClaim | None:
    await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.workflow_type == WorkflowType.OPTIMIZATION,
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
        and_(WorkflowRun.status == WorkflowStatus.PROCESSING, WorkflowRun.lease_expires_at < func.now()),
    )
    run = await session.scalar(
        select(WorkflowRun)
        .where(
            WorkflowRun.workflow_type == WorkflowType.OPTIMIZATION,
            eligible,
            WorkflowRun.attempt_count < 3,
        )
        .order_by(WorkflowRun.created_at, WorkflowRun.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if run is None:
        await session.commit()
        return None
    run.status = WorkflowStatus.PROCESSING
    run.lease_owner = lease_owner
    run.lease_expires_at = workflow_lease_expiry(session, lease_seconds)
    run.attempt_count += 1
    run.current_step = "claimed"
    run.error_code = None
    await session.commit()
    return OptimizationClaim(run.id, lease_owner, run.attempt_count)


async def renew_optimization_lease(
    session: AsyncSession, *, workflow_run_id: str, lease_owner: str, lease_seconds: int
) -> bool:
    return await commit_owned_workflow_update(
        session,
        update(WorkflowRun)
        .where(*owned_workflow_lease(workflow_run_id, WorkflowType.OPTIMIZATION, lease_owner))
        .values(lease_expires_at=workflow_lease_expiry(session, lease_seconds)),
    )


async def update_optimization_step(
    session: AsyncSession, *, workflow_run_id: str, lease_owner: str, current_step: str
) -> bool:
    if not isinstance(current_step, str) or not current_step or len(current_step) > 64:
        raise ValueError("current_step must be a nonempty string of at most 64 characters")
    return await commit_owned_workflow_update(
        session,
        update(WorkflowRun)
        .where(*owned_workflow_lease(workflow_run_id, WorkflowType.OPTIMIZATION, lease_owner))
        .values(current_step=current_step),
    )


async def _locked_context(
    session: AsyncSession, workflow_run_id: str, lease_owner: str
) -> _Locked | str | None:
    run = await session.scalar(
        select(WorkflowRun)
        .where(*owned_workflow_lease(workflow_run_id, WorkflowType.OPTIMIZATION, lease_owner))
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if run is None:
        return None
    proposal = await session.scalar(
        select(ProductProposal)
        .where(ProductProposal.optimization_run_id == run.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if proposal is None:
        return "OPTIMIZATION_CONTEXT_NOT_FOUND"
    product = await session.scalar(
        select(Product).where(Product.id == proposal.product_id).execution_options(populate_existing=True).with_for_update()
    )
    analysis = await session.scalar(
        select(WorkflowRun).where(WorkflowRun.id == proposal.analysis_run_id).execution_options(populate_existing=True).with_for_update()
    )
    candidate = await session.scalar(
        select(AnalysisCandidate)
        .where(AnalysisCandidate.id == proposal.analysis_candidate_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    store = await session.scalar(
        select(Store).where(Store.id == proposal.store_id).execution_options(populate_existing=True).with_for_update()
    )
    user = await session.scalar(
        select(User).where(User.id == run.created_by).execution_options(populate_existing=True).with_for_update()
    )
    scope = await session.scalar(
        select(UserStoreScope)
        .where(UserStoreScope.user_id == run.created_by, UserStoreScope.store_id == proposal.store_id)
        .execution_options(populate_existing=True)
    )
    if product is None or analysis is None or candidate is None or store is None or user is None:
        return "OPTIMIZATION_CONTEXT_NOT_FOUND"
    if (
        proposal.store_id != run.store_id
        or proposal.product_id != product.id
        or analysis.workflow_type != WorkflowType.ANALYSIS
        or analysis.status != WorkflowStatus.COMPLETED
        or analysis.current_step != "product_selected"
        or candidate.workflow_run_id != analysis.id
        or candidate.product_id != product.id
    ):
        return "OPTIMIZATION_CONTEXT_INCONSISTENT"
    if not store.enabled or user.status != UserStatus.ACTIVE or user.role != UserRole.OPERATOR or scope is None:
        return "OPTIMIZATION_AUTHORIZATION_CHANGED"
    if not product.enabled:
        return "OPTIMIZATION_FACT_ERROR"
    if proposal.base_product_version != product.current_version:
        return "PRODUCT_VERSION_CONFLICT"
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
        return "OPTIMIZATION_CONTEXT_INCONSISTENT"
    try:
        metrics = ProductMetrics.model_validate(candidate.metrics)
        if metrics.product_id != product.id or metrics.product_code != product.code:
            return "OPTIMIZATION_CONTEXT_INCONSISTENT"
        context = await _context_progress(session, run, proposal, product, candidate, skus, metrics)
    except (TypeError, ValueError, ValidationError):
        return "OPTIMIZATION_CONTEXT_INCONSISTENT"
    if not await _recheck_canonical_citations(
        session, product, context.current_canonical_citations
    ):
        return "OPTIMIZATION_FACT_ERROR"
    return _Locked(run, proposal, product, context)


async def _context_progress(
    session: AsyncSession,
    run: WorkflowRun,
    proposal: ProductProposal,
    product: Product,
    candidate: AnalysisCandidate,
    skus: list[ProductSku],
    metrics: ProductMetrics,
) -> OwnedOptimizationContext:
    revision_id = proposal.current_revision_id
    revision: ProposalRevision | None = None
    output: OptimizationProposalOutput | None = None
    citations: tuple[CanonicalRuleCitation, ...] = ()
    review: ComplianceReview | None = None
    changes: tuple[ValidatedRequiredChange, ...] = ()
    if revision_id is not None:
        revision = await session.scalar(
            select(ProposalRevision)
            .where(ProposalRevision.id == revision_id, ProposalRevision.proposal_id == proposal.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if revision is None:
            raise ValueError("orphan current revision")
        if not await _automatic_revision_chain_valid(
            session, proposal, revision, run.created_by
        ):
            raise ValueError("invalid automatic revision chain")
        output = OptimizationProposalOutput.model_validate(revision.proposal_output)
        citations = tuple(CanonicalRuleCitation.model_validate(value) for value in revision.citations)
        citation_ids = [citation.chunk_id for citation in citations]
        if len(citation_ids) != len(set(citation_ids)) or any(
            not citation.active or not citation.applicable for citation in citations
        ):
            raise ValueError("invalid saved citations")
        review = await session.scalar(
            select(ComplianceReview)
            .where(ComplianceReview.proposal_revision_id == revision.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if review is not None:
            if review.proposal_id != proposal.id or review.iteration != revision.iteration:
                raise ValueError("inconsistent review")
            changes = tuple(ValidatedRequiredChange.model_validate(value) for value in review.required_changes)
    else:
        stale = await session.scalar(
            select(ProposalRevision.id)
            .where(ProposalRevision.proposal_id == proposal.id)
            .execution_options(populate_existing=True)
        )
        if stale is not None:
            raise ValueError("unpointed revision")
    return OwnedOptimizationContext(
        workflow_run_id=run.id,
        proposal_id=proposal.id,
        analysis_run_id=proposal.analysis_run_id,
        analysis_candidate_id=proposal.analysis_candidate_id,
        store_id=proposal.store_id,
        product_id=product.id,
        created_by=run.created_by,
        base_product_version=proposal.base_product_version,
        title=product.title,
        category=product.category,
        brand=product.brand,
        selling_points=tuple(product.selling_points),
        description=product.description,
        search_keywords=tuple(product.search_keywords),
        attributes=dict(product.attributes),
        skus=tuple(
            TrustedProductSku(id=sku.id, code=sku.code, spec=sku.spec, price=sku.price, stock=sku.current_stock)
            for sku in skus
        ),
        candidate_metrics=metrics,
        candidate_evidence=tuple(candidate.evidence),
        current_revision_id=revision.id if revision else None,
        current_revision_iteration=revision.iteration if revision else None,
        current_proposal_output=output,
        current_canonical_citations=citations,
        current_review_id=review.id if review else None,
        current_review_passed=review.passed if review else None,
        current_review_quality_status=review.quality_status if review else None,
        current_review_error_code=review.error_code if review else None,
        current_required_changes=changes,
    )


async def _fail_locked(session: AsyncSession, run: WorkflowRun, error_code: str) -> None:
    run.status = WorkflowStatus.FAILED
    run.lease_owner = None
    run.lease_expires_at = None
    run.current_step = "failed"
    run.error_code = error_code
    await session.commit()


async def _lock_or_result(
    session: AsyncSession, workflow_run_id: str, lease_owner: str
) -> _Locked | OwnedOptimizationContextResult:
    locked = await _locked_context(session, workflow_run_id, lease_owner)
    if locked is None:
        await session.rollback()
        return OwnedOptimizationContextResult("lease_lost", None, None)
    if isinstance(locked, str):
        run = await session.scalar(
            select(WorkflowRun)
            .where(*owned_workflow_lease(workflow_run_id, WorkflowType.OPTIMIZATION, lease_owner))
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if run is None:
            await session.rollback()
            return OwnedOptimizationContextResult("lease_lost", None, None)
        await _fail_locked(session, run, locked)
        return OwnedOptimizationContextResult("failed", None, locked)
    return locked


async def load_owned_optimization_context(
    session: AsyncSession, *, workflow_run_id: str, lease_owner: str
) -> OwnedOptimizationContextResult:
    locked = await _lock_or_result(session, workflow_run_id, lease_owner)
    if isinstance(locked, OwnedOptimizationContextResult):
        return locked
    if locked.product.current_version != locked.context.base_product_version:
        await _fail_locked(session, locked.run, "PRODUCT_VERSION_CONFLICT")
        return OwnedOptimizationContextResult("failed", None, "PRODUCT_VERSION_CONFLICT")
    context = locked.context
    await session.commit()
    return OwnedOptimizationContextResult("ready", context, None)


def _trusted_matches(context: OwnedOptimizationContext, trusted: TrustedOptimizationInput) -> bool:
    return (
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
        and sorted(trusted.skus, key=lambda sku: sku.id) == sorted(context.skus, key=lambda sku: sku.id)
        and trusted.candidate_metrics == context.candidate_metrics
        and tuple(trusted.candidate_evidence) == context.candidate_evidence
    )


def _citation_values(values: Sequence[CanonicalRuleCitation]) -> list[dict[str, object]] | None:
    citations = list(values)
    ids = [citation.chunk_id for citation in citations]
    if len(ids) != len(set(ids)) or ids != sorted(ids) or any(
        not citation.active or not citation.applicable for citation in citations
    ):
        return None
    return [citation.model_dump(mode="json") for citation in citations]


async def _recheck_canonical_citations(
    session: AsyncSession,
    product: Product,
    citations: Sequence[CanonicalRuleCitation],
) -> bool:
    citation_ids = [citation.chunk_id for citation in citations]
    if len(citation_ids) != len(set(citation_ids)):
        return False
    if not citation_ids:
        return True
    rows = (
        await session.execute(
            select(KnowledgeChunk, KnowledgeDocumentVersion, KnowledgeDocument)
            .join(KnowledgeDocumentVersion, KnowledgeChunk.version_id == KnowledgeDocumentVersion.id)
            .join(KnowledgeDocument, KnowledgeDocument.current_version_id == KnowledgeDocumentVersion.id)
            .where(
                KnowledgeChunk.id.in_(citation_ids),
                KnowledgeChunk.version_id == KnowledgeDocumentVersion.id,
                KnowledgeDocumentVersion.document_id == KnowledgeDocument.id,
                KnowledgeDocument.enabled.is_(True),
                KnowledgeDocumentVersion.status == KnowledgeVersionStatus.ACTIVE,
                KnowledgeDocument.category.in_((product.category, "通用规则")),
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).all()
    current = {chunk.id: (chunk, version, document) for chunk, version, document in rows}
    if len(current) != len(citation_ids):
        return False
    return all(
        (row := current.get(citation.chunk_id)) is not None
        and citation.document_id == row[2].id
        and citation.version_id == row[1].id
        and citation.document_name == row[2].name
        and citation.version_number == row[1].version_number
        and citation.category == row[2].category
        and citation.canonical_text == row[0].canonical_text
        for citation in citations
    )


def _audit_values(call: OptimizationAgentCallRecord | ComplianceAgentCallRecord) -> dict[str, object]:
    return {
        "node_name": call.node_name,
        "call_type": call.call_type,
        "iteration": call.iteration,
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


def _validate_calls(
    calls: Sequence[OptimizationAgentCallRecord] | Sequence[ComplianceAgentCallRecord],
    iteration: int,
    nodes: set[str],
) -> bool:
    keys: set[tuple[object, ...]] = set()
    for call in calls:
        if (
            call.node_name not in nodes
            or call.iteration != iteration
            or call.attempt < 0
        ):
            return False
        key = (call.node_name, call.call_type, call.iteration, call.attempt)
        if key in keys:
            return False
        keys.add(key)
    return True


async def _store_calls(
    session: AsyncSession,
    run_id: str,
    calls: Sequence[OptimizationAgentCallRecord] | Sequence[ComplianceAgentCallRecord],
) -> bool:
    existing_keys: set[tuple[object, ...]] = set()
    for call in calls:
        key = (call.node_name, call.call_type, call.iteration, call.attempt)
        values = _audit_values(call)
        existing = await session.scalar(
            select(AgentCall).where(
                AgentCall.workflow_run_id == run_id,
                AgentCall.node_name == call.node_name,
                AgentCall.call_type == call.call_type,
                AgentCall.iteration == call.iteration,
                AgentCall.attempt == call.attempt,
            )
            .execution_options(populate_existing=True)
        )
        if existing is not None:
            existing_values = {
                key: getattr(existing, key)
                for key in values
            }
            if _json(existing_values) != _json(values):
                return False
            existing_keys.add(key)
    for call in calls:
        key = (call.node_name, call.call_type, call.iteration, call.attempt)
        if key in existing_keys:
            continue
        values = _audit_values(call)
        session.add(AgentCall(id=str(uuid4()), workflow_run_id=run_id, **values))
    return True


async def _calls_present_exact(
    session: AsyncSession,
    run_id: str,
    calls: Sequence[OptimizationAgentCallRecord] | Sequence[ComplianceAgentCallRecord],
) -> bool:
    for call in calls:
        existing = await session.scalar(
            select(AgentCall)
            .where(
                AgentCall.workflow_run_id == run_id,
                AgentCall.node_name == call.node_name,
                AgentCall.call_type == call.call_type,
                AgentCall.iteration == call.iteration,
                AgentCall.attempt == call.attempt,
            )
            .execution_options(populate_existing=True)
        )
        if existing is None or _json({key: getattr(existing, key) for key in _AUDIT_KEYS}) != _json(_audit_values(call)):
            return False
    return True


async def _audit_matches(
    session: AsyncSession,
    run_id: str,
    calls: Sequence[OptimizationAgentCallRecord] | Sequence[ComplianceAgentCallRecord],
    nodes: set[str],
    iteration: int,
) -> bool:
    actual = list(
        await session.scalars(
            select(AgentCall)
            .where(
                AgentCall.workflow_run_id == run_id,
                AgentCall.iteration == iteration,
                AgentCall.node_name.in_(nodes),
            )
            .order_by(AgentCall.node_name, AgentCall.call_type, AgentCall.attempt)
            .execution_options(populate_existing=True)
        )
    )
    expected = sorted((_audit_values(call) for call in calls), key=_json)
    observed = sorted(({key: getattr(call, key) for key in _AUDIT_KEYS} for call in actual), key=_json)
    return _json(observed) == _json(expected)


async def _automatic_revision_chain_valid(
    session: AsyncSession,
    proposal: ProductProposal,
    revision: ProposalRevision,
    created_by: str,
) -> bool:
    current = revision
    while True:
        iteration = current.iteration
        if (
            not isinstance(iteration, int)
            or not 0 <= iteration <= 2
            or current.proposal_id != proposal.id
            or current.revision_number != iteration + 1
            or current.origin != ProposalRevisionOrigin.AGENT
            or current.created_by != created_by
            or current.base_product_version != proposal.base_product_version
        ):
            return False
        if iteration == 0:
            return current.parent_revision_id is None
        if current.parent_revision_id is None:
            return False
        parent = await session.scalar(
            select(ProposalRevision)
            .where(
                ProposalRevision.id == current.parent_revision_id,
                ProposalRevision.proposal_id == proposal.id,
                ProposalRevision.iteration == iteration - 1,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if parent is None:
            return False
        current = parent


async def _revision_for(
    session: AsyncSession,
    proposal: ProductProposal,
    revision_id: str,
    iteration: int,
    created_by: str,
) -> ProposalRevision | None:
    revision = await session.scalar(
        select(ProposalRevision)
        .where(
            ProposalRevision.id == revision_id,
            ProposalRevision.proposal_id == proposal.id,
            ProposalRevision.iteration == iteration,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if revision is None or not await _automatic_revision_chain_valid(
        session, proposal, revision, created_by
    ):
        return None
    return revision


async def _persist_optimization_revision(
    session: AsyncSession,
    *,
    workflow_run_id: str,
    lease_owner: str,
    iteration: int,
    trusted: TrustedOptimizationInput,
    output: OptimizationProposalOutput,
    canonical_citations: Sequence[CanonicalRuleCitation],
    calls: Sequence[OptimizationAgentCallRecord],
    integrity_error: IntegrityError | None,
) -> RevisionPersistenceResult:
    locked = await _lock_or_result(session, workflow_run_id, lease_owner)
    if isinstance(locked, OwnedOptimizationContextResult):
        return RevisionPersistenceResult(locked.disposition, None, locked.error_code)
    if (
        not 0 <= iteration <= 2
        or not _trusted_matches(locked.context, trusted)
        or not _validate_calls(calls, iteration, _OPTIMIZATION_NODES)
    ):
        await _fail_locked(session, locked.run, "OPTIMIZATION_FACT_ERROR")
        return RevisionPersistenceResult("failed", None, "OPTIMIZATION_FACT_ERROR")
    trusted_citations = _citation_values(trusted.canonical_rule_citations)
    stored_citations = _citation_values(canonical_citations)
    if trusted_citations is None or stored_citations is None or _json(trusted_citations) != _json(stored_citations):
        await _fail_locked(session, locked.run, "OPTIMIZATION_FACT_ERROR")
        return RevisionPersistenceResult("failed", None, "OPTIMIZATION_FACT_ERROR")
    if not await _recheck_canonical_citations(session, locked.product, canonical_citations):
        await _fail_locked(session, locked.run, "OPTIMIZATION_FACT_ERROR")
        return RevisionPersistenceResult("failed", None, "OPTIMIZATION_FACT_ERROR")
    fact_hash = hashlib.sha256(_json(trusted).encode("utf-8")).hexdigest()
    existing = await session.scalar(
        select(ProposalRevision)
        .where(ProposalRevision.proposal_id == locked.proposal.id, ProposalRevision.iteration == iteration)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if existing is not None:
        exact = (
            await _automatic_revision_chain_valid(
                session, locked.proposal, existing, locked.run.created_by
            )
            and existing.base_product_version == locked.context.base_product_version
            and existing.trusted_fact_hash == fact_hash
            and _json(existing.proposal_output) == _json(output)
            and _json(existing.citations) == _json(stored_citations)
            and await _audit_matches(session, locked.run.id, calls, _OPTIMIZATION_NODES, iteration)
        )
        if exact:
            await session.commit()
            return RevisionPersistenceResult("replayed", existing.id, None)
        if integrity_error is not None:
            raise integrity_error
        await _fail_locked(session, locked.run, "OPTIMIZATION_REPLAY_CONFLICT")
        return RevisionPersistenceResult("failed", None, "OPTIMIZATION_REPLAY_CONFLICT")
    if integrity_error is not None:
        raise integrity_error
    if iteration == 0:
        allowed = locked.proposal.current_revision_id is None
        parent_revision_id = None
    else:
        prior = await session.scalar(
            select(ProposalRevision)
            .where(ProposalRevision.proposal_id == locked.proposal.id, ProposalRevision.iteration == iteration - 1)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        review = await session.scalar(
            select(ComplianceReview)
            .where(ComplianceReview.proposal_revision_id == prior.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ) if prior is not None else None
        allowed = bool(
            prior is not None
            and await _automatic_revision_chain_valid(
                session, locked.proposal, prior, locked.run.created_by
            )
            and locked.proposal.current_revision_id == prior.id
            and review is not None
            and review.proposal_id == locked.proposal.id
            and review.iteration == prior.iteration
            and _actionable_prior_review(locked.context, prior, review)
        )
        parent_revision_id = prior.id if prior is not None else None
    if not allowed:
        await _fail_locked(session, locked.run, "OPTIMIZATION_REPLAY_CONFLICT")
        return RevisionPersistenceResult("failed", None, "OPTIMIZATION_REPLAY_CONFLICT")
    if not await _store_calls(session, locked.run.id, calls):
        await _fail_locked(session, locked.run, "OPTIMIZATION_REPLAY_CONFLICT")
        return RevisionPersistenceResult("failed", None, "OPTIMIZATION_REPLAY_CONFLICT")
    revision = ProposalRevision(
        id=str(uuid4()),
        proposal_id=locked.proposal.id,
        iteration=iteration,
        revision_number=iteration + 1,
        origin=ProposalRevisionOrigin.AGENT,
        created_by=locked.run.created_by,
        parent_revision_id=parent_revision_id,
        base_product_version=locked.context.base_product_version,
        trusted_fact_hash=fact_hash,
        proposal_output=output.model_dump(mode="json"),
        citations=stored_citations,
    )
    session.add(revision)
    try:
        await session.flush()
    except IntegrityError as error:
        await session.rollback()
        raise _ImmutableWriteConflict(error) from error
    locked.proposal.current_revision_id = revision.id
    await session.commit()
    return RevisionPersistenceResult("created", revision.id, None)


async def persist_optimization_revision(
    session: AsyncSession,
    *,
    workflow_run_id: str,
    lease_owner: str,
    iteration: int,
    trusted: TrustedOptimizationInput,
    output: OptimizationProposalOutput,
    canonical_citations: Sequence[CanonicalRuleCitation],
    calls: Sequence[OptimizationAgentCallRecord],
) -> RevisionPersistenceResult:
    try:
        return await _persist_optimization_revision(
            session, workflow_run_id=workflow_run_id, lease_owner=lease_owner, iteration=iteration,
            trusted=trusted, output=output, canonical_citations=canonical_citations, calls=calls,
            integrity_error=None,
        )
    except _ImmutableWriteConflict as conflict:
        return await _persist_optimization_revision(
            session, workflow_run_id=workflow_run_id, lease_owner=lease_owner, iteration=iteration,
            trusted=trusted, output=output, canonical_citations=canonical_citations, calls=calls,
            integrity_error=conflict.error,
        )


def _trusted_from_context(context: OwnedOptimizationContext, citations: Sequence[CanonicalRuleCitation]) -> TrustedOptimizationInput:
    return TrustedOptimizationInput(
        store_id=context.store_id, product_id=context.product_id,
        base_product_version=context.base_product_version, title=context.title,
        category=context.category, brand=context.brand, selling_points=list(context.selling_points),
        description=context.description, search_keywords=list(context.search_keywords),
        attributes=context.attributes, skus=list(context.skus), candidate_metrics=context.candidate_metrics,
        candidate_evidence=list(context.candidate_evidence), rag_quality="normal",
        canonical_rule_citations=list(citations),
    )


def _result_json(result: DeterministicComplianceResult) -> dict[str, object]:
    return {
        "passed": result.passed,
        "violations": [asdict(violation) for violation in result.violations],
        "canonical_citations": _models(result.canonical_citations),
    }


def _changes_valid(
    deterministic: DeterministicComplianceResult,
    semantic: ComplianceAgentResponse | None,
    changes: Sequence[ValidatedRequiredChange],
    citation_ids: set[str],
) -> bool:
    if any(
        len(change.citation_chunk_ids) != len(set(change.citation_chunk_ids))
        or any(item not in citation_ids for item in change.citation_chunk_ids)
        for change in changes
    ):
        return False
    deterministic_pairs = [(violation.code, violation.field) for violation in deterministic.violations]
    if len(deterministic_pairs) != len(set(deterministic_pairs)):
        return False
    actual_deterministic = [
        (change.source_violation_code, change.field)
        for change in changes if change.source_track == "deterministic"
    ]
    if sorted(actual_deterministic) != sorted(deterministic_pairs) or len(actual_deterministic) != len(set(actual_deterministic)):
        return False
    semantic_changes = [change for change in changes if change.source_track == "semantic"]
    if semantic is None:
        return not semantic_changes
    return _json(_models(semantic_changes)) == _json(_models(semantic.required_changes))


def _stored_deterministic(
    value: object, citations: Sequence[CanonicalRuleCitation]
) -> DeterministicComplianceResult | None:
    if not isinstance(value, dict) or set(value) != {"passed", "violations", "canonical_citations"}:
        return None
    if not isinstance(value["passed"], bool) or not isinstance(value["violations"], list):
        return None
    try:
        saved_citations = tuple(
            CanonicalRuleCitation.model_validate(item) for item in value["canonical_citations"]
        )
    except (TypeError, ValueError, ValidationError):
        return None
    if _citation_values(saved_citations) != _citation_values(citations):
        return None
    violations: list[DeterministicViolation] = []
    for item in value["violations"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"code", "field", "message_zh"}
            or not all(isinstance(item.get(key), str) and item[key] for key in item)
        ):
            return None
        violations.append(DeterministicViolation(**item))
    return DeterministicComplianceResult(value["passed"], tuple(violations), saved_citations)


def _actionable_prior_review(
    context: OwnedOptimizationContext,
    revision: ProposalRevision,
    review: ComplianceReview,
) -> bool:
    if (
        review.passed
        or review.quality_status != WorkflowQuality.NORMAL
        or review.error_code is not None
        or review.proposal_id != context.proposal_id
        or review.iteration != revision.iteration
    ):
        return False
    try:
        OptimizationProposalOutput.model_validate(revision.proposal_output)
        citations = tuple(CanonicalRuleCitation.model_validate(value) for value in revision.citations)
        changes = tuple(ValidatedRequiredChange.model_validate(value) for value in review.required_changes)
        semantic = ComplianceAgentResponse.model_validate(review.semantic_review)
    except (TypeError, ValueError, ValidationError):
        return False
    citation_values = _citation_values(citations)
    if citation_values is None or _json(review.citations) != _json(citation_values):
        return False
    deterministic = _stored_deterministic(review.deterministic_checks, citations)
    if deterministic is None or semantic.degraded:
        return False
    try:
        validate_compliance_response(_trusted_from_context(context, citations), semantic)
    except ValueError:
        return False
    return bool(
        not (deterministic.passed and semantic.passed)
        and changes
        and _changes_valid(deterministic, semantic, changes, {citation.chunk_id for citation in citations})
    )


async def _review_input(
    session: AsyncSession,
    locked: _Locked,
    revision_id: str,
    iteration: int,
    canonical_citations: Sequence[CanonicalRuleCitation],
) -> tuple[ProposalRevision, list[dict[str, object]], TrustedOptimizationInput] | None:
    revision = await _revision_for(
        session, locked.proposal, revision_id, iteration, locked.run.created_by
    )
    if revision is None:
        return None
    try:
        saved = [CanonicalRuleCitation.model_validate(item) for item in revision.citations]
    except (TypeError, ValueError, ValidationError):
        return None
    saved_values = _citation_values(saved)
    passed_values = _citation_values(canonical_citations)
    if (
        saved_values is None
        or passed_values is None
        or _json(saved_values) != _json(passed_values)
        or not await _recheck_canonical_citations(session, locked.product, saved)
    ):
        return None
    return revision, saved_values, _trusted_from_context(locked.context, saved)


async def _persist_review(
    session: AsyncSession,
    *,
    locked: _Locked,
    revision: ProposalRevision,
    iteration: int,
    deterministic: DeterministicComplianceResult,
    semantic_json: dict[str, object],
    passed: bool,
    risk_level: ComplianceRiskLevel,
    quality_status: WorkflowQuality,
    error_code: str | None,
    required_changes: Sequence[ValidatedRequiredChange],
    citations: list[dict[str, object]],
    calls: Sequence[ComplianceAgentCallRecord],
    integrity_error: IntegrityError | None,
) -> ReviewPersistenceResult:
    deterministic_json = _result_json(deterministic)
    required_json = _models(required_changes)
    existing = await session.scalar(
        select(ComplianceReview)
        .where(ComplianceReview.proposal_revision_id == revision.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if existing is not None:
        exact = (
            existing.proposal_id == locked.proposal.id
            and existing.iteration == iteration
            and _json(existing.deterministic_checks) == _json(deterministic_json)
            and _json(existing.semantic_review) == _json(semantic_json)
            and existing.passed == passed
            and existing.risk_level == risk_level
            and existing.quality_status == quality_status
            and existing.error_code == error_code
            and _json(existing.required_changes) == _json(required_json)
            and _json(existing.citations) == _json(citations)
            and await _audit_matches(session, locked.run.id, calls, _COMPLIANCE_NODES, iteration)
        )
        if exact:
            await session.commit()
            return ReviewPersistenceResult("replayed", existing.id, existing.passed, None)
        if integrity_error is not None:
            raise integrity_error
        await _fail_locked(session, locked.run, "OPTIMIZATION_REPLAY_CONFLICT")
        return ReviewPersistenceResult("failed", None, None, "OPTIMIZATION_REPLAY_CONFLICT")
    if integrity_error is not None:
        raise integrity_error
    if locked.proposal.current_revision_id != revision.id or locked.product.current_version != locked.context.base_product_version:
        await _fail_locked(session, locked.run, "PRODUCT_VERSION_CONFLICT" if locked.product.current_version != locked.context.base_product_version else "OPTIMIZATION_REPLAY_CONFLICT")
        code = "PRODUCT_VERSION_CONFLICT" if locked.product.current_version != locked.context.base_product_version else "OPTIMIZATION_REPLAY_CONFLICT"
        return ReviewPersistenceResult("failed", None, None, code)
    if not await _store_calls(session, locked.run.id, calls):
        await _fail_locked(session, locked.run, "OPTIMIZATION_REPLAY_CONFLICT")
        return ReviewPersistenceResult("failed", None, None, "OPTIMIZATION_REPLAY_CONFLICT")
    review = ComplianceReview(
        id=str(uuid4()), proposal_id=locked.proposal.id, proposal_revision_id=revision.id,
        iteration=iteration, deterministic_checks=deterministic_json, semantic_review=semantic_json,
        passed=passed, risk_level=risk_level, required_changes=required_json, citations=citations,
        error_code=error_code, quality_status=quality_status,
    )
    session.add(review)
    try:
        await session.flush()
    except IntegrityError as error:
        await session.rollback()
        raise _ImmutableWriteConflict(error) from error
    await session.commit()
    return ReviewPersistenceResult("created", review.id, review.passed, None)


async def _persist_compliance_review(
    session: AsyncSession,
    *,
    workflow_run_id: str,
    lease_owner: str,
    revision_id: str,
    iteration: int,
    deterministic: DeterministicComplianceResult,
    semantic: ComplianceAgentResponse,
    required_changes: Sequence[ValidatedRequiredChange],
    canonical_citations: Sequence[CanonicalRuleCitation],
    calls: Sequence[ComplianceAgentCallRecord],
    integrity_error: IntegrityError | None,
) -> ReviewPersistenceResult:
    locked = await _lock_or_result(session, workflow_run_id, lease_owner)
    if isinstance(locked, OwnedOptimizationContextResult):
        return ReviewPersistenceResult(locked.disposition, None, None, locked.error_code)
    checked = await _review_input(session, locked, revision_id, iteration, canonical_citations)
    if (
        not 0 <= iteration <= 2 or checked is None or not _validate_calls(calls, iteration, _COMPLIANCE_NODES)
    ):
        await _fail_locked(session, locked.run, "OPTIMIZATION_FACT_ERROR")
        return ReviewPersistenceResult("failed", None, None, "OPTIMIZATION_FACT_ERROR")
    revision, citations, trusted = checked
    try:
        validate_compliance_response(trusted, semantic)
    except ValueError:
        await _fail_locked(session, locked.run, "OPTIMIZATION_FACT_ERROR")
        return ReviewPersistenceResult("failed", None, None, "OPTIMIZATION_FACT_ERROR")
    if not _changes_valid(deterministic, semantic, required_changes, {item["chunk_id"] for item in citations}):
        await _fail_locked(session, locked.run, "OPTIMIZATION_FACT_ERROR")
        return ReviewPersistenceResult("failed", None, None, "OPTIMIZATION_FACT_ERROR")
    passed = deterministic.passed and semantic.passed and not semantic.degraded
    quality = WorkflowQuality.DEGRADED if semantic.degraded else WorkflowQuality.NORMAL
    error = "COMPLIANCE_AGENT_DEGRADED" if semantic.degraded else None
    return await _persist_review(
        session, locked=locked, revision=revision, iteration=iteration, deterministic=deterministic,
        semantic_json=semantic.model_dump(mode="json"), passed=passed, risk_level=semantic.risk_level,
        quality_status=quality, error_code=error, required_changes=required_changes, citations=citations,
        calls=calls, integrity_error=integrity_error,
    )


async def persist_compliance_review(
    session: AsyncSession,
    *,
    workflow_run_id: str,
    lease_owner: str,
    revision_id: str,
    iteration: int,
    deterministic: DeterministicComplianceResult,
    semantic: ComplianceAgentResponse,
    required_changes: Sequence[ValidatedRequiredChange],
    canonical_citations: Sequence[CanonicalRuleCitation],
    calls: Sequence[ComplianceAgentCallRecord],
) -> ReviewPersistenceResult:
    try:
        return await _persist_compliance_review(
            session, workflow_run_id=workflow_run_id, lease_owner=lease_owner,
            revision_id=revision_id, iteration=iteration, deterministic=deterministic,
            semantic=semantic, required_changes=required_changes,
            canonical_citations=canonical_citations, calls=calls, integrity_error=None,
        )
    except _ImmutableWriteConflict as conflict:
        return await _persist_compliance_review(
            session, workflow_run_id=workflow_run_id, lease_owner=lease_owner,
            revision_id=revision_id, iteration=iteration, deterministic=deterministic,
            semantic=semantic, required_changes=required_changes,
            canonical_citations=canonical_citations, calls=calls, integrity_error=conflict.error,
        )


async def _persist_compliance_failure(
    session: AsyncSession,
    *,
    workflow_run_id: str,
    lease_owner: str,
    revision_id: str,
    iteration: int,
    deterministic: DeterministicComplianceResult,
    error_code: PendingManualErrorCode,
    required_changes: Sequence[ValidatedRequiredChange],
    canonical_citations: Sequence[CanonicalRuleCitation],
    calls: Sequence[ComplianceAgentCallRecord],
    integrity_error: IntegrityError | None,
) -> ReviewPersistenceResult:
    locked = await _lock_or_result(session, workflow_run_id, lease_owner)
    if isinstance(locked, OwnedOptimizationContextResult):
        return ReviewPersistenceResult(locked.disposition, None, None, locked.error_code)
    checked = await _review_input(session, locked, revision_id, iteration, canonical_citations)
    if (
        error_code not in _FAILURE_REVIEW_CODES or not 0 <= iteration <= 2 or checked is None
        or not _validate_calls(calls, iteration, _COMPLIANCE_NODES)
        or not _changes_valid(deterministic, None, required_changes, {item["chunk_id"] for item in checked[1]} if checked else set())
    ):
        await _fail_locked(session, locked.run, "OPTIMIZATION_FACT_ERROR")
        return ReviewPersistenceResult("failed", None, None, "OPTIMIZATION_FACT_ERROR")
    revision, citations, _ = checked
    return await _persist_review(
        session, locked=locked, revision=revision, iteration=iteration, deterministic=deterministic,
        semantic_json={"status": "unavailable", "error_code": error_code}, passed=False,
        risk_level=ComplianceRiskLevel.HIGH, quality_status=WorkflowQuality.DEGRADED,
        error_code=error_code, required_changes=required_changes, citations=citations, calls=calls,
        integrity_error=integrity_error,
    )


async def persist_compliance_failure(
    session: AsyncSession,
    *,
    workflow_run_id: str,
    lease_owner: str,
    revision_id: str,
    iteration: int,
    deterministic: DeterministicComplianceResult,
    error_code: PendingManualErrorCode,
    required_changes: Sequence[ValidatedRequiredChange],
    canonical_citations: Sequence[CanonicalRuleCitation],
    calls: Sequence[ComplianceAgentCallRecord],
) -> ReviewPersistenceResult:
    try:
        return await _persist_compliance_failure(
            session, workflow_run_id=workflow_run_id, lease_owner=lease_owner,
            revision_id=revision_id, iteration=iteration, deterministic=deterministic,
            error_code=error_code, required_changes=required_changes,
            canonical_citations=canonical_citations, calls=calls, integrity_error=None,
        )
    except _ImmutableWriteConflict as conflict:
        return await _persist_compliance_failure(
            session, workflow_run_id=workflow_run_id, lease_owner=lease_owner,
            revision_id=revision_id, iteration=iteration, deterministic=deterministic,
            error_code=error_code, required_changes=required_changes,
            canonical_citations=canonical_citations, calls=calls, integrity_error=conflict.error,
        )


def _defer_allowed(context: OwnedOptimizationContext, iteration: int, error_code: str) -> bool:
    if context.current_revision_id is None:
        return iteration == 0 and error_code not in {"COMPLIANCE_AGENT_DEGRADED", "OPTIMIZATION_ITERATION_LIMIT"}
    current = context.current_revision_iteration
    assert current is not None
    if context.current_review_id is None:
        return iteration == current and error_code not in {"COMPLIANCE_AGENT_DEGRADED", "OPTIMIZATION_ITERATION_LIMIT"}
    if context.current_review_passed:
        return False
    if error_code == "COMPLIANCE_AGENT_DEGRADED":
        return context.current_review_error_code == error_code and iteration == current
    if error_code == "OPTIMIZATION_ITERATION_LIMIT":
        return (
            current == 2 and context.current_review_quality_status == WorkflowQuality.NORMAL
            and context.current_review_error_code is None and iteration == current
        )
    if context.current_review_error_code is not None:
        return error_code == context.current_review_error_code and iteration == current
    if context.current_review_quality_status == WorkflowQuality.NORMAL and context.current_review_error_code is None and context.current_required_changes:
        return iteration == current + 1 and current < 2
    return iteration == current


async def _defer_optimization_manual(
    session: AsyncSession,
    *,
    workflow_run_id: str,
    lease_owner: str,
    error_code: PendingManualErrorCode,
    iteration: int,
    integrity_error: IntegrityError | None,
    optimization_calls: Sequence[OptimizationAgentCallRecord] = (),
) -> OptimizationTerminalResult:
    locked = await _lock_or_result(session, workflow_run_id, lease_owner)
    if isinstance(locked, OwnedOptimizationContextResult):
        return OptimizationTerminalResult(locked.disposition, locked.error_code)
    if (
        error_code not in _PENDING_CODES or not 0 <= iteration <= 2
        or not _validate_calls(optimization_calls, iteration, _OPTIMIZATION_NODES)
        or not _defer_allowed(locked.context, iteration, error_code)
    ):
        await _fail_locked(session, locked.run, "OPTIMIZATION_REPLAY_CONFLICT")
        return OptimizationTerminalResult("failed", "OPTIMIZATION_REPLAY_CONFLICT")
    if locked.product.current_version != locked.context.base_product_version:
        await _fail_locked(session, locked.run, "PRODUCT_VERSION_CONFLICT")
        return OptimizationTerminalResult("failed", "PRODUCT_VERSION_CONFLICT")
    if integrity_error is not None and not await _calls_present_exact(
        session, locked.run.id, optimization_calls
    ):
        raise integrity_error
    if not await _store_calls(session, locked.run.id, optimization_calls):
        await _fail_locked(session, locked.run, "OPTIMIZATION_REPLAY_CONFLICT")
        return OptimizationTerminalResult("failed", "OPTIMIZATION_REPLAY_CONFLICT")
    if optimization_calls:
        try:
            await session.flush()
        except IntegrityError as error:
            await session.rollback()
            raise _ImmutableWriteConflict(error) from error
    locked.run.status = WorkflowStatus.PENDING_MANUAL
    locked.run.quality_status = WorkflowQuality.DEGRADED
    locked.run.current_step = "pending_manual"
    locked.run.error_code = error_code
    locked.run.lease_owner = None
    locked.run.lease_expires_at = None
    await session.commit()
    return OptimizationTerminalResult("pending_manual", error_code)


async def defer_optimization_manual(
    session: AsyncSession,
    *,
    workflow_run_id: str,
    lease_owner: str,
    error_code: PendingManualErrorCode,
    iteration: int,
    optimization_calls: Sequence[OptimizationAgentCallRecord] = (),
) -> OptimizationTerminalResult:
    try:
        return await _defer_optimization_manual(
            session, workflow_run_id=workflow_run_id, lease_owner=lease_owner,
            error_code=error_code, iteration=iteration, optimization_calls=optimization_calls,
            integrity_error=None,
        )
    except _ImmutableWriteConflict as conflict:
        return await _defer_optimization_manual(
            session, workflow_run_id=workflow_run_id, lease_owner=lease_owner,
            error_code=error_code, iteration=iteration, optimization_calls=optimization_calls,
            integrity_error=conflict.error,
        )


async def finalize_optimization_draft(
    session: AsyncSession, *, workflow_run_id: str, lease_owner: str
) -> OptimizationTerminalResult:
    locked = await _lock_or_result(session, workflow_run_id, lease_owner)
    if isinstance(locked, OwnedOptimizationContextResult):
        return OptimizationTerminalResult(locked.disposition, locked.error_code)
    context = locked.context
    if (
        context.current_revision_id is None or context.current_review_id is None
        or not context.current_review_passed or context.current_review_quality_status != WorkflowQuality.NORMAL
        or context.current_review_error_code is not None
    ):
        await _fail_locked(session, locked.run, "OPTIMIZATION_REPLAY_CONFLICT")
        return OptimizationTerminalResult("failed", "OPTIMIZATION_REPLAY_CONFLICT")
    locked.run.status = WorkflowStatus.DRAFT_READY
    locked.run.quality_status = WorkflowQuality.NORMAL
    locked.run.current_step = "draft_ready"
    locked.run.error_code = None
    locked.run.lease_owner = None
    locked.run.lease_expires_at = None
    await session.commit()
    return OptimizationTerminalResult("draft_ready", None)


async def fail_optimization_run(
    session: AsyncSession,
    *,
    workflow_run_id: str,
    lease_owner: str,
    error_code: OptimizationFailureCode,
) -> OptimizationTerminalResult:
    if error_code not in _FAILURE_CODES:
        return OptimizationTerminalResult("failed", "OPTIMIZATION_REPLAY_CONFLICT")
    locked = await _lock_or_result(session, workflow_run_id, lease_owner)
    if isinstance(locked, OwnedOptimizationContextResult):
        return OptimizationTerminalResult(locked.disposition, locked.error_code)
    await _fail_locked(session, locked.run, error_code)
    return OptimizationTerminalResult("failed", error_code)
