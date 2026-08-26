import json
import os
from decimal import Decimal

import pytest
import httpx

from backend.analysis_agent import (
    AgentInvocation,
    AgentSchemaError,
    DeepSeekAnalysisClient,
    validate_agent_response,
)
from backend.common import AgentCallType
from backend.config import Settings, get_settings
from backend.schemas import (
    AgentAnalysisResponse,
    AnalysisFacts,
    ProductMetrics,
    StoreMetrics,
    TrustedAnalysisCandidate,
)


def minimal_trusted_facts() -> AnalysisFacts:
    metrics = ProductMetrics.from_totals(
        impressions=10,
        clicks=1,
        orders=1,
        units=1,
        revenue=Decimal("1.00"),
        refunds=0,
    ).model_copy(update={"product_id": "smoke-product", "product_code": "SMOKE-001"})
    return AnalysisFacts(
        store_summary=StoreMetrics(
            store_id="smoke-store",
            impressions=10,
            clicks=1,
            orders=1,
            units=1,
            revenue=Decimal("1.00"),
            refunds=0,
            ctr=Decimal("0.1000"),
            conversion_rate=Decimal("1.0000"),
            refund_rate=Decimal("0.0000"),
            average_order_value=Decimal("1.0000"),
        ),
        candidates=[
            TrustedAnalysisCandidate(
                product_id="smoke-product",
                product_code="SMOKE-001",
                anomaly_types=["smoke"],
                metrics=metrics,
                business_impact=Decimal("1.00"),
                evidence=["smoke=1"],
            )
        ],
    )


def _mock_client(transport: httpx.AsyncBaseTransport) -> DeepSeekAnalysisClient:
    return DeepSeekAnalysisClient(
        Settings(
            _env_file=None,
            jwt_secret_key="test-only-secret-at-least-32-characters",
            deepseek_api_key="test-only-transport-token",
            deepseek_base_url="https://mock.deepseek.invalid",
        ),
        transport=transport,
    )


def _valid_content(facts: AnalysisFacts) -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "product_id": candidate.product_id,
                    "rank": rank,
                    "impact_explanation": "影响说明",
                    "reason": "原因",
                    "recommended_action": "建议",
                    "confidence": "0.8",
                }
                for rank, candidate in enumerate(facts.candidates, start=1)
            ]
        }
    )


def _completion(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


async def _run_smoke(
    client: DeepSeekAnalysisClient, facts: AnalysisFacts
) -> AgentInvocation:
    posts = 0

    async def allow_post(_call_type: AgentCallType, _attempt: int) -> bool:
        nonlocal posts
        if posts >= 2:
            return False
        posts += 1
        return True

    async def invoke(
        call_type: AgentCallType,
    ) -> tuple[AgentInvocation, AgentAnalysisResponse | None, str | None]:
        invocation = await client.request(
            facts, call_type=call_type, before_http_attempt=allow_post
        )
        if invocation.response is None:
            return invocation, None, invocation.error_code
        try:
            validate_agent_response(facts, invocation.response)
        except AgentSchemaError:
            return invocation, None, "DEEPSEEK_SCHEMA_INVALID"
        return invocation, invocation.response, None

    primary, response, error_code = await invoke(AgentCallType.PRIMARY)
    if response is not None or error_code != "DEEPSEEK_SCHEMA_INVALID":
        return AgentInvocation(response=response, records=primary.records, error_code=error_code)

    repair, response, error_code = await invoke(AgentCallType.SCHEMA_REPAIR)
    return AgentInvocation(
        response=response,
        records=[*primary.records, *repair.records],
        error_code=error_code,
    )


async def test_smoke_orchestration_repairs_one_schema_invalid_primary() -> None:
    facts = minimal_trusted_facts()
    posts: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return _completion("not-json") if len(posts) == 1 else _completion(_valid_content(facts))

    result = await _run_smoke(_mock_client(httpx.MockTransport(handler)), facts)

    assert len(posts) == 2
    assert [record.call_type for record in result.records] == [
        AgentCallType.PRIMARY,
        AgentCallType.SCHEMA_REPAIR,
    ]
    assert result.response is not None
    assert all(post["model"] == "deepseek-v4-flash" for post in posts)
    assert all(record.model == "deepseek-v4-flash" for record in result.records)
    assert all(
        not {"headers", "prompt", "raw_response", "key", "authorization"} & set(vars(record))
        for record in result.records
    )


async def test_smoke_orchestration_does_not_repair_a_valid_primary() -> None:
    facts = minimal_trusted_facts()
    posts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return _completion(_valid_content(facts))

    result = await _run_smoke(_mock_client(httpx.MockTransport(handler)), facts)

    assert posts == 1
    assert [record.call_type for record in result.records] == [AgentCallType.PRIMARY]
    assert result.response is not None


async def test_smoke_orchestration_stops_after_one_invalid_repair() -> None:
    facts = minimal_trusted_facts()
    posts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return _completion("not-json")

    result = await _run_smoke(_mock_client(httpx.MockTransport(handler)), facts)

    assert posts == 2
    assert [record.call_type for record in result.records] == [
        AgentCallType.PRIMARY,
        AgentCallType.SCHEMA_REPAIR,
    ]
    assert result.response is None
    assert result.error_code == "DEEPSEEK_SCHEMA_INVALID"


async def test_smoke_orchestration_caps_transient_retries_at_two_posts() -> None:
    posts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return httpx.Response(500)

    result = await _run_smoke(
        _mock_client(httpx.MockTransport(handler)), minimal_trusted_facts()
    )

    assert posts == 2
    assert len(result.records) == 2
    assert result.error_code == "LEASE_LOST"


@pytest.mark.deepseek_smoke
@pytest.mark.skipif(
    os.getenv("RUN_DEEPSEEK_SMOKE") != "1", reason="explicit opt-in required"
)
async def test_explicit_deepseek_v4_flash_schema_smoke() -> None:
    settings = get_settings()
    if settings.deepseek_api_key is None:
        pytest.skip("DeepSeek key is not configured")
    client = DeepSeekAnalysisClient(settings)
    result = await _run_smoke(client, minimal_trusted_facts())
    assert result.response is not None
    assert 1 <= len(result.records) <= 2
    assert result.records[0].call_type is AgentCallType.PRIMARY
    assert all(record.model == "deepseek-v4-flash" for record in result.records)
    assert all(
        not {"headers", "prompt", "raw_response", "key", "authorization"} & set(vars(record))
        for record in result.records
    )
    assert [record.call_type for record in result.records] in (
        [AgentCallType.PRIMARY],
        [AgentCallType.PRIMARY, AgentCallType.SCHEMA_REPAIR],
    )
