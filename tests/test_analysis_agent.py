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


async def test_client_post_contract_separates_primary_and_schema_repair_instructions() -> None:
    facts = make_facts()
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/chat/completions"
        requests.append(json.loads(request.content))
        return completion(response_content(facts))

    primary = await client(httpx.MockTransport(handler)).request(
        facts, call_type=AgentCallType.PRIMARY
    )
    repair = await client(httpx.MockTransport(handler)).request(
        facts, call_type=AgentCallType.SCHEMA_REPAIR
    )

    assert primary.response is not None
    assert repair.response is not None
    assert len(requests) == 2
    primary_payload, repair_payload = requests
    assert primary_payload["response_format"] == repair_payload["response_format"] == {
        "type": "json_object"
    }
    assert primary_payload["model"] == repair_payload["model"] == "deepseek-v4-flash"
    primary_instruction = primary_payload["messages"][0]["content"]
    assert primary_instruction == (
        "Return JSON candidates with only product_id, rank, impact_explanation, reason, "
        "recommended_action, and confidence. impact_explanation、reason、recommended_action "
        "必须使用简洁简体中文。"
    )
    repair_instruction = repair_payload["messages"][0]["content"]
    assert repair_instruction != primary_instruction
    assert all(
        rule in repair_instruction
        for rule in (
            "仅基于提供的可信 facts 重新生成 JSON。",
            '顶层 JSON 对象只能包含 "candidates" 键，结构必须为 {"candidates":[...]}。',
            "facts.candidates 中每个 product_id 恰好一项且 ID 原样使用。",
            "candidates 数组中每个对象只能包含 product_id、rank、impact_explanation、reason、recommended_action、confidence 六个字段。",
            "rank 恰为 1..N 且不重复。",
            "confidence 是 0..1 数字。",
            "impact_explanation、reason、recommended_action 必须使用简洁简体中文。",
            "仅输出 JSON、无 Markdown、无额外字段。",
        )
    )
    assert primary_payload["messages"][1]["content"] == repair_payload["messages"][1]["content"]
    assert json.loads(repair_payload["messages"][1]["content"]) == {
        "facts": facts.model_dump(mode="json")
    }
    assert repair.records[0].node_name == "validate_and_reconcile"
    assert not {"headers", "prompt", "raw_response", "key", "authorization"} & set(
        vars(repair.records[0])
    )


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
    assert get_settings().deepseek_model == "deepseek-v4-flash"

    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-custom")
    get_settings.cache_clear()
    assert get_settings().deepseek_model == "deepseek-custom"
    get_settings.cache_clear()
