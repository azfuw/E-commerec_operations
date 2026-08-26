import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from time import perf_counter
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


def _nonnegative_token(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        token = int(value)
    except (TypeError, ValueError):
        return 0
    return token if token >= 0 else 0


class DeepSeekAnalysisClient:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def request(
        self,
        facts: AnalysisFacts,
        *,
        call_type: AgentCallType,
        before_http_attempt: BeforeHttpAttempt | None = None,
    ) -> AgentInvocation:
        api_key = self.settings.deepseek_api_key
        if api_key is None or not api_key.get_secret_value().strip():
            return AgentInvocation(response=None, records=[], error_code="DEEPSEEK_KEY_MISSING")

        input_hash = _input_hash(facts)
        payload = {
            "model": self.settings.deepseek_model,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": "Return JSON candidates with only product_id, rank, impact_explanation, reason, recommended_action, and confidence. impact_explanation、reason、recommended_action 必须使用简洁简体中文。",
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"facts": facts.model_dump(mode="json")}, ensure_ascii=False, separators=(",", ":")
                    ),
                },
            ],
        }
        records: list[AgentCallRecord] = []
        headers = {"Authorization": f"Bearer {api_key.get_secret_value()}"}
        timeout = httpx.Timeout(self.settings.deepseek_timeout_seconds)
        async with httpx.AsyncClient(
            base_url=self.settings.deepseek_base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=self.transport,
        ) as http_client:
            for attempt in range(1, 4):
                if before_http_attempt is not None and not await before_http_attempt(call_type, attempt):
                    return AgentInvocation(response=None, records=records, error_code="LEASE_LOST")
                started = perf_counter()
                try:
                    response = await http_client.post("/chat/completions", json=payload)
                except httpx.TimeoutException:
                    error_code = "DEEPSEEK_TIMEOUT"
                except httpx.TransportError:
                    error_code = "DEEPSEEK_TRANSPORT"
                else:
                    if response.status_code == 429:
                        error_code = "DEEPSEEK_RATE_LIMIT"
                    elif 500 <= response.status_code <= 599:
                        error_code = "DEEPSEEK_SERVER_ERROR"
                    elif response.status_code == 401:
                        error_code = "DEEPSEEK_UNAUTHORIZED"
                    elif response.status_code == 403:
                        error_code = "DEEPSEEK_FORBIDDEN"
                    elif response.is_success:
                        try:
                            content = response.json()["choices"][0]["message"]["content"]
                            parsed = parse_agent_response(content)
                        except (KeyError, IndexError, TypeError, ValueError):
                            error_code = "DEEPSEEK_SCHEMA_INVALID"
                        else:
                            records.append(
                                self._record(
                                    call_type, attempt, input_hash, "succeeded", None, response, started
                                )
                            )
                            return AgentInvocation(response=parsed, records=records, error_code=None)
                    else:
                        error_code = "DEEPSEEK_HTTP_ERROR"

                records.append(
                    self._record(call_type, attempt, input_hash, "failed", error_code, None, started)
                )
                if error_code not in {
                    "DEEPSEEK_TIMEOUT",
                    "DEEPSEEK_TRANSPORT",
                    "DEEPSEEK_RATE_LIMIT",
                    "DEEPSEEK_SERVER_ERROR",
                } or attempt == 3:
                    return AgentInvocation(response=None, records=records, error_code=error_code)

        raise AssertionError("request loop must return")

    def _record(
        self,
        call_type: AgentCallType,
        attempt: int,
        input_hash: str,
        status: str,
        error_code: str | None,
        response: httpx.Response | None,
        started: float,
    ) -> AgentCallRecord:
        try:
            body = response.json() if response is not None else {}
        except ValueError:
            body = {}
        usage = body.get("usage") if isinstance(body, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        prompt_tokens = _nonnegative_token(usage.get("prompt_tokens"))
        completion_tokens = _nonnegative_token(usage.get("completion_tokens"))
        total_tokens = _nonnegative_token(usage.get("total_tokens"))
        price = self.settings.deepseek_price_per_million_tokens
        estimated_cost = (
            None
            if price is None
            else (Decimal(total_tokens) * max(price, Decimal("0")) / Decimal(1_000_000)).quantize(
                Decimal("0.000001")
            )
        )
        return AgentCallRecord(
            node_name=_node_name(call_type),
            call_type=call_type,
            attempt=attempt,
            model=self.settings.deepseek_model,
            prompt_version=PROMPT_VERSION,
            status=status,
            input_hash=input_hash,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            duration_ms=int((perf_counter() - started) * 1000),
            estimated_cost=estimated_cost,
            error_code=error_code,
        )
