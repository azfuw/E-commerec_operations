import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError

import backend.manual_review_runs as manual
from backend.audit_events import add_audit_event
from backend.common import (
    AuditEventType,
    AuditOutcome,
    ComplianceRiskLevel,
    KnowledgeVersionStatus,
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
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
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
from backend.schemas import (
    CanonicalRuleCitation,
    DescriptionSection,
    EvidenceRef,
    OptimizationChange,
    OptimizationProposalOutput,
    OutputCitation,
    PriceSuggestion,
    ProductMetrics,
    SkuSuggestion,
    TrustedOptimizationInput,
)


def _citation() -> CanonicalRuleCitation:
    return CanonicalRuleCitation(
        document_id="document-1",
        version_id="version-1",
        chunk_id="chunk-1",
        document_name="通用规则",
        version_number=1,
        category="通用规则",
        canonical_text="商品文案应有依据。",
        active=True,
        applicable=True,
    )


def _second_citation() -> CanonicalRuleCitation:
    return CanonicalRuleCitation(
        document_id="document-2",
        version_id="version-2",
        chunk_id="chunk-2",
        document_name="家居规则",
        version_number=1,
        category="家居",
        canonical_text="商品标题不得夸大。",
        active=True,
        applicable=True,
    )


def _proposal_output() -> OptimizationProposalOutput:
    fact_title = EvidenceRef(kind="fact", value="product.title")
    fact_description = EvidenceRef(kind="fact", value="product.description")
    citation = EvidenceRef(kind="citation", value="chunk-1")
    section = DescriptionSection(
        heading="商品详情",
        body="适合日常使用",
        evidence=[fact_description],
    )
    return OptimizationProposalOutput(
        title="人工修订标题",
        selling_points=["棉质家居设计"],
        description=[section],
        keywords=["家居"],
        attribute_completions=[],
        changes=[
            OptimizationChange(
                field="title",
                current_value="原商品标题",
                suggested_value="人工修订标题",
                reason="优化标题表达",
                evidence=[fact_title],
            ),
            OptimizationChange(
                field="description",
                current_value="原始详情",
                suggested_value=[section],
                reason="优化详情表达",
                evidence=[fact_description],
            ),
        ],
        citations=[OutputCitation(chunk_id="chunk-1")],
        price_suggestions=[
            PriceSuggestion(
                target_sku_id="sku-1",
                current_price=Decimal("100.00"),
                suggested_price=Decimal("90.00"),
                reason="价格建议",
                evidence=[citation],
            )
        ],
        sku_suggestions=[
            SkuSuggestion(
                target_sku_id="sku-1",
                current_code="SKU-RED",
                current_spec={"颜色": "红"},
                suggested_code="SKU-RED-NEW",
                suggested_spec={"颜色": "红色"},
                reason="SKU 建议",
                evidence=[citation],
            )
        ],
    )


def _metrics() -> ProductMetrics:
    return ProductMetrics.from_totals(
        impressions=100,
        clicks=10,
        orders=1,
        units=1,
        revenue=Decimal("100.00"),
        refunds=0,
    ).model_copy(update={"product_id": "product-1", "product_code": "HOME-001"})


def _trusted_seed() -> TrustedOptimizationInput:
    return TrustedOptimizationInput(
        store_id="store-1",
        product_id="product-1",
        base_product_version=7,
        title="原商品标题",
        category="家居",
        brand="好物品牌",
        selling_points=["棉质家居设计"],
        description="原始详情",
        search_keywords=["家居"],
        attributes={"材质": "棉"},
        skus=[
            {
                "id": "sku-1",
                "code": "SKU-RED",
                "spec": {"颜色": "红"},
                "price": "100.00",
                "stock": 10,
            }
        ],
        candidate_metrics=_metrics(),
        candidate_evidence=["orders=1"],
        rag_quality="normal",
        canonical_rule_citations=[_citation()],
    )


def _trusted_hash() -> str:
    encoded = json.dumps(
        _trusted_seed().model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _seed_manual_chain(
    session,
    *,
    status: WorkflowStatus = WorkflowStatus.PROCESSING,
    attempt_count: int = 1,
    lease_owner: str | None = "worker-a",
    lease_expires_at: datetime | None = None,
    with_review: bool = False,
) -> dict[str, object]:
    if status is WorkflowStatus.PROCESSING and lease_expires_at is None:
        lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    if status is not WorkflowStatus.PROCESSING:
        lease_owner = None
        lease_expires_at = None
    user = User(
        id="operator-1",
        username="operator",
        password_hash="hash",
        role=UserRole.OPERATOR,
    )
    store = Store(id="store-1", name="目标店铺", code="target")
    other_store = Store(id="store-2", name="其他店铺", code="other")
    session.add_all([user, store, other_store])
    await session.flush()
    session.add(UserStoreScope(user_id=user.id, store_id=store.id))
    product = Product(
        id="product-1",
        store_id=store.id,
        code="HOME-001",
        title="原商品标题",
        category="家居",
        brand="好物品牌",
        selling_points=["棉质家居设计"],
        description="原始详情",
        search_keywords=["家居"],
        attributes={"材质": "棉"},
        current_version=7,
    )
    other_product = Product(
        id="product-2",
        store_id=other_store.id,
        code="OTHER-001",
        title="其他商品",
        category="家居",
    )
    session.add_all([product, other_product])
    await session.flush()
    sku = ProductSku(
        id="sku-1",
        product_id=product.id,
        code="SKU-RED",
        spec={"颜色": "红"},
        price=Decimal("100.00"),
        current_stock=10,
    )
    session.add(sku)
    await session.flush()
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
        status=WorkflowStatus.PENDING_MANUAL,
        quality_status=WorkflowQuality.NORMAL,
        current_step="manual_review_pending",
        input={
            "proposal_id": "proposal-1",
            "source_analysis_run_id": "analysis-1",
            "analysis_candidate_id": "candidate-1",
            "product_id": product.id,
            "store_id": store.id,
        },
    )
    manual_workflow = WorkflowRun(
        id="manual-workflow-1",
        workflow_type=WorkflowType.MANUAL_REVIEW,
        store_id=store.id,
        created_by=user.id,
        status=status,
        quality_status=WorkflowQuality.NORMAL,
        attempt_count=attempt_count,
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
        current_step="claimed" if status is WorkflowStatus.PROCESSING else None,
        input={
            "manual_review_run_id": "manual-run-1",
            "proposal_id": "proposal-1",
            "proposal_revision_id": "revision-2",
            "parent_revision_id": "revision-1",
            "product_id": product.id,
            "store_id": store.id,
        },
    )
    session.add_all([analysis, optimization, manual_workflow])
    await session.flush()
    candidate = AnalysisCandidate(
        id="candidate-1",
        workflow_run_id=analysis.id,
        product_id=product.id,
        rank=1,
        product_code=product.code,
        anomaly_types=["low_conversion"],
        metrics=_metrics().model_dump(mode="json"),
        business_impact=Decimal("100.00"),
        evidence=["orders=1"],
        impact_explanation="影响说明",
        reason="原因",
        recommended_action="建议",
        confidence=Decimal("0.8000"),
    )
    session.add(candidate)
    await session.flush()
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
    session.add(proposal)
    await session.flush()
    canonical = _citation().model_dump(mode="json")
    parent = ProposalRevision(
        id="revision-1",
        proposal_id=proposal.id,
        iteration=0,
        revision_number=1,
        origin=ProposalRevisionOrigin.AGENT,
        created_by=user.id,
        parent_revision_id=None,
        base_product_version=7,
        trusted_fact_hash="b" * 64,
        proposal_output=_proposal_output().model_dump(mode="json"),
        citations=[canonical],
    )
    manual_revision = ProposalRevision(
        id="revision-2",
        proposal_id=proposal.id,
        iteration=None,
        revision_number=2,
        origin=ProposalRevisionOrigin.MANUAL,
        created_by=user.id,
        parent_revision_id=parent.id,
        base_product_version=7,
        trusted_fact_hash=_trusted_hash(),
        proposal_output=_proposal_output().model_dump(mode="json"),
        citations=[canonical],
    )
    session.add_all([parent, manual_revision])
    await session.flush()
    parent_review = ComplianceReview(
        id="review-1",
        proposal_id=proposal.id,
        proposal_revision_id=parent.id,
        iteration=0,
        deterministic_checks={"passed": True, "violations": []},
        semantic_review={"passed": True, "violations": []},
        passed=True,
        risk_level=ComplianceRiskLevel.LOW,
        required_changes=[],
        citations=[canonical],
        quality_status=WorkflowQuality.NORMAL,
    )
    session.add(parent_review)
    await session.flush()
    manual_run = ManualReviewRun(
        id="manual-run-1",
        workflow_run_id=manual_workflow.id,
        proposal_id=proposal.id,
        proposal_revision_id=manual_revision.id,
        submitted_by=user.id,
        idempotency_key_hash="d" * 64,
        request_hash="e" * 64,
    )
    session.add(manual_run)
    await session.flush()
    proposal.current_revision_id = manual_revision.id
    proposal.active_manual_review_run_id = manual_run.id
    if with_review:
        session.add(
            ComplianceReview(
                id="review-2",
                proposal_id=proposal.id,
                proposal_revision_id=manual_revision.id,
                iteration=None,
                deterministic_checks={"passed": True, "violations": []},
                semantic_review={"passed": True, "violations": []},
                passed=True,
                risk_level=ComplianceRiskLevel.LOW,
                required_changes=[],
                citations=[canonical],
                quality_status=WorkflowQuality.NORMAL,
            )
        )
    document = KnowledgeDocument(
        id="document-1",
        name="通用规则",
        category="通用规则",
        created_by=user.id,
    )
    session.add(document)
    await session.flush()
    version = KnowledgeDocumentVersion(
        id="version-1",
        document_id=document.id,
        version_number=1,
        sha256="f" * 64,
        original_filename="rule.txt",
        mime_type="text/plain",
        storage_path="test/rule.txt",
        status=KnowledgeVersionStatus.ACTIVE,
    )
    session.add(version)
    await session.flush()
    chunk = KnowledgeChunk(
        id="chunk-1",
        version_id=version.id,
        chunk_index=0,
        chunk_hash="1" * 64,
        canonical_text="商品文案应有依据。",
        chunk_metadata={},
        token_count=8,
    )
    session.add(chunk)
    await session.flush()
    document.current_version_id = version.id
    add_audit_event(
        session,
        event_type=AuditEventType.MANUAL_REVISION_CREATED,
        outcome=AuditOutcome.SUCCESS,
        actor_id=user.id,
        actor_role=user.role,
        store_id=store.id,
        proposal_id=proposal.id,
        proposal_revision_id=manual_revision.id,
        workflow_run_id=manual_workflow.id,
        details={
            "from_status": WorkflowStatus.DRAFT_READY.value,
            "to_status": WorkflowStatus.PENDING_MANUAL.value,
            "revision_number": 2,
            "origin": ProposalRevisionOrigin.MANUAL.value,
            "workflow_type": WorkflowType.MANUAL_REVIEW.value,
            "quality_status": WorkflowQuality.NORMAL.value,
            "current_step": "manual_review_pending",
            "changed_fields": ["title", "description"],
        },
    )
    await session.commit()
    return {
        "user": user,
        "store": store,
        "product": product,
        "sku": sku,
        "analysis": analysis,
        "optimization": optimization,
        "manual_workflow": manual_workflow,
        "proposal": proposal,
        "revision": manual_revision,
        "manual_run": manual_run,
        "document": document,
    }


async def _add_type_controls(
    session, *, status: WorkflowStatus, attempt_count: int, expired: bool
) -> tuple[str, str]:
    expiry = datetime.now(UTC) - timedelta(seconds=1) if expired else None
    owner = "old-owner" if status is WorkflowStatus.PROCESSING else None
    analysis = WorkflowRun(
        id="analysis-control",
        workflow_type=WorkflowType.ANALYSIS,
        store_id="store-1",
        created_by="operator-1",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2),
        status=status,
        quality_status=WorkflowQuality.NORMAL,
        attempt_count=attempt_count,
        lease_owner=owner,
        lease_expires_at=expiry,
    )
    optimization = WorkflowRun(
        id="optimization-control",
        workflow_type=WorkflowType.OPTIMIZATION,
        store_id="store-1",
        created_by="operator-1",
        status=status,
        quality_status=WorkflowQuality.NORMAL,
        attempt_count=attempt_count,
        lease_owner=owner,
        lease_expires_at=expiry,
    )
    session.add_all([analysis, optimization])
    await session.commit()
    return analysis.id, optimization.id


@pytest.mark.parametrize(
    ("status", "attempt_count", "expired", "expected_attempt"),
    [
        (WorkflowStatus.ACCEPTED, 0, False, 1),
        (WorkflowStatus.PROCESSING, 2, True, 3),
    ],
)
async def test_manual_claim_is_type_isolated_server_timed_and_audited(
    session,
    status: WorkflowStatus,
    attempt_count: int,
    expired: bool,
    expected_attempt: int,
) -> None:
    await _seed_manual_chain(
        session,
        status=status,
        attempt_count=attempt_count,
        lease_owner="old-owner" if expired else None,
        lease_expires_at=(datetime.now(UTC) - timedelta(seconds=1)) if expired else None,
    )
    control_ids = await _add_type_controls(
        session, status=status, attempt_count=attempt_count, expired=expired
    )

    claim = await manual.claim_next_manual_review_run(
        session, lease_owner="worker-new", lease_seconds=60
    )

    assert claim is not None
    assert (
        claim.workflow_run_id,
        claim.manual_review_run_id,
        claim.lease_owner,
        claim.attempt_count,
    ) == ("manual-workflow-1", "manual-run-1", "worker-new", expected_attempt)
    claimed = await session.get(WorkflowRun, "manual-workflow-1", populate_existing=True)
    assert claimed is not None
    assert (
        claimed.status,
        claimed.attempt_count,
        claimed.lease_owner,
        claimed.current_step,
        claimed.error_code,
    ) == (
        WorkflowStatus.PROCESSING,
        expected_attempt,
        "worker-new",
        "claimed",
        None,
    )
    assert claimed.lease_expires_at is not None
    for control_id in control_ids:
        control = await session.get(WorkflowRun, control_id, populate_existing=True)
        assert control is not None
        assert (control.status, control.attempt_count, control.lease_owner) == (
            status,
            attempt_count,
            "old-owner" if expired else None,
        )
    audit = await session.scalar(
        select(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.MANUAL_REVIEW_CLAIMED
        )
    )
    assert audit is not None
    assert (
        audit.outcome,
        audit.workflow_run_id,
        audit.proposal_id,
        audit.proposal_revision_id,
        audit.error_code,
    ) == (
        AuditOutcome.SUCCESS,
        "manual-workflow-1",
        "proposal-1",
        "revision-2",
        None,
    )
    assert audit.details == {
        "from_status": status.value,
        "to_status": WorkflowStatus.PROCESSING.value,
        "workflow_type": WorkflowType.MANUAL_REVIEW.value,
        "quality_status": WorkflowQuality.NORMAL.value,
        "current_step": "claimed",
    }


async def test_expired_attempt_three_fails_both_runs_and_clears_active_pointer(session) -> None:
    await _seed_manual_chain(
        session,
        attempt_count=3,
        lease_owner="old-owner",
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    control_ids = await _add_type_controls(
        session,
        status=WorkflowStatus.PROCESSING,
        attempt_count=3,
        expired=True,
    )

    assert await manual.claim_next_manual_review_run(
        session, lease_owner="worker-new", lease_seconds=60
    ) is None

    manual_run = await session.get(WorkflowRun, "manual-workflow-1", populate_existing=True)
    original = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    proposal = await session.get(ProductProposal, "proposal-1", populate_existing=True)
    assert manual_run is not None and original is not None and proposal is not None
    assert (
        manual_run.status,
        manual_run.quality_status,
        manual_run.current_step,
        manual_run.error_code,
        manual_run.lease_owner,
        manual_run.lease_expires_at,
    ) == (
        WorkflowStatus.FAILED,
        WorkflowQuality.DEGRADED,
        "failed",
        "LEASE_ATTEMPTS_EXHAUSTED",
        None,
        None,
    )
    assert (
        original.status,
        original.quality_status,
        original.current_step,
        original.error_code,
    ) == (
        WorkflowStatus.FAILED,
        WorkflowQuality.DEGRADED,
        "manual_review_failed",
        "LEASE_ATTEMPTS_EXHAUSTED",
    )
    assert proposal.active_manual_review_run_id is None
    for control_id in control_ids:
        control = await session.get(WorkflowRun, control_id, populate_existing=True)
        assert control is not None
        assert (control.status, control.attempt_count, control.lease_owner) == (
            WorkflowStatus.PROCESSING,
            3,
            "old-owner",
        )
    audits = list(
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.MANUAL_REVIEW_FAILED
            )
        )
    )
    assert len(audits) == 1
    assert audits[0].error_code == "LEASE_ATTEMPTS_EXHAUSTED"
    assert audits[0].details == {
        "from_status": WorkflowStatus.PROCESSING.value,
        "to_status": WorkflowStatus.FAILED.value,
        "workflow_type": WorkflowType.MANUAL_REVIEW.value,
        "quality_status": WorkflowQuality.DEGRADED.value,
        "current_step": "failed",
    }


@pytest.mark.parametrize("failure_boundary", ["audit", "commit"])
async def test_exhaustion_failure_rolls_back_both_runs_pointer_and_audit(
    session, monkeypatch, failure_boundary: str
) -> None:
    await _seed_manual_chain(
        session,
        attempt_count=3,
        lease_owner="old-owner",
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    if failure_boundary == "audit":
        def fail_audit(*args, **kwargs):
            raise SQLAlchemyError("simulated audit failure")

        monkeypatch.setattr(manual, "add_audit_event", fail_audit)
    else:
        async def fail_commit() -> None:
            raise SQLAlchemyError("simulated commit failure")

        monkeypatch.setattr(session, "commit", fail_commit)

    with pytest.raises(SQLAlchemyError, match=f"simulated {failure_boundary} failure"):
        await manual.claim_next_manual_review_run(
            session, lease_owner="worker-new", lease_seconds=60
        )

    monkeypatch.undo()
    manual_workflow = await session.get(
        WorkflowRun, "manual-workflow-1", populate_existing=True
    )
    original = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    proposal = await session.get(ProductProposal, "proposal-1", populate_existing=True)
    assert manual_workflow is not None and original is not None and proposal is not None
    assert (
        manual_workflow.status,
        manual_workflow.quality_status,
        manual_workflow.lease_owner,
        manual_workflow.error_code,
    ) == (
        WorkflowStatus.PROCESSING,
        WorkflowQuality.NORMAL,
        "old-owner",
        None,
    )
    assert (
        original.status,
        original.quality_status,
        original.current_step,
        original.error_code,
    ) == (
        WorkflowStatus.PENDING_MANUAL,
        WorkflowQuality.NORMAL,
        "manual_review_pending",
        None,
    )
    assert proposal.active_manual_review_run_id == "manual-run-1"
    assert int(
        await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.MANUAL_REVIEW_FAILED)
        )
        or 0
    ) == 0


@pytest.mark.parametrize("with_review", [False, True])
async def test_owned_context_is_fresh_typed_and_trusted_loader_compatible(
    session, with_review: bool
) -> None:
    await _seed_manual_chain(session, with_review=with_review)

    result = await manual.load_owned_manual_review_context(
        session,
        workflow_run_id="manual-workflow-1",
        lease_owner="worker-a",
    )

    assert (result.disposition, result.error_code) == ("ready", None)
    context = result.context
    assert context is not None
    assert (
        context.workflow_run_id,
        context.manual_review_run_id,
        context.proposal_id,
        context.proposal_revision_id,
        context.revision_number,
        context.store_id,
        context.product_id,
        context.submitted_by,
        context.base_product_version,
        context.current_review_id,
    ) == (
        "manual-workflow-1",
        "manual-run-1",
        "proposal-1",
        "revision-2",
        2,
        "store-1",
        "product-1",
        "operator-1",
        7,
        "review-2" if with_review else None,
    )
    assert context.proposal_output == _proposal_output()
    assert context.canonical_citations == (_citation(),)
    trusted = TrustedOptimizationInput(
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
        rag_quality="normal",
        canonical_rule_citations=list(context.canonical_citations),
    )
    assert trusted.product_id == "product-1"
    assert trusted.skus[0].price == Decimal("100.00")


async def test_owned_context_accepts_output_citation_subset_of_current_canonical_facts(
    session,
) -> None:
    await _seed_manual_chain(session)
    second = _second_citation()
    document = KnowledgeDocument(
        id=second.document_id,
        name=second.document_name,
        category=second.category,
        created_by="operator-1",
    )
    session.add(document)
    await session.flush()
    version = KnowledgeDocumentVersion(
        id=second.version_id,
        document_id=document.id,
        version_number=second.version_number,
        sha256="2" * 64,
        original_filename="home-rule.txt",
        mime_type="text/plain",
        storage_path="test/home-rule.txt",
        status=KnowledgeVersionStatus.ACTIVE,
    )
    session.add(version)
    await session.flush()
    session.add(
        KnowledgeChunk(
            id=second.chunk_id,
            version_id=version.id,
            chunk_index=0,
            chunk_hash="3" * 64,
            canonical_text=second.canonical_text,
            chunk_metadata={},
            token_count=8,
        )
    )
    await session.flush()
    document.current_version_id = version.id
    citations = [_citation(), second]
    trusted = _trusted_seed().model_copy(
        update={"canonical_rule_citations": citations}
    )
    trusted_hash = hashlib.sha256(
        json.dumps(
            trusted.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    canonical = [citation.model_dump(mode="json") for citation in citations]
    parent = await session.get(ProposalRevision, "revision-1")
    revision = await session.get(ProposalRevision, "revision-2")
    assert parent is not None and revision is not None
    parent.citations = canonical
    revision.citations = canonical
    revision.trusted_fact_hash = trusted_hash
    await session.commit()

    result = await manual.load_owned_manual_review_context(
        session,
        workflow_run_id="manual-workflow-1",
        lease_owner="worker-a",
    )

    assert (result.disposition, result.error_code) == ("ready", None)
    assert result.context is not None
    assert result.context.canonical_citations == tuple(citations)
    assert [item.chunk_id for item in result.context.proposal_output.citations] == [
        "chunk-1"
    ]


async def test_malformed_product_json_fails_both_runs_and_clears_active_pointer(
    session,
) -> None:
    await _seed_manual_chain(session)
    await session.execute(
        update(Product)
        .where(Product.id == "product-1")
        .values(selling_points=None)
        .execution_options(synchronize_session=False)
    )
    await session.commit()

    result = await manual.load_owned_manual_review_context(
        session,
        workflow_run_id="manual-workflow-1",
        lease_owner="worker-a",
    )

    assert (result.disposition, result.context, result.error_code) == (
        "failed",
        None,
        "MANUAL_REVIEW_FACT_ERROR",
    )
    manual_workflow = await session.get(
        WorkflowRun, "manual-workflow-1", populate_existing=True
    )
    original = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    proposal = await session.get(ProductProposal, "proposal-1", populate_existing=True)
    assert manual_workflow is not None and original is not None and proposal is not None
    assert (manual_workflow.status, manual_workflow.error_code) == (
        WorkflowStatus.FAILED,
        "MANUAL_REVIEW_FACT_ERROR",
    )
    assert (original.status, original.current_step, original.error_code) == (
        WorkflowStatus.FAILED,
        "manual_review_failed",
        "MANUAL_REVIEW_FACT_ERROR",
    )
    assert proposal.active_manual_review_run_id is None
    audits = list(
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.MANUAL_REVIEW_FAILED
            )
        )
    )
    assert len(audits) == 1
    assert audits[0].error_code == "MANUAL_REVIEW_FACT_ERROR"


async def test_owned_context_locks_authorization_before_proposal_chain(
    session, monkeypatch
) -> None:
    await _seed_manual_chain(session)
    observed: list[tuple[str, bool]] = []
    real_scalar = session.scalar

    async def trace_scalar(statement, *args, **kwargs):
        entity = statement.column_descriptions[0].get("entity")
        if entity is not None:
            observed.append(
                (entity.__name__, getattr(statement, "_for_update_arg", None) is not None)
            )
        return await real_scalar(statement, *args, **kwargs)

    monkeypatch.setattr(session, "scalar", trace_scalar)

    result = await manual.load_owned_manual_review_context(
        session,
        workflow_run_id="manual-workflow-1",
        lease_owner="worker-a",
    )

    assert result.disposition == "ready"
    assert observed[:7] == [
        ("WorkflowRun", True),
        ("ManualReviewRun", False),
        ("ProductProposal", False),
        ("User", True),
        ("Store", True),
        ("UserStoreScope", True),
        ("ProductProposal", True),
    ]


@pytest.mark.parametrize("broken_original", ["store", "type", "input"])
async def test_bad_original_ownership_clears_pointer_without_mutating_original(
    session, broken_original: str
) -> None:
    await _seed_manual_chain(session)
    values: dict[str, object]
    if broken_original == "store":
        values = {"store_id": "store-2"}
    elif broken_original == "type":
        values = {
            "workflow_type": WorkflowType.ANALYSIS,
            "status": WorkflowStatus.COMPLETED,
            "start_date": date(2026, 8, 1),
            "end_date": date(2026, 8, 2),
        }
    else:
        values = {"input": {"proposal_id": "other-proposal"}}
    await session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == "optimization-1")
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    original = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    assert original is not None
    original_before = (
        original.workflow_type,
        original.store_id,
        original.status,
        original.quality_status,
        original.current_step,
        original.error_code,
        original.input,
    )

    result = await manual.load_owned_manual_review_context(
        session,
        workflow_run_id="manual-workflow-1",
        lease_owner="worker-a",
    )

    assert (result.disposition, result.error_code) == (
        "failed",
        "MANUAL_REVIEW_CONTEXT_INCONSISTENT",
    )
    manual_workflow = await session.get(
        WorkflowRun, "manual-workflow-1", populate_existing=True
    )
    original = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    proposal = await session.get(ProductProposal, "proposal-1", populate_existing=True)
    assert manual_workflow is not None and original is not None and proposal is not None
    assert (manual_workflow.status, manual_workflow.error_code) == (
        WorkflowStatus.FAILED,
        "MANUAL_REVIEW_CONTEXT_INCONSISTENT",
    )
    assert proposal.active_manual_review_run_id is None
    assert (
        original.workflow_type,
        original.store_id,
        original.status,
        original.quality_status,
        original.current_step,
        original.error_code,
        original.input,
    ) == original_before
    audits = list(
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.MANUAL_REVIEW_FAILED
            )
        )
    )
    assert len(audits) == 1
    assert audits[0].error_code == "MANUAL_REVIEW_CONTEXT_INCONSISTENT"


@pytest.mark.parametrize(
    ("broken_fact", "expected_code"),
    [
        ("actor-disabled", "MANUAL_REVIEW_AUTHORIZATION_CHANGED"),
        ("scope-missing", "MANUAL_REVIEW_AUTHORIZATION_CHANGED"),
        ("store-disabled", "MANUAL_REVIEW_AUTHORIZATION_CHANGED"),
        ("product-disabled", "MANUAL_REVIEW_FACT_ERROR"),
        ("product-brand", "MANUAL_REVIEW_FACT_ERROR"),
        ("sku-price", "MANUAL_REVIEW_FACT_ERROR"),
        ("citation-disabled", "MANUAL_REVIEW_FACT_ERROR"),
        ("active-pointer", "MANUAL_REVIEW_CONTEXT_INCONSISTENT"),
        ("run-input", "MANUAL_REVIEW_CONTEXT_INCONSISTENT"),
        ("revision-number", "MANUAL_REVIEW_CONTEXT_INCONSISTENT"),
        ("manual-run-missing", "MANUAL_REVIEW_CONTEXT_NOT_FOUND"),
    ],
)
async def test_owned_context_reloads_stale_rows_and_returns_closed_fact_codes(
    session, broken_fact: str, expected_code: str
) -> None:
    await _seed_manual_chain(session)
    if broken_fact == "actor-disabled":
        statement = update(User).where(User.id == "operator-1").values(
            status=UserStatus.DISABLED
        )
    elif broken_fact == "scope-missing":
        statement = delete(UserStoreScope).where(
            UserStoreScope.user_id == "operator-1",
            UserStoreScope.store_id == "store-1",
        )
    elif broken_fact == "store-disabled":
        statement = update(Store).where(Store.id == "store-1").values(enabled=False)
    elif broken_fact == "product-disabled":
        statement = update(Product).where(Product.id == "product-1").values(enabled=False)
    elif broken_fact == "product-brand":
        statement = update(Product).where(Product.id == "product-1").values(brand="其他品牌")
    elif broken_fact == "sku-price":
        statement = update(ProductSku).where(ProductSku.id == "sku-1").values(
            price=Decimal("101.00")
        )
    elif broken_fact == "citation-disabled":
        statement = update(KnowledgeDocument).where(
            KnowledgeDocument.id == "document-1"
        ).values(enabled=False)
    elif broken_fact == "active-pointer":
        statement = update(ProductProposal).where(
            ProductProposal.id == "proposal-1"
        ).values(active_manual_review_run_id=None)
    elif broken_fact == "run-input":
        statement = update(WorkflowRun).where(
            WorkflowRun.id == "optimization-1"
        ).values(input={"proposal_id": "other-proposal"})
    elif broken_fact == "revision-number":
        statement = update(ProposalRevision).where(
            ProposalRevision.id == "revision-2"
        ).values(revision_number=3)
    else:
        await session.execute(
            update(ProductProposal)
            .where(ProductProposal.id == "proposal-1")
            .values(active_manual_review_run_id=None)
            .execution_options(synchronize_session=False)
        )
        statement = delete(ManualReviewRun).where(ManualReviewRun.id == "manual-run-1")
    await session.execute(statement.execution_options(synchronize_session=False))
    await session.commit()

    result = await manual.load_owned_manual_review_context(
        session,
        workflow_run_id="manual-workflow-1",
        lease_owner="worker-a",
    )

    assert (result.disposition, result.context, result.error_code) == (
        "failed",
        None,
        expected_code,
    )


async def test_product_version_change_fails_both_runs_and_clears_pointer(session) -> None:
    await _seed_manual_chain(session)
    await session.execute(
        update(Product)
        .where(Product.id == "product-1")
        .values(current_version=8)
        .execution_options(synchronize_session=False)
    )
    await session.commit()

    result = await manual.load_owned_manual_review_context(
        session,
        workflow_run_id="manual-workflow-1",
        lease_owner="worker-a",
    )

    assert (result.disposition, result.error_code) == (
        "failed",
        "PRODUCT_VERSION_CONFLICT",
    )
    manual_workflow = await session.get(
        WorkflowRun, "manual-workflow-1", populate_existing=True
    )
    original = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    proposal = await session.get(ProductProposal, "proposal-1", populate_existing=True)
    assert manual_workflow is not None and original is not None and proposal is not None
    assert (
        manual_workflow.status,
        manual_workflow.quality_status,
        manual_workflow.current_step,
        manual_workflow.error_code,
    ) == (
        WorkflowStatus.FAILED,
        WorkflowQuality.DEGRADED,
        "failed",
        "PRODUCT_VERSION_CONFLICT",
    )
    assert (
        original.status,
        original.quality_status,
        original.current_step,
        original.error_code,
    ) == (
        WorkflowStatus.FAILED,
        WorkflowQuality.DEGRADED,
        "manual_review_failed",
        "PRODUCT_VERSION_CONFLICT",
    )
    assert proposal.active_manual_review_run_id is None


async def test_owner_loss_returns_lease_lost_without_any_write(session) -> None:
    await _seed_manual_chain(session)
    before = {
        "manual": await session.get(WorkflowRun, "manual-workflow-1"),
        "original": await session.get(WorkflowRun, "optimization-1"),
        "proposal": await session.get(ProductProposal, "proposal-1"),
        "audit_count": int(await session.scalar(select(func.count()).select_from(AuditEvent)) or 0),
    }
    before_values = (
        before["manual"].status,
        before["manual"].lease_owner,
        before["original"].status,
        before["proposal"].active_manual_review_run_id,
        before["audit_count"],
    )

    result = await manual.load_owned_manual_review_context(
        session,
        workflow_run_id="manual-workflow-1",
        lease_owner="replaced-owner",
    )

    assert (result.disposition, result.context, result.error_code) == (
        "lease_lost",
        None,
        None,
    )
    manual_workflow = await session.get(
        WorkflowRun, "manual-workflow-1", populate_existing=True
    )
    original = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    proposal = await session.get(ProductProposal, "proposal-1", populate_existing=True)
    assert manual_workflow is not None and original is not None and proposal is not None
    assert (
        manual_workflow.status,
        manual_workflow.lease_owner,
        original.status,
        proposal.active_manual_review_run_id,
        int(await session.scalar(select(func.count()).select_from(AuditEvent)) or 0),
    ) == before_values


async def test_context_database_error_is_stable_and_rolls_back(session, monkeypatch) -> None:
    await _seed_manual_chain(session)

    async def fail_scalar(*args, **kwargs):
        raise SQLAlchemyError("simulated context database failure")

    monkeypatch.setattr(session, "scalar", fail_scalar)
    result = await manual.load_owned_manual_review_context(
        session,
        workflow_run_id="manual-workflow-1",
        lease_owner="worker-a",
    )

    assert (result.disposition, result.context, result.error_code) == (
        "failed",
        None,
        "MANUAL_REVIEW_DATABASE_ERROR",
    )
