import json
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

import backend.analysis_agent as analysis_agent
from backend.analysis_agent import (
    AgentSchemaError,
    DeepSeekAnalysisClient,
    build_degraded_drafts,
    collect_analysis_facts,
    parse_agent_response,
    validate_agent_response,
)
from backend.common import AgentCallType
from backend.config import Settings, get_settings
from backend.models import Store
from backend.schemas import AnalysisFacts, ProductMetrics, StoreMetrics, TrustedAnalysisCandidate
from backend.seed import seed_demo_data


def make_facts(count: int = 2) -> AnalysisFacts:
    candidates = []
    for rank in range(1, count + 1):
        product_id = f"product-{rank}"
        product_code = f"PRODUCT-{rank:03d}"
        candidates.append(
            TrustedAnalysisCandidate(
                product_id=product_id,
                product_code=product_code,
                anomaly_types=["low_conversion"],
                metrics=ProductMetrics(
                    product_id=product_id,
                    product_code=product_code,
                    impressions=100,
                    clicks=10,
                    orders=1,
                    units=1,
                    revenue=Decimal("10.00"),
                    refunds=0,
                    ctr=Decimal("0.1000"),
                    conversion_rate=Decimal("0.1000"),
                    refund_rate=Decimal("0.0000"),
                    average_order_value=Decimal("10.0000"),
                ),
                business_impact=Decimal("10.00"),
                evidence=["clicks=10"],
            )
        )
    return AnalysisFacts(
        store_summary=StoreMetrics(
            store_id="store-1",
            impressions=100,
            clicks=10,
            orders=1,
            units=1,
            revenue=Decimal("10.00"),
            refunds=0,
            ctr=Decimal("0.1000"),
            conversion_rate=Decimal("0.1000"),
            refund_rate=Decimal("0.0000"),
            average_order_value=Decimal("10.0000"),
        ),
        candidates=candidates,
    )


def response_content(facts: AnalysisFacts, **first_changes: object) -> str:
    candidates = [
        {
            "product_id": candidate.product_id,
            "rank": rank,
            "impact_explanation": f"影响说明 {rank}",
            "reason": f"原因 {rank}",
            "recommended_action": f"建议 {rank}",
            "confidence": "0.8000",
        }
        for rank, candidate in enumerate(facts.candidates, start=1)
    ]
    candidates[0].update(first_changes)
    return json.dumps({"candidates": candidates})


def completion(content: str, *, total_tokens: int = 8) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": total_tokens - 3,
                "total_tokens": total_tokens,
            },
        },
    )


def client(
    transport: httpx.AsyncBaseTransport,
    *,
    api_key: str | None = "mock-key",
    price: Decimal | None = None,
) -> DeepSeekAnalysisClient:
    return DeepSeekAnalysisClient(
        Settings(
            _env_file=None,
            jwt_secret_key="test-only-secret-at-least-32-characters",
            deepseek_api_key=api_key,
            deepseek_base_url="https://mock.deepseek.invalid",
            deepseek_price_per_million_tokens=price,
        ),
        transport=transport,
    )


async def test_collect_facts_calls_all_tools_and_preserves_real_server_values(session, monkeypatch) -> None:
    await seed_demo_data(session)
    store_id = await session.scalar(select(Store.id).where(Store.code == "flagship"))
    assert store_id is not None
    start_date, end_date = date(2026, 7, 26), date(2026, 8, 24)
    expected_summary = await analysis_agent.get_store_summary(session, store_id, start_date, end_date)
    expected_candidates = await analysis_agent.find_anomalous_products(
        session, store_id, start_date, end_date, limit=5
    )

    summary = AsyncMock(wraps=analysis_agent.get_store_summary)
    anomalies = AsyncMock(wraps=analysis_agent.find_anomalous_products)
    product_metrics = AsyncMock(wraps=analysis_agent.get_product_metrics)
    comparison = AsyncMock(wraps=analysis_agent.compare_store_products)
    inventory = AsyncMock(wraps=analysis_agent.get_inventory_risk)
    monkeypatch.setattr(analysis_agent, "get_store_summary", summary)
    monkeypatch.setattr(analysis_agent, "find_anomalous_products", anomalies)
    monkeypatch.setattr(analysis_agent, "get_product_metrics", product_metrics)
    monkeypatch.setattr(analysis_agent, "compare_store_products", comparison)
    monkeypatch.setattr(analysis_agent, "get_inventory_risk", inventory)

    facts = await collect_analysis_facts(session, store_id, start_date, end_date)

    summary.assert_awaited_once_with(session, store_id, start_date, end_date)
    anomalies.assert_awaited_once_with(session, store_id, start_date, end_date, limit=5)
    assert product_metrics.await_count == 5
    assert comparison.await_count == 1
    assert inventory.await_count == 5
    assert facts.store_summary == expected_summary
    assert [candidate.product_id for candidate in facts.candidates] == [
        candidate.product_id for candidate in expected_candidates
    ]
    assert [candidate.product_code for candidate in facts.candidates] == [
        candidate.product_code for candidate in expected_candidates
    ]
    assert [candidate.metrics for candidate in facts.candidates] == [
        candidate.metrics for candidate in expected_candidates
    ]
    assert [candidate.anomaly_types for candidate in facts.candidates] == [
        list(candidate.anomaly_types) for candidate in expected_candidates
    ]
    assert [candidate.business_impact for candidate in facts.candidates] == [
        candidate.business_impact for candidate in expected_candidates
    ]
    assert [candidate.evidence for candidate in facts.candidates] == [
        candidate.evidence for candidate in expected_candidates
    ]


async def test_collect_facts_rejects_inconsistent_tool_results(session, monkeypatch) -> None:
    await seed_demo_data(session)
    store_id = await session.scalar(select(Store.id).where(Store.code == "flagship"))
    assert store_id is not None

    async def inconsistent_comparison(*_args, **_kwargs) -> list[ProductMetrics]:
        return []

    monkeypatch.setattr(analysis_agent, "compare_store_products", inconsistent_comparison)

    with pytest.raises(ValueError, match="comparison"):
        await collect_analysis_facts(session, store_id, date(2026, 7, 26), date(2026, 8, 24))


def test_parse_response_rejects_invalid_json_schema_and_confidence_bounds() -> None:
    facts = make_facts()
    invalid_contents = [
        "not-json",
        response_content(facts, unexpected_fact="forged"),
        response_content(facts, confidence="-0.0001"),
        response_content(facts, confidence="1.0001"),
    ]

    for content in invalid_contents:
        with pytest.raises(AgentSchemaError):
            parse_agent_response(content)


def test_parse_response_does_not_chain_raw_model_content_into_the_safe_error() -> None:
    raw_content = "not-json-model-content"

    with pytest.raises(AgentSchemaError) as error:
        parse_agent_response(raw_content)

    assert str(error.value) == "invalid agent response"
    assert error.value.__cause__ is None


def test_validate_response_accepts_only_model_explanations_for_exact_trusted_set() -> None:
    facts = make_facts()
    response = parse_agent_response(response_content(facts))

    drafts = validate_agent_response(facts, response)

    assert [draft.product_id for draft in drafts] == [candidate.product_id for candidate in facts.candidates]
    assert [draft.rank for draft in drafts] == [1, 2]
    assert set(drafts[0].model_dump()) == {
        "product_id",
        "rank",
        "impact_explanation",
        "reason",
        "recommended_action",
        "confidence",
    }
    trusted = facts.candidates[0]
    assert trusted.product_code == "PRODUCT-001"
    assert trusted.metrics.product_code == "PRODUCT-001"
    assert trusted.anomaly_types == ["low_conversion"]
    assert trusted.business_impact == Decimal("10.00")
    assert trusted.evidence == ["clicks=10"]


@pytest.mark.parametrize(
    "changes",
    [
        {"product_id": "unknown"},
        {"product_id": "product-2"},
        {"rank": 2},
        {"rank": 3},
    ],
)
def test_validate_response_rejects_trusted_set_or_rank_mismatches(changes: dict[str, object]) -> None:
    facts = make_facts()
    response = parse_agent_response(response_content(facts, **changes))

    with pytest.raises(AgentSchemaError):
        validate_agent_response(facts, response)


async def test_client_retries_rate_limits_and_renews_before_each_attempt() -> None:
    facts = make_facts()
    responses = iter([httpx.Response(429), httpx.Response(429), completion(response_content(facts))])
    callback_calls: list[tuple[AgentCallType, int]] = []
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return next(responses)

    async def before_attempt(call_type: AgentCallType, attempt: int) -> bool:
        callback_calls.append((call_type, attempt))
        return True

    invocation = await client(httpx.MockTransport(handler)).request(
        facts, call_type=AgentCallType.PRIMARY, before_http_attempt=before_attempt
    )

    assert invocation.error_code is None
    assert invocation.response is not None
    assert callback_calls == [(AgentCallType.PRIMARY, 1), (AgentCallType.PRIMARY, 2), (AgentCallType.PRIMARY, 3)]
    assert paths == ["/chat/completions", "/chat/completions", "/chat/completions"]
    assert len(invocation.records) == 3
    assert {record.node_name for record in invocation.records} == {"call_analysis_agent"}


async def test_client_post_contract_requires_concise_simplified_chinese_explanations() -> None:
    facts = make_facts()
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/chat/completions"
        requests.append(json.loads(request.content))
        return completion(response_content(facts))

    invocation = await client(httpx.MockTransport(handler)).request(
        facts, call_type=AgentCallType.PRIMARY
    )

    assert invocation.response is not None
    assert requests[0]["response_format"] == {"type": "json_object"}
    instruction = requests[0]["messages"][0]["content"]
    assert "impact_explanation、reason、recommended_action 必须使用简洁简体中文。" in instruction
    assert all(
        field in instruction
        for field in (
            "product_id",
            "rank",
            "impact_explanation",
            "reason",
            "recommended_action",
            "confidence",
        )
    )


@pytest.mark.parametrize(
    ("failure", "error_code"),
    [
        ("timeout", "DEEPSEEK_TIMEOUT"),
        ("transport", "DEEPSEEK_TRANSPORT"),
        ("server", "DEEPSEEK_SERVER_ERROR"),
    ],
)
async def test_client_retries_only_transient_failures_three_times(
    failure: str, error_code: str
) -> None:
    facts = make_facts()
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if failure == "timeout":
            raise httpx.TimeoutException("timeout", request=request)
        if failure == "transport":
            raise httpx.ConnectError("transport", request=request)
        return httpx.Response(500)

    invocation = await client(httpx.MockTransport(handler)).request(
        facts, call_type=AgentCallType.PRIMARY
    )

    assert invocation.response is None
    assert invocation.error_code == error_code
    assert requests == len(invocation.records) == 3


@pytest.mark.parametrize(
    ("status_code", "error_code"),
    [
        (400, "DEEPSEEK_HTTP_ERROR"),
        (401, "DEEPSEEK_UNAUTHORIZED"),
        (403, "DEEPSEEK_FORBIDDEN"),
    ],
)
async def test_client_does_not_retry_model_authentication_failures(
    status_code: int, error_code: str
) -> None:
    facts = make_facts()
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(status_code)

    invocation = await client(httpx.MockTransport(handler)).request(
        facts, call_type=AgentCallType.PRIMARY
    )

    assert invocation.response is None
    assert invocation.error_code == error_code
    assert requests == len(invocation.records) == 1


@pytest.mark.parametrize("kind", ["invalid_json", "extra_field", "low_confidence", "high_confidence"])
async def test_client_handles_schema_errors_and_schema_repair_node_mapping(kind: str) -> None:
    facts = make_facts()

    invalid_content = {
        "invalid_json": "not-json",
        "extra_field": response_content(facts, unexpected_fact="forged"),
        "low_confidence": response_content(facts, confidence="-0.0001"),
        "high_confidence": response_content(facts, confidence="1.0001"),
    }[kind]

    async def invalid_handler(_request: httpx.Request) -> httpx.Response:
        return completion(invalid_content)

    invalid = await client(httpx.MockTransport(invalid_handler)).request(
        facts, call_type=AgentCallType.PRIMARY
    )
    assert invalid.response is None
    assert invalid.error_code == "DEEPSEEK_SCHEMA_INVALID"
    assert len(invalid.records) == 1
    assert invalid.records[0].node_name == "call_analysis_agent"

    async def repair_handler(_request: httpx.Request) -> httpx.Response:
        return completion(response_content(facts))

    repair = await client(httpx.MockTransport(repair_handler)).request(
        facts, call_type=AgentCallType.SCHEMA_REPAIR
    )
    assert repair.response is not None
    assert repair.records[0].node_name == "validate_and_reconcile"


async def test_client_stops_before_http_when_lease_is_lost_or_key_is_missing() -> None:
    facts = make_facts()
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return completion(response_content(facts))

    async def lost_lease(_call_type: AgentCallType, _attempt: int) -> bool:
        return False

    lease_lost = await client(httpx.MockTransport(handler)).request(
        facts, call_type=AgentCallType.PRIMARY, before_http_attempt=lost_lease
    )
    missing_key = await client(httpx.MockTransport(handler), api_key=None).request(
        facts, call_type=AgentCallType.PRIMARY
    )
    blank_key = await client(httpx.MockTransport(handler), api_key="   ").request(
        facts, call_type=AgentCallType.PRIMARY
    )

    assert lease_lost.error_code == "LEASE_LOST"
    assert lease_lost.records == []
    assert missing_key.error_code == "DEEPSEEK_KEY_MISSING"
    assert missing_key.records == []
    assert blank_key.error_code == "DEEPSEEK_KEY_MISSING"
    assert blank_key.records == []
    assert requests == 0


async def test_client_records_safe_hash_and_optional_cost() -> None:
    facts = make_facts()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return completion(response_content(facts), total_tokens=8)

    without_price = await client(httpx.MockTransport(handler)).request(
        facts, call_type=AgentCallType.PRIMARY
    )
    with_price = await client(
        httpx.MockTransport(handler), price=Decimal("2.5")
    ).request(facts, call_type=AgentCallType.PRIMARY)

    assert without_price.records[0].estimated_cost is None
    assert with_price.records[0].estimated_cost == Decimal("0.000020")
    for record in (*without_price.records, *with_price.records):
        assert len(record.input_hash) == 64
        assert not {"headers", "prompt", "raw_response", "key", "authorization"} & set(vars(record))


@pytest.mark.parametrize(
    "usage",
    [
        None,
        [],
        {"prompt_tokens": "invalid", "completion_tokens": "invalid", "total_tokens": "invalid"},
        {"prompt_tokens": -1, "completion_tokens": -1, "total_tokens": -1},
    ],
)
async def test_client_normalizes_untrusted_usage_without_breaking_success(usage: object) -> None:
    facts = make_facts()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": response_content(facts)}}],
                "usage": usage,
            },
        )

    invocation = await client(
        httpx.MockTransport(handler), price=Decimal("2.5")
    ).request(facts, call_type=AgentCallType.PRIMARY)

    assert invocation.response is not None
    assert invocation.error_code is None
    record = invocation.records[0]
    assert (record.prompt_tokens, record.completion_tokens, record.total_tokens) == (0, 0, 0)
    assert record.estimated_cost == Decimal("0.000000")


def test_degraded_drafts_are_fixed_chinese_and_cover_trusted_candidates() -> None:
    facts = make_facts(3)

    drafts = build_degraded_drafts(facts)

    assert [draft.product_id for draft in drafts] == [candidate.product_id for candidate in facts.candidates]
    assert [draft.rank for draft in drafts] == [1, 2, 3]
    assert all(draft.impact_explanation == "模型解释暂不可用，请人工核验。" for draft in drafts)
    assert all(draft.reason == "模型解释暂不可用，请人工核验。" for draft in drafts)
    assert all(draft.recommended_action == "模型解释暂不可用，请人工核验。" for draft in drafts)


def test_validate_response_rejects_missing_trusted_candidate() -> None:
    facts = make_facts()
    payload = json.loads(response_content(facts))
    payload["candidates"].pop()

    with pytest.raises(AgentSchemaError):
        validate_agent_response(facts, parse_agent_response(json.dumps(payload)))


def test_settings_default_and_model_override(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JWT_SECRET_KEY", "test-only-secret-at-least-32-characters")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    get_settings.cache_clear()
    assert get_settings().deepseek_model == "deepseek-flash"

    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-custom")
    get_settings.cache_clear()
    assert get_settings().deepseek_model == "deepseek-custom"
    get_settings.cache_clear()
