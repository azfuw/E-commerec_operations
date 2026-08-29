from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from backend.common import (
    AgentCallType,
    ComplianceRiskLevel,
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


async def _chain(session, *, live: bool = False) -> dict[str, object]:
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
    assert await session.scalar(select(ProposalRevision).where(ProposalRevision.id == revision_zero.revision_id))


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
