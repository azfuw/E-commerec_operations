import hashlib
import json
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.auth import create_access_token, hash_password
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
from backend.config import get_settings
from backend.database import get_session
from backend.main import create_app
from backend.manual_reviews import ManualReviewDomainError, compose_manual_output
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
from backend.optimization_validation import validate_optimization_output
from backend.schemas import (
    AttributeCompletion,
    CanonicalRuleCitation,
    DescriptionSection,
    EvidenceRef,
    ManualRevisionAccepted,
    ManualRevisionRequest,
    OptimizationChange,
    OptimizationProposalOutput,
    OutputCitation,
    PriceSuggestion,
    ProductMetrics,
    SkuSuggestion,
    TrustedOptimizationInput,
    TrustedProductSku,
)


RULE_CHUNK = "rule-chunk-1"


def _fact(value: str) -> EvidenceRef:
    return EvidenceRef(kind="fact", value=value)


def _citation(value: str = RULE_CHUNK) -> EvidenceRef:
    return EvidenceRef(kind="citation", value=value)


def _section(*, heading: str = "商品详情", body: str = "适合日常使用") -> DescriptionSection:
    return DescriptionSection(heading=heading, body=body, evidence=[_citation()])


def _title_change(
    *,
    current: str = "原商品标题",
    suggested: str = "优选家居商品",
    evidence: list[EvidenceRef] | None = None,
) -> OptimizationChange:
    return OptimizationChange(
        field="title",
        current_value=current,
        suggested_value=suggested,
        reason="优化标题表达",
        evidence=[_fact("product.title")] if evidence is None else evidence,
    )


def _description_change(
    *, sections: list[DescriptionSection] | None = None
) -> OptimizationChange:
    return OptimizationChange(
        field="description",
        current_value="原始详情",
        suggested_value=sections or [_section()],
        reason="优化详情表达",
        evidence=[_citation()],
    )


def _attribute(
    *,
    target: str = "材质",
    current: str | None = "棉",
    evidence: list[EvidenceRef] | None = None,
) -> AttributeCompletion:
    return AttributeCompletion(
        target_attribute=target,
        current_value=current,
        suggested_value="精梳棉",
        reason="补全商品属性",
        evidence=[_fact("product.attributes.材质")] if evidence is None else evidence,
    )


def _price(
    *, target: str = "sku-1", current: Decimal = Decimal("100.00")
) -> PriceSuggestion:
    return PriceSuggestion(
        target_sku_id=target,
        current_price=current,
        suggested_price=Decimal("90.00"),
        reason="价格建议",
        evidence=[_citation()],
    )


def _sku(
    *,
    target: str = "sku-1",
    current_code: str = "SKU-RED",
    current_spec: dict[str, str] | None = None,
) -> SkuSuggestion:
    return SkuSuggestion(
        target_sku_id=target,
        current_code=current_code,
        current_spec={"颜色": "红"} if current_spec is None else current_spec,
        suggested_code="SKU-RED-NEW",
        suggested_spec={"颜色": "红色"},
        reason="SKU 建议",
        evidence=[_citation()],
    )


def _request_values() -> dict[str, object]:
    section = _section()
    return {
        "parent_revision_id": "revision-1",
        "base_product_version": 7,
        "title": "优选家居商品",
        "selling_points": ["棉质家居设计"],
        "description": [section],
        "keywords": ["家居"],
        "attribute_completions": [_attribute()],
        "changes": [_title_change(), _description_change(sections=[section])],
    }


def _request(**updates: object) -> ManualRevisionRequest:
    values = _request_values()
    values.update(updates)
    return ManualRevisionRequest.model_validate(values)


def _parent(**updates: object) -> OptimizationProposalOutput:
    values: dict[str, object] = {
        "title": "父版本标题",
        "selling_points": ["父版本卖点"],
        "description": [_section(body="父版本详情")],
        "keywords": ["父版本关键词"],
        "attribute_completions": [],
        "changes": [],
        "citations": [OutputCitation(chunk_id=RULE_CHUNK)],
        "price_suggestions": [_price()],
        "sku_suggestions": [_sku()],
    }
    values.update(updates)
    return OptimizationProposalOutput.model_validate(values)


def _trusted() -> TrustedOptimizationInput:
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
            TrustedProductSku(
                id="sku-1",
                code="SKU-RED",
                spec={"颜色": "红"},
                price=Decimal("100.00"),
                stock=10,
            )
        ],
        candidate_metrics=ProductMetrics.from_totals(
            impressions=100,
            clicks=10,
            orders=1,
            units=1,
            revenue=Decimal("100.00"),
            refunds=0,
        ).model_copy(update={"product_id": "product-1", "product_code": "HOME-001"}),
        candidate_evidence=["orders=1"],
        rag_quality="normal",
        canonical_rule_citations=[
            CanonicalRuleCitation(
                document_id="document-1",
                version_id="version-1",
                chunk_id=RULE_CHUNK,
                document_name="通用规则",
                version_number=1,
                category="通用规则",
                canonical_text="商品文案应有依据。",
                active=True,
                applicable=True,
            )
        ],
    )


def test_manual_revision_request_accepts_exact_minimum_and_maximum_boundaries() -> None:
    minimum = ManualRevisionRequest(
        parent_revision_id="r",
        base_product_version=1,
        title="中",
        selling_points=["x"],
        description=[DescriptionSection(heading="x", body="x", evidence=[])],
        keywords=["x"],
        attribute_completions=[],
        changes=[],
    )
    sections = [
        DescriptionSection(heading="中" * 40, body="中" * 1000, evidence=[])
        for _ in range(10)
    ]
    maximum = ManualRevisionRequest(
        parent_revision_id="r" * 36,
        base_product_version=1,
        title="中" * 60,
        selling_points=["中" * 80] * 5,
        description=sections,
        keywords=["中" * 32] * 20,
        attribute_completions=[
            _attribute(target=f"属性{index}", current=None, evidence=[_citation()])
            for index in range(20)
        ],
        changes=[
            OptimizationChange(
                field=field,
                current_value="原值",
                suggested_value="新值",
                reason="原因",
                evidence=[],
            )
            for field in ("title", "selling_points", "description", "keywords")
        ],
    )

    assert minimum.base_product_version == 1
    assert len(maximum.parent_revision_id) == 36
    assert len(maximum.description[-1].body) == 1000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_product_version", 0),
        ("parent_revision_id", ""),
        ("parent_revision_id", "r" * 37),
        ("title", ""),
        ("title", "中" * 61),
        ("title", "English title"),
        ("selling_points", []),
        ("selling_points", ["x"] * 6),
        ("selling_points", [""]),
        ("selling_points", ["x" * 81]),
        ("description", []),
        ("description", [_section()] * 11),
        ("description", [DescriptionSection(heading="", body="x", evidence=[])]),
        ("description", [DescriptionSection(heading="x" * 41, body="x", evidence=[])]),
        ("description", [DescriptionSection(heading="x", body="", evidence=[])]),
        ("description", [DescriptionSection(heading="x", body="x" * 1001, evidence=[])]),
        ("keywords", []),
        ("keywords", ["x"] * 21),
        ("keywords", [""]),
        ("keywords", ["x" * 33]),
        (
            "attribute_completions",
            [_attribute(target=f"属性{index}") for index in range(21)],
        ),
        ("changes", [_title_change()] * 5),
    ],
    ids=[
        "base-version-zero",
        "parent-id-empty",
        "parent-id-too-long",
        "title-empty",
        "title-too-long",
        "title-without-chinese",
        "selling-points-empty",
        "selling-points-too-many",
        "selling-point-empty",
        "selling-point-too-long",
        "description-empty",
        "description-too-many",
        "heading-empty",
        "heading-too-long",
        "body-empty",
        "body-too-long",
        "keywords-empty",
        "keywords-too-many",
        "keyword-empty",
        "keyword-too-long",
        "attributes-too-many",
        "changes-too-many",
    ],
)
def test_manual_revision_request_rejects_every_approved_boundary(
    field: str, value: object
) -> None:
    values = _request_values()
    values[field] = value

    with pytest.raises(ValidationError):
        ManualRevisionRequest.model_validate(values)


@pytest.mark.parametrize("readonly_field", ["citations", "price_suggestions", "sku_suggestions"])
def test_manual_revision_request_forbids_client_supplied_readonly_groups(
    readonly_field: str,
) -> None:
    values = _request_values()
    values[readonly_field] = []

    with pytest.raises(ValidationError) as error:
        ManualRevisionRequest.model_validate(values)

    assert error.value.errors()[0]["type"] == "extra_forbidden"


def test_manual_revision_accepted_has_typed_status_and_domain_error_is_stable() -> None:
    accepted = ManualRevisionAccepted(
        revision_id="revision-2",
        manual_review_workflow_run_id="workflow-1",
        status="accepted",
    )
    error = ManualReviewDomainError(code="MANUAL_REVISION_INVALID", status_code=422)

    assert accepted.model_dump() == {
        "revision_id": "revision-2",
        "manual_review_workflow_run_id": "workflow-1",
        "status": "accepted",
    }
    assert (error.code, error.status_code) == ("MANUAL_REVISION_INVALID", 422)
    with pytest.raises(ValidationError):
        ManualRevisionAccepted(
            revision_id="revision-2",
            manual_review_workflow_run_id="workflow-1",
            status="processing",
        )


def test_manual_review_domain_error_allows_standard_traceback_lifecycle() -> None:
    error = ManualReviewDomainError(code="MANUAL_REVISION_INVALID", status_code=422)

    with pytest.raises(ManualReviewDomainError) as raised:
        raise error

    raised.value.__traceback__ = None
    assert (raised.value.code, raised.value.status_code) == (
        "MANUAL_REVISION_INVALID",
        422,
    )


def test_compose_manual_output_uses_only_editable_request_and_readonly_parent_groups() -> None:
    request = _request()
    parent = _parent()
    request_before = request.model_dump(mode="json")
    parent_before = parent.model_dump(mode="json")

    output = compose_manual_output(request, parent)

    assert output.model_dump(mode="json") == {
        "title": request_before["title"],
        "selling_points": request_before["selling_points"],
        "description": request_before["description"],
        "keywords": request_before["keywords"],
        "attribute_completions": request_before["attribute_completions"],
        "changes": request_before["changes"],
        "citations": parent_before["citations"],
        "price_suggestions": parent_before["price_suggestions"],
        "sku_suggestions": parent_before["sku_suggestions"],
    }
    assert request.model_dump(mode="json") == request_before
    assert parent.model_dump(mode="json") == parent_before


def test_composed_manual_output_passes_typed_and_existing_trust_boundaries() -> None:
    output = compose_manual_output(_request(), _parent())
    typed = OptimizationProposalOutput.model_validate(output.model_dump())

    result = validate_optimization_output(_trusted(), typed)

    assert result.passed is True
    assert result.violations == ()
    assert result.canonical_citations == (_trusted().canonical_rule_citations[0],)


def _break_unknown_fact(request: ManualRevisionRequest, parent: OptimizationProposalOutput) -> None:
    request.changes[0].evidence = [_fact("product.attributes.不存在")]


def _break_forged_current(request: ManualRevisionRequest, parent: OptimizationProposalOutput) -> None:
    request.changes[0].current_value = "伪造当前标题"


def _break_duplicate_change(request: ManualRevisionRequest, parent: OptimizationProposalOutput) -> None:
    request.changes.append(request.changes[0].model_copy(deep=True))


def _break_missing_evidence(request: ManualRevisionRequest, parent: OptimizationProposalOutput) -> None:
    request.changes[0].evidence = []


def _break_unknown_citation(request: ManualRevisionRequest, parent: OptimizationProposalOutput) -> None:
    parent.citations = [OutputCitation(chunk_id="unknown-chunk")]


def _break_unknown_sku(request: ManualRevisionRequest, parent: OptimizationProposalOutput) -> None:
    parent.sku_suggestions = [_sku(target="missing-sku")]


def _break_stale_price(request: ManualRevisionRequest, parent: OptimizationProposalOutput) -> None:
    parent.price_suggestions = [_price(current=Decimal("99.99"))]


def _break_stale_sku_code(request: ManualRevisionRequest, parent: OptimizationProposalOutput) -> None:
    parent.sku_suggestions = [_sku(current_code="STALE")]


def _break_stale_sku_spec(request: ManualRevisionRequest, parent: OptimizationProposalOutput) -> None:
    parent.sku_suggestions = [_sku(current_spec={"颜色": "蓝"})]


def _break_untrusted_attribute(
    request: ManualRevisionRequest, parent: OptimizationProposalOutput
) -> None:
    request.attribute_completions = [
        _attribute(target="新属性", current=None, evidence=[_fact("product.title")])
    ]


@pytest.mark.parametrize(
    ("break_output", "expected_code"),
    [
        (_break_unknown_fact, "EVIDENCE_FACT_PATH_INVALID"),
        (_break_forged_current, "CHANGE_CURRENT_MISMATCH"),
        (_break_duplicate_change, "CHANGE_TARGET_DUPLICATE"),
        (_break_missing_evidence, "EVIDENCE_MISSING"),
        (_break_unknown_citation, "CITATION_UNKNOWN"),
        (_break_unknown_sku, "SKU_UNKNOWN"),
        (_break_stale_price, "PRICE_CURRENT_MISMATCH"),
        (_break_stale_sku_code, "SKU_CURRENT_MISMATCH"),
        (_break_stale_sku_spec, "SKU_CURRENT_MISMATCH"),
        (_break_untrusted_attribute, "ATTRIBUTE_SOURCE_MISSING"),
    ],
    ids=[
        "unknown-fact-path",
        "forged-current-value",
        "duplicate-change-target",
        "missing-evidence",
        "unknown-citation",
        "unknown-sku",
        "stale-price",
        "stale-sku-code",
        "stale-sku-spec",
        "untrusted-new-attribute",
    ],
)
def test_composed_manual_output_delegates_trust_failures_to_existing_validator(
    break_output, expected_code: str
) -> None:
    request = _request()
    parent = _parent()
    break_output(request, parent)

    output = compose_manual_output(request, parent)
    codes = {
        violation.code for violation in validate_optimization_output(_trusted(), output).violations
    }

    assert expected_code in codes


def test_reviewable_restricted_copy_is_not_rejected_by_manual_request_schema() -> None:
    request = _request(
        title="治疗家居商品",
        changes=[_title_change(suggested="治疗家居商品"), _description_change()],
    )
    output = compose_manual_output(request, _parent())

    result = validate_optimization_output(_trusted(), output)

    assert "RESTRICTED_PHRASE" in {violation.code for violation in result.violations}


def _manual_headers(user: User, key: str | None = "manual-key-1") -> dict[str, str]:
    headers = {"Authorization": f"Bearer {create_access_token(user, get_settings())}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _manual_body(**updates: object) -> dict[str, object]:
    values = _request().model_dump(mode="json")
    values.update(updates)
    return values


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _model_count(session, model) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


@pytest_asyncio.fixture
async def manual_client(session) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def override_get_session():
        yield session

    app.dependency_overrides[get_session] = override_get_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def manual_route_data(session) -> dict[str, object]:
    password_hash = hash_password("DemoPass!2026")
    users = {
        "operator": User(
            id="operator-1",
            username="operator",
            password_hash=password_hash,
            role=UserRole.OPERATOR,
        ),
        "supervisor": User(
            id="supervisor-1",
            username="supervisor",
            password_hash=password_hash,
            role=UserRole.SUPERVISOR,
        ),
        "admin": User(
            id="admin-1",
            username="admin",
            password_hash=password_hash,
            role=UserRole.ADMIN,
        ),
        "other": User(
            id="other-1",
            username="other",
            password_hash=password_hash,
            role=UserRole.OPERATOR,
        ),
    }
    store = Store(id="store-1", name="目标店铺", code="target")
    other_store = Store(id="store-2", name="其他店铺", code="other")
    session.add_all([*users.values(), store, other_store])
    await session.flush()
    session.add_all(
        [
            UserStoreScope(user_id=users[role].id, store_id=store.id)
            for role in ("operator", "supervisor", "admin")
        ]
        + [UserStoreScope(user_id=users["other"].id, store_id=other_store.id)]
    )
    await session.flush()

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
        current_version=1,
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
        created_by=users["operator"].id,
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
        created_by=users["operator"].id,
        status=WorkflowStatus.DRAFT_READY,
        quality_status=WorkflowQuality.NORMAL,
        current_step="draft_ready",
        input={
            "proposal_id": "proposal-1",
            "source_analysis_run_id": "analysis-1",
            "analysis_candidate_id": "candidate-1",
            "product_id": product.id,
            "store_id": store.id,
        },
    )
    session.add_all([analysis, optimization])
    await session.flush()
    metrics = _trusted().candidate_metrics
    candidate = AnalysisCandidate(
        id="candidate-1",
        workflow_run_id=analysis.id,
        product_id=product.id,
        rank=1,
        product_code=product.code,
        anomaly_types=["low_conversion"],
        metrics=metrics.model_dump(mode="json"),
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

    citation = _trusted().canonical_rule_citations[0]
    parent = ProposalRevision(
        id="revision-1",
        proposal_id=proposal.id,
        iteration=0,
        revision_number=1,
        origin=ProposalRevisionOrigin.AGENT,
        created_by=users["operator"].id,
        parent_revision_id=None,
        base_product_version=7,
        trusted_fact_hash="b" * 64,
        proposal_output=_parent().model_dump(mode="json"),
        citations=[citation.model_dump(mode="json")],
    )
    session.add(parent)
    await session.flush()
    proposal.current_revision_id = parent.id
    review = ComplianceReview(
        id="review-1",
        proposal_id=proposal.id,
        proposal_revision_id=parent.id,
        iteration=0,
        deterministic_checks={"passed": True, "violations": []},
        semantic_review={"passed": True, "violations": []},
        passed=True,
        risk_level=ComplianceRiskLevel.LOW,
        required_changes=[],
        citations=[citation.model_dump(mode="json")],
        quality_status=WorkflowQuality.NORMAL,
    )
    session.add(review)
    await session.flush()

    document = KnowledgeDocument(
        id="document-1",
        name="通用规则",
        category="通用规则",
        created_by=users["admin"].id,
    )
    session.add(document)
    await session.flush()
    version = KnowledgeDocumentVersion(
        id="version-1",
        document_id=document.id,
        version_number=1,
        sha256="c" * 64,
        original_filename="rule.txt",
        mime_type="text/plain",
        storage_path="test/rule.txt",
        status=KnowledgeVersionStatus.ACTIVE,
    )
    session.add(version)
    await session.flush()
    chunk = KnowledgeChunk(
        id=RULE_CHUNK,
        version_id=version.id,
        chunk_index=0,
        chunk_hash="d" * 64,
        canonical_text="商品文案应有依据。",
        chunk_metadata={},
        token_count=8,
    )
    session.add(chunk)
    await session.flush()
    document.current_version_id = version.id
    await session.commit()
    return {
        **users,
        "store": store,
        "other_store": other_store,
        "product": product,
        "other_product": other_product,
        "sku": sku,
        "analysis": analysis,
        "candidate": candidate,
        "optimization": optimization,
        "proposal": proposal,
        "parent": parent,
        "review": review,
        "document": document,
        "version": version,
        "chunk": chunk,
    }


@pytest.mark.parametrize("actor_name", ["operator", "supervisor", "admin"])
async def test_manual_revision_route_commits_one_complete_trusted_chain_for_every_allowed_role(
    manual_client, manual_route_data, session, actor_name: str
) -> None:
    actor = manual_route_data[actor_name]
    response = await manual_client.post(
        "/proposals/proposal-1/manual-revision",
        json=_manual_body(),
        headers=_manual_headers(actor),
    )

    assert response.status_code == 202
    assert set(response.json()) == {"revision_id", "manual_review_workflow_run_id", "status"}
    assert response.json()["status"] == "accepted"
    revision = await session.get(ProposalRevision, response.json()["revision_id"])
    workflow = await session.get(WorkflowRun, response.json()["manual_review_workflow_run_id"])
    manual_run = await session.scalar(
        select(ManualReviewRun).where(ManualReviewRun.workflow_run_id == workflow.id)
    )
    audit = await session.scalar(select(AuditEvent))
    proposal = await session.get(ProductProposal, "proposal-1", populate_existing=True)
    optimization = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    assert revision is not None and workflow is not None and manual_run is not None and audit is not None
    assert (
        revision.proposal_id,
        revision.iteration,
        revision.revision_number,
        revision.origin,
        revision.created_by,
        revision.parent_revision_id,
        revision.base_product_version,
    ) == (
        "proposal-1",
        None,
        2,
        ProposalRevisionOrigin.MANUAL,
        actor.id,
        "revision-1",
        7,
    )
    assert revision.trusted_fact_hash == _canonical_hash(_trusted().model_dump(mode="json"))
    assert revision.citations == [
        _trusted().canonical_rule_citations[0].model_dump(mode="json")
    ]
    expected_request_hash = _canonical_hash(
        {
            "action": "manual_revision",
            "actor_id": actor.id,
            "proposal_id": "proposal-1",
            "parent_revision_id": "revision-1",
            "base_product_version": 7,
            "request": _manual_body(),
        }
    )
    assert manual_run.request_hash == expected_request_hash
    assert manual_run.idempotency_key_hash == hashlib.sha256(b"manual-key-1").hexdigest()
    assert (
        manual_run.proposal_id,
        manual_run.proposal_revision_id,
        manual_run.submitted_by,
    ) == ("proposal-1", revision.id, actor.id)
    assert (
        workflow.workflow_type,
        workflow.store_id,
        workflow.created_by,
        workflow.status,
        workflow.quality_status,
        workflow.current_step,
    ) == (
        WorkflowType.MANUAL_REVIEW,
        "store-1",
        actor.id,
        WorkflowStatus.ACCEPTED,
        WorkflowQuality.NORMAL,
        "accepted",
    )
    assert workflow.input == {
        "manual_review_run_id": manual_run.id,
        "proposal_id": "proposal-1",
        "proposal_revision_id": revision.id,
        "parent_revision_id": "revision-1",
        "product_id": "product-1",
        "store_id": "store-1",
    }
    assert audit.event_type is AuditEventType.MANUAL_REVISION_CREATED
    assert audit.outcome is AuditOutcome.SUCCESS
    assert (audit.actor_id, audit.actor_role, audit.proposal_revision_id, audit.workflow_run_id) == (
        actor.id,
        actor.role,
        revision.id,
        workflow.id,
    )
    assert audit.details == {
        "from_status": "draft_ready",
        "to_status": "pending_manual",
        "revision_number": 2,
        "origin": "manual",
        "workflow_type": "manual_review",
        "quality_status": "normal",
        "current_step": "manual_review_pending",
        "changed_fields": [
            "attribute_completions",
            "description",
            "keywords",
            "selling_points",
            "title",
        ],
    }
    assert proposal is not None and (
        proposal.current_revision_id,
        proposal.active_manual_review_run_id,
    ) == (revision.id, manual_run.id)
    assert optimization is not None and (
        optimization.status,
        optimization.quality_status,
        optimization.current_step,
        optimization.error_code,
    ) == (
        WorkflowStatus.PENDING_MANUAL,
        WorkflowQuality.NORMAL,
        "manual_review_pending",
        None,
    )
    assert await _model_count(session, ProposalRevision) == 2
    assert await _model_count(session, WorkflowRun) == 3
    assert await _model_count(session, ManualReviewRun) == 1
    assert await _model_count(session, AuditEvent) == 1
    serialized = response.text
    assert all(
        unsafe not in serialized
        for unsafe in ("manual-key-1", manual_run.request_hash, revision.trusted_fact_hash, "lease_owner")
    )


@pytest.mark.parametrize("key", [None, "   ", "k" * 129])
async def test_manual_revision_route_rejects_invalid_idempotency_key_before_writes(
    manual_client, manual_route_data, session, key: str | None
) -> None:
    response = await manual_client.post(
        "/proposals/proposal-1/manual-revision",
        json=_manual_body(),
        headers=_manual_headers(manual_route_data["operator"], key),
    )

    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "IDEMPOTENCY_KEY_INVALID"}}
    assert await _model_count(session, ProposalRevision) == 1
    assert await _model_count(session, ManualReviewRun) == 0
    assert await _model_count(session, AuditEvent) == 0


@pytest.mark.parametrize(
    ("path", "body", "expected_status"),
    [
        ("", _manual_body(), 404),
        ("p" * 37, _manual_body(), 422),
        ("proposal-1", _manual_body(parent_revision_id=""), 422),
        ("proposal-1", _manual_body(parent_revision_id="r" * 37), 422),
        ("proposal-1", _manual_body(base_product_version=0), 422),
    ],
)
async def test_manual_revision_route_rejects_invalid_path_and_body_resource_bounds(
    manual_client, manual_route_data, session, path: str, body: dict[str, object], expected_status: int
) -> None:
    response = await manual_client.post(
        f"/proposals/{path}/manual-revision",
        json=body,
        headers=_manual_headers(manual_route_data["operator"]),
    )

    assert response.status_code == expected_status
    assert await _model_count(session, ProposalRevision) == 1
    assert await _model_count(session, ManualReviewRun) == 0


@pytest.mark.parametrize(
    ("case", "expected_status"),
    [
        ("disabled-actor", 401),
        ("disabled-store", 404),
        ("missing-scope", 404),
        ("cross-store", 404),
        ("wrong-resource-chain", 404),
    ],
)
async def test_manual_revision_route_freshly_enforces_actor_store_scope_and_ownership(
    manual_client, manual_route_data, session, case: str, expected_status: int
) -> None:
    actor = manual_route_data["operator"]
    if case == "disabled-actor":
        await session.execute(
            update(User)
            .where(User.id == actor.id)
            .values(status=UserStatus.DISABLED)
            .execution_options(synchronize_session=False)
        )
    elif case == "disabled-store":
        await session.execute(
            update(Store)
            .where(Store.id == "store-1")
            .values(enabled=False)
            .execution_options(synchronize_session=False)
        )
    elif case == "missing-scope":
        await session.execute(
            delete(UserStoreScope).where(
                UserStoreScope.user_id == actor.id,
                UserStoreScope.store_id == "store-1",
            )
        )
    elif case == "cross-store":
        actor = manual_route_data["other"]
    else:
        await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == "optimization-1")
            .values(input={"proposal_id": "other-proposal"})
            .execution_options(synchronize_session=False)
        )
    await session.commit()

    response = await manual_client.post(
        "/proposals/proposal-1/manual-revision",
        json=_manual_body(),
        headers=_manual_headers(actor),
    )

    assert response.status_code == expected_status
    assert response.json() == (
        {"detail": "Invalid credentials"} if case == "disabled-actor"
        else {"detail": {"code": "PROPOSAL_NOT_FOUND"}}
    )
    assert await _model_count(session, ProposalRevision) == 1
    assert await _model_count(session, ManualReviewRun) == 0
    assert await _model_count(session, AuditEvent) == 0


async def test_admin_without_exact_scope_does_not_bypass_manual_revision_authorization(
    manual_client, manual_route_data, session
) -> None:
    admin = manual_route_data["admin"]
    await session.execute(
        delete(UserStoreScope).where(
            UserStoreScope.user_id == admin.id,
            UserStoreScope.store_id == "store-1",
        )
    )
    await session.commit()

    response = await manual_client.post(
        "/proposals/proposal-1/manual-revision",
        json=_manual_body(),
        headers=_manual_headers(admin),
    )

    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "PROPOSAL_NOT_FOUND"}}


async def test_manual_revision_refreshes_actor_role_before_writing_audit(
    manual_client, manual_route_data, session
) -> None:
    actor = manual_route_data["operator"]
    headers = _manual_headers(actor)
    await session.execute(
        update(User)
        .where(User.id == actor.id)
        .values(role=UserRole.SUPERVISOR)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    assert actor.role is UserRole.OPERATOR

    response = await manual_client.post(
        "/proposals/proposal-1/manual-revision",
        json=_manual_body(),
        headers=headers,
    )

    assert response.status_code == 202
    audit = await session.scalar(select(AuditEvent))
    assert audit is not None and audit.actor_role is UserRole.SUPERVISOR


async def test_manual_revision_exact_replay_ignores_later_current_and_active_pointers(
    manual_client, manual_route_data, session
) -> None:
    actor = manual_route_data["operator"]
    first = await manual_client.post(
        "/proposals/proposal-1/manual-revision",
        json=_manual_body(),
        headers=_manual_headers(actor),
    )
    assert first.status_code == 202
    first_body = first.json()
    first_manual = await session.scalar(
        select(ManualReviewRun).where(
            ManualReviewRun.workflow_run_id == first_body["manual_review_workflow_run_id"]
        )
    )
    first_workflow = await session.get(
        WorkflowRun, first_body["manual_review_workflow_run_id"]
    )
    assert first_manual is not None and first_workflow is not None
    first_workflow.status = WorkflowStatus.COMPLETED
    first_workflow.current_step = "completed"
    later_revision = ProposalRevision(
        id="revision-later",
        proposal_id="proposal-1",
        iteration=None,
        revision_number=3,
        origin=ProposalRevisionOrigin.MANUAL,
        created_by=actor.id,
        parent_revision_id=first_body["revision_id"],
        base_product_version=7,
        trusted_fact_hash="e" * 64,
        proposal_output=_parent().model_dump(mode="json"),
        citations=[_trusted().canonical_rule_citations[0].model_dump(mode="json")],
    )
    later_workflow = WorkflowRun(
        id="workflow-later",
        workflow_type=WorkflowType.MANUAL_REVIEW,
        store_id="store-1",
        created_by=actor.id,
        status=WorkflowStatus.ACCEPTED,
        quality_status=WorkflowQuality.NORMAL,
    )
    session.add_all([later_revision, later_workflow])
    await session.flush()
    later_manual = ManualReviewRun(
        id="manual-later",
        workflow_run_id=later_workflow.id,
        proposal_id="proposal-1",
        proposal_revision_id=later_revision.id,
        submitted_by=actor.id,
        idempotency_key_hash="f" * 64,
        request_hash="0" * 64,
    )
    session.add(later_manual)
    await session.flush()
    proposal = await session.get(ProductProposal, "proposal-1")
    assert proposal is not None
    proposal.current_revision_id = later_revision.id
    proposal.active_manual_review_run_id = later_manual.id
    await session.commit()
    before = {
        "revision": proposal.current_revision_id,
        "active": proposal.active_manual_review_run_id,
        "revisions": await _model_count(session, ProposalRevision),
        "workflows": await _model_count(session, WorkflowRun),
        "manual_runs": await _model_count(session, ManualReviewRun),
        "audits": await _model_count(session, AuditEvent),
    }

    replay = await manual_client.post(
        "/proposals/proposal-1/manual-revision",
        json=_manual_body(),
        headers=_manual_headers(actor),
    )

    assert replay.status_code == 200
    assert replay.json() == first_body
    await session.refresh(proposal)
    assert {
        "revision": proposal.current_revision_id,
        "active": proposal.active_manual_review_run_id,
        "revisions": await _model_count(session, ProposalRevision),
        "workflows": await _model_count(session, WorkflowRun),
        "manual_runs": await _model_count(session, ManualReviewRun),
        "audits": await _model_count(session, AuditEvent),
    } == before


@pytest.mark.parametrize(
    "ownership_break",
    ["product-store", "run-store", "run-type", "run-input"],
)
async def test_manual_revision_exact_replay_rechecks_product_and_original_run_ownership(
    manual_client, manual_route_data, session, ownership_break: str
) -> None:
    actor = manual_route_data["operator"]
    first = await manual_client.post(
        "/proposals/proposal-1/manual-revision",
        json=_manual_body(),
        headers=_manual_headers(actor),
    )
    assert first.status_code == 202
    proposal = await session.get(ProductProposal, "proposal-1")
    assert proposal is not None
    before = {
        "revision": proposal.current_revision_id,
        "active": proposal.active_manual_review_run_id,
        "revisions": await _model_count(session, ProposalRevision),
        "workflows": await _model_count(session, WorkflowRun),
        "manual_runs": await _model_count(session, ManualReviewRun),
        "audits": await _model_count(session, AuditEvent),
    }
    if ownership_break == "product-store":
        statement = (
            update(Product)
            .where(Product.id == "product-1")
            .values(store_id="store-2")
        )
    elif ownership_break == "run-store":
        statement = (
            update(WorkflowRun)
            .where(WorkflowRun.id == "optimization-1")
            .values(store_id="store-2")
        )
    elif ownership_break == "run-type":
        statement = (
            update(WorkflowRun)
            .where(WorkflowRun.id == "optimization-1")
            .values(
                workflow_type=WorkflowType.MANUAL_REVIEW,
                status=WorkflowStatus.COMPLETED,
            )
        )
    else:
        statement = (
            update(WorkflowRun)
            .where(WorkflowRun.id == "optimization-1")
            .values(input={"proposal_id": "other-proposal"})
        )
    await session.execute(statement.execution_options(synchronize_session=False))
    await session.commit()

    replay = await manual_client.post(
        "/proposals/proposal-1/manual-revision",
        json=_manual_body(),
        headers=_manual_headers(actor),
    )

    assert replay.status_code == 404
    assert replay.json() == {"detail": {"code": "PROPOSAL_NOT_FOUND"}}
    await session.refresh(proposal)
    assert {
        "revision": proposal.current_revision_id,
        "active": proposal.active_manual_review_run_id,
        "revisions": await _model_count(session, ProposalRevision),
        "workflows": await _model_count(session, WorkflowRun),
        "manual_runs": await _model_count(session, ManualReviewRun),
        "audits": await _model_count(session, AuditEvent),
    } == before


async def test_manual_revision_same_key_different_body_conflicts_without_mutation(
    manual_client, manual_route_data, session
) -> None:
    actor = manual_route_data["operator"]
    first = await manual_client.post(
        "/proposals/proposal-1/manual-revision",
        json=_manual_body(),
        headers=_manual_headers(actor),
    )
    assert first.status_code == 202
    changed = _manual_body(
        title="另一优选家居商品",
        changes=[
            _title_change(suggested="另一优选家居商品").model_dump(mode="json"),
            _description_change().model_dump(mode="json"),
        ],
    )

    conflict = await manual_client.post(
        "/proposals/proposal-1/manual-revision",
        json=changed,
        headers=_manual_headers(actor),
    )

    assert conflict.status_code == 409
    assert conflict.json() == {"detail": {"code": "IDEMPOTENCY_REPLAY_CONFLICT"}}
    assert await _model_count(session, ProposalRevision) == 2
    assert await _model_count(session, ManualReviewRun) == 1
    assert await _model_count(session, AuditEvent) == 1


async def test_manual_revision_new_key_conflicts_with_existing_active_run(
    manual_client, manual_route_data, session
) -> None:
    actor = manual_route_data["operator"]
    first = await manual_client.post(
        "/proposals/proposal-1/manual-revision",
        json=_manual_body(),
        headers=_manual_headers(actor),
    )
    assert first.status_code == 202

    conflict = await manual_client.post(
        "/proposals/proposal-1/manual-revision",
        json=_manual_body(),
        headers=_manual_headers(actor, "different-key"),
    )

    assert conflict.status_code == 409
    assert conflict.json() == {"detail": {"code": "MANUAL_REVIEW_ACTIVE"}}
    assert await _model_count(session, ProposalRevision) == 2
    assert await _model_count(session, ManualReviewRun) == 1


@pytest.mark.parametrize(
    ("fact", "expected_status", "expected_code"),
    [
        ("current-pointer", 409, "PROPOSAL_EDIT_FORBIDDEN"),
        ("product-version", 409, "PRODUCT_VERSION_CONFLICT"),
        ("sku-price", 422, "TRUSTED_EVIDENCE_INVALID"),
        ("citation-current-version", 422, "TRUSTED_EVIDENCE_INVALID"),
        ("citation-cross-owned-version", 422, "TRUSTED_EVIDENCE_INVALID"),
        ("missing-parent-review", 503, "PROPOSAL_DATA_INCONSISTENT"),
        ("review-citations-mismatch", 503, "PROPOSAL_DATA_INCONSISTENT"),
    ],
)
async def test_manual_revision_reloads_current_parent_product_sku_review_and_citations(
    manual_client,
    manual_route_data,
    session,
    fact: str,
    expected_status: int,
    expected_code: str,
) -> None:
    if fact == "current-pointer":
        statement = (
            update(ProductProposal)
            .where(ProductProposal.id == "proposal-1")
            .values(current_revision_id=None)
        )
    elif fact == "product-version":
        statement = update(Product).where(Product.id == "product-1").values(current_version=8)
    elif fact == "sku-price":
        statement = update(ProductSku).where(ProductSku.id == "sku-1").values(price=Decimal("99.99"))
    elif fact == "missing-parent-review":
        await session.execute(delete(ComplianceReview).where(ComplianceReview.id == "review-1"))
        statement = None
    elif fact == "review-citations-mismatch":
        statement = (
            update(ComplianceReview)
            .where(ComplianceReview.id == "review-1")
            .values(citations=[])
        )
    else:
        other_document = KnowledgeDocument(
            id="document-2",
            name="另一规则",
            category="通用规则",
            created_by="admin-1",
        )
        session.add(other_document)
        await session.flush()
        other_version = KnowledgeDocumentVersion(
            id="version-2",
            document_id=(
                "document-2" if fact == "citation-cross-owned-version" else "document-1"
            ),
            version_number=(1 if fact == "citation-cross-owned-version" else 2),
            sha256="9" * 64,
            original_filename="other.txt",
            mime_type="text/plain",
            storage_path="test/other.txt",
            status=KnowledgeVersionStatus.ACTIVE,
        )
        session.add(other_version)
        await session.flush()
        statement = (
            update(KnowledgeDocument)
            .where(KnowledgeDocument.id == "document-1")
            .values(current_version_id=other_version.id)
        )
    if statement is not None:
        await session.execute(statement.execution_options(synchronize_session=False))
    await session.commit()

    response = await manual_client.post(
        "/proposals/proposal-1/manual-revision",
        json=_manual_body(),
        headers=_manual_headers(manual_route_data["operator"]),
    )

    assert response.status_code == expected_status
    assert response.json() == {"detail": {"code": expected_code}}
    assert await _model_count(session, ProposalRevision) == 1
    assert await _model_count(session, ManualReviewRun) == 0
    assert await _model_count(session, AuditEvent) == 0


def test_audit_event_helper_rejects_unknown_unsafe_and_oversized_details_before_add(
    session,
) -> None:
    from backend.audit_events import AUDIT_DETAIL_KEYS, add_audit_event

    assert AUDIT_DETAIL_KEYS == frozenset(
        {
            "from_status",
            "to_status",
            "revision_number",
            "origin",
            "workflow_type",
            "quality_status",
            "current_step",
            "changed_fields",
            "review_passed",
            "risk_level",
            "published_from_version",
            "published_to_version",
            "from_role",
            "to_role",
            "from_user_status",
            "to_user_status",
            "scope_count",
            "store_enabled",
            "document_status",
            "evaluation_agent_type",
            "evaluation_status",
            "case_count",
        }
    )
    invalid = [
        {"secret": "value"},
        {"revision_number": float("nan")},
        {"current_step": object()},
        {"changed_fields": ["x" * 4096]},
    ]
    for details in invalid:
        before = set(session.new)
        with pytest.raises(ValueError):
            add_audit_event(
                session,
                event_type=AuditEventType.MANUAL_REVISION_CREATED,
                outcome=AuditOutcome.SUCCESS,
                store_id="store-1",
                details=details,
            )
        assert set(session.new) == before


@pytest.mark.parametrize("failure_boundary", ["revision-flush", "manual-flush", "audit-flush", "commit"])
async def test_manual_revision_database_failure_rolls_back_every_success_fact(
    manual_route_data, session, monkeypatch, failure_boundary: str
) -> None:
    from backend.manual_reviews import create_manual_revision

    if failure_boundary == "commit":
        async def fail_commit() -> None:
            raise SQLAlchemyError("simulated commit failure")

        monkeypatch.setattr(session, "commit", fail_commit)
    else:
        original_flush = session.flush
        target = {"revision-flush": 1, "manual-flush": 2, "audit-flush": 3}[
            failure_boundary
        ]
        flush_count = 0

        async def fail_flush(*args, **kwargs) -> None:
            nonlocal flush_count
            flush_count += 1
            if flush_count == target:
                raise SQLAlchemyError(f"simulated {failure_boundary} failure")
            await original_flush(*args, **kwargs)

        monkeypatch.setattr(session, "flush", fail_flush)
    with pytest.raises(ManualReviewDomainError) as error:
        await create_manual_revision(
            session,
            actor_id="operator-1",
            proposal_id="proposal-1",
            request=_request(),
            idempotency_key="manual-key-1",
            request_id="request-1",
        )

    assert (error.value.code, error.value.status_code) == (
        "PROPOSAL_DATA_INCONSISTENT",
        503,
    )
    monkeypatch.undo()
    await session.rollback()
    proposal = await session.get(ProductProposal, "proposal-1", populate_existing=True)
    optimization = await session.get(WorkflowRun, "optimization-1", populate_existing=True)
    assert proposal is not None and (
        proposal.current_revision_id,
        proposal.active_manual_review_run_id,
    ) == ("revision-1", None)
    assert optimization is not None and (
        optimization.status,
        optimization.current_step,
    ) == (WorkflowStatus.DRAFT_READY, "draft_ready")
    assert await _model_count(session, ProposalRevision) == 1
    assert await _model_count(session, WorkflowRun) == 2
    assert await _model_count(session, ManualReviewRun) == 0
    assert await _model_count(session, AuditEvent) == 0


async def test_manual_revision_integrity_race_replays_the_exact_winner(
    manual_route_data, session, monkeypatch
) -> None:
    from backend.manual_reviews import create_manual_revision

    original_flush = session.flush
    flush_count = 0

    async def race_flush(*args, **kwargs) -> None:
        nonlocal flush_count
        flush_count += 1
        if flush_count < 3:
            await original_flush(*args, **kwargs)
            return
        if flush_count > 3:
            await original_flush(*args, **kwargs)
            return
        audit = next(row for row in session.new if isinstance(row, AuditEvent))
        persisted = list(session.identity_map.values())
        revision = next(
            row
            for row in persisted
            if isinstance(row, ProposalRevision) and row.origin is ProposalRevisionOrigin.MANUAL
        )
        workflow = next(
            row
            for row in persisted
            if isinstance(row, WorkflowRun) and row.workflow_type is WorkflowType.MANUAL_REVIEW
        )
        manual_run = next(row for row in persisted if isinstance(row, ManualReviewRun))
        await session.rollback()
        session.add_all([revision, workflow])
        await original_flush()
        session.add(manual_run)
        await original_flush()
        session.add(audit)
        await original_flush()
        await session.execute(
            update(ProductProposal)
            .where(ProductProposal.id == "proposal-1")
            .values(
                current_revision_id=revision.id,
                active_manual_review_run_id=manual_run.id,
            )
        )
        await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == "optimization-1")
            .values(
                status=WorkflowStatus.PENDING_MANUAL,
                quality_status=WorkflowQuality.NORMAL,
                current_step="manual_review_pending",
                error_code=None,
            )
        )
        await session.commit()
        assert workflow.id == manual_run.workflow_run_id
        raise IntegrityError("insert", {}, RuntimeError("simulated unique-key race"))

    monkeypatch.setattr(session, "flush", race_flush)
    result = await create_manual_revision(
        session,
        actor_id="operator-1",
        proposal_id="proposal-1",
        request=_request(),
        idempotency_key="manual-key-1",
        request_id="request-1",
    )

    assert result.created is False
    assert await _model_count(session, ProposalRevision) == 2
    assert await _model_count(session, WorkflowRun) == 3
    assert await _model_count(session, ManualReviewRun) == 1
    assert await _model_count(session, AuditEvent) == 1
