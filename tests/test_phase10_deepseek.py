import json
import os
from decimal import Decimal
from typing import Any

import httpx
import pytest

from backend.analysis_agent import (
    AgentInvocation,
    AgentSchemaError,
    DeepSeekAnalysisClient,
    validate_agent_response,
)
from backend.common import AgentCallType, ComplianceRiskLevel
from backend.compliance_agent import (
    ComplianceAgentInvocation,
    ComplianceAgentResponse,
    ProductComplianceAgentClient,
    validate_compliance_response,
)
from backend.config import Settings, get_settings
from backend.optimization_agent import (
    OptimizationAgentInvocation,
    ProductOptimizationAgentClient,
    validate_optimization_response,
)
from backend.optimization_validation import validate_optimization_output
from backend.schemas import (
    AgentAnalysisResponse,
    AnalysisFacts,
    AttributeCompletion,
    CanonicalRuleCitation,
    DescriptionSection,
    EvidenceRef,
    OptimizationChange,
    OptimizationProposalOutput,
    OutputCitation,
    ProductMetrics,
    StoreMetrics,
    TrustedAnalysisCandidate,
    TrustedOptimizationInput,
    TrustedProductSku,
)


pytest_plugins = ("pytester",)

_SUMMARY_PREFIX = "PHASE10_DEEPSEEK_SUMMARY="
_SAFE_ERROR_CODES = frozenset(
    {
        "DEEPSEEK_KEY_MISSING",
        "LEASE_LOST",
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
_SYNTHETIC_INPUT_TEXT = (
    "phase10-smoke-store",
    "phase10-smoke-product",
    "phase10-smoke-sku",
    "phase10-smoke-rule-chunk",
    "合成桌面收纳盒",
    "用于整理桌面小物件，便于分类收纳。",
    "商品宣传应如实描述，不得夸大效果。",
)


def _enabled() -> bool:
    return os.getenv("RUN_PHASE10_DEEPSEEK") == "1"


def _settings_or_fail() -> Settings:
    settings = get_settings()
    if settings.deepseek_model != "deepseek-v4-flash":
        pytest.fail("DEEPSEEK_SMOKE_MODEL_MISMATCH", pytrace=False)
    if settings.deepseek_base_url != "https://api.deepseek.com":
        pytest.fail("DEEPSEEK_SMOKE_BASE_URL_MISMATCH", pytrace=False)
    if settings.deepseek_api_key is None or not settings.deepseek_api_key.get_secret_value().strip():
        pytest.fail("DEEPSEEK_SMOKE_KEY_REQUIRED", pytrace=False)
    return settings


def two_attempt_guard():
    calls = 0

    async def before_http_attempt(*_args: object) -> bool:
        nonlocal calls
        calls += 1
        return calls <= 2

    return before_http_attempt, lambda: calls


def safe_record(agent_type: str, record: Any) -> dict[str, object]:
    return {
        "agent_type": agent_type,
        "model": record.model,
        "prompt_version": record.prompt_version,
        "call_type": record.call_type.value,
        "attempt": record.attempt,
        "duration_ms": record.duration_ms,
        "prompt_tokens": record.prompt_tokens,
        "completion_tokens": record.completion_tokens,
        "total_tokens": record.total_tokens,
        "estimated_cost": None if record.estimated_cost is None else str(record.estimated_cost),
        "error_code": record.error_code,
    }


def _summary_line(agent_type: str, records: list[Any], api_key: str | None = None) -> str:
    payload = {"records": [safe_record(agent_type, record) for record in records]}
    line = _SUMMARY_PREFIX + json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    lowered = line.lower()
    if any(
        forbidden in lowered
        for forbidden in (
            '"authorization":',
            '"api_key":',
            '"key":',
            '"prompt":',
            '"payload":',
            '"raw_response":',
            '"traceback":',
        )
    ):
        pytest.fail("PHASE10_SUMMARY_FIELD_REJECTED", pytrace=False)
    if any(value in line for value in _SYNTHETIC_INPUT_TEXT):
        pytest.fail("PHASE10_SUMMARY_INPUT_REJECTED", pytrace=False)
    if api_key and api_key in line:
        pytest.fail("PHASE10_SUMMARY_SECRET_REJECTED", pytrace=False)
    return line


def _safe_error_code(error_code: str | None) -> str:
    return error_code if error_code in _SAFE_ERROR_CODES else "UNKNOWN"


def _analysis_facts() -> AnalysisFacts:
    metrics = ProductMetrics.from_totals(
        impressions=100,
        clicks=10,
        orders=1,
        units=1,
        revenue=Decimal("100.00"),
        refunds=0,
    ).model_copy(
        update={
            "product_id": "phase10-smoke-product-1",
            "product_code": "phase10-smoke-product-code-1",
        }
    )
    return AnalysisFacts(
        store_summary=StoreMetrics(
            store_id="phase10-smoke-store-1",
            impressions=100,
            clicks=10,
            orders=1,
            units=1,
            revenue=Decimal("100.00"),
            refunds=0,
            ctr=Decimal("0.1000"),
            conversion_rate=Decimal("0.1000"),
            refund_rate=Decimal("0.0000"),
            average_order_value=Decimal("100.0000"),
        ),
        candidates=[
            TrustedAnalysisCandidate(
                product_id="phase10-smoke-product-1",
                product_code="phase10-smoke-product-code-1",
                anomaly_types=["低转化"],
                metrics=metrics,
                business_impact=Decimal("100.00"),
                evidence=["近30日点击10次，成交1件"],
            )
        ],
    )


def _trusted_optimization_input() -> TrustedOptimizationInput:
    metrics = ProductMetrics.from_totals(
        impressions=100,
        clicks=10,
        orders=1,
        units=1,
        revenue=Decimal("100.00"),
        refunds=0,
    ).model_copy(
        update={
            "product_id": "phase10-smoke-product-1",
            "product_code": "phase10-smoke-product-code-1",
        }
    )
    return TrustedOptimizationInput(
        store_id="phase10-smoke-store-1",
        product_id="phase10-smoke-product-1",
        base_product_version=1,
        title="合成桌面收纳盒",
        category="家居",
        brand="演示品牌",
        selling_points=["分区收纳"],
        description="用于整理桌面小物件，便于分类收纳。",
        search_keywords=["收纳盒", "桌面整理"],
        attributes={"材质": "棉"},
        skus=[
            TrustedProductSku(
                id="phase10-smoke-sku-1",
                code="PHASE10-SMOKE-RED",
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
                document_id="phase10-smoke-document-1",
                version_id="phase10-smoke-version-1",
                chunk_id="phase10-smoke-rule-chunk-1",
                document_name="通用商品规则",
                version_number=1,
                category="通用规则",
                canonical_text="商品宣传应如实描述，不得夸大效果。",
                active=True,
                applicable=True,
            )
        ],
    )


def _valid_analysis_response(facts: AnalysisFacts) -> AgentAnalysisResponse:
    return AgentAnalysisResponse.model_validate(
        {
            "candidates": [
                {
                    "product_id": candidate.product_id,
                    "rank": rank,
                    "impact_explanation": "成交表现偏低",
                    "reason": "点击后成交不足",
                    "recommended_action": "核验标题与详情",
                    "confidence": "0.8",
                }
                for rank, candidate in enumerate(facts.candidates, start=1)
            ]
        }
    )


def _valid_optimization_output() -> OptimizationProposalOutput:
    citation = EvidenceRef(kind="citation", value="phase10-smoke-rule-chunk-1")
    section = DescriptionSection(
        heading="商品说明",
        body="适合日常桌面分类收纳",
        evidence=[citation],
    )
    return OptimizationProposalOutput(
        title="桌面分区收纳盒",
        selling_points=["分区收纳"],
        description=[section],
        keywords=["收纳盒", "桌面整理"],
        attribute_completions=[
            AttributeCompletion(
                target_attribute="颜色",
                current_value=None,
                suggested_value="红色",
                reason="补全商品属性",
                evidence=[citation],
            )
        ],
        changes=[
            OptimizationChange(
                field="title",
                current_value="合成桌面收纳盒",
                suggested_value="桌面分区收纳盒",
                reason="明确商品用途",
                evidence=[EvidenceRef(kind="fact", value="product.title")],
            ),
            OptimizationChange(
                field="description",
                current_value="用于整理桌面小物件，便于分类收纳。",
                suggested_value=[section],
                reason="整理详情结构",
                evidence=[citation],
            ),
        ],
        citations=[OutputCitation(chunk_id="phase10-smoke-rule-chunk-1")],
        price_suggestions=[],
        sku_suggestions=[],
    )


def _valid_compliance_response() -> ComplianceAgentResponse:
    return ComplianceAgentResponse(
        passed=True,
        risk_level=ComplianceRiskLevel.LOW,
        violations=[],
        required_changes=[],
        citations=[OutputCitation(chunk_id="phase10-smoke-rule-chunk-1")],
        confidence=Decimal("0.9"),
        degraded=False,
    )


def _completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        },
    )


def _mock_settings() -> Settings:
    return Settings(
        _env_file=None,
        jwt_secret_key="phase10-test-secret-at-least-32-characters",
        deepseek_api_key="phase10-test-key",
        deepseek_base_url="https://mock.deepseek.invalid",
    )


async def _request_analysis(
    client: DeepSeekAnalysisClient, facts: AnalysisFacts
) -> tuple[AgentInvocation, int]:
    guard, call_count = two_attempt_guard()
    records = []
    error_code: str | None = None
    for call_type in (AgentCallType.PRIMARY, AgentCallType.SCHEMA_REPAIR):
        invocation = await client.request(
            facts,
            call_type=call_type,
            before_http_attempt=guard,
            max_attempts=1,
        )
        records.extend(invocation.records)
        error_code = invocation.error_code
        if invocation.response is not None:
            try:
                validate_agent_response(facts, invocation.response)
            except AgentSchemaError:
                error_code = "DEEPSEEK_SCHEMA_INVALID"
            else:
                return AgentInvocation(invocation.response, records, None), call_count()
        if call_type is AgentCallType.PRIMARY and error_code == "DEEPSEEK_SCHEMA_INVALID":
            continue
        break
    return AgentInvocation(None, records, error_code), call_count()


async def _request_optimization(
    client: ProductOptimizationAgentClient, trusted: TrustedOptimizationInput
) -> tuple[OptimizationAgentInvocation, int]:
    guard, call_count = two_attempt_guard()
    records = []
    invocation: OptimizationAgentInvocation | None = None
    for call_type in (AgentCallType.PRIMARY, AgentCallType.SCHEMA_REPAIR):
        invocation = await client.request(
            trusted,
            required_changes=(),
            call_type=call_type,
            iteration=0,
            before_http_attempt=guard,
            max_attempts=1,
        )
        records.extend(invocation.records)
        if invocation.response is not None or invocation.error_code != "DEEPSEEK_SCHEMA_INVALID":
            return OptimizationAgentInvocation(
                invocation.response, records, invocation.error_code
            ), call_count()
    assert invocation is not None
    return OptimizationAgentInvocation(None, records, invocation.error_code), call_count()


async def _request_compliance(
    client: ProductComplianceAgentClient,
    proposal: OptimizationProposalOutput,
    trusted: TrustedOptimizationInput,
) -> tuple[ComplianceAgentInvocation, int]:
    deterministic = validate_optimization_output(trusted, proposal)
    guard, call_count = two_attempt_guard()
    records = []
    invocation: ComplianceAgentInvocation | None = None
    for call_type in (AgentCallType.PRIMARY, AgentCallType.SCHEMA_REPAIR):
        invocation = await client.request(
            proposal,
            deterministic,
            trusted,
            call_type=call_type,
            iteration=0,
            before_http_attempt=guard,
            max_attempts=1,
        )
        records.extend(invocation.records)
        if invocation.response is not None or invocation.error_code != "DEEPSEEK_SCHEMA_INVALID":
            return ComplianceAgentInvocation(
                invocation.response, records, invocation.error_code
            ), call_count()
    assert invocation is not None
    return ComplianceAgentInvocation(None, records, invocation.error_code), call_count()


def test_phase10_live_tests_skip_without_opt_in(pytester, monkeypatch) -> None:
    monkeypatch.delenv("RUN_PHASE10_DEEPSEEK", raising=False)

    class ForbiddenAsyncClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("OFFLINE_HTTP_CLIENT_CONSTRUCTED")

    monkeypatch.setattr(httpx, "AsyncClient", ForbiddenAsyncClient)
    result = pytester.runpytest_inprocess(
        __file__,
        "-m",
        "phase10_deepseek",
        "-q",
        "--tb=short",
        "--override-ini=asyncio_default_fixture_loop_scope=function",
        "-W",
        "ignore::pytest.PytestDeprecationWarning",
    )
    result.assert_outcomes(skipped=3)


async def test_analysis_attempt_cap_preserves_safe_runtime_error() -> None:
    posts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return httpx.Response(500)

    guard, call_count = two_attempt_guard()
    result = await DeepSeekAnalysisClient(
        _mock_settings(), httpx.MockTransport(handler)
    ).request(
        _analysis_facts(),
        call_type=AgentCallType.PRIMARY,
        before_http_attempt=guard,
        max_attempts=1,
    )

    assert posts == call_count() == len(result.records) == 1
    assert result.error_code == "DEEPSEEK_SERVER_ERROR"


async def test_analysis_gate_repairs_one_validator_failure() -> None:
    facts = _analysis_facts()
    posts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        if posts == 1:
            invalid = _valid_analysis_response(facts).model_copy(deep=True)
            invalid.candidates[0].product_id = "phase10-smoke-untrusted-product"
            return _completion(invalid.model_dump_json())
        return _completion(_valid_analysis_response(facts).model_dump_json())

    result, guard_calls = await _request_analysis(
        DeepSeekAnalysisClient(_mock_settings(), httpx.MockTransport(handler)), facts
    )

    assert posts == guard_calls == len(result.records) == 2
    assert [record.call_type for record in result.records] == [
        AgentCallType.PRIMARY,
        AgentCallType.SCHEMA_REPAIR,
    ]
    assert result.response is not None
    validate_agent_response(facts, result.response)


async def test_optimization_gate_repairs_one_schema_failure() -> None:
    trusted = _trusted_optimization_input()
    posts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        content = "not-json" if posts == 1 else _valid_optimization_output().model_dump_json()
        return _completion(content)

    result, guard_calls = await _request_optimization(
        ProductOptimizationAgentClient(_mock_settings(), httpx.MockTransport(handler)),
        trusted,
    )

    assert posts == guard_calls == len(result.records) == 2
    assert [record.call_type for record in result.records] == [
        AgentCallType.PRIMARY,
        AgentCallType.SCHEMA_REPAIR,
    ]
    assert result.response is not None
    validate_optimization_response(trusted, (), result.response)
    assert validate_optimization_output(trusted, result.response).passed


async def test_compliance_gate_stops_after_one_invalid_repair() -> None:
    trusted = _trusted_optimization_input()
    posts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return _completion("not-json")

    result, guard_calls = await _request_compliance(
        ProductComplianceAgentClient(_mock_settings(), httpx.MockTransport(handler)),
        _valid_optimization_output(),
        trusted,
    )

    assert posts == guard_calls == len(result.records) == 2
    assert [record.call_type for record in result.records] == [
        AgentCallType.PRIMARY,
        AgentCallType.SCHEMA_REPAIR,
    ]
    assert result.response is None
    assert result.error_code == "DEEPSEEK_SCHEMA_INVALID"
    assert _safe_error_code(result.error_code) == "DEEPSEEK_SCHEMA_INVALID"
    assert _safe_error_code("provider said secret value") == "UNKNOWN"


async def test_success_summary_is_canonical_and_allowlisted() -> None:
    facts = _analysis_facts()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return _completion(_valid_analysis_response(facts).model_dump_json())

    result, _ = await _request_analysis(
        DeepSeekAnalysisClient(_mock_settings(), httpx.MockTransport(handler)), facts
    )
    line = _summary_line("analysis", result.records, "phase10-test-key")

    serialized = line.removeprefix(_SUMMARY_PREFIX)
    assert serialized == json.dumps(
        json.loads(serialized),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    assert set(json.loads(serialized)) == {"records"}
    assert set(json.loads(serialized)["records"][0]) == {
        "agent_type",
        "model",
        "prompt_version",
        "call_type",
        "attempt",
        "duration_ms",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "estimated_cost",
        "error_code",
    }


@pytest.mark.phase10_deepseek
@pytest.mark.skipif(
    not _enabled(), reason="explicit Phase 10 DeepSeek opt-in required"
)
async def test_live_phase10_analysis_contract() -> None:
    settings = _settings_or_fail()
    facts = _analysis_facts()
    result, attempts = await _request_analysis(DeepSeekAnalysisClient(settings), facts)
    if result.response is None:
        pytest.fail(
            f"PHASE10_ANALYSIS_{_safe_error_code(result.error_code)}", pytrace=False
        )
    if not 1 <= attempts == len(result.records) <= 2:
        pytest.fail("PHASE10_ANALYSIS_ATTEMPT_CEILING_INVALID", pytrace=False)
    try:
        validate_agent_response(facts, result.response)
    except AgentSchemaError:
        pytest.fail("PHASE10_ANALYSIS_CONTRACT_INVALID", pytrace=False)
    key = settings.deepseek_api_key
    print(_summary_line("analysis", result.records, key.get_secret_value() if key else None))


@pytest.mark.phase10_deepseek
@pytest.mark.skipif(
    not _enabled(), reason="explicit Phase 10 DeepSeek opt-in required"
)
async def test_live_phase10_optimization_contract() -> None:
    settings = _settings_or_fail()
    trusted = _trusted_optimization_input()
    result, attempts = await _request_optimization(
        ProductOptimizationAgentClient(settings), trusted
    )
    if result.response is None:
        pytest.fail(
            f"PHASE10_OPTIMIZATION_{_safe_error_code(result.error_code)}",
            pytrace=False,
        )
    if not 1 <= attempts == len(result.records) <= 2:
        pytest.fail("PHASE10_OPTIMIZATION_ATTEMPT_CEILING_INVALID", pytrace=False)
    try:
        output = validate_optimization_response(trusted, (), result.response)
    except ValueError:
        pytest.fail("PHASE10_OPTIMIZATION_CONTRACT_INVALID", pytrace=False)
    if not validate_optimization_output(trusted, output).passed:
        pytest.fail("PHASE10_OPTIMIZATION_BUSINESS_INVALID", pytrace=False)
    key = settings.deepseek_api_key
    print(_summary_line("optimization", result.records, key.get_secret_value() if key else None))


@pytest.mark.phase10_deepseek
@pytest.mark.skipif(
    not _enabled(), reason="explicit Phase 10 DeepSeek opt-in required"
)
async def test_live_phase10_compliance_contract() -> None:
    settings = _settings_or_fail()
    trusted = _trusted_optimization_input()
    proposal = _valid_optimization_output()
    if not validate_optimization_output(trusted, proposal).passed:
        pytest.fail("PHASE10_COMPLIANCE_FIXTURE_INVALID", pytrace=False)
    result, attempts = await _request_compliance(
        ProductComplianceAgentClient(settings), proposal, trusted
    )
    if result.response is None:
        pytest.fail(
            f"PHASE10_COMPLIANCE_{_safe_error_code(result.error_code)}", pytrace=False
        )
    if not 1 <= attempts == len(result.records) <= 2:
        pytest.fail("PHASE10_COMPLIANCE_ATTEMPT_CEILING_INVALID", pytrace=False)
    try:
        validate_compliance_response(trusted, result.response)
    except ValueError:
        pytest.fail("PHASE10_COMPLIANCE_CONTRACT_INVALID", pytrace=False)
    key = settings.deepseek_api_key
    print(_summary_line("compliance", result.records, key.get_secret_value() if key else None))
