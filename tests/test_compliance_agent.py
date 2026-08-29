import json
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from backend.common import AgentCallType, ComplianceRiskLevel
from backend.compliance_agent import (
    COMPLIANCE_PRIMARY_PROMPT,
    COMPLIANCE_PROMPT_VERSION,
    COMPLIANCE_SCHEMA_REPAIR_PROMPT,
    ComplianceAgentResponse,
    ComplianceAgentSchemaError,
    ComplianceSemanticViolation,
    ProductComplianceAgentClient,
    parse_compliance_response,
    validate_compliance_response,
)
from backend.config import Settings
from backend.optimization_validation import (
    DeterministicComplianceResult,
    DeterministicViolation,
)
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
    TrustedOptimizationInput,
    TrustedProductSku,
    ValidatedRequiredChange,
)


RULE_CHUNK = "rule-chunk-1"


def _payload_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_payload_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_payload_keys(item) for item in value)) if value else set()
    return set()


def _settings(api_key: str | None = "test-only-agent-token") -> Settings:
    return Settings(
        _env_file=None,
        jwt_secret_key=SecretStr("local-product-optimization-agent-test-secret"),
        deepseek_api_key=SecretStr(api_key) if api_key is not None else None,
        deepseek_base_url="https://mock.deepseek.invalid",
    )


def _citation() -> EvidenceRef:
    return EvidenceRef(kind="citation", value=RULE_CHUNK)


def trusted_input(**updates: object) -> TrustedOptimizationInput:
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


def legal_output() -> OptimizationProposalOutput:
    section = DescriptionSection(heading="商品说明", body="适合日常使用", evidence=[_citation()])
    return OptimizationProposalOutput(
        title="优选家居商品",
        selling_points=["红色家居设计"],
        description=[section],
        keywords=["家居"],
        attribute_completions=[
            AttributeCompletion(
                target_attribute="颜色",
                current_value=None,
                suggested_value="红色",
                reason="补全商品属性",
                evidence=[_citation()],
            )
        ],
        changes=[
            OptimizationChange(
                field="title",
                current_value="原商品标题",
                suggested_value="优选家居商品",
                reason="优化标题表达",
                evidence=[EvidenceRef(kind="fact", value="product.title")],
            ),
            OptimizationChange(
                field="description",
                current_value="原始详情",
                suggested_value=[section],
                reason="优化详情表达",
                evidence=[_citation()],
            ),
        ],
        citations=[OutputCitation(chunk_id=RULE_CHUNK)],
        price_suggestions=[
            PriceSuggestion(
                target_sku_id="sku-1",
                current_price=Decimal("100.00"),
                suggested_price=Decimal("70.00"),
                reason="价格建议",
                evidence=[_citation()],
            )
        ],
        sku_suggestions=[],
    )


def deterministic_result(passed: bool = True) -> DeterministicComplianceResult:
    return DeterministicComplianceResult(
        passed=passed,
        violations=() if passed else (DeterministicViolation("TITLE_LANGUAGE", "title", "标题必须包含中文字符"),),
        canonical_citations=(trusted_input().canonical_rule_citations[0],),
    )


def semantic_change(**updates: object) -> ValidatedRequiredChange:
    values: dict[str, object] = {
        "source_track": "semantic",
        "source_violation_code": "EXAGGERATION",
        "field": "title",
        "instruction": "删除夸大表述",
        "citation_chunk_ids": [RULE_CHUNK],
    }
    values.update(updates)
    return ValidatedRequiredChange.model_validate(values)


def semantic_violation(**updates: object) -> ComplianceSemanticViolation:
    values: dict[str, object] = {
        "code": "EXAGGERATION",
        "field": "title",
        "message_zh": "标题存在夸大表述",
        "citation_chunk_ids": [RULE_CHUNK],
    }
    values.update(updates)
    return ComplianceSemanticViolation.model_validate(values)


def failed_response(**updates: object) -> ComplianceAgentResponse:
    values: dict[str, object] = {
        "passed": False,
        "risk_level": ComplianceRiskLevel.MEDIUM,
        "violations": [semantic_violation()],
        "required_changes": [semantic_change()],
        "citations": [OutputCitation(chunk_id=RULE_CHUNK)],
        "confidence": Decimal("0.80"),
        "degraded": False,
    }
    values.update(updates)
    return ComplianceAgentResponse.model_validate(values)


def _completion(content: str, status_code: int = 200) -> httpx.Response:
    if status_code != 200:
        return httpx.Response(status_code)
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        },
    )


def test_compliance_prompts_declare_the_complete_semantic_json_contract() -> None:
    top_level_fields = (
        "passed",
        "risk_level",
        "violations",
        "required_changes",
        "citations",
        "confidence",
        "degraded",
    )
    nested_fields = (
        "code",
        "field",
        "message_zh",
        "citation_chunk_ids",
        "source_track",
        "source_violation_code",
        "instruction",
        "chunk_id",
    )
    semantic_codes = (
        "EXAGGERATION",
        "MEDICALIZATION",
        "MISLEADING",
        "SEMANTIC_CONTRADICTION",
        "UNPROVABLE_PROMISE",
        "INSUFFICIENT_EVIDENCE",
    )
    for prompt in (COMPLIANCE_PRIMARY_PROMPT, COMPLIANCE_SCHEMA_REPAIR_PROMPT):
        assert all(field in prompt for field in top_level_fields + nested_fields)
        assert all(code in prompt for code in semantic_codes)
        assert (
            "message_zh 与 instruction 必须包含中文，且不得含换行或任何 Unicode control character；"
            "instruction 必须简洁。"
        ) in prompt
        assert all(
            rule in prompt
            for rule in (
                "JSON",
                "额外字段",
                "semantic",
                "独立",
                "不得修改",
                "不得用语义结论覆盖 deterministic_result",
                "message_zh",
                "instruction",
                "包含中文",
                "换行",
                "Unicode control character",
                "简洁",
            )
        )


async def test_compliance_client_uses_independent_nodes_and_safe_equal_shape_payloads() -> None:
    trusted = trusted_input()
    posts: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return _completion(failed_response().model_dump_json())

    client = ProductComplianceAgentClient(_settings(), httpx.MockTransport(handler))
    primary = await client.request(
        legal_output(), deterministic_result(), trusted, call_type=AgentCallType.PRIMARY, iteration=0
    )
    repair = await client.request(
        legal_output(), deterministic_result(), trusted, call_type=AgentCallType.SCHEMA_REPAIR, iteration=1
    )
    final_iteration = await client.request(
        legal_output(), deterministic_result(), trusted, call_type=AgentCallType.PRIMARY, iteration=2
    )

    assert all(invocation.response == failed_response() for invocation in (primary, repair, final_iteration))
    assert [record.node_name for record in (primary.records[0], repair.records[0])] == [
        "call_product_compliance_agent",
        "repair_product_compliance_schema",
    ]
    assert all(record.prompt_version == COMPLIANCE_PROMPT_VERSION for record in primary.records + repair.records)
    assert all(record.iteration in {0, 1, 2} for record in primary.records + repair.records + final_iteration.records)
    assert all(
        not {"prompt", "payload", "raw_response", "headers", "authorization", "api_key", "key"}
        & set(vars(record))
        for record in primary.records + repair.records + final_iteration.records
    )

    primary_user = json.loads(posts[0]["messages"][1]["content"])
    repair_user = json.loads(posts[1]["messages"][1]["content"])
    assert set(primary_user) == set(repair_user) == {
        "iteration",
        "candidate_output",
        "deterministic_result",
        "trusted_facts",
        "canonical_citations",
    }
    assert primary_user["candidate_output"] == repair_user["candidate_output"] == legal_output().model_dump(mode="json")
    assert primary_user["trusted_facts"] == repair_user["trusted_facts"] == trusted.model_dump(mode="json")
    assert primary_user["canonical_citations"] == repair_user["canonical_citations"] == [
        trusted.canonical_rule_citations[0].model_dump(mode="json")
    ]
    assert "独立" in posts[0]["messages"][0]["content"]
    assert "独立" in posts[1]["messages"][0]["content"]
    assert not {
        "storage_path",
        "path",
        "vector",
        "embedding",
        "authorization",
        "cookie",
        "api_key",
        "raw_response",
        "chain_of_thought",
    } & _payload_keys(primary_user)


def test_compliance_validator_enforces_allowlists_consistency_and_independent_tracks() -> None:
    trusted = trusted_input()
    semantic_pass = ComplianceAgentResponse(
        passed=True,
        risk_level=ComplianceRiskLevel.LOW,
        violations=[],
        required_changes=[],
        citations=[],
        confidence=Decimal("0.80"),
        degraded=False,
    )
    deterministic_failure = deterministic_result(passed=False)
    response_before = semantic_pass.model_copy(deep=True)

    assert validate_compliance_response(trusted, semantic_pass) is semantic_pass
    assert semantic_pass.passed is True
    assert deterministic_failure.passed is False
    assert semantic_pass == response_before

    inactive = trusted_input(
        canonical_rule_citations=[
            trusted.canonical_rule_citations[0].model_copy(update={"active": False})
        ]
    )
    inapplicable = trusted_input(
        canonical_rule_citations=[
            trusted.canonical_rule_citations[0].model_copy(update={"applicable": False})
        ]
    )
    base = failed_response()
    invalid_responses = [
        base.model_copy(update={"citations": [OutputCitation(chunk_id="unknown")]}),
        base.model_copy(
            update={"citations": [OutputCitation(chunk_id=RULE_CHUNK), OutputCitation(chunk_id=RULE_CHUNK)]}
        ),
        base.model_copy(update={"required_changes": [semantic_change(citation_chunk_ids=["unknown"])]}),
        base.model_copy(update={"violations": [semantic_violation(citation_chunk_ids=["unknown"])]}),
        base.model_copy(
            update={
                "violations": [
                    semantic_violation().model_copy(
                        update={"citation_chunk_ids": [RULE_CHUNK, RULE_CHUNK]}
                    )
                ]
            }
        ),
        base.model_copy(
            update={
                "required_changes": [
                    semantic_change().model_copy(
                        update={"citation_chunk_ids": [RULE_CHUNK, RULE_CHUNK]}
                    )
                ]
            }
        ),
        base.model_copy(update={"required_changes": [semantic_change(field="description")]}),
        base.model_copy(
            update={"required_changes": [semantic_change().model_copy(update={"source_track": "deterministic"})]}
        ),
        base.model_copy(update={"violations": []}),
        base.model_copy(update={"passed": True}),
        base.model_copy(update={"degraded": True, "passed": True}),
    ]
    for response in invalid_responses:
        with pytest.raises(ComplianceAgentSchemaError):
            validate_compliance_response(trusted, response)
    with pytest.raises(ComplianceAgentSchemaError):
        validate_compliance_response(inactive, failed_response())
    with pytest.raises(ComplianceAgentSchemaError):
        validate_compliance_response(inapplicable, failed_response())

    citation_free = failed_response(
        violations=[semantic_violation(code="INSUFFICIENT_EVIDENCE", citation_chunk_ids=[])],
        required_changes=[
            semantic_change(
                source_violation_code="INSUFFICIENT_EVIDENCE", citation_chunk_ids=[]
            )
        ],
        citations=[],
    )
    assert validate_compliance_response(trusted, citation_free) is citation_free


@pytest.mark.parametrize(
    "message",
    ["", "English only", "中文\n换行", "中文\x00控制", "中" * 501],
    ids=["empty", "english", "newline", "control", "overlong"],
)
def test_compliance_provider_display_text_is_safe_and_errors_do_not_echo_it(message: str) -> None:
    raw = json.dumps(
        {
            **failed_response().model_dump(mode="json"),
            "violations": [{**semantic_violation().model_dump(), "message_zh": message}],
        }
    )
    with pytest.raises(ComplianceAgentSchemaError) as error:
        parse_compliance_response(raw)
    assert str(error.value).startswith("invalid compliance response: violations[0]")
    if message:
        assert message not in str(error.value)

    with pytest.raises(ValidationError):
        semantic_change(instruction=message)


@pytest.mark.parametrize(
    "instruction",
    ["", "English only", "中文\n换行", "中文\x00控制", "中" * 241],
    ids=["empty", "english", "newline", "control", "overlong"],
)
def test_compliance_parser_rejects_unsafe_required_change_instruction_without_echoing_it(
    instruction: str,
) -> None:
    payload = failed_response().model_dump(mode="json")
    payload["required_changes"][0]["instruction"] = instruction

    with pytest.raises(ComplianceAgentSchemaError) as error:
        parse_compliance_response(json.dumps(payload))

    assert str(error.value).startswith("invalid compliance response: required_changes[0]")
    if instruction:
        assert instruction not in str(error.value)


@pytest.mark.parametrize(
    ("content", "safe_location", "provider_text"),
    [
        (
            lambda: json.dumps(
                {
                    **failed_response().model_dump(mode="json"),
                    "violations": [
                        {
                            **semantic_violation().model_dump(),
                            "message_zh": "模型原文不得回显" * 100,
                        }
                    ],
                }
            ),
            "violations[0].message_zh",
            "模型原文不得回显",
        ),
        (
            lambda: json.dumps(
                {
                    **failed_response().model_dump(mode="json"),
                    "required_changes": [
                        {
                            **semantic_change().model_dump(),
                            "instruction": "模型指令不得回显" * 100,
                        }
                    ],
                }
            ),
            "required_changes[0].instruction",
            "模型指令不得回显",
        ),
        (
            lambda: json.dumps(
                {**failed_response().model_dump(mode="json"), "forged_provider_field": "不得回显"}
            ),
            "extra",
            "不得回显",
        ),
        (lambda: json.dumps(["provider-shape"]), "root", "provider-shape"),
        (lambda: "provider-not-json", "json", "provider-not-json"),
    ],
    ids=["violation-message", "required-instruction", "extra", "shape", "json"],
)
def test_compliance_parser_reports_only_safe_contract_locations(
    content, safe_location: str, provider_text: str
) -> None:
    with pytest.raises(ComplianceAgentSchemaError) as error:
        parse_compliance_response(content())

    assert str(error.value) == f"invalid compliance response: {safe_location}"
    assert provider_text not in str(error.value)


def test_compliance_models_reject_extra_fields_and_declared_caps() -> None:
    with pytest.raises(ValidationError):
        ComplianceSemanticViolation.model_validate({**semantic_violation().model_dump(), "extra": "no"})
    with pytest.raises(ValidationError):
        ComplianceAgentResponse.model_validate({**failed_response().model_dump(), "extra": "no"})
    with pytest.raises(ValidationError):
        semantic_violation(field="x" * 65)
    with pytest.raises(ValidationError):
        semantic_violation(citation_chunk_ids=[RULE_CHUNK] * 21)
    with pytest.raises(ValidationError):
        failed_response(citations=[OutputCitation(chunk_id=RULE_CHUNK)] * 21)


async def test_compliance_client_maps_runtime_failures_without_second_retry() -> None:
    trusted = trusted_input()
    posts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return _completion("", status_code=429)

    client = ProductComplianceAgentClient(_settings(), httpx.MockTransport(handler))
    for iteration in (-1, 3):
        with pytest.raises(ValueError):
            await client.request(
                legal_output(), deterministic_result(), trusted,
                call_type=AgentCallType.PRIMARY, iteration=iteration,
            )
    with pytest.raises(ValueError):
        await client.request(
            legal_output(), deterministic_result(), trusted,
            call_type="primary",  # type: ignore[arg-type]
            iteration=0,
        )
    rate_limited = await client.request(
        legal_output(), deterministic_result(), trusted,
        call_type=AgentCallType.PRIMARY, iteration=0, max_attempts=1,
    )

    async def lost_lease(_attempt: int) -> bool:
        return False

    lost = await ProductComplianceAgentClient(_settings(), httpx.MockTransport(handler)).request(
        legal_output(), deterministic_result(), trusted,
        call_type=AgentCallType.PRIMARY, iteration=0, before_http_attempt=lost_lease,
    )
    missing = await ProductComplianceAgentClient(_settings(None), httpx.MockTransport(handler)).request(
        legal_output(), deterministic_result(), trusted,
        call_type=AgentCallType.PRIMARY, iteration=0,
    )

    assert (rate_limited.response, rate_limited.error_code, len(rate_limited.records)) == (None, "DEEPSEEK_RATE_LIMIT", 1)
    assert (lost.error_code, missing.error_code) == ("LEASE_LOST", "DEEPSEEK_KEY_MISSING")
    assert lost.records == missing.records == []
    assert posts == 1
