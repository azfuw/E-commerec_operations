from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.optimization_validation import validate_optimization_output
from backend.schemas import (
    AttributeCompletion,
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
    TrustedProductSku,
)


RULE_CHUNK = "rule-chunk-1"


def _fact(value: str) -> EvidenceRef:
    return EvidenceRef(kind="fact", value=value)


def _citation(value: str = RULE_CHUNK) -> EvidenceRef:
    return EvidenceRef(kind="citation", value=value)


def _section(*, heading: str = "商品说明", body: str = "适合日常使用") -> DescriptionSection:
    return DescriptionSection(heading=heading, body=body, evidence=[_citation()])


def complete_trusted_input(**updates: object) -> TrustedOptimizationInput:
    values: dict[str, object] = {
        "store_id": "store-1",
        "product_id": "product-1",
        "base_product_version": 7,
        "title": "原商品标题",
        "category": "家居",
        "brand": "好物品牌",
        "selling_points": ["红色家居设计"],
        "description": "原始详情",
        "search_keywords": ["家居"],
        "attributes": {"材质": "棉"},
        "skus": [
            TrustedProductSku(
                id="sku-1",
                code="SKU-RED",
                spec={"颜色": "红"},
                price=Decimal("100.00"),
                stock=10,
            )
        ],
        "candidate_metrics": ProductMetrics.from_totals(
            impressions=100,
            clicks=10,
            orders=1,
            units=1,
            revenue=Decimal("100.00"),
            refunds=0,
        ).model_copy(update={"product_id": "product-1", "product_code": "HOME-001"}),
        "candidate_evidence": ["orders=1"],
        "rag_quality": "normal",
        "canonical_rule_citations": [
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
    }
    values.update(updates)
    return TrustedOptimizationInput.model_validate(values)


def _title_change(*, current_value: str = "原商品标题", suggested_value: str = "优选家居商品") -> OptimizationChange:
    return OptimizationChange(
        field="title",
        current_value=current_value,
        suggested_value=suggested_value,
        reason="优化标题表达",
        evidence=[_fact("product.title")],
    )


def _description_change(
    *,
    current_value: str = "原始详情",
    suggested_value: list[DescriptionSection] | None = None,
) -> OptimizationChange:
    return OptimizationChange(
        field="description",
        current_value=current_value,
        suggested_value=suggested_value or [_section()],
        reason="优化详情表达",
        evidence=[_citation()],
    )


def _attribute(*, target: str = "颜色", value: str = "红色", evidence: list[EvidenceRef] | None = None) -> AttributeCompletion:
    return AttributeCompletion(
        target_attribute=target,
        current_value=None,
        suggested_value=value,
        reason="补全商品属性",
        evidence=evidence or [_citation()],
    )


def _price(
    *,
    target: str = "sku-1",
    current: Decimal = Decimal("100.00"),
    suggested: Decimal = Decimal("70.00"),
) -> PriceSuggestion:
    return PriceSuggestion(
        target_sku_id=target,
        current_price=current,
        suggested_price=suggested,
        reason="价格建议",
        evidence=[_citation()],
    )


def _sku(
    *,
    target: str = "sku-1",
    current_code: str = "SKU-RED",
    current_spec: dict[str, str] | None = None,
    suggested_code: str = "SKU-RED-NEW",
    suggested_spec: dict[str, str] | None = None,
) -> SkuSuggestion:
    return SkuSuggestion(
        target_sku_id=target,
        current_code=current_code,
        current_spec=current_spec or {"颜色": "红"},
        suggested_code=suggested_code,
        suggested_spec=suggested_spec or {"颜色": "红色"},
        reason="SKU 建议",
        evidence=[_citation()],
    )


def complete_legal_output(**updates: object) -> OptimizationProposalOutput:
    section = _section()
    values: dict[str, object] = {
        "title": "优选家居商品",
        "selling_points": ["红色家居设计"],
        "description": [section],
        "keywords": ["家居"],
        "attribute_completions": [_attribute()],
        "changes": [_title_change(), _description_change(suggested_value=[section])],
        "citations": [OutputCitation(chunk_id=RULE_CHUNK)],
        "price_suggestions": [_price()],
        "sku_suggestions": [],
    }
    values.update(updates)
    return OptimizationProposalOutput.model_validate(values)


def _codes(trusted: TrustedOptimizationInput, output: OptimizationProposalOutput) -> set[str]:
    return {violation.code for violation in validate_optimization_output(trusted, output).violations}


def test_valid_output_returns_only_canonical_citations_and_does_not_mutate_inputs() -> None:
    trusted = complete_trusted_input()
    output = complete_legal_output()
    trusted_before = trusted.model_copy(deep=True)
    output_before = output.model_copy(deep=True)

    result = validate_optimization_output(trusted, output)

    assert result.passed is True
    assert result.violations == ()
    assert result.canonical_citations == (trusted.canonical_rule_citations[0],)
    assert trusted == trusted_before
    assert output == output_before


def test_contract_models_forbid_extra_fields_at_trust_boundaries() -> None:
    with pytest.raises(ValidationError):
        OutputCitation(chunk_id=RULE_CHUNK, document_name="伪造元数据")
    with pytest.raises(ValidationError):
        DescriptionSection(heading="说明", body="正文", evidence=[_citation()], extra="no")
    with pytest.raises(ValidationError):
        CanonicalRuleCitation(
            document_id="document-1",
            version_id="version-1",
            chunk_id=RULE_CHUNK,
            document_name="通用规则",
            version_number=1,
            category="通用规则",
            canonical_text="正文",
            active=True,
            applicable=True,
            provider_value="untrusted",
        )
    with pytest.raises(ValidationError):
        complete_legal_output().model_validate({**complete_legal_output().model_dump(), "extra": "no"})


def test_every_internal_contract_model_forbids_extra_fields() -> None:
    trusted = complete_trusted_input()
    output = complete_legal_output()
    instances = [
        trusted.skus[0],
        trusted,
        _fact("product.title"),
        output.citations[0],
        output.description[0],
        output.changes[0],
        output.attribute_completions[0],
        output.price_suggestions[0],
        _sku(),
        output,
    ]

    for instance in instances:
        with pytest.raises(ValidationError):
            instance.__class__.model_validate({**instance.model_dump(), "extra": "no"})


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (complete_legal_output(title="English title"), "TITLE_LANGUAGE"),
        (complete_legal_output(title="治疗家居商品"), "RESTRICTED_PHRASE"),
        (complete_legal_output(title="中" * 61), "OUTPUT_BUSINESS_LENGTH"),
        (complete_legal_output(selling_points=[]), "OUTPUT_BUSINESS_LENGTH"),
        (complete_legal_output(keywords=["" ]), "OUTPUT_BUSINESS_LENGTH"),
        (complete_legal_output(description=[]), "OUTPUT_BUSINESS_LENGTH"),
    ],
)
def test_validator_enforces_title_and_output_business_limits(
    output: OptimizationProposalOutput, expected: str
) -> None:
    assert expected in _codes(complete_trusted_input(), output)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            complete_legal_output(
                description=[_section(heading="", body="正文")],
                changes=[_title_change(), _description_change(suggested_value=[_section(heading="", body="正文")])],
            ),
            "OUTPUT_BUSINESS_LENGTH",
        ),
        (
            complete_legal_output(
                description=[_section(heading="说明", body="")],
                changes=[_title_change(), _description_change(suggested_value=[_section(heading="说明", body="")])],
            ),
            "OUTPUT_BUSINESS_LENGTH",
        ),
        (
            complete_legal_output(
                changes=[
                    OptimizationChange(
                        field="title",
                        current_value="原商品标题",
                        suggested_value="优选家居商品",
                        reason="",
                        evidence=[_fact("product.title")],
                    ),
                    _description_change(),
                ]
            ),
            "OUTPUT_BUSINESS_LENGTH",
        ),
        (complete_legal_output(attribute_completions=[_attribute(value="")]), "OUTPUT_BUSINESS_LENGTH"),
        (complete_legal_output(sku_suggestions=[_sku(suggested_code="")]), "OUTPUT_BUSINESS_LENGTH"),
        (complete_legal_output(sku_suggestions=[_sku(suggested_spec={"": "值"})]), "OUTPUT_BUSINESS_LENGTH"),
        (complete_legal_output(description=[DescriptionSection(heading="说明", body="正文", evidence=[])]), "EVIDENCE_MISSING"),
    ],
)
def test_validator_enforces_nonempty_business_strings_and_evidence(
    output: OptimizationProposalOutput, expected: str
) -> None:
    assert expected in _codes(complete_trusted_input(), output)


@pytest.mark.parametrize(
    "output",
    [
        complete_legal_output(selling_points=["中" * 81]),
        complete_legal_output(keywords=["中" * 33]),
        complete_legal_output(
            description=[_section()] * 11,
            changes=[_title_change(), _description_change(suggested_value=[_section()] * 11)],
        ),
        complete_legal_output(
            changes=[
                _title_change().model_copy(update={"reason": "中" * 501}),
                _description_change(),
            ]
        ),
        complete_legal_output(
            attribute_completions=[_attribute().model_copy(update={"suggested_value": "中" * 257})]
        ),
        complete_legal_output(
            price_suggestions=[_price().model_copy(update={"reason": "中" * 501})]
        ),
        complete_legal_output(sku_suggestions=[_sku(suggested_spec={"规格": "中" * 129})]),
    ],
)
def test_validator_enforces_all_business_string_maximums(output: OptimizationProposalOutput) -> None:
    assert "OUTPUT_BUSINESS_LENGTH" in _codes(complete_trusted_input(), output)


@pytest.mark.parametrize(
    "output",
    [
        complete_legal_output(selling_points=["治疗"], changes=[_title_change(), _description_change()]),
        complete_legal_output(
            description=[_section(heading="治疗", body="日常使用")],
            changes=[_title_change(), _description_change(suggested_value=[_section(heading="治疗", body="日常使用")])],
        ),
        complete_legal_output(keywords=["治疗"], changes=[_title_change(), _description_change()]),
        complete_legal_output(
            changes=[_title_change().model_copy(update={"reason": "治疗"}), _description_change()]
        ),
        complete_legal_output(attribute_completions=[_attribute(value="治疗")]),
        complete_legal_output(price_suggestions=[_price().model_copy(update={"reason": "治疗"})]),
        complete_legal_output(sku_suggestions=[_sku(suggested_code="治疗SKU")]),
        complete_legal_output(sku_suggestions=[_sku(suggested_spec={"规格": "治疗"})]),
    ],
)
def test_validator_scans_every_human_facing_output_string(output: OptimizationProposalOutput) -> None:
    assert "RESTRICTED_PHRASE" in _codes(complete_trusted_input(), output)


def test_validator_rejects_missing_trusted_facts_and_keeps_failed_inputs_immutable() -> None:
    trusted = complete_trusted_input(title="", candidate_evidence=[])
    output = complete_legal_output()
    trusted_before = trusted.model_copy(deep=True)
    output_before = output.model_copy(deep=True)

    result = validate_optimization_output(trusted, output)

    assert "MISSING_TRUSTED_FACT" in {violation.code for violation in result.violations}
    assert trusted == trusted_before
    assert output == output_before


def test_validator_checks_declared_changes_against_snapshot_and_output() -> None:
    section = _section(body="建议详情")
    output = complete_legal_output(
        selling_points=["新的卖点"],
        changes=[
            _title_change(current_value="伪造当前标题"),
            _description_change(suggested_value=[section]),
        ],
        description=[_section()],
    )

    codes = _codes(complete_trusted_input(), output)

    assert {"CHANGE_CURRENT_MISMATCH", "CHANGE_SUGGESTED_MISMATCH", "OUTPUT_CHANGE_UNDECLARED"} <= codes


def test_validator_rejects_duplicate_change_targets() -> None:
    output = complete_legal_output(changes=[_title_change(), _title_change(), _description_change()])

    assert "CHANGE_TARGET_DUPLICATE" in _codes(complete_trusted_input(), output)


def test_validator_accepts_only_allowlisted_citations_and_linked_citation_evidence() -> None:
    trusted = complete_trusted_input(
        canonical_rule_citations=[
            complete_trusted_input().canonical_rule_citations[0],
            CanonicalRuleCitation(
                document_id="document-2",
                version_id="version-2",
                chunk_id="inactive-chunk",
                document_name="停用规则",
                version_number=2,
                category="通用规则",
                canonical_text="停用正文",
                active=False,
                applicable=True,
            ),
            CanonicalRuleCitation(
                document_id="document-3",
                version_id="version-3",
                chunk_id="inapplicable-chunk",
                document_name="不适用规则",
                version_number=3,
                category="其他",
                canonical_text="不适用正文",
                active=True,
                applicable=False,
            ),
        ]
    )
    output = complete_legal_output(
        citations=[
            OutputCitation(chunk_id=RULE_CHUNK),
            OutputCitation(chunk_id=RULE_CHUNK),
            OutputCitation(chunk_id="inactive-chunk"),
            OutputCitation(chunk_id="inapplicable-chunk"),
            OutputCitation(chunk_id="unknown-chunk"),
        ]
    )

    result = validate_optimization_output(trusted, output)

    assert {"CITATION_DUPLICATE", "CITATION_UNKNOWN"} <= {
        violation.code for violation in result.violations
    }
    assert result.canonical_citations == (trusted.canonical_rule_citations[0],)


def test_validator_rejects_unlisted_citation_evidence_and_invalid_fact_paths() -> None:
    output = complete_legal_output(
        citations=[],
        changes=[
            OptimizationChange(
                field="title",
                current_value="原商品标题",
                suggested_value="优选家居商品",
                reason="优化标题表达",
                evidence=[_fact("product.attributes.不存在")],
            ),
            _description_change(),
        ],
    )

    codes = _codes(complete_trusted_input(), output)

    assert {"EVIDENCE_FACT_PATH_INVALID", "CITATION_EVIDENCE_UNLISTED"} <= codes


def test_validator_requires_citation_source_for_new_attributes_and_rejects_duplicate_targets() -> None:
    output = complete_legal_output(
        attribute_completions=[
            _attribute(evidence=[_fact("product.title")]),
            _attribute(),
        ]
    )

    codes = _codes(complete_trusted_input(), output)

    assert {"ATTRIBUTE_SOURCE_MISSING", "ATTRIBUTE_TARGET_DUPLICATE"} <= codes


def test_validator_checks_existing_attribute_current_value() -> None:
    output = complete_legal_output(
        attribute_completions=[
            AttributeCompletion(
                target_attribute="材质",
                current_value="伪造材质",
                suggested_value="精梳棉",
                reason="补全材质",
                evidence=[_citation()],
            )
        ]
    )

    assert "ATTRIBUTE_CURRENT_MISMATCH" in _codes(complete_trusted_input(), output)


@pytest.mark.parametrize(
    ("suggestions", "expected"),
    [
        ([_sku(target="missing-sku")], "SKU_UNKNOWN"),
        ([_sku(), _sku()], "SKU_TARGET_DUPLICATE"),
        ([_sku(current_code="FORGED")], "SKU_CURRENT_MISMATCH"),
        ([_sku(current_spec={"颜色": "蓝"})], "SKU_CURRENT_MISMATCH"),
    ],
)
def test_validator_checks_sku_targets_and_current_snapshot(
    suggestions: list[SkuSuggestion], expected: str
) -> None:
    assert expected in _codes(complete_trusted_input(), complete_legal_output(sku_suggestions=suggestions))


@pytest.mark.parametrize(
    ("suggestions", "expected"),
    [
        ([_price(target="missing-sku")], "PRICE_SKU_UNKNOWN"),
        ([_price(), _price()], "PRICE_TARGET_DUPLICATE"),
        ([_price(current=Decimal("99.99"))], "PRICE_CURRENT_MISMATCH"),
        ([_price(suggested=Decimal("0.00"))], "PRICE_NONPOSITIVE"),
        ([_price(suggested=Decimal("70.001"))], "PRICE_PRECISION"),
        ([_price(suggested=Decimal("69.99"))], "PRICE_RANGE"),
        ([_price(suggested=Decimal("130.01"))], "PRICE_RANGE"),
    ],
)
def test_validator_checks_price_targets_precision_and_range(
    suggestions: list[PriceSuggestion], expected: str
) -> None:
    assert expected in _codes(complete_trusted_input(), complete_legal_output(price_suggestions=suggestions))


@pytest.mark.parametrize("price", [Decimal("70.00"), Decimal("130.00")])
def test_validator_accepts_inclusive_price_boundaries(price: Decimal) -> None:
    result = validate_optimization_output(
        complete_trusted_input(), complete_legal_output(price_suggestions=[_price(suggested=price)])
    )

    assert result.passed is True


def test_validator_returns_stable_price_range_for_finite_decimal_extremes() -> None:
    extreme = Decimal("1E+100000")
    suggested_result = validate_optimization_output(
        complete_trusted_input(),
        complete_legal_output(price_suggestions=[_price(suggested=extreme)]),
    )
    current_result = validate_optimization_output(
        complete_trusted_input(
            skus=[
                TrustedProductSku(
                    id="sku-1",
                    code="SKU-RED",
                    spec={"颜色": "红"},
                    price=extreme,
                    stock=10,
                )
            ]
        ),
        complete_legal_output(price_suggestions=[_price(current=extreme, suggested=extreme)]),
    )

    assert "PRICE_RANGE" in {violation.code for violation in suggested_result.violations}
    assert "PRICE_RANGE" in {violation.code for violation in current_result.violations}


def test_validator_rejects_positive_price_suggestion_when_current_price_is_zero() -> None:
    trusted = complete_trusted_input(
        skus=[
            TrustedProductSku(
                id="sku-1",
                code="SKU-RED",
                spec={"颜色": "红"},
                price=Decimal("0.00"),
                stock=10,
            )
        ]
    )

    assert "PRICE_CURRENT_NONPOSITIVE" in _codes(trusted, complete_legal_output())


def test_validator_requires_active_listed_citation_evidence_for_price_and_sku_suggestions() -> None:
    price = _price().model_copy(update={"evidence": [_fact("product.title")]})
    sku = _sku().model_copy(update={"evidence": [_fact("product.title")]})

    codes = _codes(
        complete_trusted_input(),
        complete_legal_output(price_suggestions=[price], sku_suggestions=[sku]),
    )

    assert "EVIDENCE_CITATION_INVALID" in codes


@pytest.mark.parametrize("quality", ["zero_hit", "low_confidence"])
def test_validator_rejects_insufficient_rag_quality(quality: str) -> None:
    assert "RAG_QUALITY_INSUFFICIENT" in _codes(
        complete_trusted_input(rag_quality=quality), complete_legal_output()
    )


def test_validator_applies_fixed_collection_limits_and_stable_violation_order() -> None:
    output = complete_legal_output(
        citations=[OutputCitation(chunk_id=RULE_CHUNK)] * 21,
        changes=[_title_change(), _description_change()] * 3,
        attribute_completions=[_attribute(target=f"属性{index}") for index in range(21)],
        price_suggestions=[_price() for _ in range(21)],
        sku_suggestions=[_sku() for _ in range(21)],
    )

    result = validate_optimization_output(complete_trusted_input(), output)

    assert "OUTPUT_BUSINESS_LENGTH" in {violation.code for violation in result.violations}
    assert [(violation.field, violation.code) for violation in result.violations] == sorted(
        (violation.field, violation.code) for violation in result.violations
    )


def test_validator_marks_unknown_and_inactive_duplicate_output_citations() -> None:
    inactive = CanonicalRuleCitation(
        document_id="document-2",
        version_id="version-2",
        chunk_id="inactive-chunk",
        document_name="停用规则",
        version_number=2,
        category="通用规则",
        canonical_text="停用正文",
        active=False,
        applicable=True,
    )
    trusted = complete_trusted_input(
        canonical_rule_citations=[complete_trusted_input().canonical_rule_citations[0], inactive]
    )
    output = complete_legal_output(
        citations=[
            OutputCitation(chunk_id=RULE_CHUNK),
            OutputCitation(chunk_id="unknown-chunk"),
            OutputCitation(chunk_id="unknown-chunk"),
            OutputCitation(chunk_id="inactive-chunk"),
            OutputCitation(chunk_id="inactive-chunk"),
        ]
    )

    result = validate_optimization_output(trusted, output)

    assert {"CITATION_UNKNOWN", "CITATION_DUPLICATE"} <= {
        violation.code for violation in result.violations
    }
    assert result.canonical_citations == (trusted.canonical_rule_citations[0],)


def test_validator_allows_empty_brand_when_other_trusted_facts_are_complete() -> None:
    result = validate_optimization_output(
        complete_trusted_input(brand=""), complete_legal_output()
    )

    assert result.passed is True


def test_validator_accepts_fact_paths_with_dotted_attribute_keys_and_sku_ids() -> None:
    trusted = complete_trusted_input(
        attributes={"尺寸.厘米": "10"},
        skus=[
            TrustedProductSku(
                id="sku.1",
                code="SKU-RED",
                spec={"颜色": "红"},
                price=Decimal("100.00"),
                stock=10,
            )
        ],
    )
    output = complete_legal_output(
        attribute_completions=[
            AttributeCompletion(
                target_attribute="尺寸.厘米",
                current_value="10",
                suggested_value="12",
                reason="补全尺寸",
                evidence=[_fact("product.attributes.尺寸.厘米")],
            )
        ],
        price_suggestions=[_price(target="sku.1")],
        sku_suggestions=[
            SkuSuggestion(
                target_sku_id="sku.1",
                current_code="SKU-RED",
                current_spec={"颜色": "红"},
                suggested_code="SKU-RED-NEW",
                suggested_spec={"颜色": "红色"},
                reason="SKU 建议",
                evidence=[_fact("product.skus.sku.1.code"), _citation()],
            )
        ],
    )

    assert validate_optimization_output(trusted, output).passed is True
