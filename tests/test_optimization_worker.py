import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import event
from langgraph.checkpoint.memory import InMemorySaver

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
    ComplianceSemanticViolation,
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
import backend.optimization_worker as optimization_worker
from backend.optimization_runs import (
    OptimizationClaim,
    claim_next_optimization_run,
    defer_optimization_manual,
    finalize_optimization_draft,
    load_owned_optimization_context,
    persist_compliance_failure,
    persist_compliance_review,
    persist_optimization_revision,
    renew_optimization_lease,
    update_optimization_step,
)
from backend.config import Settings
from backend.database import Base
from backend.optimization_validation import (
    DeterministicComplianceResult,
    DeterministicViolation,
    validate_optimization_output,
)
from backend.schemas import (
    CanonicalRuleCitation,
    DescriptionSection,
    EvidenceRef,
    OptimizationChange,
    OptimizationProposalOutput,
    OutputCitation,
    ProductMetrics,
    TrustedOptimizationInput,
    TrustedProductSku,
    ValidatedRequiredChange,
)


async def _chain(
    session, *, live: bool = False, seed_citation: bool = True
) -> dict[str, object]:
    store = Store(id="store-1", name="旗舰店", code="flagship", enabled=True)
    user = User(
        id="user-1",
        username="operator",
        password_hash="hash",
        role=UserRole.OPERATOR,
        status=UserStatus.ACTIVE,
    )
    product = Product(
        id="product-1",
        store_id=store.id,
        code="PHONE-1",
        title="手机",
        category="数码",
        brand="",
        selling_points=["续航持久"],
        description="普通手机",
        search_keywords=["手机"],
        attributes={"颜色": "黑色"},
        current_version=7,
        enabled=True,
    )
    now = datetime.now(UTC)
    analysis = WorkflowRun(
        id="analysis-1",
        workflow_type=WorkflowType.ANALYSIS,
        store_id=store.id,
        created_by=user.id,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2),
        status=WorkflowStatus.COMPLETED,
        quality_status=WorkflowQuality.NORMAL,
        current_step="product_selected",
    )
    optimization = WorkflowRun(
        id="optimization-1",
        workflow_type=WorkflowType.OPTIMIZATION,
        store_id=store.id,
        created_by=user.id,
        start_date=None,
        end_date=None,
        status=WorkflowStatus.PROCESSING if live else WorkflowStatus.ACCEPTED,
        quality_status=WorkflowQuality.NORMAL,
        attempt_count=1 if live else 0,
        lease_owner="worker-a" if live else None,
        lease_expires_at=now + timedelta(minutes=5) if live else None,
        current_step="claimed" if live else None,
    )
    metrics = ProductMetrics(
        product_id=product.id,
        product_code=product.code,
        impressions=100,
        clicks=20,
        orders=4,
        units=4,
        revenue=Decimal("400.00"),
        refunds=0,
        ctr=Decimal("0.2000"),
        conversion_rate=Decimal("0.2000"),
        refund_rate=Decimal("0.0000"),
        average_order_value=Decimal("100.0000"),
    )
    candidate = AnalysisCandidate(
        id="candidate-1",
        workflow_run_id=analysis.id,
        product_id=product.id,
        rank=1,
        product_code=product.code,
        anomaly_types=["low_conversion"],
        metrics=metrics.model_dump(mode="json"),
        business_impact=Decimal("1.00"),
        evidence=["clicks=20"],
        impact_explanation="影响说明",
        reason="原因",
        recommended_action="建议",
        confidence=Decimal("0.8000"),
    )
    sku_one = ProductSku(
        id="sku-1",
        product_id=product.id,
        code="PHONE-BLACK",
        spec={"颜色": "黑色"},
        price=Decimal("100.00"),
        current_stock=8,
    )
    sku_two = ProductSku(
        id="sku-2",
        product_id=product.id,
        code="PHONE-WHITE",
        spec={"颜色": "白色"},
        price=Decimal("120.00"),
        current_stock=9,
    )
    proposal = ProductProposal(
        id="proposal-1",
        analysis_run_id=analysis.id,
        analysis_candidate_id=candidate.id,
        optimization_run_id=optimization.id,
        store_id=store.id,
        product_id=product.id,
        base_product_version=7,
        selection_idempotency_hash="a" * 64,
    )
    session.add_all([store, user, UserStoreScope(user_id=user.id, store_id=store.id)])
    await session.flush()
    session.add(product)
    await session.flush()
    session.add_all([analysis, optimization])
    await session.flush()
    session.add_all([candidate, sku_one, sku_two])
    await session.flush()
    session.add(proposal)
    await session.commit()
    if seed_citation:
        await _seed_current_citation(session)
    return {
        "store": store,
        "user": user,
        "product": product,
        "analysis": analysis,
        "run": optimization,
        "candidate": candidate,
        "proposal": proposal,
        "metrics": metrics,
        "skus": (sku_one, sku_two),
    }


def _citation() -> CanonicalRuleCitation:
    return CanonicalRuleCitation(
        document_id="document-1",
        version_id="version-1",
        chunk_id="chunk-1",
        document_name="通用规则",
        version_number=1,
        category="通用规则",
        canonical_text="商品描述应当真实准确。",
        active=True,
        applicable=True,
    )


def _trusted(chain: dict[str, object]) -> TrustedOptimizationInput:
    product = chain["product"]
    candidate = chain["candidate"]
    skus = chain["skus"]
    assert isinstance(product, Product)
    assert isinstance(candidate, AnalysisCandidate)
    assert isinstance(skus, tuple)
    return TrustedOptimizationInput(
        store_id="store-1",
        product_id=product.id,
        base_product_version=7,
        title=product.title,
        category=product.category,
        brand=product.brand,
        selling_points=product.selling_points,
        description=product.description,
        search_keywords=product.search_keywords,
        attributes=product.attributes,
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
        candidate_metrics=ProductMetrics.model_validate(candidate.metrics),
        candidate_evidence=candidate.evidence,
        rag_quality="normal",
        canonical_rule_citations=[_citation()],
    )


def _output() -> OptimizationProposalOutput:
    evidence = [EvidenceRef(kind="citation", value="chunk-1")]
    description = [DescriptionSection(heading="商品详情", body="适合日常使用", evidence=evidence)]
    return OptimizationProposalOutput(
        title="手机",
        selling_points=["续航持久"],
        description=description,
        keywords=["手机"],
        attribute_completions=[],
        changes=[
            OptimizationChange(
                field="description",
                current_value="普通手机",
                suggested_value=description,
                reason="补充商品详情",
                evidence=evidence,
            )
        ],
        citations=[OutputCitation(chunk_id="chunk-1")],
        price_suggestions=[],
        sku_suggestions=[],
    )


def _optimization_calls(
    iteration: int, *, include_repair: bool = False
) -> list[OptimizationAgentCallRecord]:
    calls = [
        OptimizationAgentCallRecord(
            node_name="call_product_optimization_agent",
            call_type=AgentCallType.PRIMARY,
            iteration=iteration,
            attempt=1,
            model="deepseek-v4-flash",
            prompt_version="product-optimization-v1",
            status="success",
            input_hash="b" * 64,
            prompt_tokens=10,
            completion_tokens=10,
            total_tokens=20,
            duration_ms=1,
            estimated_cost=Decimal("0.000001"),
            error_code=None,
        )
    ]
    if include_repair:
        calls.append(
            OptimizationAgentCallRecord(
                node_name="repair_product_optimization_schema",
                call_type=AgentCallType.SCHEMA_REPAIR,
                iteration=iteration,
                attempt=2,
                model="deepseek-v4-flash",
                prompt_version="product-optimization-v1",
                status="schema_invalid",
                input_hash="d" * 64,
                prompt_tokens=10,
                completion_tokens=10,
                total_tokens=20,
                duration_ms=1,
                estimated_cost=Decimal("0.000001"),
                error_code="DEEPSEEK_SCHEMA_INVALID",
            )
        )
    return calls


def _compliance_calls(iteration: int) -> list[ComplianceAgentCallRecord]:
    return [
        ComplianceAgentCallRecord(
            node_name="call_product_compliance_agent",
            call_type=AgentCallType.PRIMARY,
            iteration=iteration,
            attempt=1,
            model="deepseek-v4-flash",
            prompt_version="product-compliance-v1",
            status="success",
            input_hash="c" * 64,
            prompt_tokens=10,
            completion_tokens=10,
            total_tokens=20,
            duration_ms=1,
            estimated_cost=Decimal("0.000001"),
            error_code=None,
        )
    ]


def _normal_failed_review() -> tuple[
    DeterministicComplianceResult, ComplianceAgentResponse, list[ValidatedRequiredChange]
]:
    deterministic = DeterministicComplianceResult(
        passed=False,
        violations=(DeterministicViolation("TITLE_LANGUAGE", "title", "标题必须包含中文字符"),),
        canonical_citations=(_citation(),),
    )
    semantic_change = ValidatedRequiredChange(
        source_track="semantic",
        source_violation_code="EXAGGERATION",
        field="title",
        instruction="删除夸大用语",
        citation_chunk_ids=["chunk-1"],
    )
    semantic = ComplianceAgentResponse(
        passed=False,
        risk_level=ComplianceRiskLevel.MEDIUM,
        violations=[
            ComplianceSemanticViolation(
                code="EXAGGERATION",
                field="title",
                message_zh="标题存在夸大表达",
                citation_chunk_ids=["chunk-1"],
            )
        ],
        required_changes=[semantic_change],
        citations=[OutputCitation(chunk_id="chunk-1")],
        confidence=Decimal("0.8"),
        degraded=False,
    )
    return (
        deterministic,
        semantic,
        [
            ValidatedRequiredChange(
                source_track="deterministic",
                source_violation_code="TITLE_LANGUAGE",
                field="title",
                instruction="标题补充中文字符",
                citation_chunk_ids=[],
            ),
            semantic_change,
        ],
    )


async def test_claim_optimization_never_claims_or_exhausts_analysis_rows(session) -> None:
    chain = await _chain(session)
    analysis = WorkflowRun(
        id="analysis-expired",
        workflow_type=WorkflowType.ANALYSIS,
        store_id="store-1",
        created_by="user-1",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2),
        status=WorkflowStatus.PROCESSING,
        quality_status=WorkflowQuality.NORMAL,
        attempt_count=3,
        lease_owner="old-owner",
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    session.add(analysis)
    await session.commit()

    claim = await claim_next_optimization_run(
        session, lease_owner="worker-a", lease_seconds=60
    )

    run = chain["run"]
    assert isinstance(run, WorkflowRun)
    assert claim == OptimizationClaim(run.id, "worker-a", 1)
    fresh_analysis = await session.get(WorkflowRun, analysis.id, populate_existing=True)
    fresh_optimization = await session.get(WorkflowRun, run.id, populate_existing=True)
    assert fresh_analysis is not None and fresh_optimization is not None
    assert (fresh_analysis.status, fresh_analysis.attempt_count) == (
        WorkflowStatus.PROCESSING,
        3,
    )
    assert (fresh_optimization.status, fresh_optimization.attempt_count) == (
        WorkflowStatus.PROCESSING,
        1,
    )


async def test_optimization_simple_updates_are_type_and_owner_isolated(session) -> None:
    chain = await _chain(session, live=True)
    analysis = WorkflowRun(
        id="analysis-live",
        workflow_type=WorkflowType.ANALYSIS,
        store_id="store-1",
        created_by="user-1",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2),
        status=WorkflowStatus.PROCESSING,
        quality_status=WorkflowQuality.NORMAL,
        lease_owner="worker-a",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    session.add(analysis)
    await session.commit()
    analysis_id = analysis.id
    run = chain["run"]
    assert isinstance(run, WorkflowRun)
    optimization_id = run.id

    assert await renew_optimization_lease(
        session, workflow_run_id=analysis_id, lease_owner="worker-a", lease_seconds=60
    ) is False
    assert await update_optimization_step(
        session, workflow_run_id=analysis_id, lease_owner="worker-a", current_step="next"
    ) is False
    assert await update_optimization_step(
        session,
        workflow_run_id=optimization_id,
        lease_owner="worker-a",
        current_step="persist_revision",
    ) is True

    fresh = await session.get(WorkflowRun, analysis_id, populate_existing=True)
    assert fresh is not None and fresh.current_step is None


async def test_context_refreshes_operator_authorization_from_database(session) -> None:
    chain = await _chain(session, live=True)
    loaded = await session.get(User, "user-1")
    assert loaded is not None and loaded.status is UserStatus.ACTIVE
    await session.execute(
        update(User)
        .where(User.id == "user-1")
        .values(status=UserStatus.DISABLED)
        .execution_options(synchronize_session=False)
    )
    await session.commit()

    result = await load_owned_optimization_context(
        session, workflow_run_id="optimization-1", lease_owner="worker-a"
    )

    assert (result.disposition, result.error_code, result.context) == (
        "failed",
        "OPTIMIZATION_AUTHORIZATION_CHANGED",
        None,
    )
    fresh = await session.get(WorkflowRun, str(chain["run"].id), populate_existing=True)
    assert fresh is not None
    assert (fresh.status, fresh.lease_owner, fresh.error_code) == (
        WorkflowStatus.FAILED,
        None,
        "OPTIMIZATION_AUTHORIZATION_CHANGED",
    )


async def test_context_rejects_candidate_metrics_for_a_different_product(session) -> None:
    await _chain(session, live=True)
    await session.execute(
        update(AnalysisCandidate)
        .where(AnalysisCandidate.id == "candidate-1")
        .values(metrics={
            "product_id": "another-product", "product_code": "PHONE-1", "impressions": 100,
            "clicks": 20, "orders": 4, "units": 4, "revenue": "400.00", "refunds": 0,
            "ctr": "0.2000", "conversion_rate": "0.2000", "refund_rate": "0.0000",
            "average_order_value": "100.0000",
        })
        .execution_options(synchronize_session=False)
    )
    await session.commit()

    result = await load_owned_optimization_context(
        session, workflow_run_id="optimization-1", lease_owner="worker-a"
    )

    assert (result.disposition, result.error_code) == (
        "failed", "OPTIMIZATION_CONTEXT_INCONSISTENT"
    )


async def test_revision_sequence_uses_complete_normal_failed_review_and_exact_replay(session) -> None:
    chain = await _chain(session, live=True)
    trusted = _trusted(chain)
    output = _output()
    revision_zero = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=trusted,
        output=output,
        canonical_citations=[_citation()],
        calls=_optimization_calls(0),
    )
    deterministic, semantic, changes = _normal_failed_review()
    review_zero = await persist_compliance_review(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        revision_id=str(revision_zero.revision_id),
        iteration=0,
        deterministic=deterministic,
        semantic=semantic,
        required_changes=changes,
        canonical_citations=[_citation()],
        calls=_compliance_calls(0),
    )
    revision_one = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=1,
        trusted=trusted,
        output=output,
        canonical_citations=[_citation()],
        calls=_optimization_calls(1),
    )
    replay_zero = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=trusted,
        output=output,
        canonical_citations=[_citation()],
        calls=_optimization_calls(0),
    )

    assert revision_zero.disposition == "created"
    assert review_zero.disposition == "created"
    assert revision_one.disposition == "created"
    assert replay_zero == type(replay_zero)("replayed", revision_zero.revision_id, None)
    proposal = await session.get(ProductProposal, "proposal-1", populate_existing=True)
    assert proposal is not None and proposal.current_revision_id == revision_one.revision_id
    stored_zero = await session.scalar(
        select(ProposalRevision).where(ProposalRevision.id == revision_zero.revision_id)
    )
    stored_one = await session.scalar(
        select(ProposalRevision).where(ProposalRevision.id == revision_one.revision_id)
    )
    assert stored_zero is not None and stored_one is not None
    assert (
        stored_zero.revision_number,
        stored_zero.origin,
        stored_zero.created_by,
        stored_zero.parent_revision_id,
    ) == (1, ProposalRevisionOrigin.AGENT, "user-1", None)
    assert (
        stored_one.revision_number,
        stored_one.origin,
        stored_one.created_by,
        stored_one.parent_revision_id,
    ) == (2, ProposalRevisionOrigin.AGENT, "user-1", stored_zero.id)


async def test_automatic_revision_load_rejects_a_different_creator(session) -> None:
    chain = await _chain(session, live=True)
    revision = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=_trusted(chain),
        output=_output(),
        canonical_citations=[_citation()],
        calls=_optimization_calls(0),
    )
    other = User(
        id="user-2",
        username="other-operator",
        password_hash="hash",
        role=UserRole.OPERATOR,
        status=UserStatus.ACTIVE,
    )
    session.add(other)
    await session.flush()
    await session.execute(
        update(ProposalRevision)
        .where(ProposalRevision.id == revision.revision_id)
        .values(created_by=other.id)
        .execution_options(synchronize_session=False)
    )
    await session.commit()

    result = await load_owned_optimization_context(
        session, workflow_run_id="optimization-1", lease_owner="worker-a"
    )

    assert (result.disposition, result.error_code) == (
        "failed",
        "OPTIMIZATION_CONTEXT_INCONSISTENT",
    )


async def test_failure_review_is_fixed_degraded_and_cannot_unlock_next_iteration(session) -> None:
    chain = await _chain(session, live=True)
    trusted = _trusted(chain)
    revision = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=trusted,
        output=_output(),
        canonical_citations=[_citation()],
        calls=_optimization_calls(0),
    )
    deterministic = validate_optimization_output(trusted, _output())
    failure = await persist_compliance_failure(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        revision_id=str(revision.revision_id),
        iteration=0,
        deterministic=deterministic,
        error_code="DEEPSEEK_TIMEOUT",
        required_changes=[],
        canonical_citations=[_citation()],
        calls=_compliance_calls(0),
    )
    blocked = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=1,
        trusted=trusted,
        output=_output(),
        canonical_citations=[_citation()],
        calls=_optimization_calls(1),
    )

    assert (failure.disposition, failure.passed) == ("created", False)
    review = await session.get(ComplianceReview, str(failure.review_id))
    assert review is not None
    assert (review.semantic_review, review.risk_level, review.quality_status, review.error_code) == (
        {"status": "unavailable", "error_code": "DEEPSEEK_TIMEOUT"},
        ComplianceRiskLevel.HIGH,
        WorkflowQuality.DEGRADED,
        "DEEPSEEK_TIMEOUT",
    )
    assert (blocked.disposition, blocked.error_code) == ("failed", "OPTIMIZATION_REPLAY_CONFLICT")


async def test_review_rejects_omitted_deterministic_required_change(session) -> None:
    chain = await _chain(session, live=True)
    revision = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=_trusted(chain),
        output=_output(),
        canonical_citations=[_citation()],
        calls=_optimization_calls(0),
    )
    deterministic, semantic, changes = _normal_failed_review()

    result = await persist_compliance_review(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        revision_id=str(revision.revision_id),
        iteration=0,
        deterministic=deterministic,
        semantic=semantic,
        required_changes=changes[1:],
        canonical_citations=[_citation()],
        calls=_compliance_calls(0),
    )

    assert (result.disposition, result.error_code) == ("failed", "OPTIMIZATION_FACT_ERROR")
    assert await session.scalar(select(ComplianceReview.id)) is None


async def test_pre_revision_timeout_persists_only_safe_audit_with_pending_manual(session) -> None:
    await _chain(session, live=True)
    result = await defer_optimization_manual(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        error_code="DEEPSEEK_TIMEOUT",
        iteration=0,
        optimization_calls=_optimization_calls(0),
    )

    assert result.disposition == "pending_manual"
    run = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    calls = list(
        await session.scalars(select(AgentCall).where(AgentCall.workflow_run_id == "optimization-1"))
    )
    assert run is not None
    assert (run.status, run.quality_status, run.error_code, run.lease_owner) == (
        WorkflowStatus.PENDING_MANUAL,
        WorkflowQuality.DEGRADED,
        "DEEPSEEK_TIMEOUT",
        None,
    )
    assert [(call.node_name, call.iteration, call.attempt) for call in calls] == [
        ("call_product_optimization_agent", 0, 1)
    ]


async def test_conflicting_optimization_audit_never_creates_revision_or_overwrites_history(session) -> None:
    chain = await _chain(session, live=True)
    call = _optimization_calls(0)[0]
    session.add(
        AgentCall(
            id="existing-call",
            workflow_run_id="optimization-1",
            node_name=call.node_name,
            call_type=call.call_type,
            iteration=call.iteration,
            attempt=call.attempt,
            model="other-model",
            prompt_version=call.prompt_version,
            status=call.status,
            input_hash=call.input_hash,
            prompt_tokens=call.prompt_tokens,
            completion_tokens=call.completion_tokens,
            total_tokens=call.total_tokens,
            duration_ms=call.duration_ms,
            estimated_cost=call.estimated_cost,
            error_code=call.error_code,
        )
    )
    await session.commit()

    result = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=_trusted(chain),
        output=_output(),
        canonical_citations=[_citation()],
        calls=[call],
    )

    assert (result.disposition, result.error_code) == ("failed", "OPTIMIZATION_REPLAY_CONFLICT")
    assert await session.scalar(select(ProposalRevision.id)) is None
    existing = await session.get(AgentCall, "existing-call", populate_existing=True)
    assert existing is not None and existing.model == "other-model"


async def test_claim_exhausts_only_expired_optimization_attempt_three(session) -> None:
    chain = await _chain(session)
    exhausted = WorkflowRun(
        id="optimization-exhausted",
        workflow_type=WorkflowType.OPTIMIZATION,
        store_id="store-1",
        created_by="user-1",
        start_date=None,
        end_date=None,
        status=WorkflowStatus.PROCESSING,
        quality_status=WorkflowQuality.NORMAL,
        attempt_count=3,
        lease_owner="old-owner",
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    session.add(exhausted)
    await session.commit()

    claim = await claim_next_optimization_run(session, lease_owner="worker-a", lease_seconds=60)

    assert claim == OptimizationClaim("optimization-1", "worker-a", 1)
    fresh = await session.get(WorkflowRun, exhausted.id, populate_existing=True)
    assert fresh is not None
    assert (fresh.status, fresh.error_code, fresh.lease_owner) == (
        WorkflowStatus.FAILED,
        "LEASE_ATTEMPTS_EXHAUSTED",
        None,
    )


async def test_context_recovery_exposes_validated_revision_and_review_only(session) -> None:
    chain = await _chain(session, live=True)
    trusted = _trusted(chain)
    revision = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=trusted,
        output=_output(),
        canonical_citations=[_citation()],
        calls=_optimization_calls(0),
    )
    before_review = await load_owned_optimization_context(
        session, workflow_run_id="optimization-1", lease_owner="worker-a"
    )
    deterministic, semantic, changes = _normal_failed_review()
    review = await persist_compliance_review(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        revision_id=str(revision.revision_id),
        iteration=0,
        deterministic=deterministic,
        semantic=semantic,
        required_changes=changes,
        canonical_citations=[_citation()],
        calls=_compliance_calls(0),
    )
    after_review = await load_owned_optimization_context(
        session, workflow_run_id="optimization-1", lease_owner="worker-a"
    )

    assert before_review.context is not None
    assert (before_review.context.current_revision_id, before_review.context.current_revision_iteration) == (
        revision.revision_id,
        0,
    )
    assert before_review.context.current_review_id is None
    assert after_review.context is not None
    assert (
        after_review.context.current_review_id,
        after_review.context.current_review_passed,
        after_review.context.current_review_quality_status,
        after_review.context.current_review_error_code,
    ) == (review.review_id, False, WorkflowQuality.NORMAL, None)
    assert after_review.context.current_required_changes == tuple(changes)


async def test_combined_pass_finalizes_only_after_persisted_review(session) -> None:
    chain = await _chain(session, live=True)
    trusted = _trusted(chain)
    output = _output()
    revision = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=trusted,
        output=output,
        canonical_citations=[_citation()],
        calls=_optimization_calls(0),
    )
    deterministic = validate_optimization_output(trusted, output)
    semantic = ComplianceAgentResponse(
        passed=True,
        risk_level=ComplianceRiskLevel.LOW,
        violations=[],
        required_changes=[],
        citations=[],
        confidence=Decimal("1"),
        degraded=False,
    )
    review = await persist_compliance_review(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        revision_id=str(revision.revision_id),
        iteration=0,
        deterministic=deterministic,
        semantic=semantic,
        required_changes=[],
        canonical_citations=[_citation()],
        calls=_compliance_calls(0),
    )
    terminal = await finalize_optimization_draft(
        session, workflow_run_id="optimization-1", lease_owner="worker-a"
    )

    assert (review.disposition, review.passed) == ("created", True)
    assert terminal.disposition == "draft_ready"
    run = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    assert run is not None and (run.status, run.quality_status, run.lease_owner) == (
        WorkflowStatus.DRAFT_READY,
        WorkflowQuality.NORMAL,
        None,
    )


async def test_semantic_degraded_review_maps_to_fixed_pending_manual_error(session) -> None:
    chain = await _chain(session, live=True)
    trusted = _trusted(chain)
    output = _output()
    revision = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=trusted,
        output=output,
        canonical_citations=[_citation()],
        calls=_optimization_calls(0),
    )
    semantic = ComplianceAgentResponse(
        passed=False,
        risk_level=ComplianceRiskLevel.HIGH,
        violations=[],
        required_changes=[],
        citations=[],
        confidence=Decimal("0"),
        degraded=True,
    )
    review = await persist_compliance_review(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        revision_id=str(revision.revision_id),
        iteration=0,
        deterministic=validate_optimization_output(trusted, output),
        semantic=semantic,
        required_changes=[],
        canonical_citations=[_citation()],
        calls=_compliance_calls(0),
    )
    terminal = await defer_optimization_manual(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        error_code="COMPLIANCE_AGENT_DEGRADED",
        iteration=0,
    )

    assert (review.disposition, review.passed) == ("created", False)
    stored = await session.get(ComplianceReview, str(review.review_id))
    assert stored is not None and (stored.quality_status, stored.error_code) == (
        WorkflowQuality.DEGRADED,
        "COMPLIANCE_AGENT_DEGRADED",
    )
    assert terminal.disposition == "pending_manual"


@pytest.mark.parametrize(
    ("corrupt_changes", "expected_error"),
    [
        (lambda changes: changes[:1], "OPTIMIZATION_REPLAY_CONFLICT"),
        (
            lambda changes: [changes[0].model_copy(update={"field": "wrong-field"}), changes[1]],
            "OPTIMIZATION_REPLAY_CONFLICT",
        ),
        (lambda changes: [changes[0], changes[0], changes[1]], "OPTIMIZATION_REPLAY_CONFLICT"),
        (lambda changes: [{"source_track": "deterministic"}], "OPTIMIZATION_CONTEXT_INCONSISTENT"),
    ],
    ids=["omitted-semantic", "replaced-deterministic", "duplicate-deterministic", "malformed"],
)
async def test_damaged_persisted_review_never_unlocks_next_revision(
    session, corrupt_changes, expected_error
) -> None:
    chain = await _chain(session, live=True)
    trusted = _trusted(chain)
    revision = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=trusted,
        output=_output(),
        canonical_citations=[_citation()],
        calls=_optimization_calls(0),
    )
    deterministic, semantic, changes = _normal_failed_review()
    review = await persist_compliance_review(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        revision_id=str(revision.revision_id),
        iteration=0,
        deterministic=deterministic,
        semantic=semantic,
        required_changes=changes,
        canonical_citations=[_citation()],
        calls=_compliance_calls(0),
    )
    loaded_review = await session.get(ComplianceReview, str(review.review_id))
    assert loaded_review is not None
    await session.execute(
        update(ComplianceReview)
        .where(ComplianceReview.id == review.review_id)
        .values(required_changes=[
            change.model_dump(mode="json") if isinstance(change, ValidatedRequiredChange) else change
            for change in corrupt_changes(changes)
        ])
        .execution_options(synchronize_session=False)
    )
    await session.commit()

    result = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=1,
        trusted=trusted,
        output=_output(),
        canonical_citations=[_citation()],
        calls=_optimization_calls(1),
    )

    assert (result.disposition, result.error_code) == ("failed", expected_error)
    assert await session.scalar(
        select(ProposalRevision.id).where(ProposalRevision.iteration == 1)
    ) is None
    assert await session.scalar(
        select(AgentCall.id).where(AgentCall.iteration == 1)
    ) is None


async def test_defer_requires_same_error_as_persisted_failure_review(session) -> None:
    chain = await _chain(session, live=True)
    trusted = _trusted(chain)
    revision = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=trusted,
        output=_output(),
        canonical_citations=[_citation()],
        calls=_optimization_calls(0),
    )
    failure = await persist_compliance_failure(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        revision_id=str(revision.revision_id),
        iteration=0,
        deterministic=validate_optimization_output(trusted, _output()),
        error_code="DEEPSEEK_TIMEOUT",
        required_changes=[],
        canonical_citations=[_citation()],
        calls=_compliance_calls(0),
    )
    assert failure.disposition == "created"

    wrong = await defer_optimization_manual(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        error_code="KNOWLEDGE_MODEL_UNAVAILABLE",
        iteration=0,
    )

    assert (wrong.disposition, wrong.error_code) == ("failed", "OPTIMIZATION_REPLAY_CONFLICT")
    run = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    assert run is not None and run.status is WorkflowStatus.FAILED


async def test_defer_allows_exact_persisted_failure_error(session) -> None:
    chain = await _chain(session, live=True)
    trusted = _trusted(chain)
    revision = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=trusted,
        output=_output(),
        canonical_citations=[_citation()],
        calls=_optimization_calls(0),
    )
    await persist_compliance_failure(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        revision_id=str(revision.revision_id),
        iteration=0,
        deterministic=validate_optimization_output(trusted, _output()),
        error_code="DEEPSEEK_TIMEOUT",
        required_changes=[],
        canonical_citations=[_citation()],
        calls=_compliance_calls(0),
    )

    terminal = await defer_optimization_manual(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        error_code="DEEPSEEK_TIMEOUT",
        iteration=0,
    )

    assert (terminal.disposition, terminal.error_code) == ("pending_manual", "DEEPSEEK_TIMEOUT")


async def test_revision_refreshes_stale_sku_and_audit_rows_before_comparing(session) -> None:
    chain = await _chain(session, live=True)
    trusted = _trusted(chain)
    cached_sku = await session.get(ProductSku, "sku-1")
    assert cached_sku is not None and cached_sku.price == Decimal("100.00")
    await session.execute(
        update(ProductSku)
        .where(ProductSku.id == "sku-1")
        .values(price=Decimal("101.00"), current_stock=7)
        .execution_options(synchronize_session=False)
    )
    await session.commit()

    fact_result = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=trusted,
        output=_output(),
        canonical_citations=[_citation()],
        calls=_optimization_calls(0),
    )

    assert (fact_result.disposition, fact_result.error_code) == ("failed", "OPTIMIZATION_FACT_ERROR")
    assert await session.scalar(select(ProposalRevision.id)) is None


async def test_revision_replay_refreshes_stale_audit_row_before_exact_comparison(session) -> None:
    chain = await _chain(session, live=True)
    trusted = _trusted(chain)
    output = _output()
    created = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=trusted,
        output=output,
        canonical_citations=[_citation()],
        calls=_optimization_calls(0),
    )
    cached = await session.scalar(select(AgentCall).where(AgentCall.iteration == 0))
    assert cached is not None and cached.model == "deepseek-v4-flash"
    await session.execute(
        update(AgentCall)
        .where(AgentCall.id == cached.id)
        .values(model="stale-db-value")
        .execution_options(synchronize_session=False)
    )
    await session.commit()

    replay = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=trusted,
        output=output,
        canonical_citations=[_citation()],
        calls=_optimization_calls(0),
    )

    assert (replay.disposition, replay.error_code) == ("failed", "OPTIMIZATION_REPLAY_CONFLICT")
    stored = await session.get(AgentCall, str(cached.id), populate_existing=True)
    assert stored is not None and stored.model == "stale-db-value"
    assert created.revision_id is not None


async def test_revision_integrity_retry_rechecks_owner_after_rollback(session, monkeypatch) -> None:
    chain = await _chain(session, live=True)
    original_flush = session.flush
    injected = False

    async def race_flush(*args, **kwargs) -> None:
        nonlocal injected
        if injected:
            await original_flush(*args, **kwargs)
            return
        injected = True
        await session.rollback()
        await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == "optimization-1")
            .values(lease_owner="worker-b")
        )
        await session.commit()
        raise IntegrityError("insert", {}, RuntimeError("simulated conflict"))

    monkeypatch.setattr(session, "flush", race_flush)
    result = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=_trusted(chain),
        output=_output(),
        canonical_citations=[_citation()],
        calls=_optimization_calls(0),
    )

    assert result.disposition == "lease_lost"
    assert await session.scalar(select(ProposalRevision.id)) is None
    assert await session.scalar(select(AgentCall.id)) is None


async def test_review_integrity_retry_rechecks_version_after_rollback(session, monkeypatch) -> None:
    chain = await _chain(session, live=True)
    trusted = _trusted(chain)
    revision = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=trusted,
        output=_output(),
        canonical_citations=[_citation()],
        calls=_optimization_calls(0),
    )
    deterministic, semantic, changes = _normal_failed_review()
    original_flush = session.flush
    injected = False

    async def race_flush(*args, **kwargs) -> None:
        nonlocal injected
        if injected:
            await original_flush(*args, **kwargs)
            return
        injected = True
        await session.rollback()
        await session.execute(
            update(Product)
            .where(Product.id == "product-1")
            .values(current_version=8)
        )
        await session.commit()
        raise IntegrityError("insert", {}, RuntimeError("simulated conflict"))

    monkeypatch.setattr(session, "flush", race_flush)
    result = await persist_compliance_review(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        revision_id=str(revision.revision_id),
        iteration=0,
        deterministic=deterministic,
        semantic=semantic,
        required_changes=changes,
        canonical_citations=[_citation()],
        calls=_compliance_calls(0),
    )

    assert (result.disposition, result.error_code) == ("failed", "PRODUCT_VERSION_CONFLICT")
    assert await session.scalar(select(ComplianceReview.id)) is None
    assert await session.scalar(
        select(AgentCall.id).where(AgentCall.node_name == "call_product_compliance_agent")
    ) is None


async def test_failure_review_integrity_retry_rechecks_owner_after_rollback(session, monkeypatch) -> None:
    chain = await _chain(session, live=True)
    trusted = _trusted(chain)
    revision = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=trusted,
        output=_output(),
        canonical_citations=[_citation()],
        calls=_optimization_calls(0),
    )
    original_flush = session.flush
    injected = False

    async def race_flush(*args, **kwargs) -> None:
        nonlocal injected
        if injected:
            await original_flush(*args, **kwargs)
            return
        injected = True
        await session.rollback()
        await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == "optimization-1")
            .values(lease_owner="worker-b")
        )
        await session.commit()
        raise IntegrityError("insert", {}, RuntimeError("simulated conflict"))

    monkeypatch.setattr(session, "flush", race_flush)
    result = await persist_compliance_failure(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        revision_id=str(revision.revision_id),
        iteration=0,
        deterministic=validate_optimization_output(trusted, _output()),
        error_code="DEEPSEEK_TIMEOUT",
        required_changes=[],
        canonical_citations=[_citation()],
        calls=_compliance_calls(0),
    )

    assert result.disposition == "lease_lost"
    assert await session.scalar(select(ComplianceReview.id)) is None
    assert await session.scalar(
        select(AgentCall.id).where(AgentCall.node_name == "call_product_compliance_agent")
    ) is None


async def test_terminal_audit_integrity_retry_rechecks_version_after_rollback(session, monkeypatch) -> None:
    await _chain(session, live=True)
    original_flush = session.flush
    injected = False

    async def race_flush(*args, **kwargs) -> None:
        nonlocal injected
        if injected:
            await original_flush(*args, **kwargs)
            return
        injected = True
        await session.rollback()
        await session.execute(
            update(Product)
            .where(Product.id == "product-1")
            .values(current_version=8)
        )
        await session.commit()
        raise IntegrityError("insert", {}, RuntimeError("simulated conflict"))

    monkeypatch.setattr(session, "flush", race_flush)
    result = await defer_optimization_manual(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        error_code="DEEPSEEK_TIMEOUT",
        iteration=0,
        optimization_calls=_optimization_calls(0),
    )

    assert (result.disposition, result.error_code) == ("failed", "PRODUCT_VERSION_CONFLICT")
    assert await session.scalar(select(AgentCall.id)) is None
    run = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    assert run is not None and run.status is WorkflowStatus.FAILED


async def test_terminal_audit_integrity_retry_reuses_only_exact_existing_call(session, monkeypatch) -> None:
    await _chain(session, live=True)
    call = _optimization_calls(0)[0]
    session.add(
        AgentCall(
            id="existing-call",
            workflow_run_id="optimization-1",
            node_name=call.node_name,
            call_type=call.call_type,
            iteration=call.iteration,
            attempt=call.attempt,
            model=call.model,
            prompt_version=call.prompt_version,
            status=call.status,
            input_hash=call.input_hash,
            prompt_tokens=call.prompt_tokens,
            completion_tokens=call.completion_tokens,
            total_tokens=call.total_tokens,
            duration_ms=call.duration_ms,
            estimated_cost=call.estimated_cost,
            error_code=call.error_code,
        )
    )
    await session.commit()
    original_flush = session.flush
    injected = False

    async def race_flush(*args, **kwargs) -> None:
        nonlocal injected
        if injected:
            await original_flush(*args, **kwargs)
            return
        injected = True
        await session.rollback()
        raise IntegrityError("update", {}, RuntimeError("simulated conflict"))

    monkeypatch.setattr(session, "flush", race_flush)
    result = await defer_optimization_manual(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        error_code="DEEPSEEK_TIMEOUT",
        iteration=0,
        optimization_calls=[call],
    )

    assert result.disposition == "pending_manual"
    assert len(list(await session.scalars(select(AgentCall)))) == 1


async def test_revision_integrity_retry_replays_exact_immutable_race(session, monkeypatch) -> None:
    chain = await _chain(session, live=True)
    trusted = _trusted(chain)
    original_flush = session.flush
    injected = False

    async def race_flush(*args, **kwargs) -> None:
        nonlocal injected
        if injected:
            await original_flush(*args, **kwargs)
            return
        injected = True
        revision = next(row for row in session.new if isinstance(row, ProposalRevision))
        call = next(row for row in session.new if isinstance(row, AgentCall))
        await session.rollback()
        session.add_all([revision, call])
        await session.execute(
            update(ProductProposal)
            .where(ProductProposal.id == "proposal-1")
            .values(current_revision_id=revision.id)
        )
        await session.commit()
        raise IntegrityError("insert", {}, RuntimeError("simulated conflict"))

    monkeypatch.setattr(session, "flush", race_flush)
    result = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=trusted,
        output=_output(),
        canonical_citations=[_citation()],
        calls=_optimization_calls(0),
    )

    assert result.disposition == "replayed"
    assert await session.scalar(select(ProposalRevision.id)) == result.revision_id
    assert len(list(await session.scalars(select(AgentCall)))) == 1
    proposal = await session.get(ProductProposal, "proposal-1", populate_existing=True)
    assert proposal is not None and proposal.current_revision_id == result.revision_id


async def test_normal_review_integrity_retry_replays_exact_immutable_race(session, monkeypatch) -> None:
    chain = await _chain(session, live=True)
    trusted = _trusted(chain)
    revision = await persist_optimization_revision(
        session, workflow_run_id="optimization-1", lease_owner="worker-a", iteration=0,
        trusted=trusted, output=_output(), canonical_citations=[_citation()], calls=_optimization_calls(0),
    )
    deterministic, semantic, changes = _normal_failed_review()
    original_flush = session.flush
    injected = False

    async def race_flush(*args, **kwargs) -> None:
        nonlocal injected
        if injected:
            await original_flush(*args, **kwargs)
            return
        injected = True
        review = next(row for row in session.new if isinstance(row, ComplianceReview))
        call = next(row for row in session.new if isinstance(row, AgentCall))
        await session.rollback()
        session.add_all([review, call])
        await session.commit()
        raise IntegrityError("insert", {}, RuntimeError("simulated conflict"))

    monkeypatch.setattr(session, "flush", race_flush)
    result = await persist_compliance_review(
        session, workflow_run_id="optimization-1", lease_owner="worker-a",
        revision_id=str(revision.revision_id), iteration=0, deterministic=deterministic,
        semantic=semantic, required_changes=changes, canonical_citations=[_citation()], calls=_compliance_calls(0),
    )

    assert result.disposition == "replayed"
    assert await session.scalar(select(ComplianceReview.id)) == result.review_id
    assert len(list(await session.scalars(select(AgentCall)))) == 2


async def test_failure_review_integrity_retry_replays_exact_immutable_race(session, monkeypatch) -> None:
    chain = await _chain(session, live=True)
    trusted = _trusted(chain)
    revision = await persist_optimization_revision(
        session, workflow_run_id="optimization-1", lease_owner="worker-a", iteration=0,
        trusted=trusted, output=_output(), canonical_citations=[_citation()], calls=_optimization_calls(0),
    )
    original_flush = session.flush
    injected = False

    async def race_flush(*args, **kwargs) -> None:
        nonlocal injected
        if injected:
            await original_flush(*args, **kwargs)
            return
        injected = True
        review = next(row for row in session.new if isinstance(row, ComplianceReview))
        call = next(row for row in session.new if isinstance(row, AgentCall))
        await session.rollback()
        session.add_all([review, call])
        await session.commit()
        raise IntegrityError("insert", {}, RuntimeError("simulated conflict"))

    monkeypatch.setattr(session, "flush", race_flush)
    result = await persist_compliance_failure(
        session, workflow_run_id="optimization-1", lease_owner="worker-a",
        revision_id=str(revision.revision_id), iteration=0,
        deterministic=validate_optimization_output(trusted, _output()), error_code="DEEPSEEK_TIMEOUT",
        required_changes=[], canonical_citations=[_citation()], calls=_compliance_calls(0),
    )

    assert result.disposition == "replayed"
    assert await session.scalar(select(ComplianceReview.id)) == result.review_id
    assert len(list(await session.scalars(select(AgentCall)))) == 2


async def test_revision_integrity_retry_reraises_missing_immutable_race(session, monkeypatch) -> None:
    chain = await _chain(session, live=True)
    original_flush = session.flush
    injected = False

    async def race_flush(*args, **kwargs) -> None:
        nonlocal injected
        if injected:
            await original_flush(*args, **kwargs)
            return
        injected = True
        await session.rollback()
        raise IntegrityError("insert", {}, RuntimeError("simulated conflict"))

    monkeypatch.setattr(session, "flush", race_flush)
    with pytest.raises(IntegrityError, match="simulated conflict"):
        await persist_optimization_revision(
            session, workflow_run_id="optimization-1", lease_owner="worker-a", iteration=0,
            trusted=_trusted(chain), output=_output(), canonical_citations=[_citation()],
            calls=_optimization_calls(0),
        )

    assert await session.scalar(select(ProposalRevision.id)) is None
    assert await session.scalar(select(AgentCall.id)) is None
    proposal = await session.get(ProductProposal, "proposal-1", populate_existing=True)
    run = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    assert proposal is not None and proposal.current_revision_id is None
    assert run is not None and run.status is WorkflowStatus.PROCESSING and run.lease_owner == "worker-a"


async def test_revision_integrity_retry_reraises_nonexact_existing_race(session, monkeypatch) -> None:
    chain = await _chain(session, live=True)
    trusted = _trusted(chain)
    original_flush = session.flush
    injected = False

    async def race_flush(*args, **kwargs) -> None:
        nonlocal injected
        if injected:
            await original_flush(*args, **kwargs)
            return
        injected = True
        revision = next(row for row in session.new if isinstance(row, ProposalRevision))
        call = next(row for row in session.new if isinstance(row, AgentCall))
        await session.rollback()
        call.model = "different-model"
        session.add_all([revision, call])
        await session.execute(
            update(ProductProposal)
            .where(ProductProposal.id == "proposal-1")
            .values(current_revision_id=revision.id)
        )
        await session.commit()
        raise IntegrityError("insert", {}, RuntimeError("simulated conflict"))

    monkeypatch.setattr(session, "flush", race_flush)
    with pytest.raises(IntegrityError, match="simulated conflict"):
        await persist_optimization_revision(
            session, workflow_run_id="optimization-1", lease_owner="worker-a", iteration=0,
            trusted=trusted, output=_output(), canonical_citations=[_citation()], calls=_optimization_calls(0),
        )

    assert len(list(await session.scalars(select(ProposalRevision)))) == 1
    assert len(list(await session.scalars(select(AgentCall)))) == 1
    proposal = await session.get(ProductProposal, "proposal-1", populate_existing=True)
    run = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    assert proposal is not None and proposal.current_revision_id is not None
    assert run is not None and run.status is WorkflowStatus.PROCESSING and run.lease_owner == "worker-a"


async def test_empty_terminal_commit_integrity_error_is_not_retried(session, monkeypatch) -> None:
    await _chain(session, live=True)

    async def broken_commit() -> None:
        raise IntegrityError("update", {}, RuntimeError("terminal database error"))

    monkeypatch.setattr(session, "commit", broken_commit)
    with pytest.raises(IntegrityError, match="terminal database error"):
        await defer_optimization_manual(
            session,
            workflow_run_id="optimization-1",
            lease_owner="worker-a",
            error_code="DEEPSEEK_TIMEOUT",
            iteration=0,
        )

    await session.rollback()
    run = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    assert run is not None and run.status is WorkflowStatus.PROCESSING and run.lease_owner == "worker-a"


async def test_multi_call_race_reaches_the_controlled_flush_before_any_autoflush(
    session, monkeypatch
) -> None:
    chain = await _chain(session, live=True)
    trusted = _trusted(chain)
    calls = _optimization_calls(0, include_repair=True)
    original_flush = session.flush
    injected = False

    async def race_flush(*args, **kwargs) -> None:
        nonlocal injected
        if injected:
            await original_flush(*args, **kwargs)
            return
        injected = True
        staged_calls = [row for row in session.new if isinstance(row, AgentCall)]
        assert {(row.node_name, row.call_type, row.attempt) for row in staged_calls} == {
            (call.node_name, call.call_type, call.attempt) for call in calls
        }
        revision = next(row for row in session.new if isinstance(row, ProposalRevision))
        await session.rollback()
        session.add_all([revision, *staged_calls])
        await session.execute(
            update(ProductProposal)
            .where(ProductProposal.id == "proposal-1")
            .values(current_revision_id=revision.id)
        )
        await session.commit()
        raise IntegrityError("insert", {}, RuntimeError("simulated conflict"))

    monkeypatch.setattr(session, "flush", race_flush)
    result = await persist_optimization_revision(
        session, workflow_run_id="optimization-1", lease_owner="worker-a", iteration=0,
        trusted=trusted, output=_output(), canonical_citations=[_citation()], calls=calls,
    )

    assert result.disposition == "replayed"
    assert len(list(await session.scalars(select(ProposalRevision)))) == 1
    assert len(list(await session.scalars(select(AgentCall)))) == 2


@pytest_asyncio.fixture
async def optimization_worker_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _worker_settings() -> Settings:
    return Settings(
        _env_file=None,
        jwt_secret_key="test-only-secret-at-least-32-characters",
        deepseek_api_key="mock-key",
        deepseek_base_url="https://mock.deepseek.invalid",
        optimization_lease_seconds=60,
    )


class _RecordingSaver(InMemorySaver):
    def __init__(self) -> None:
        super().__init__()
        self.thread_ids: set[str] = set()
        self.states: list[dict[str, object]] = []

    async def aput(self, config, checkpoint, metadata, new_versions):
        self.thread_ids.add(config["configurable"]["thread_id"])
        values = checkpoint.get("channel_values", {})
        self.states.append(dict(values))
        return await super().aput(config, checkpoint, metadata, new_versions)


class _TrustedLoader:
    def __init__(self, trusted: TrustedOptimizationInput, error_code: str | None = None) -> None:
        self.trusted = trusted
        self.error_code = error_code
        self.calls = 0
        self.boundaries = 0

    async def __call__(self, context, before_external) -> TrustedOptimizationInput:
        self.calls += 1
        await before_external()
        self.boundaries += 1
        if self.error_code is not None:
            raise optimization_worker.TrustedInputLoadFailure(self.error_code)
        return self.trusted


async def _worker_chain(
    factory: async_sessionmaker[AsyncSession], *, live: bool = False
) -> dict[str, object]:
    async with factory() as session:
        return await _chain(session, live=live)


async def _worker_counts(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int, int]:
    async with factory() as session:
        return (
            len(list(await session.scalars(select(ProposalRevision)))),
            len(list(await session.scalars(select(ComplianceReview)))),
            len(list(await session.scalars(select(AgentCall)))),
        )


async def _worker_run(factory: async_sessionmaker[AsyncSession]) -> WorkflowRun:
    async with factory() as session:
        run = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
        assert run is not None
        return run


def _worker_transport(
    output: OptimizationProposalOutput,
    compliance: ComplianceAgentResponse,
    requests: list[dict[str, object]],
    *,
    schema_invalid: bool = False,
) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)["messages"][1]["content"]
        request_payload = json.loads(payload)
        requests.append(request_payload)
        content = (
            compliance.model_dump_json()
            if "candidate_output" in request_payload
            else output.model_dump_json()
        )
        if schema_invalid:
            content = "{}"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
            },
        )

    return httpx.MockTransport(handler)


def _passing_compliance() -> ComplianceAgentResponse:
    return ComplianceAgentResponse(
        passed=True,
        risk_level=ComplianceRiskLevel.LOW,
        violations=[],
        required_changes=[],
        citations=[OutputCitation(chunk_id="chunk-1")],
        confidence=Decimal("0.8"),
        degraded=False,
    )


async def _reset_to_accepted(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == "optimization-1")
            .values(
                status=WorkflowStatus.ACCEPTED,
                lease_owner=None,
                lease_expires_at=None,
                current_step="claimed",
                error_code=None,
            )
        )
        await session.commit()


async def _seed_actionable_revision(
    factory: async_sessionmaker[AsyncSession], *, add_second_revision: bool = False
) -> TrustedOptimizationInput:
    chain = await _worker_chain(factory, live=True)
    trusted = _trusted(chain)
    async with factory() as session:
        first = await persist_optimization_revision(
            session,
            workflow_run_id="optimization-1",
            lease_owner="worker-a",
            iteration=0,
            trusted=trusted,
            output=_output(),
            canonical_citations=[_citation()],
            calls=_optimization_calls(0),
        )
        assert first.revision_id is not None
        deterministic, semantic, changes = _normal_failed_review()
        review = await persist_compliance_review(
            session,
            workflow_run_id="optimization-1",
            lease_owner="worker-a",
            revision_id=first.revision_id,
            iteration=0,
            deterministic=deterministic,
            semantic=semantic,
            required_changes=changes,
            canonical_citations=[_citation()],
            calls=_compliance_calls(0),
        )
        assert review.review_id is not None
        if add_second_revision:
            second = await persist_optimization_revision(
                session,
                workflow_run_id="optimization-1",
                lease_owner="worker-a",
                iteration=1,
                trusted=trusted,
                output=_output(),
                canonical_citations=[_citation()],
                calls=_optimization_calls(1),
            )
            assert second.revision_id is not None
    await _reset_to_accepted(factory)
    return trusted


async def test_optimization_worker_iteration_zero_finalizes_with_only_safe_checkpoint_progress(
    optimization_worker_factory,
) -> None:
    chain = await _worker_chain(optimization_worker_factory)
    trusted = _trusted(chain)
    requests: list[dict[str, object]] = []
    saver = _RecordingSaver()

    processed = await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=saver,
        trusted_input_loader=_TrustedLoader(trusted),
        transport=_worker_transport(_output(), _passing_compliance(), requests),
        max_agent_attempts=1,
    )

    run = await _worker_run(optimization_worker_factory)
    assert processed == "optimization-1"
    assert (run.status, run.quality_status) == (WorkflowStatus.DRAFT_READY, WorkflowQuality.NORMAL)
    assert await _worker_counts(optimization_worker_factory) == (1, 1, 2)
    assert saver.thread_ids == {"optimization-1"}
    allowed = {
        "workflow_run_id", "iteration", "revision_id", "review_id", "review_passed",
        "review_quality_status", "error_code", "next_node",
    }
    for state in saver.states:
        business_keys = {
            key for key in state
            if key != "__start__" and not key.startswith("branch:") and not key.startswith("start:")
        }
        assert business_keys <= allowed
        assert "普通手机" not in json.dumps(state, ensure_ascii=False, default=str)
    assert ["candidate_output" in request for request in requests] == [False, True]


async def test_optimization_worker_uses_database_review_to_call_iteration_one_without_cycle(
    optimization_worker_factory,
) -> None:
    trusted = await _seed_actionable_revision(optimization_worker_factory)
    requests: list[dict[str, object]] = []

    await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_RecordingSaver(),
        trusted_input_loader=_TrustedLoader(trusted),
        transport=_worker_transport(_output(), _passing_compliance(), requests),
        max_agent_attempts=1,
    )

    assert [request["iteration"] for request in requests] == [1, 1]
    assert await _worker_counts(optimization_worker_factory) == (2, 2, 4)
    assert (await _worker_run(optimization_worker_factory)).status is WorkflowStatus.DRAFT_READY


async def test_optimization_worker_skips_existing_target_revision_and_only_reviews_database_iteration(
    optimization_worker_factory,
) -> None:
    trusted = await _seed_actionable_revision(optimization_worker_factory, add_second_revision=True)
    requests: list[dict[str, object]] = []
    saver = _RecordingSaver()

    await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=saver,
        trusted_input_loader=_TrustedLoader(trusted),
        transport=_worker_transport(_output(), _passing_compliance(), requests),
        max_agent_attempts=1,
    )

    assert len(requests) == 1 and "candidate_output" in requests[0]
    assert requests[0]["iteration"] == 1
    assert await _worker_counts(optimization_worker_factory) == (2, 2, 4)


@pytest.mark.parametrize(
    ("rag_quality", "error_code"),
    [("zero_hit", "KNOWLEDGE_ZERO_HIT"), ("low_confidence", "KNOWLEDGE_LOW_CONFIDENCE")],
)
async def test_optimization_worker_low_quality_before_revision_degrades_without_agent_or_immutable_rows(
    optimization_worker_factory, rag_quality, error_code
) -> None:
    chain = await _worker_chain(optimization_worker_factory)
    trusted = _trusted(chain).model_copy(update={"rag_quality": rag_quality})
    requests: list[dict[str, object]] = []

    await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_RecordingSaver(),
        trusted_input_loader=_TrustedLoader(trusted),
        transport=_worker_transport(_output(), _passing_compliance(), requests),
        max_agent_attempts=1,
    )

    run = await _worker_run(optimization_worker_factory)
    assert (run.status, run.quality_status, run.error_code) == (
        WorkflowStatus.PENDING_MANUAL, WorkflowQuality.DEGRADED, error_code
    )
    assert not requests
    assert await _worker_counts(optimization_worker_factory) == (0, 0, 0)


async def test_optimization_worker_persists_pre_revision_schema_attempts_before_manual_defer(
    optimization_worker_factory,
) -> None:
    chain = await _worker_chain(optimization_worker_factory)
    trusted = _trusted(chain)
    requests: list[dict[str, object]] = []

    await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_RecordingSaver(),
        trusted_input_loader=_TrustedLoader(trusted),
        transport=_worker_transport(_output(), _passing_compliance(), requests, schema_invalid=True),
        max_agent_attempts=1,
    )

    run = await _worker_run(optimization_worker_factory)
    assert (run.status, run.quality_status, run.error_code) == (
        WorkflowStatus.PENDING_MANUAL, WorkflowQuality.DEGRADED, "DEEPSEEK_SCHEMA_INVALID"
    )
    assert len(requests) == 2
    assert await _worker_counts(optimization_worker_factory) == (0, 0, 2)


async def test_optimization_worker_lease_loss_before_loader_boundary_stops_without_request_or_terminal_write(
    optimization_worker_factory, monkeypatch
) -> None:
    chain = await _worker_chain(optimization_worker_factory)
    trusted = _trusted(chain)
    requests: list[dict[str, object]] = []

    async def lost_renew(*args, **kwargs) -> bool:
        return False

    monkeypatch.setattr(optimization_worker, "renew_optimization_lease", lost_renew)
    loader = _TrustedLoader(trusted)
    await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_RecordingSaver(),
        trusted_input_loader=loader,
        transport=_worker_transport(_output(), _passing_compliance(), requests),
        max_agent_attempts=1,
    )

    run = await _worker_run(optimization_worker_factory)
    assert loader.calls == 1 and loader.boundaries == 0
    assert not requests and await _worker_counts(optimization_worker_factory) == (0, 0, 0)
    assert (run.status, run.lease_owner, run.error_code) == (
        WorkflowStatus.PROCESSING, "worker-a", None
    )


async def test_optimization_worker_propagates_cancelled_loader_without_terminal_write(
    optimization_worker_factory,
) -> None:
    await _worker_chain(optimization_worker_factory)

    async def cancelled_loader(context, before_external) -> TrustedOptimizationInput:
        await before_external()
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await optimization_worker.run_once(
            optimization_worker_factory,
            settings=_worker_settings(),
            lease_owner="worker-a",
            checkpointer=_RecordingSaver(),
            trusted_input_loader=cancelled_loader,
            transport=_worker_transport(_output(), _passing_compliance(), []),
            max_agent_attempts=1,
        )

    run = await _worker_run(optimization_worker_factory)
    assert (run.status, run.lease_owner, run.error_code) == (
        WorkflowStatus.PROCESSING, "worker-a", None
    )


def test_trusted_input_load_failure_only_accepts_knowledge_dependency_codes() -> None:
    with pytest.raises(ValueError, match="unsupported knowledge load error"):
        optimization_worker.TrustedInputLoadFailure("DEEPSEEK_TIMEOUT")
    assert optimization_worker.TrustedInputLoadFailure(
        "KNOWLEDGE_DEPENDENCY_ERROR"
    ).error_code == "KNOWLEDGE_DEPENDENCY_ERROR"


async def test_optimization_worker_rejects_invalid_attempt_cap_before_claiming_a_run(
    optimization_worker_factory,
) -> None:
    chain = await _worker_chain(optimization_worker_factory)
    with pytest.raises(ValueError, match="max_agent_attempts"):
        await optimization_worker.run_once(
            optimization_worker_factory,
            settings=_worker_settings(),
            lease_owner="worker-a",
            checkpointer=_RecordingSaver(),
            trusted_input_loader=_TrustedLoader(_trusted(chain)),
            max_agent_attempts=0,
        )
    run = await _worker_run(optimization_worker_factory)
    assert (run.status, run.attempt_count, run.lease_owner) == (
        WorkflowStatus.ACCEPTED, 0, None
    )


async def test_optimization_worker_bounds_normal_failed_reviews_at_iteration_two(
    optimization_worker_factory,
) -> None:
    chain = await _worker_chain(optimization_worker_factory)
    trusted = _trusted(chain)
    requests: list[dict[str, object]] = []
    _deterministic, semantic, _changes = _normal_failed_review()

    await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_RecordingSaver(),
        trusted_input_loader=_TrustedLoader(trusted),
        transport=_worker_transport(_output(), semantic, requests),
        max_agent_attempts=1,
    )

    run = await _worker_run(optimization_worker_factory)
    assert (run.status, run.quality_status, run.error_code) == (
        WorkflowStatus.PENDING_MANUAL,
        WorkflowQuality.DEGRADED,
        "OPTIMIZATION_ITERATION_LIMIT",
    )
    assert [request["iteration"] for request in requests] == [0, 0, 1, 1, 2, 2]
    assert await _worker_counts(optimization_worker_factory) == (3, 3, 6)


@pytest.mark.parametrize("degraded", [False, True])
async def test_optimization_worker_never_reopens_unavailable_or_degraded_review(
    optimization_worker_factory, degraded
) -> None:
    chain = await _worker_chain(optimization_worker_factory, live=True)
    trusted = _trusted(chain)
    async with optimization_worker_factory() as session:
        revision = await persist_optimization_revision(
            session,
            workflow_run_id="optimization-1",
            lease_owner="worker-a",
            iteration=0,
            trusted=trusted,
            output=_output(),
            canonical_citations=[_citation()],
            calls=_optimization_calls(0),
        )
        assert revision.revision_id is not None
        deterministic = validate_optimization_output(trusted, _output())
        if degraded:
            semantic = ComplianceAgentResponse(
                passed=False,
                risk_level=ComplianceRiskLevel.HIGH,
                violations=[],
                required_changes=[],
                citations=[OutputCitation(chunk_id="chunk-1")],
                confidence=Decimal("0.8"),
                degraded=True,
            )
            review = await persist_compliance_review(
                session,
                workflow_run_id="optimization-1",
                lease_owner="worker-a",
                revision_id=revision.revision_id,
                iteration=0,
                deterministic=deterministic,
                semantic=semantic,
                required_changes=[],
                canonical_citations=[_citation()],
                calls=_compliance_calls(0),
            )
            expected_error = "COMPLIANCE_AGENT_DEGRADED"
        else:
            review = await persist_compliance_failure(
                session,
                workflow_run_id="optimization-1",
                lease_owner="worker-a",
                revision_id=revision.revision_id,
                iteration=0,
                deterministic=deterministic,
                error_code="DEEPSEEK_TIMEOUT",
                required_changes=[],
                canonical_citations=[_citation()],
                calls=(),
            )
            expected_error = "DEEPSEEK_TIMEOUT"
        assert review.review_id is not None
    await _reset_to_accepted(optimization_worker_factory)
    requests: list[dict[str, object]] = []

    await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_RecordingSaver(),
        trusted_input_loader=_TrustedLoader(trusted),
        transport=_worker_transport(_output(), _passing_compliance(), requests),
        max_agent_attempts=1,
    )

    run = await _worker_run(optimization_worker_factory)
    assert not requests
    assert (run.status, run.quality_status, run.error_code) == (
        WorkflowStatus.PENDING_MANUAL, WorkflowQuality.DEGRADED, expected_error
    )


class _FailingReadSaver(InMemorySaver):
    async def aget_tuple(self, *args, **kwargs):
        raise RuntimeError("checkpoint read failed")


async def test_optimization_worker_checkpoint_failure_uses_guarded_fresh_failure_session(
    optimization_worker_factory,
) -> None:
    chain = await _worker_chain(optimization_worker_factory)
    result = await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_FailingReadSaver(),
        trusted_input_loader=_TrustedLoader(_trusted(chain)),
        max_agent_attempts=1,
    )
    assert result == "optimization-1"
    run = await _worker_run(optimization_worker_factory)
    assert (run.status, run.error_code, run.lease_owner) == (
        WorkflowStatus.FAILED, "OPTIMIZATION_CHECKPOINT_ERROR", None
    )


async def test_optimization_worker_http_lease_loss_stops_before_post_or_write(
    optimization_worker_factory, monkeypatch
) -> None:
    chain = await _worker_chain(optimization_worker_factory)
    trusted = _trusted(chain)
    requests: list[dict[str, object]] = []
    renewals = 0

    async def renew_once(*args, **kwargs) -> bool:
        nonlocal renewals
        renewals += 1
        return renewals == 1

    monkeypatch.setattr(optimization_worker, "renew_optimization_lease", renew_once)
    await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_RecordingSaver(),
        trusted_input_loader=_TrustedLoader(trusted),
        transport=_worker_transport(_output(), _passing_compliance(), requests),
        max_agent_attempts=1,
    )

    run = await _worker_run(optimization_worker_factory)
    assert renewals == 2
    assert not requests and await _worker_counts(optimization_worker_factory) == (0, 0, 0)
    assert (run.status, run.lease_owner, run.error_code) == (
        WorkflowStatus.PROCESSING, "worker-a", None
    )


async def test_optimization_worker_compliance_http_lease_loss_stops_before_post_or_write(
    optimization_worker_factory, monkeypatch
) -> None:
    chain = await _worker_chain(optimization_worker_factory, live=True)
    trusted = _trusted(chain)
    async with optimization_worker_factory() as session:
        revision = await persist_optimization_revision(
            session,
            workflow_run_id="optimization-1",
            lease_owner="worker-a",
            iteration=0,
            trusted=trusted,
            output=_output(),
            canonical_citations=[_citation()],
            calls=_optimization_calls(0),
        )
        assert revision.revision_id is not None
    await _reset_to_accepted(optimization_worker_factory)
    requests: list[dict[str, object]] = []
    renewals = 0

    async def renew_once(*args, **kwargs) -> bool:
        nonlocal renewals
        renewals += 1
        return renewals == 1

    monkeypatch.setattr(optimization_worker, "renew_optimization_lease", renew_once)
    await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_RecordingSaver(),
        trusted_input_loader=_TrustedLoader(trusted),
        transport=_worker_transport(_output(), _passing_compliance(), requests),
        max_agent_attempts=1,
    )

    run = await _worker_run(optimization_worker_factory)
    assert renewals == 2
    assert not requests and await _worker_counts(optimization_worker_factory) == (1, 0, 1)
    assert (run.status, run.lease_owner, run.error_code) == (
        WorkflowStatus.PROCESSING, "worker-a", None
    )


async def test_optimization_worker_persists_unavailable_review_after_revision_before_manual_defer(
    optimization_worker_factory,
) -> None:
    chain = await _worker_chain(optimization_worker_factory, live=True)
    trusted = _trusted(chain)
    async with optimization_worker_factory() as session:
        revision = await persist_optimization_revision(
            session,
            workflow_run_id="optimization-1",
            lease_owner="worker-a",
            iteration=0,
            trusted=trusted,
            output=_output(),
            canonical_citations=[_citation()],
            calls=_optimization_calls(0),
        )
        assert revision.revision_id is not None
    await _reset_to_accepted(optimization_worker_factory)
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)["messages"][1]["content"]
        requests.append(json.loads(payload))
        content = "{}" if "candidate_output" in requests[-1] else _output().model_dump_json()
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_RecordingSaver(),
        trusted_input_loader=_TrustedLoader(trusted),
        transport=httpx.MockTransport(handler),
        max_agent_attempts=1,
    )

    run = await _worker_run(optimization_worker_factory)
    assert ["candidate_output" in request for request in requests] == [True, True]
    assert (run.status, run.quality_status, run.error_code) == (
        WorkflowStatus.PENDING_MANUAL, WorkflowQuality.DEGRADED, "DEEPSEEK_SCHEMA_INVALID"
    )
    assert await _worker_counts(optimization_worker_factory) == (1, 1, 3)


@pytest.mark.parametrize("change", ["owner", "version"])
async def test_optimization_worker_reloads_context_after_loader_before_any_agent_request(
    optimization_worker_factory, change
) -> None:
    chain = await _worker_chain(optimization_worker_factory)
    trusted = _trusted(chain)
    requests: list[dict[str, object]] = []

    async def stale_loader(context, before_external) -> TrustedOptimizationInput:
        await before_external()
        async with optimization_worker_factory() as replacement_session:
            if change == "owner":
                await replacement_session.execute(
                    update(WorkflowRun)
                    .where(WorkflowRun.id == "optimization-1")
                    .values(lease_owner="worker-b")
                )
            else:
                await replacement_session.execute(
                    update(Product).where(Product.id == "product-1").values(current_version=8)
                )
            await replacement_session.commit()
        return trusted

    await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_RecordingSaver(),
        trusted_input_loader=stale_loader,
        transport=_worker_transport(_output(), _passing_compliance(), requests),
        max_agent_attempts=1,
    )

    run = await _worker_run(optimization_worker_factory)
    assert not requests and await _worker_counts(optimization_worker_factory) == (0, 0, 0)
    if change == "owner":
        assert (run.status, run.lease_owner, run.error_code) == (
            WorkflowStatus.PROCESSING, "worker-b", None
        )
    else:
        assert (run.status, run.lease_owner, run.error_code) == (
            WorkflowStatus.FAILED, None, "PRODUCT_VERSION_CONFLICT"
        )


async def test_optimization_worker_checkpoint_uses_persisted_degraded_review_fields(
    optimization_worker_factory,
) -> None:
    chain = await _worker_chain(optimization_worker_factory)
    trusted = _trusted(chain)
    semantic = ComplianceAgentResponse(
        passed=False,
        risk_level=ComplianceRiskLevel.HIGH,
        violations=[],
        required_changes=[],
        citations=[OutputCitation(chunk_id="chunk-1")],
        confidence=Decimal("0.8"),
        degraded=True,
    )
    saver = _RecordingSaver()

    await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=saver,
        trusted_input_loader=_TrustedLoader(trusted),
        transport=_worker_transport(_output(), semantic, []),
        max_agent_attempts=1,
    )

    review_states = [state for state in saver.states if state.get("review_id")]
    assert review_states
    assert all(
        state.get("review_quality_status") == "degraded"
        and state.get("error_code") == "COMPLIANCE_AGENT_DEGRADED"
        for state in review_states
    )


class _CancelAfterRevisionSaver(_RecordingSaver):
    def __init__(self, method: str) -> None:
        super().__init__()
        self.method = method
        self.cancelled = False

    async def aput(self, config, checkpoint, metadata, new_versions):
        if (
            self.method == "aput"
            and not self.cancelled
            and checkpoint.get("channel_values", {}).get("revision_id")
        ):
            self.cancelled = True
            raise asyncio.CancelledError()
        return await super().aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(self, *args, **kwargs):
        writes = kwargs.get("writes") or args[1]
        if (
            self.method == "aput_writes"
            and not self.cancelled
            and any(channel == "revision_id" and value for channel, value in writes)
        ):
            self.cancelled = True
            raise asyncio.CancelledError()
        return await super().aput_writes(*args, **kwargs)


@pytest.mark.parametrize("method", ["aput", "aput_writes"])
async def test_optimization_worker_reclaims_after_cancelled_checkpoint_without_repeating_revision(
    optimization_worker_factory, method
) -> None:
    chain = await _worker_chain(optimization_worker_factory)
    trusted = _trusted(chain)
    saver = _CancelAfterRevisionSaver(method)
    first_requests: list[dict[str, object]] = []

    with pytest.raises(asyncio.CancelledError):
        await optimization_worker.run_once(
            optimization_worker_factory,
            settings=_worker_settings(),
            lease_owner="worker-a",
            checkpointer=saver,
            trusted_input_loader=_TrustedLoader(trusted),
            transport=_worker_transport(_output(), _passing_compliance(), first_requests),
            max_agent_attempts=1,
        )

    first_run = await _worker_run(optimization_worker_factory)
    assert (first_run.status, first_run.lease_owner) == (WorkflowStatus.PROCESSING, "worker-a")
    assert await _worker_counts(optimization_worker_factory) == (1, 0, 1)
    assert ["candidate_output" in request for request in first_requests] == [False]
    async with optimization_worker_factory() as session:
        await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == "optimization-1")
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    resumed_requests: list[dict[str, object]] = []

    resumed = await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-b",
        checkpointer=saver,
        trusted_input_loader=_TrustedLoader(trusted),
        transport=_worker_transport(_output(), _passing_compliance(), resumed_requests),
        max_agent_attempts=1,
    )

    assert resumed == "optimization-1"
    assert ["candidate_output" in request for request in resumed_requests] == [True]
    assert await _worker_counts(optimization_worker_factory) == (1, 1, 2)
    assert saver.thread_ids == {"optimization-1"}


class _FailingWriteSaver(InMemorySaver):
    def __init__(self, method: str) -> None:
        super().__init__()
        self.method = method

    async def aput(self, *args, **kwargs):
        if self.method == "aput":
            raise RuntimeError("checkpoint write failed")
        return await super().aput(*args, **kwargs)

    async def aput_writes(self, *args, **kwargs):
        if self.method == "aput_writes":
            raise RuntimeError("checkpoint writes failed")
        return await super().aput_writes(*args, **kwargs)


@pytest.mark.parametrize("method", ["aput", "aput_writes"])
async def test_optimization_worker_checkpoint_write_failure_marks_live_owner_failed(
    optimization_worker_factory, method
) -> None:
    chain = await _worker_chain(optimization_worker_factory)
    result = await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_FailingWriteSaver(method),
        trusted_input_loader=_TrustedLoader(_trusted(chain)),
        max_agent_attempts=1,
    )

    assert result == "optimization-1"
    run = await _worker_run(optimization_worker_factory)
    assert (run.status, run.error_code, run.lease_owner) == (
        WorkflowStatus.FAILED, "OPTIMIZATION_CHECKPOINT_ERROR", None
    )


async def test_optimization_worker_checkpoint_failure_after_owner_change_writes_nothing(
    optimization_worker_factory,
) -> None:
    chain = await _worker_chain(optimization_worker_factory)

    class _StaleOwnerWriteSaver(InMemorySaver):
        async def aput(self, *args, **kwargs):
            async with optimization_worker_factory() as replacement_session:
                await replacement_session.execute(
                    update(WorkflowRun)
                    .where(WorkflowRun.id == "optimization-1")
                    .values(lease_owner="worker-b")
                )
                await replacement_session.commit()
            raise RuntimeError("checkpoint write failed")

    await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_StaleOwnerWriteSaver(),
        trusted_input_loader=_TrustedLoader(_trusted(chain)),
        max_agent_attempts=1,
    )

    run = await _worker_run(optimization_worker_factory)
    assert (run.status, run.error_code, run.lease_owner) == (
        WorkflowStatus.PROCESSING, None, "worker-b"
    )
    assert await _worker_counts(optimization_worker_factory) == (0, 0, 0)


async def test_optimization_worker_database_failure_uses_distinct_fresh_failure_session(
    optimization_worker_factory, monkeypatch
) -> None:
    chain = await _worker_chain(optimization_worker_factory)
    graph_session_ids: list[int] = []
    failure_session_ids: list[int] = []
    original_fail = optimization_worker.fail_optimization_run

    async def broken_load(session, **kwargs):
        graph_session_ids.append(id(session))
        raise SQLAlchemyError("graph database failure")

    async def record_fail(session, **kwargs):
        failure_session_ids.append(id(session))
        return await original_fail(session, **kwargs)

    monkeypatch.setattr(optimization_worker, "load_owned_optimization_context", broken_load)
    monkeypatch.setattr(optimization_worker, "fail_optimization_run", record_fail)
    result = await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_RecordingSaver(),
        trusted_input_loader=_TrustedLoader(_trusted(chain)),
        max_agent_attempts=1,
    )

    assert result == "optimization-1"
    assert graph_session_ids and failure_session_ids
    assert graph_session_ids[0] != failure_session_ids[0]
    run = await _worker_run(optimization_worker_factory)
    assert (run.status, run.error_code) == (WorkflowStatus.FAILED, "OPTIMIZATION_DATABASE_ERROR")


async def test_optimization_worker_propagates_second_database_failure_from_fresh_failure_session(
    optimization_worker_factory, monkeypatch
) -> None:
    chain = await _worker_chain(optimization_worker_factory)

    async def broken_load(session, **kwargs):
        raise SQLAlchemyError("graph database failure")

    async def broken_fail(session, **kwargs):
        raise SQLAlchemyError("fresh database failure")

    monkeypatch.setattr(optimization_worker, "load_owned_optimization_context", broken_load)
    monkeypatch.setattr(optimization_worker, "fail_optimization_run", broken_fail)
    with pytest.raises(SQLAlchemyError, match="fresh database failure"):
        await optimization_worker.run_once(
            optimization_worker_factory,
            settings=_worker_settings(),
            lease_owner="worker-a",
            checkpointer=_RecordingSaver(),
            trusted_input_loader=_TrustedLoader(_trusted(chain)),
            max_agent_attempts=1,
        )

    run = await _worker_run(optimization_worker_factory)
    assert (run.status, run.lease_owner, run.error_code) == (
        WorkflowStatus.PROCESSING, "worker-a", None
    )


@pytest.mark.parametrize(
    "error_code",
    [
        "KNOWLEDGE_MODEL_UNAVAILABLE",
        "KNOWLEDGE_DEPENDENCY_TIMEOUT",
        "KNOWLEDGE_DEPENDENCY_ERROR",
        "KNOWLEDGE_ZERO_HIT",
        "KNOWLEDGE_LOW_CONFIDENCE",
    ],
)
async def test_optimization_worker_loader_dependency_failures_defer_without_agent_or_immutable_rows(
    optimization_worker_factory, error_code
) -> None:
    chain = await _worker_chain(optimization_worker_factory)
    requests: list[dict[str, object]] = []
    await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_RecordingSaver(),
        trusted_input_loader=_TrustedLoader(_trusted(chain), error_code),
        transport=_worker_transport(_output(), _passing_compliance(), requests),
        max_agent_attempts=1,
    )

    run = await _worker_run(optimization_worker_factory)
    assert not requests and await _worker_counts(optimization_worker_factory) == (0, 0, 0)
    assert (run.status, run.quality_status, run.error_code) == (
        WorkflowStatus.PENDING_MANUAL, WorkflowQuality.DEGRADED, error_code
    )


async def test_optimization_worker_existing_revision_loader_failure_defers_without_review(
    optimization_worker_factory,
) -> None:
    chain = await _worker_chain(optimization_worker_factory, live=True)
    trusted = _trusted(chain)
    async with optimization_worker_factory() as session:
        revision = await persist_optimization_revision(
            session,
            workflow_run_id="optimization-1",
            lease_owner="worker-a",
            iteration=0,
            trusted=trusted,
            output=_output(),
            canonical_citations=[_citation()],
            calls=_optimization_calls(0),
        )
        assert revision.revision_id is not None
    await _reset_to_accepted(optimization_worker_factory)
    await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_RecordingSaver(),
        trusted_input_loader=_TrustedLoader(trusted, "KNOWLEDGE_DEPENDENCY_ERROR"),
        transport=_worker_transport(_output(), _passing_compliance(), []),
        max_agent_attempts=1,
    )

    run = await _worker_run(optimization_worker_factory)
    assert await _worker_counts(optimization_worker_factory) == (1, 0, 1)
    assert (run.status, run.quality_status, run.error_code) == (
        WorkflowStatus.PENDING_MANUAL,
        WorkflowQuality.DEGRADED,
        "KNOWLEDGE_DEPENDENCY_ERROR",
    )


@pytest.mark.parametrize(
    ("rag_quality", "error_code"),
    [("zero_hit", "KNOWLEDGE_ZERO_HIT"), ("low_confidence", "KNOWLEDGE_LOW_CONFIDENCE")],
)
async def test_optimization_worker_iteration_one_low_quality_rag_skips_agent_and_new_immutable_rows(
    optimization_worker_factory, rag_quality, error_code
) -> None:
    trusted = await _seed_actionable_revision(optimization_worker_factory)
    low_quality = trusted.model_copy(update={"rag_quality": rag_quality})
    requests: list[dict[str, object]] = []
    await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_RecordingSaver(),
        trusted_input_loader=_TrustedLoader(low_quality),
        transport=_worker_transport(_output(), _passing_compliance(), requests),
        max_agent_attempts=1,
    )

    run = await _worker_run(optimization_worker_factory)
    assert not requests and await _worker_counts(optimization_worker_factory) == (1, 1, 2)
    assert (run.status, run.quality_status, run.error_code) == (
        WorkflowStatus.PENDING_MANUAL, WorkflowQuality.DEGRADED, error_code
    )


async def test_optimization_worker_iteration_one_schema_failure_persists_only_safe_calls_with_manual_terminal(
    optimization_worker_factory,
) -> None:
    trusted = await _seed_actionable_revision(optimization_worker_factory)
    requests: list[dict[str, object]] = []
    await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_RecordingSaver(),
        trusted_input_loader=_TrustedLoader(trusted),
        transport=_worker_transport(_output(), _passing_compliance(), requests, schema_invalid=True),
        max_agent_attempts=1,
    )

    run = await _worker_run(optimization_worker_factory)
    assert [request["iteration"] for request in requests] == [1, 1]
    assert await _worker_counts(optimization_worker_factory) == (1, 1, 4)
    assert (run.status, run.quality_status, run.error_code) == (
        WorkflowStatus.PENDING_MANUAL,
        WorkflowQuality.DEGRADED,
        "DEEPSEEK_SCHEMA_INVALID",
    )


async def test_optimization_worker_timeout_before_revision_persists_one_safe_call_only(
    optimization_worker_factory,
) -> None:
    chain = await _worker_chain(optimization_worker_factory)
    requests = 0

    async def timeout(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise httpx.ReadTimeout("timeout", request=request)

    await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_RecordingSaver(),
        trusted_input_loader=_TrustedLoader(_trusted(chain)),
        transport=httpx.MockTransport(timeout),
        max_agent_attempts=1,
    )

    run = await _worker_run(optimization_worker_factory)
    assert requests == 1 and await _worker_counts(optimization_worker_factory) == (0, 0, 1)
    assert (run.status, run.quality_status, run.error_code) == (
        WorkflowStatus.PENDING_MANUAL, WorkflowQuality.DEGRADED, "DEEPSEEK_TIMEOUT"
    )


@pytest.mark.parametrize("change", ["owner", "version"])
async def test_optimization_worker_terminal_guard_discards_safe_calls_when_owner_or_version_changes(
    optimization_worker_factory, change
) -> None:
    chain = await _worker_chain(optimization_worker_factory)
    trusted = _trusted(chain)
    requests = 0

    async def timeout_after_change(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        async with optimization_worker_factory() as replacement_session:
            if change == "owner":
                await replacement_session.execute(
                    update(WorkflowRun)
                    .where(WorkflowRun.id == "optimization-1")
                    .values(lease_owner="worker-b")
                )
            else:
                await replacement_session.execute(
                    update(Product).where(Product.id == "product-1").values(current_version=8)
                )
            await replacement_session.commit()
        raise httpx.ReadTimeout("timeout", request=request)

    await optimization_worker.run_once(
        optimization_worker_factory,
        settings=_worker_settings(),
        lease_owner="worker-a",
        checkpointer=_RecordingSaver(),
        trusted_input_loader=_TrustedLoader(trusted),
        transport=httpx.MockTransport(timeout_after_change),
        max_agent_attempts=1,
    )

    run = await _worker_run(optimization_worker_factory)
    assert requests == 1 and await _worker_counts(optimization_worker_factory) == (0, 0, 0)
    if change == "owner":
        assert (run.status, run.lease_owner, run.error_code) == (
            WorkflowStatus.PROCESSING, "worker-b", None
        )
    else:
        assert (run.status, run.lease_owner, run.error_code) == (
            WorkflowStatus.FAILED, None, "PRODUCT_VERSION_CONFLICT"
        )


async def test_optimization_worker_propagates_cancelled_transport_without_terminal_write(
    optimization_worker_factory,
) -> None:
    chain = await _worker_chain(optimization_worker_factory)

    async def cancelled(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await optimization_worker.run_once(
            optimization_worker_factory,
            settings=_worker_settings(),
            lease_owner="worker-a",
            checkpointer=_RecordingSaver(),
            trusted_input_loader=_TrustedLoader(_trusted(chain)),
            transport=httpx.MockTransport(cancelled),
            max_agent_attempts=1,
        )

    run = await _worker_run(optimization_worker_factory)
    assert await _worker_counts(optimization_worker_factory) == (0, 0, 0)
    assert (run.status, run.lease_owner, run.error_code) == (
        WorkflowStatus.PROCESSING, "worker-a", None
    )


async def test_optimization_worker_forced_passed_review_defer_uses_task_seven_replay_guard(
    optimization_worker_factory,
) -> None:
    chain = await _worker_chain(optimization_worker_factory, live=True)
    trusted = _trusted(chain)
    async with optimization_worker_factory() as session:
        revision = await persist_optimization_revision(
            session,
            workflow_run_id="optimization-1",
            lease_owner="worker-a",
            iteration=0,
            trusted=trusted,
            output=_output(),
            canonical_citations=[_citation()],
            calls=_optimization_calls(0),
        )
        assert revision.revision_id is not None
        semantic = _passing_compliance()
        review = await persist_compliance_review(
            session,
            workflow_run_id="optimization-1",
            lease_owner="worker-a",
            revision_id=revision.revision_id,
            iteration=0,
            deterministic=validate_optimization_output(trusted, _output()),
            semantic=semantic,
            required_changes=[],
            canonical_citations=[_citation()],
            calls=_compliance_calls(0),
        )
        assert review.review_id is not None
        graph = optimization_worker.build_optimization_graph(
            session=session,
            settings=_worker_settings(),
            lease_owner="worker-a",
            checkpointer=_RecordingSaver(),
            trusted_input_loader=_TrustedLoader(trusted),
            max_agent_attempts=1,
        )
        state = await graph.nodes["defer_manual"].node.steps[0].ainvoke(
            {"workflow_run_id": "optimization-1"}
        )

    run = await _worker_run(optimization_worker_factory)
    assert state["error_code"] == "OPTIMIZATION_REPLAY_CONFLICT"
    assert (run.status, run.error_code) == (
        WorkflowStatus.FAILED, "OPTIMIZATION_REPLAY_CONFLICT"
    )


async def _seed_current_citation(session: AsyncSession) -> CanonicalRuleCitation:
    document = KnowledgeDocument(
        id="document-1", name="通用规则", category="通用规则", enabled=True,
        created_by="user-1", idempotency_key="optimization-citation",
    )
    session.add(document)
    await session.flush()
    version = KnowledgeDocumentVersion(
        id="version-1", document_id=document.id, version_number=1, sha256="a" * 64,
        original_filename="rules.md", mime_type="text/markdown", storage_path="d:/test/rules.md",
        status=KnowledgeVersionStatus.ACTIVE,
    )
    session.add(version)
    await session.flush()
    session.add(KnowledgeChunk(
        id="chunk-1", version_id=version.id, chunk_index=0, chunk_hash="b" * 64,
        canonical_text="商品描述应当真实准确。", chunk_metadata={}, token_count=1,
    ))
    document.current_version_id = version.id
    await session.commit()
    return _citation()


async def _seed_cross_owned_citation(session: AsyncSession) -> CanonicalRuleCitation:
    owner = KnowledgeDocument(
        id="document-version-owner", name="版本所有者", category="通用规则", enabled=True,
        created_by="user-1", idempotency_key="optimization-cross-owner",
    )
    session.add(owner)
    await session.flush()
    version = KnowledgeDocumentVersion(
        id="version-1", document_id=owner.id, version_number=1, sha256="c" * 64,
        original_filename="rules.md", mime_type="text/markdown", storage_path="d:/test/rules.md",
        status=KnowledgeVersionStatus.ACTIVE,
    )
    session.add(version)
    await session.flush()
    session.add(KnowledgeChunk(
        id="chunk-1", version_id=version.id, chunk_index=0, chunk_hash="d" * 64,
        canonical_text="商品描述应当真实准确。", chunk_metadata={}, token_count=1,
    ))
    session.add(KnowledgeDocument(
        id="document-1", name="通用规则", category="通用规则", enabled=True,
        created_by="user-1", idempotency_key="optimization-cross-current",
        current_version_id=version.id,
    ))
    await session.commit()
    return _citation()


async def test_context_reload_rejects_saved_citation_that_is_no_longer_current(
    session,
) -> None:
    chain = await _chain(session, live=True)
    citation = _citation()
    revision = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=_trusted(chain),
        output=_output(),
        canonical_citations=[citation],
        calls=_optimization_calls(0),
    )
    assert revision.revision_id is not None
    await session.execute(
        update(KnowledgeDocument).where(KnowledgeDocument.id == citation.document_id).values(enabled=False)
    )
    await session.commit()

    loaded = await load_owned_optimization_context(
        session, workflow_run_id="optimization-1", lease_owner="worker-a"
    )

    assert (loaded.disposition, loaded.error_code) == ("failed", "OPTIMIZATION_FACT_ERROR")
    run = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    assert run is not None and (run.status, run.error_code) == (
        WorkflowStatus.FAILED, "OPTIMIZATION_FACT_ERROR"
    )


async def test_revision_persistence_rejects_cross_document_current_version_pointer(session) -> None:
    chain = await _chain(session, live=True, seed_citation=False)
    citation = await _seed_cross_owned_citation(session)

    result = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=_trusted(chain),
        output=_output(),
        canonical_citations=[citation],
        calls=_optimization_calls(0),
    )

    assert (result.disposition, result.error_code) == ("failed", "OPTIMIZATION_FACT_ERROR")
    assert not list(await session.scalars(select(ProposalRevision)))
    assert not list(await session.scalars(select(AgentCall)))


async def test_revision_persistence_rechecks_current_citation_facts_before_audit_or_insert(session) -> None:
    chain = await _chain(session, live=True)
    citation = _citation()
    await session.execute(
        update(KnowledgeDocument).where(KnowledgeDocument.id == citation.document_id).values(enabled=False)
    )
    await session.commit()

    result = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=_trusted(chain),
        output=_output(),
        canonical_citations=[citation],
        calls=_optimization_calls(0),
    )

    assert (result.disposition, result.error_code) == ("failed", "OPTIMIZATION_FACT_ERROR")
    assert not list(await session.scalars(select(ProposalRevision)))
    assert not list(await session.scalars(select(AgentCall)))


@pytest.mark.parametrize(
    "mutation",
    [
        "current_version", "inactive_version", "outside_category", "document_name",
        "version_number", "canonical_text", "orphan_chunk",
    ],
)
async def test_revision_persistence_rechecks_each_current_citation_database_fact(
    session, mutation: str
) -> None:
    chain = await _chain(session, live=True)
    citation = _citation()
    if mutation == "current_version":
        session.add(KnowledgeDocumentVersion(
            id="version-2", document_id=citation.document_id, version_number=2, sha256="e" * 64,
            original_filename="rules.md", mime_type="text/markdown", storage_path="d:/test/rules-v2.md",
            status=KnowledgeVersionStatus.ACTIVE,
        ))
        await session.flush()
        await session.execute(
            update(KnowledgeDocument)
            .where(KnowledgeDocument.id == citation.document_id)
            .values(current_version_id="version-2")
        )
    elif mutation == "inactive_version":
        await session.execute(
            update(KnowledgeDocumentVersion)
            .where(KnowledgeDocumentVersion.id == citation.version_id)
            .values(status=KnowledgeVersionStatus.DISABLED)
        )
    elif mutation == "outside_category":
        await session.execute(
            update(KnowledgeDocument)
            .where(KnowledgeDocument.id == citation.document_id)
            .values(category="服饰")
        )
    elif mutation == "document_name":
        await session.execute(
            update(KnowledgeDocument)
            .where(KnowledgeDocument.id == citation.document_id)
            .values(name="已变更规则")
        )
    elif mutation == "version_number":
        await session.execute(
            update(KnowledgeDocumentVersion)
            .where(KnowledgeDocumentVersion.id == citation.version_id)
            .values(version_number=2)
        )
    elif mutation == "canonical_text":
        await session.execute(
            update(KnowledgeChunk)
            .where(KnowledgeChunk.id == citation.chunk_id)
            .values(canonical_text="已变更规则文本。")
        )
    else:
        await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.id == citation.chunk_id))
    await session.commit()

    result = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=_trusted(chain),
        output=_output(),
        canonical_citations=[citation],
        calls=_optimization_calls(0),
    )

    assert (result.disposition, result.error_code) == ("failed", "OPTIMIZATION_FACT_ERROR")
    assert not list(await session.scalars(select(ProposalRevision)))
    assert not list(await session.scalars(select(AgentCall)))


async def test_invalid_citation_with_a_replaced_owner_writes_nothing(session) -> None:
    chain = await _chain(session, live=True)
    citation = _citation()
    await session.execute(
        update(KnowledgeDocument)
        .where(KnowledgeDocument.id == citation.document_id)
        .values(enabled=False)
    )
    await session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == "optimization-1")
        .values(lease_owner="worker-b")
    )
    await session.commit()

    result = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=_trusted(chain),
        output=_output(),
        canonical_citations=[citation],
        calls=_optimization_calls(0),
    )

    assert result.disposition == "lease_lost"
    assert not list(await session.scalars(select(ProposalRevision)))
    assert not list(await session.scalars(select(AgentCall)))
    run = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    assert run is not None and (run.status, run.lease_owner, run.error_code) == (
        WorkflowStatus.PROCESSING, "worker-b", None
    )


@pytest.mark.parametrize("failure_review", [False, True])
async def test_review_persistence_rechecks_current_citation_facts_before_review_or_audit(
    session, failure_review
) -> None:
    chain = await _chain(session, live=True)
    citation = _citation()
    trusted = _trusted(chain)
    revision = await persist_optimization_revision(
        session,
        workflow_run_id="optimization-1",
        lease_owner="worker-a",
        iteration=0,
        trusted=trusted,
        output=_output(),
        canonical_citations=[citation],
        calls=_optimization_calls(0),
    )
    assert revision.revision_id is not None
    await session.execute(
        update(KnowledgeDocument).where(KnowledgeDocument.id == citation.document_id).values(enabled=False)
    )
    await session.commit()
    deterministic = validate_optimization_output(trusted, _output())
    if failure_review:
        result = await persist_compliance_failure(
            session,
            workflow_run_id="optimization-1",
            lease_owner="worker-a",
            revision_id=revision.revision_id,
            iteration=0,
            deterministic=deterministic,
            error_code="DEEPSEEK_TIMEOUT",
            required_changes=[],
            canonical_citations=[citation],
            calls=_compliance_calls(0),
        )
    else:
        result = await persist_compliance_review(
            session,
            workflow_run_id="optimization-1",
            lease_owner="worker-a",
            revision_id=revision.revision_id,
            iteration=0,
            deterministic=deterministic,
            semantic=_passing_compliance(),
            required_changes=[],
            canonical_citations=[citation],
            calls=_compliance_calls(0),
        )

    assert (result.disposition, result.error_code) == ("failed", "OPTIMIZATION_FACT_ERROR")
    assert not list(await session.scalars(select(ComplianceReview)))
    calls = list(await session.scalars(select(AgentCall)))
    assert [(call.node_name, call.iteration) for call in calls] == [
        ("call_product_optimization_agent", 0)
    ]
