import json
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from backend.common import AgentCallType
from backend.config import Settings
from backend.optimization_agent import (
    OPTIMIZATION_PRIMARY_PROMPT,
    OPTIMIZATION_PROMPT_VERSION,
    OPTIMIZATION_SCHEMA_REPAIR_PROMPT,
    OptimizationAgentSchemaError,
    ProductOptimizationAgentClient,
    parse_optimization_response,
    validate_optimization_response,
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


def _fact(path: str) -> EvidenceRef:
    return EvidenceRef(kind="fact", value=path)


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


def legal_output(**updates: object) -> OptimizationProposalOutput:
    section = DescriptionSection(heading="商品说明", body="适合日常使用", evidence=[_citation()])
    values: dict[str, object] = {
        "title": "优选家居商品",
        "selling_points": ["红色家居设计"],
        "description": [section],
        "keywords": ["家居"],
        "attribute_completions": [
            AttributeCompletion(
                target_attribute="颜色",
                current_value=None,
                suggested_value="红色",
                reason="补全商品属性",
                evidence=[_citation()],
            )
        ],
        "changes": [
            OptimizationChange(
                field="title",
                current_value="原商品标题",
                suggested_value="优选家居商品",
                reason="优化标题表达",
                evidence=[_fact("product.title")],
            ),
            OptimizationChange(
                field="description",
                current_value="原始详情",
                suggested_value=[section],
                reason="优化详情表达",
                evidence=[_citation()],
            ),
        ],
        "citations": [OutputCitation(chunk_id=RULE_CHUNK)],
        "price_suggestions": [
            PriceSuggestion(
                target_sku_id="sku-1",
                current_price=Decimal("100.00"),
                suggested_price=Decimal("70.00"),
                reason="价格建议",
                evidence=[_citation()],
            )
        ],
        "sku_suggestions": [],
    }
    values.update(updates)
    return OptimizationProposalOutput.model_validate(values)


def semantic_change() -> ValidatedRequiredChange:
    return ValidatedRequiredChange(
        source_track="semantic",
        source_violation_code="EXAGGERATION",
        field="title",
        instruction="删除夸大表述",
        citation_chunk_ids=[RULE_CHUNK],
    )


def deterministic_change() -> ValidatedRequiredChange:
    return ValidatedRequiredChange(
        source_track="deterministic",
        source_violation_code="TITLE_LANGUAGE",
        field="title",
        instruction="标题补充中文字符",
        citation_chunk_ids=[],
    )


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


def test_optimization_prompts_declare_the_complete_json_contract() -> None:
    top_level_fields = (
        "title",
        "selling_points",
        "description",
        "keywords",
        "attribute_completions",
        "changes",
        "citations",
        "price_suggestions",
        "sku_suggestions",
    )
    nested_fields = (
        "heading",
        "body",
        "evidence",
        "kind",
        "value",
        "field",
        "current_value",
        "suggested_value",
        "reason",
        "target_attribute",
        "target_sku_id",
        "current_price",
        "suggested_price",
        "current_code",
        "current_spec",
        "suggested_code",
        "suggested_spec",
        "chunk_id",
    )
    change_fields = ("title", "selling_points", "description", "keywords")
    for prompt in (OPTIMIZATION_PRIMARY_PROMPT, OPTIMIZATION_SCHEMA_REPAIR_PROMPT):
        assert all(field in prompt for field in top_level_fields + nested_fields)
        assert all(field in prompt for field in change_fields)
        assert "changes.field 仅能为 title、selling_points、description、keywords" in prompt
        assert "evidence 元素必须是仅含 kind、value 的对象" in prompt
        assert all(
            rule in prompt
            for rule in (
                "JSON",
                "额外字段",
                "allowed_fact_paths",
                "allowed_rule_chunk_ids",
                "fact",
                "citation",
                "仅是建议",
            )
        )


async def test_optimization_client_uses_distinct_nodes_and_safe_equal_shape_payloads() -> None:
    trusted = trusted_input()
    posts: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/chat/completions"
        posts.append(json.loads(request.content))
        return _completion(legal_output().model_dump_json())

    client = ProductOptimizationAgentClient(_settings(), httpx.MockTransport(handler))
    primary = await client.request(
        trusted,
        required_changes=[semantic_change()],
        call_type=AgentCallType.PRIMARY,
        iteration=0,
    )
    repair = await client.request(
        trusted,
        required_changes=[deterministic_change()],
        call_type=AgentCallType.SCHEMA_REPAIR,
        iteration=1,
    )
    final_iteration = await client.request(
        trusted,
        required_changes=[semantic_change()],
        call_type=AgentCallType.PRIMARY,
        iteration=2,
    )

    assert all(invocation.response == legal_output() for invocation in (primary, repair, final_iteration))
    assert [record.node_name for record in (primary.records[0], repair.records[0])] == [
        "call_product_optimization_agent",
        "repair_product_optimization_schema",
    ]
    assert all(record.prompt_version == OPTIMIZATION_PROMPT_VERSION for record in primary.records + repair.records)
    assert all(record.iteration in {0, 1, 2} for record in primary.records + repair.records + final_iteration.records)
    assert all(record.call_type in {AgentCallType.PRIMARY, AgentCallType.SCHEMA_REPAIR} for record in primary.records + repair.records)
    assert all(
        not {"prompt", "payload", "raw_response", "headers", "authorization", "api_key", "key"}
        & set(vars(record))
        for record in primary.records + repair.records + final_iteration.records
    )

    primary_user = json.loads(posts[0]["messages"][1]["content"])
    repair_user = json.loads(posts[1]["messages"][1]["content"])
    assert set(primary_user) == set(repair_user) == {
        "iteration",
        "trusted_facts",
        "allowed_fact_paths",
        "allowed_rule_chunk_ids",
        "required_changes",
    }
    assert primary_user["trusted_facts"] == repair_user["trusted_facts"] == trusted.model_dump(mode="json")
    assert primary_user["allowed_rule_chunk_ids"] == repair_user["allowed_rule_chunk_ids"] == [RULE_CHUNK]
    assert "product.title" in primary_user["allowed_fact_paths"]
    assert "product.skus.sku-1.price" in primary_user["allowed_fact_paths"]
    assert primary_user["required_changes"] == [semantic_change().model_dump(mode="json")]
    assert repair_user["required_changes"] == [deterministic_change().model_dump(mode="json")]
    assert "建议" in posts[0]["messages"][0]["content"]
    assert "建议" in posts[1]["messages"][0]["content"]
    forbidden = {
        "storage_path",
        "path",
        "vector",
        "embedding",
        "authorization",
        "cookie",
        "api_key",
        "raw_response",
        "chain_of_thought",
    }
    assert not forbidden & _payload_keys(primary_user)


async def test_optimization_client_rejects_invalid_server_changes_and_iterations_before_post() -> None:
    posts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return _completion(legal_output().model_dump_json())

    client = ProductOptimizationAgentClient(_settings(), httpx.MockTransport(handler))
    trusted = trusted_input()
    unknown_citation = semantic_change().model_copy(update={"citation_chunk_ids": ["unknown"]})
    duplicate_citation = ValidatedRequiredChange.model_construct(
        source_track="semantic",
        source_violation_code="EXAGGERATION",
        field="title",
        instruction="删除夸大表述",
        citation_chunk_ids=[RULE_CHUNK, RULE_CHUNK],
    )
    mismatched_track = ValidatedRequiredChange.model_construct(
        source_track="semantic",
        source_violation_code="TITLE_LANGUAGE",
        field="title",
        instruction="标题补充中文字符",
        citation_chunk_ids=[],
    )

    for iteration in (-1, 3):
        with pytest.raises(ValueError):
            await client.request(
                trusted,
                required_changes=[],
                call_type=AgentCallType.PRIMARY,
                iteration=iteration,
            )
    for changes in ([{"source_track": "semantic"}], [unknown_citation], [duplicate_citation], [mismatched_track]):
        with pytest.raises(ValueError):
            await client.request(
                trusted,
                required_changes=changes,  # type: ignore[arg-type]
                call_type=AgentCallType.PRIMARY,
                iteration=0,
            )
    for payload in (
        {**semantic_change().model_dump(), "source_violation_code": "UNKNOWN"},
        {**semantic_change().model_dump(), "source_track": "unknown"},
        {**deterministic_change().model_dump(), "source_track": "semantic"},
        {**semantic_change().model_dump(), "extra": "no"},
    ):
        with pytest.raises(ValidationError):
            ValidatedRequiredChange.model_validate(payload)

    assert posts == 0


async def test_optimization_client_maps_schema_runtime_and_pre_http_failures_without_payload_leaks() -> None:
    trusted = trusted_input()
    posts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        if posts == 1:
            return _completion(json.dumps({**legal_output().model_dump(mode="json"), "forged": "no"}))
        return _completion("", status_code=429)

    client = ProductOptimizationAgentClient(_settings(), httpx.MockTransport(handler))
    invalid = await client.request(
        trusted,
        required_changes=[],
        call_type=AgentCallType.PRIMARY,
        iteration=0,
    )
    rate_limited = await client.request(
        trusted,
        required_changes=[],
        call_type=AgentCallType.PRIMARY,
        iteration=0,
        max_attempts=1,
    )

    async def lost_lease(_attempt: int) -> bool:
        return False

    lost = await ProductOptimizationAgentClient(_settings(), httpx.MockTransport(handler)).request(
        trusted,
        required_changes=[],
        call_type=AgentCallType.PRIMARY,
        iteration=0,
        before_http_attempt=lost_lease,
    )
    missing = await ProductOptimizationAgentClient(_settings(None), httpx.MockTransport(handler)).request(
        trusted,
        required_changes=[],
        call_type=AgentCallType.PRIMARY,
        iteration=0,
    )
    blank = await ProductOptimizationAgentClient(_settings("   "), httpx.MockTransport(handler)).request(
        trusted,
        required_changes=[],
        call_type=AgentCallType.PRIMARY,
        iteration=0,
    )

    assert (invalid.response, invalid.error_code, len(invalid.records)) == (None, "DEEPSEEK_SCHEMA_INVALID", 1)
    assert (rate_limited.response, rate_limited.error_code, len(rate_limited.records)) == (None, "DEEPSEEK_RATE_LIMIT", 1)
    assert (lost.error_code, missing.error_code, blank.error_code) == (
        "LEASE_LOST",
        "DEEPSEEK_KEY_MISSING",
        "DEEPSEEK_KEY_MISSING",
    )
    assert lost.records == missing.records == blank.records == []
    assert posts == 2


def test_optimization_response_parser_and_validator_keep_model_output_typed_and_allowlisted() -> None:
    raw_content = "raw-provider-content"
    with pytest.raises(OptimizationAgentSchemaError) as error:
        parse_optimization_response(raw_content)
    assert raw_content not in str(error.value)

    output = legal_output(citations=[OutputCitation(chunk_id="unknown")])
    with pytest.raises(OptimizationAgentSchemaError):
        validate_optimization_response(trusted_input(), [], output)
