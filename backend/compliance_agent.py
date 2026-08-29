from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Literal
import unicodedata

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from backend.common import AgentCallType, ComplianceRiskLevel
from backend.config import Settings
from backend.deepseek_runtime import BeforeHttpAttempt, DeepSeekJsonRuntime
from backend.optimization_validation import DeterministicComplianceResult
from backend.schemas import (
    CanonicalRuleCitation,
    OptimizationProposalOutput,
    OutputCitation,
    TrustedOptimizationInput,
    ValidatedRequiredChange,
)


COMPLIANCE_PROMPT_VERSION = "product-compliance-v1"
COMPLIANCE_PRIMARY_PROMPT = (
    "你是独立商品合规审核助手。只输出纯 JSON 对象，不要 Markdown，禁止额外字段。"
    "顶层必须且仅有 passed、risk_level、violations、required_changes、citations、confidence、degraded。"
    "passed 与 degraded 为布尔值；risk_level 为 low、medium 或 high；confidence 为 0..1 数字。"
    "violations 为对象数组，每项含 code、field、message_zh、citation_chunk_ids；code 必须是 "
    "EXAGGERATION|MEDICALIZATION|MISLEADING|SEMANTIC_CONTRADICTION|UNPROVABLE_PROMISE|INSUFFICIENT_EVIDENCE。"
    "required_changes 为对象数组，每项含 source_track、source_violation_code、field、instruction、"
    "citation_chunk_ids；source_track 必须为 semantic，且 source_violation_code 与 field 必须匹配"
    "某项 violation。citations 为对象数组，每项仅含 chunk_id，所有 citation_chunk_ids 只能使用"
    "可信 active/applicable canonical citations。独立审核候选，不得修改 candidate_output，"
    "message_zh 与 instruction 必须包含中文，且不得含换行或任何 Unicode control character；instruction 必须简洁。"
    "不得用语义结论覆盖 deterministic_result。"
)
COMPLIANCE_SCHEMA_REPAIR_PROMPT = (
    "你是独立商品合规审核助手。仅基于给定可信输入重新生成纯 JSON 对象，不要 Markdown，禁止额外字段。"
    "顶层必须且仅有 passed、risk_level、violations、required_changes、citations、confidence、degraded。"
    "passed 与 degraded 为布尔值；risk_level 为 low、medium 或 high；confidence 为 0..1 数字。"
    "violations 为对象数组，每项含 code、field、message_zh、citation_chunk_ids；code 必须是 "
    "EXAGGERATION|MEDICALIZATION|MISLEADING|SEMANTIC_CONTRADICTION|UNPROVABLE_PROMISE|INSUFFICIENT_EVIDENCE。"
    "required_changes 为对象数组，每项含 source_track、source_violation_code、field、instruction、"
    "citation_chunk_ids；source_track 必须为 semantic，且 source_violation_code 与 field 必须匹配"
    "某项 violation。citations 为对象数组，每项仅含 chunk_id，所有 citation_chunk_ids 只能使用"
    "可信 active/applicable canonical citations。独立审核候选，不得修改 candidate_output，"
    "message_zh 与 instruction 必须包含中文，且不得含换行或任何 Unicode control character；instruction 必须简洁。"
    "不得用语义结论覆盖 deterministic_result。"
)

_SEMANTIC_CODES = {
    "EXAGGERATION",
    "MEDICALIZATION",
    "MISLEADING",
    "SEMANTIC_CONTRADICTION",
    "UNPROVABLE_PROMISE",
    "INSUFFICIENT_EVIDENCE",
}


class ComplianceAgentSchemaError(ValueError):
    pass


_SAFE_RESPONSE_FIELDS = {
    "passed",
    "risk_level",
    "violations",
    "required_changes",
    "citations",
    "confidence",
    "degraded",
    "code",
    "field",
    "message_zh",
    "citation_chunk_ids",
    "source_track",
    "source_violation_code",
    "instruction",
    "chunk_id",
}


def _safe_schema_location(error: ValidationError) -> str:
    detail = error.errors()[0]
    if detail.get("type") == "json_invalid":
        return "json"
    if detail.get("type") == "extra_forbidden":
        return "extra"
    parts: list[str] = []
    for value in detail.get("loc", ()):
        if isinstance(value, int) and not isinstance(value, bool):
            if parts:
                parts[-1] += f"[{value}]"
        elif isinstance(value, str) and value in _SAFE_RESPONSE_FIELDS:
            parts.append(value)
    return ".".join(parts) or "root"


def _safe_chinese(value: str, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and any("\u4e00" <= character <= "\u9fff" for character in value)
        and not any(unicodedata.category(character).startswith("C") for character in value)
    )


class ComplianceSemanticViolation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal[
        "EXAGGERATION",
        "MEDICALIZATION",
        "MISLEADING",
        "SEMANTIC_CONTRADICTION",
        "UNPROVABLE_PROMISE",
        "INSUFFICIENT_EVIDENCE",
    ]
    field: str = Field(min_length=1, max_length=64)
    message_zh: str = Field(min_length=1, max_length=500)
    citation_chunk_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        max_length=20
    )

    @model_validator(mode="after")
    def validate_contract(self) -> "ComplianceSemanticViolation":
        if not _safe_chinese(self.message_zh, 500):
            raise ValueError("message must be safe Chinese text")
        if len(self.citation_chunk_ids) != len(set(self.citation_chunk_ids)):
            raise ValueError("citation chunk IDs must be unique")
        return self


class ComplianceAgentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    risk_level: ComplianceRiskLevel
    violations: list[ComplianceSemanticViolation] = Field(max_length=20)
    required_changes: list[ValidatedRequiredChange] = Field(max_length=20)
    citations: list[OutputCitation] = Field(max_length=20)
    confidence: Decimal = Field(ge=0, le=1)
    degraded: bool


@dataclass(frozen=True)
class ComplianceAgentCallRecord:
    node_name: Literal["call_product_compliance_agent", "repair_product_compliance_schema"]
    call_type: AgentCallType
    iteration: int
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
class ComplianceAgentInvocation:
    response: ComplianceAgentResponse | None
    records: list[ComplianceAgentCallRecord]
    error_code: str | None


def parse_compliance_response(content: str) -> ComplianceAgentResponse:
    try:
        return ComplianceAgentResponse.model_validate_json(content)
    except ValidationError as error:
        raise ComplianceAgentSchemaError(
            f"invalid compliance response: {_safe_schema_location(error)}"
        ) from None
    except ValueError:
        raise ComplianceAgentSchemaError("invalid compliance response: json") from None


def _allowed_citations(trusted: TrustedOptimizationInput) -> dict[str, CanonicalRuleCitation]:
    return {
        citation.chunk_id: citation
        for citation in trusted.canonical_rule_citations
        if citation.active and citation.applicable
    }


def _valid_required_change(change: object, allowed_ids: set[str]) -> bool:
    return (
        isinstance(change, ValidatedRequiredChange)
        and change.source_track == "semantic"
        and change.source_violation_code in _SEMANTIC_CODES
        and 1 <= len(change.field) <= 64
        and _safe_chinese(change.instruction, 240)
        and len(change.citation_chunk_ids) == len(set(change.citation_chunk_ids))
        and all(citation_id in allowed_ids for citation_id in change.citation_chunk_ids)
    )


def validate_compliance_response(
    trusted: TrustedOptimizationInput, response: ComplianceAgentResponse
) -> ComplianceAgentResponse:
    if not isinstance(response, ComplianceAgentResponse):
        raise ComplianceAgentSchemaError("invalid compliance response")
    allowed = _allowed_citations(trusted)
    allowed_ids = set(allowed)
    citation_ids = [citation.chunk_id for citation in response.citations if isinstance(citation, OutputCitation)]
    if (
        len(citation_ids) != len(response.citations)
        or len(citation_ids) != len(set(citation_ids))
        or any(citation_id not in allowed_ids for citation_id in citation_ids)
    ):
        raise ComplianceAgentSchemaError("invalid compliance response")

    violation_pairs: set[tuple[str, str]] = set()
    for violation in response.violations:
        if not isinstance(violation, ComplianceSemanticViolation) or not (
            violation.code in _SEMANTIC_CODES
            and 1 <= len(violation.field) <= 64
            and _safe_chinese(violation.message_zh, 500)
            and len(violation.citation_chunk_ids) == len(set(violation.citation_chunk_ids))
            and all(citation_id in allowed_ids for citation_id in violation.citation_chunk_ids)
        ):
            raise ComplianceAgentSchemaError("invalid compliance response")
        violation_pairs.add((violation.code, violation.field))

    if not all(_valid_required_change(change, allowed_ids) for change in response.required_changes):
        raise ComplianceAgentSchemaError("invalid compliance response")
    change_pairs = {
        (change.source_violation_code, change.field) for change in response.required_changes
    }
    if not change_pairs <= violation_pairs:
        raise ComplianceAgentSchemaError("invalid compliance response")
    if response.degraded and response.passed:
        raise ComplianceAgentSchemaError("invalid compliance response")
    if response.passed:
        if (
            response.risk_level is not ComplianceRiskLevel.LOW
            or response.violations
            or response.required_changes
        ):
            raise ComplianceAgentSchemaError("invalid compliance response")
    elif not response.degraded and (not response.violations or not violation_pairs <= change_pairs):
        raise ComplianceAgentSchemaError("invalid compliance response")
    return response


class ProductComplianceAgentClient:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.runtime = DeepSeekJsonRuntime(settings, transport=transport)

    async def request(
        self,
        proposal: OptimizationProposalOutput,
        deterministic: DeterministicComplianceResult,
        trusted: TrustedOptimizationInput,
        *,
        call_type: AgentCallType,
        iteration: int,
        before_http_attempt: BeforeHttpAttempt | None = None,
        max_attempts: int = 3,
    ) -> ComplianceAgentInvocation:
        if not isinstance(call_type, AgentCallType) or call_type not in {
            AgentCallType.PRIMARY,
            AgentCallType.SCHEMA_REPAIR,
        }:
            raise ValueError("unsupported compliance call type")
        if not 0 <= iteration <= 2:
            raise ValueError("iteration must be between 0 and 2")

        canonical_citations = [
            citation.model_dump(mode="json")
            for citation in trusted.canonical_rule_citations
            if citation.active and citation.applicable
        ]
        user_payload = {
            "iteration": iteration,
            "candidate_output": proposal.model_dump(mode="json"),
            "deterministic_result": {
                "passed": deterministic.passed,
                "violations": [
                    {
                        "code": violation.code,
                        "field": violation.field,
                        "message_zh": violation.message_zh,
                    }
                    for violation in deterministic.violations
                ],
                "canonical_citation_chunk_ids": [
                    citation.chunk_id for citation in deterministic.canonical_citations
                ],
            },
            "trusted_facts": trusted.model_dump(mode="json"),
            "canonical_citations": canonical_citations,
        }
        system_prompt = (
            COMPLIANCE_PRIMARY_PROMPT
            if call_type is AgentCallType.PRIMARY
            else COMPLIANCE_SCHEMA_REPAIR_PROMPT
        )
        node_name: Literal[
            "call_product_compliance_agent", "repair_product_compliance_schema"
        ] = (
            "call_product_compliance_agent"
            if call_type is AgentCallType.PRIMARY
            else "repair_product_compliance_schema"
        )

        def parse_and_validate(content: str) -> ComplianceAgentResponse:
            return validate_compliance_response(trusted, parse_compliance_response(content))

        result = await self.runtime.request(
            system_prompt=system_prompt,
            user_payload=user_payload,
            parse_response=parse_and_validate,
            before_http_attempt=before_http_attempt,
            max_attempts=max_attempts,
        )
        return ComplianceAgentInvocation(
            response=result.response,
            records=[
                ComplianceAgentCallRecord(
                    node_name=node_name,
                    call_type=call_type,
                    iteration=iteration,
                    attempt=record.attempt,
                    model=record.model,
                    prompt_version=COMPLIANCE_PROMPT_VERSION,
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
