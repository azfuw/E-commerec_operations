from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
import unicodedata

import httpx
from pydantic import ValidationError

from backend.common import AgentCallType
from backend.config import Settings
from backend.deepseek_runtime import BeforeHttpAttempt, DeepSeekJsonRuntime
from backend.schemas import (
    OptimizationProposalOutput,
    TrustedOptimizationInput,
    ValidatedRequiredChange,
)


OPTIMIZATION_PROMPT_VERSION = "product-optimization-v1"
OPTIMIZATION_PRIMARY_PROMPT = (
    "你是商品优化助手。只输出纯 JSON 对象，不要 Markdown，禁止额外字段。"
    "顶层必须且仅有 title、selling_points、description、keywords、attribute_completions、"
    "changes、citations、price_suggestions、sku_suggestions。title 为字符串；selling_points 和 "
    "keywords 为字符串数组；description 为有序对象数组，每项含 heading、body、evidence。"
    "所有 evidence 元素必须是仅含 kind、value 的对象；kind 仅为 fact 或 citation，value 为相应 allowlist 项。"
    "changes 每项含 field、current_value、suggested_value、reason、evidence；"
    "changes.field 仅能为 title、selling_points、description、keywords；"
    "attribute_completions 每项含 target_attribute、current_value、suggested_value、reason、evidence；"
    "price_suggestions 每项含 target_sku_id、current_price、suggested_price、reason、evidence；"
    "sku_suggestions 每项含 target_sku_id、current_code、current_spec、suggested_code、suggested_spec、"
    "reason、evidence；citations 每项仅含 chunk_id。evidence 只能使用 allowed_fact_paths 中的事实"
    "路径或 allowed_rule_chunk_ids 中的规则 chunk_id；引用证据的 chunk_id 必须也在 citations 中。"
    "价格和 SKU 仅是建议，不得声称已修改商品、SKU、发布或平台。"
)
OPTIMIZATION_SCHEMA_REPAIR_PROMPT = (
    "你是商品优化助手。仅基于给定可信输入重新生成纯 JSON 对象，不要 Markdown，禁止额外字段。"
    "顶层必须且仅有 title、selling_points、description、keywords、attribute_completions、"
    "changes、citations、price_suggestions、sku_suggestions。title 为字符串；selling_points 和 "
    "keywords 为字符串数组；description 为有序对象数组，每项含 heading、body、evidence。"
    "所有 evidence 元素必须是仅含 kind、value 的对象；kind 仅为 fact 或 citation，value 为相应 allowlist 项。"
    "changes 每项含 field、current_value、suggested_value、reason、evidence；"
    "changes.field 仅能为 title、selling_points、description、keywords；"
    "attribute_completions 每项含 target_attribute、current_value、suggested_value、reason、evidence；"
    "price_suggestions 每项含 target_sku_id、current_price、suggested_price、reason、evidence；"
    "sku_suggestions 每项含 target_sku_id、current_code、current_spec、suggested_code、suggested_spec、"
    "reason、evidence；citations 每项仅含 chunk_id。evidence 只能使用 allowed_fact_paths 中的事实"
    "路径或 allowed_rule_chunk_ids 中的规则 chunk_id；引用证据的 chunk_id 必须也在 citations 中。"
    "价格和 SKU 仅是建议，不得声称已修改商品、SKU、发布或平台。"
)

_DETERMINISTIC_CODES = {
    "MISSING_TRUSTED_FACT",
    "OUTPUT_BUSINESS_LENGTH",
    "TITLE_LANGUAGE",
    "RESTRICTED_PHRASE",
    "CHANGE_TARGET_DUPLICATE",
    "CHANGE_CURRENT_MISMATCH",
    "CHANGE_SUGGESTED_MISMATCH",
    "OUTPUT_CHANGE_UNDECLARED",
    "EVIDENCE_MISSING",
    "EVIDENCE_FACT_PATH_INVALID",
    "EVIDENCE_CITATION_INVALID",
    "CITATION_DUPLICATE",
    "CITATION_UNKNOWN",
    "CITATION_EVIDENCE_UNLISTED",
    "ATTRIBUTE_TARGET_DUPLICATE",
    "ATTRIBUTE_CURRENT_MISMATCH",
    "ATTRIBUTE_SOURCE_MISSING",
    "SKU_TARGET_DUPLICATE",
    "SKU_UNKNOWN",
    "SKU_CURRENT_MISMATCH",
    "PRICE_TARGET_DUPLICATE",
    "PRICE_SKU_UNKNOWN",
    "PRICE_CURRENT_MISMATCH",
    "PRICE_CURRENT_NONPOSITIVE",
    "PRICE_NONPOSITIVE",
    "PRICE_PRECISION",
    "PRICE_RANGE",
    "RAG_QUALITY_INSUFFICIENT",
}
_SEMANTIC_CODES = {
    "EXAGGERATION",
    "MEDICALIZATION",
    "MISLEADING",
    "SEMANTIC_CONTRADICTION",
    "UNPROVABLE_PROMISE",
    "INSUFFICIENT_EVIDENCE",
}


class OptimizationAgentSchemaError(ValueError):
    pass


@dataclass(frozen=True)
class OptimizationAgentCallRecord:
    node_name: Literal[
        "call_product_optimization_agent", "repair_product_optimization_schema"
    ]
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
class OptimizationAgentInvocation:
    response: OptimizationProposalOutput | None
    records: list[OptimizationAgentCallRecord]
    error_code: str | None


def parse_optimization_response(content: str) -> OptimizationProposalOutput:
    try:
        return OptimizationProposalOutput.model_validate_json(content)
    except (ValidationError, ValueError):
        raise OptimizationAgentSchemaError("invalid optimization response") from None


def _allowed_fact_paths(trusted: TrustedOptimizationInput) -> list[str]:
    paths = [
        "product.title",
        "product.category",
        "product.brand",
        "product.selling_points",
        "product.description",
        "product.search_keywords",
    ]
    paths.extend(f"product.attributes.{key}" for key in sorted(trusted.attributes))
    for sku in sorted(trusted.skus, key=lambda item: item.id):
        paths.extend(
            f"product.skus.{sku.id}.{field}" for field in ("code", "spec", "price", "stock")
        )
    paths.extend(
        f"candidate.metrics.{field}"
        for field in sorted(trusted.candidate_metrics.__class__.model_fields)
        if getattr(trusted.candidate_metrics, field) is not None
    )
    paths.extend(
        f"candidate.evidence.{index}" for index in range(len(trusted.candidate_evidence))
    )
    return paths


def _allowed_citation_ids(trusted: TrustedOptimizationInput) -> set[str]:
    return {
        citation.chunk_id
        for citation in trusted.canonical_rule_citations
        if citation.active and citation.applicable
    }


def _validate_required_changes(
    required_changes: Sequence[ValidatedRequiredChange], allowed_citation_ids: set[str]
) -> None:
    for change in required_changes:
        if not isinstance(change, ValidatedRequiredChange):
            raise OptimizationAgentSchemaError("invalid required changes")
        if (
            (change.source_track == "deterministic" and change.source_violation_code not in _DETERMINISTIC_CODES)
            or (change.source_track == "semantic" and change.source_violation_code not in _SEMANTIC_CODES)
            or change.source_track not in {"deterministic", "semantic"}
            or not 1 <= len(change.field) <= 64
            or not 1 <= len(change.instruction) <= 240
            or not any("\u4e00" <= character <= "\u9fff" for character in change.instruction)
            or any(unicodedata.category(character).startswith("C") for character in change.instruction)
            or len(change.citation_chunk_ids) != len(set(change.citation_chunk_ids))
            or any(citation_id not in allowed_citation_ids for citation_id in change.citation_chunk_ids)
        ):
            raise OptimizationAgentSchemaError("invalid required changes")


def validate_optimization_response(
    trusted: TrustedOptimizationInput,
    required_changes: Sequence[ValidatedRequiredChange],
    response: OptimizationProposalOutput,
) -> OptimizationProposalOutput:
    allowed_citation_ids = _allowed_citation_ids(trusted)
    _validate_required_changes(required_changes, allowed_citation_ids)
    allowed_fact_paths = set(_allowed_fact_paths(trusted))
    citation_ids = [citation.chunk_id for citation in response.citations]
    if len(citation_ids) != len(set(citation_ids)) or any(
        citation_id not in allowed_citation_ids for citation_id in citation_ids
    ):
        raise OptimizationAgentSchemaError("invalid optimization response")

    evidence_lists = [section.evidence for section in response.description]
    evidence_lists.extend(change.evidence for change in response.changes)
    evidence_lists.extend(attribute.evidence for attribute in response.attribute_completions)
    evidence_lists.extend(suggestion.evidence for suggestion in response.price_suggestions)
    evidence_lists.extend(suggestion.evidence for suggestion in response.sku_suggestions)
    for evidence in evidence_lists:
        for item in evidence:
            if (item.kind == "fact" and item.value not in allowed_fact_paths) or (
                item.kind == "citation"
                and (item.value not in allowed_citation_ids or item.value not in citation_ids)
            ):
                raise OptimizationAgentSchemaError("invalid optimization response")
    return response


class ProductOptimizationAgentClient:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.runtime = DeepSeekJsonRuntime(settings, transport=transport)

    async def request(
        self,
        trusted: TrustedOptimizationInput,
        *,
        required_changes: Sequence[ValidatedRequiredChange],
        call_type: AgentCallType,
        iteration: int,
        before_http_attempt: BeforeHttpAttempt | None = None,
        max_attempts: int = 3,
    ) -> OptimizationAgentInvocation:
        if not isinstance(call_type, AgentCallType) or call_type not in {
            AgentCallType.PRIMARY,
            AgentCallType.SCHEMA_REPAIR,
        }:
            raise ValueError("unsupported optimization call type")
        if not 0 <= iteration <= 2:
            raise ValueError("iteration must be between 0 and 2")

        allowed_citation_ids = _allowed_citation_ids(trusted)
        _validate_required_changes(required_changes, allowed_citation_ids)
        user_payload = {
            "iteration": iteration,
            "trusted_facts": trusted.model_dump(mode="json"),
            "allowed_fact_paths": _allowed_fact_paths(trusted),
            "allowed_rule_chunk_ids": sorted(allowed_citation_ids),
            "required_changes": [change.model_dump(mode="json") for change in required_changes],
        }
        system_prompt = (
            OPTIMIZATION_PRIMARY_PROMPT
            if call_type is AgentCallType.PRIMARY
            else OPTIMIZATION_SCHEMA_REPAIR_PROMPT
        )
        node_name: Literal[
            "call_product_optimization_agent", "repair_product_optimization_schema"
        ] = (
            "call_product_optimization_agent"
            if call_type is AgentCallType.PRIMARY
            else "repair_product_optimization_schema"
        )

        def parse_and_validate(content: str) -> OptimizationProposalOutput:
            return validate_optimization_response(
                trusted, required_changes, parse_optimization_response(content)
            )

        result = await self.runtime.request(
            system_prompt=system_prompt,
            user_payload=user_payload,
            parse_response=parse_and_validate,
            before_http_attempt=before_http_attempt,
            max_attempts=max_attempts,
        )
        return OptimizationAgentInvocation(
            response=result.response,
            records=[
                OptimizationAgentCallRecord(
                    node_name=node_name,
                    call_type=call_type,
                    iteration=iteration,
                    attempt=record.attempt,
                    model=record.model,
                    prompt_version=OPTIMIZATION_PROMPT_VERSION,
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
