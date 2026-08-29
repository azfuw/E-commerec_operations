import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

import httpx
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.analytics import (
    compare_store_products,
    find_anomalous_products,
    get_inventory_risk,
    get_product_metrics,
    get_store_summary,
)
from backend.common import AgentCallType
from backend.config import Settings
from backend.deepseek_runtime import DeepSeekJsonRuntime
from backend.schemas import (
    AgentAnalysisResponse,
    AgentCandidateDraft,
    AnalysisFacts,
    ProductMetrics,
    TrustedAnalysisCandidate,
)

PROMPT_VERSION = "analysis-facts-v1"
DEGRADED_MESSAGE = "模型解释暂不可用，请人工核验。"


class AgentSchemaError(ValueError):
    pass


BeforeHttpAttempt = Callable[[AgentCallType, int], Awaitable[bool]]


@dataclass(frozen=True)
class AgentCallRecord:
    node_name: Literal["call_analysis_agent", "validate_and_reconcile"]
    call_type: AgentCallType
    attempt: int
    model: str
    prompt_version: str
    status: str
    input_hash: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    duration_ms: int
    estimated_cost: Decimal | None
    error_code: str | None


@dataclass(frozen=True)
class AgentInvocation:
    response: AgentAnalysisResponse | None
    records: list[AgentCallRecord]
    error_code: str | None


async def collect_analysis_facts(
    session: AsyncSession, store_id: str, start_date: date, end_date: date
) -> AnalysisFacts:
    store_summary = await get_store_summary(session, store_id, start_date, end_date)
    anomalies = await find_anomalous_products(
        session, store_id, start_date, end_date, limit=5
    )
    product_ids = [candidate.product_id for candidate in anomalies]
    if len(product_ids) != len(set(product_ids)):
        raise ValueError("duplicate anomaly candidate")

    measured_metrics = [
        await get_product_metrics(session, store_id, product_id, start_date, end_date)
        for product_id in product_ids
    ]
    comparison = await compare_store_products(
        session, store_id, product_ids, start_date, end_date
    )
    if len(comparison) != len(product_ids):
        raise ValueError("comparison result is inconsistent")

    for candidate, measured, compared in zip(
        anomalies, measured_metrics, comparison, strict=True
    ):
        if (
            candidate.product_id != measured.product_id
            or candidate.metrics != measured
            or compared != measured
        ):
            raise ValueError("product metrics are inconsistent")

    for product_id in product_ids:
        risk = await get_inventory_risk(session, store_id, product_id)
        if risk.product_id != product_id:
            raise ValueError("inventory result is inconsistent")

    return AnalysisFacts(
        store_summary=store_summary,
        candidates=[
            TrustedAnalysisCandidate(
                product_id=candidate.product_id,
                product_code=candidate.product_code,
                anomaly_types=list(candidate.anomaly_types),
                metrics=candidate.metrics,
                business_impact=candidate.business_impact,
                evidence=candidate.evidence,
            )
            for candidate in anomalies
        ],
    )


def parse_agent_response(content: str) -> AgentAnalysisResponse:
    try:
        return AgentAnalysisResponse.model_validate_json(content)
    except (ValidationError, ValueError):
        raise AgentSchemaError("invalid agent response") from None


def validate_agent_response(
    facts: AnalysisFacts, response: AgentAnalysisResponse
) -> list[AgentCandidateDraft]:
    trusted_ids = [candidate.product_id for candidate in facts.candidates]
    response_ids = [candidate.product_id for candidate in response.candidates]
    if len(response_ids) != len(trusted_ids) or set(response_ids) != set(trusted_ids):
        raise AgentSchemaError("agent response product coverage is invalid")
    if len(response_ids) != len(set(response_ids)):
        raise AgentSchemaError("agent response product IDs are duplicated")

    ranks = [candidate.rank for candidate in response.candidates]
    if set(ranks) != set(range(1, len(trusted_ids) + 1)) or len(ranks) != len(set(ranks)):
        raise AgentSchemaError("agent response ranks are invalid")
    return sorted(response.candidates, key=lambda candidate: candidate.rank)


def build_degraded_drafts(facts: AnalysisFacts) -> list[AgentCandidateDraft]:
    return [
        AgentCandidateDraft(
            product_id=candidate.product_id,
            rank=rank,
            impact_explanation=DEGRADED_MESSAGE,
            reason=DEGRADED_MESSAGE,
            recommended_action=DEGRADED_MESSAGE,
            confidence=Decimal("0"),
        )
        for rank, candidate in enumerate(facts.candidates, start=1)
    ]


def _canonical_facts(facts: AnalysisFacts) -> str:
    return json.dumps(
        facts.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _input_hash(facts: AnalysisFacts) -> str:
    return hashlib.sha256(_canonical_facts(facts).encode()).hexdigest()


def _node_name(call_type: AgentCallType) -> Literal["call_analysis_agent", "validate_and_reconcile"]:
    return (
        "call_analysis_agent"
        if call_type is AgentCallType.PRIMARY
        else "validate_and_reconcile"
    )


class DeepSeekAnalysisClient:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.runtime = DeepSeekJsonRuntime(settings, transport=transport)

    async def request(
        self,
        facts: AnalysisFacts,
        *,
        call_type: AgentCallType,
        before_http_attempt: BeforeHttpAttempt | None = None,
    ) -> AgentInvocation:
        instruction = (
            "Return JSON candidates with only product_id, rank, impact_explanation, reason, "
            "recommended_action, and confidence. impact_explanation、reason、recommended_action "
            "必须使用简洁简体中文。"
            if call_type is AgentCallType.PRIMARY
            else '仅基于提供的可信 facts 重新生成 JSON。顶层 JSON 对象只能包含 "candidates" 键，结构必须为 {"candidates":[...]}。'
            "facts.candidates 中每个 product_id 恰好一项且 ID 原样使用。"
            "candidates 数组中每个对象只能包含 product_id、rank、impact_explanation、reason、recommended_action、confidence 六个字段。"
            "rank 恰为 1..N 且不重复。confidence 是 0..1 数字。"
            "impact_explanation、reason、recommended_action 必须使用简洁简体中文。"
            "仅输出 JSON、无 Markdown、无额外字段。"
        )
        async def renew(attempt: int) -> bool:
            return before_http_attempt is None or await before_http_attempt(call_type, attempt)

        result = await self.runtime.request(
            system_prompt=instruction,
            user_payload={"facts": facts.model_dump(mode="json")},
            parse_response=parse_agent_response,
            before_http_attempt=renew if before_http_attempt is not None else None,
        )
        return AgentInvocation(
            response=result.response,
            records=[
                AgentCallRecord(
                    node_name=_node_name(call_type),
                    call_type=call_type,
                    attempt=record.attempt,
                    model=record.model,
                    prompt_version=PROMPT_VERSION,
                    status=record.status,
                    input_hash=record.input_hash,
                    prompt_tokens=record.prompt_tokens,
                    completion_tokens=record.completion_tokens,
                    total_tokens=record.total_tokens,
                    duration_ms=record.duration_ms,
                    estimated_cost=record.estimated_cost,
                    error_code=record.error_code,
                )
                for record in result.records
            ],
            error_code=result.error_code,
        )
