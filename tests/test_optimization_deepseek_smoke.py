import os
from dataclasses import asdict
from decimal import Decimal

import pytest

from backend.common import AgentCallType
from backend.compliance_agent import (
    COMPLIANCE_PROMPT_VERSION,
    ComplianceAgentSchemaError,
    ProductComplianceAgentClient,
    validate_compliance_response,
)
from backend.config import get_settings
from backend.optimization_agent import (
    OPTIMIZATION_PROMPT_VERSION,
    OptimizationAgentSchemaError,
    ProductOptimizationAgentClient,
    validate_optimization_response,
)
from backend.optimization_validation import validate_optimization_output
from backend.schemas import (
    CanonicalRuleCitation,
    ProductMetrics,
    TrustedOptimizationInput,
    TrustedProductSku,
)


pytestmark = pytest.mark.deepseek_smoke

_RUNTIME_RECORD_ERROR_CODES = frozenset(
    {
        "DEEPSEEK_TIMEOUT",
        "DEEPSEEK_TRANSPORT",
        "DEEPSEEK_RATE_LIMIT",
        "DEEPSEEK_SERVER_ERROR",
        "DEEPSEEK_UNAUTHORIZED",
        "DEEPSEEK_FORBIDDEN",
        "DEEPSEEK_SCHEMA_INVALID",
        "DEEPSEEK_HTTP_ERROR",
    }
)


def _safe_runtime_error_category(error_code: str | None) -> str:
    return error_code if error_code in _RUNTIME_RECORD_ERROR_CODES else "UNKNOWN"


def one_primary_attempt_guard():
    calls = 0

    async def before_http_attempt(attempt: int) -> bool:
        nonlocal calls
        calls += 1
        return calls == 1 and attempt == 1

    return before_http_attempt, lambda: calls


@pytest.mark.skipif(
    os.getenv("RUN_DEEPSEEK_SMOKE") != "1",
    reason="explicit DeepSeek smoke authorization required",
)
async def test_explicit_optimization_and_compliance_primary_contract_smoke() -> None:
    settings = get_settings()
    assert settings.deepseek_model == "deepseek-v4-flash", "DEEPSEEK_SMOKE_MODEL_MISMATCH"
    key = settings.deepseek_api_key
    if key is None or not key.get_secret_value().strip():
        pytest.fail("DEEPSEEK_SMOKE_KEY_REQUIRED")

    metrics = ProductMetrics.from_totals(
        impressions=100,
        clicks=10,
        orders=1,
        units=1,
        revenue=Decimal("100.00"),
        refunds=0,
    ).model_copy(update={"product_id": "smoke-product-1", "product_code": "SMOKE-001"})
    trusted = TrustedOptimizationInput(
        store_id="smoke-store-1",
        product_id="smoke-product-1",
        base_product_version=1,
        title="家居收纳盒",
        category="家居",
        brand="演示品牌",
        selling_points=["分区收纳"],
        description="用于整理桌面小物件，便于分类收纳。",
        search_keywords=["收纳盒", "桌面整理"],
        attributes={"颜色": "红"},
        skus=[
            TrustedProductSku(
                id="smoke-sku-1",
                code="SMOKE-RED",
                spec={"颜色": "红"},
                price=Decimal("100.00"),
                stock=10,
            )
        ],
        candidate_metrics=metrics,
        candidate_evidence=["近30日点击10次，成交1件"],
        rag_quality="normal",
        canonical_rule_citations=[
            CanonicalRuleCitation(
                document_id="smoke-document-1",
                version_id="smoke-version-1",
                chunk_id="smoke-rule-chunk-1",
                document_name="通用商品规则",
                version_number=1,
                category="通用规则",
                canonical_text="商品宣传应如实描述，不得夸大效果。",
                active=True,
                applicable=True,
            )
        ],
    )
    optimization_guard, optimization_guard_calls = one_primary_attempt_guard()
    compliance_guard, compliance_guard_calls = one_primary_attempt_guard()
    optimization_client = ProductOptimizationAgentClient(settings)
    compliance_client = ProductComplianceAgentClient(settings)

    optimization = await optimization_client.request(
        trusted,
        required_changes=(),
        call_type=AgentCallType.PRIMARY,
        iteration=0,
        before_http_attempt=optimization_guard,
        max_attempts=1,
    )
    assert optimization_guard_calls() == 1
    assert len(optimization.records) == 1
    if optimization.response is None:
        print(
            "DEEPSEEK_SMOKE_RUNTIME_CATEGORY="
            f"{_safe_runtime_error_category(optimization.records[0].error_code)}"
        )
        pytest.fail("OPTIMIZATION_SMOKE_TYPED_RESPONSE_REQUIRED")
    try:
        output = validate_optimization_response(trusted, (), optimization.response)
        deterministic = validate_optimization_output(trusted, output)
    except OptimizationAgentSchemaError:
        raise AssertionError("OPTIMIZATION_SMOKE_CONTRACT_INVALID") from None

    optimization_record = optimization.records[0]
    assert optimization_record.node_name == "call_product_optimization_agent"
    assert optimization_record.call_type is AgentCallType.PRIMARY
    assert optimization_record.iteration == 0
    assert optimization_record.attempt == 1
    assert optimization_record.model == "deepseek-v4-flash"
    assert optimization_record.prompt_version == OPTIMIZATION_PROMPT_VERSION

    compliance = await compliance_client.request(
        output,
        deterministic,
        trusted,
        call_type=AgentCallType.PRIMARY,
        iteration=0,
        before_http_attempt=compliance_guard,
        max_attempts=1,
    )
    assert compliance_guard_calls() == 1
    assert len(compliance.records) == 1
    if compliance.response is None:
        print(
            "DEEPSEEK_SMOKE_RUNTIME_CATEGORY="
            f"{_safe_runtime_error_category(compliance.records[0].error_code)}"
        )
        pytest.fail("COMPLIANCE_SMOKE_TYPED_RESPONSE_REQUIRED")
    try:
        semantic = validate_compliance_response(trusted, compliance.response)
    except ComplianceAgentSchemaError:
        raise AssertionError("COMPLIANCE_SMOKE_CONTRACT_INVALID") from None

    compliance_record = compliance.records[0]
    assert compliance_record.node_name == "call_product_compliance_agent"
    assert compliance_record.call_type is AgentCallType.PRIMARY
    assert compliance_record.iteration == 0
    assert compliance_record.attempt == 1
    assert compliance_record.model == "deepseek-v4-flash"
    assert compliance_record.prompt_version == COMPLIANCE_PROMPT_VERSION
    combined_passed = deterministic.passed and semantic.passed and not semantic.degraded
    assert isinstance(combined_passed, bool)

    unsafe_keys = {
        "prompt",
        "payload",
        "raw_response",
        "header",
        "headers",
        "authorization",
        "api_key",
        "cookie",
        "chain_of_thought",
    }
    for record in (optimization_record, compliance_record):
        assert unsafe_keys.isdisjoint(asdict(record))
