from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.manual_reviews import ManualReviewDomainError, compose_manual_output
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
