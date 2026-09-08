from __future__ import annotations

import json
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.analysis_agent import validate_agent_response
from backend.compliance_agent import ComplianceAgentResponse, validate_compliance_response
from backend.optimization_agent import validate_optimization_response
from backend.optimization_validation import validate_optimization_output
from backend.schemas import (
    AgentAnalysisResponse,
    AnalysisFacts,
    CanonicalRuleCitation,
    OptimizationProposalOutput,
    TrustedOptimizationInput,
    ValidatedRequiredChange,
)


_ERROR = {"detail": {"code": "DETERMINISTIC_MODEL_REQUEST_INVALID"}}
_RESERVED_PREFIX = "阶段十连续失败验证"


class _Message(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    role: Literal["system", "user"]
    content: str = Field(min_length=1, max_length=1_000_000)


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model: Literal["deepseek-v4-flash"]
    response_format: dict[Literal["type"], Literal["json_object"]]
    messages: list[_Message] = Field(min_length=2, max_length=2)


def _safe_error() -> None:
    raise HTTPException(422, {"code": "DETERMINISTIC_MODEL_REQUEST_INVALID"})


def _closed_payload(value: object, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        _safe_error()
    return value


def _strings(value: object, maximum: int) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or any(not isinstance(item, str) or not 1 <= len(item) <= 128 for item in value)
        or len(value) != len(set(value))
    ):
        _safe_error()
    return value


def _analysis_content(value: object) -> dict[str, object]:
    raw = _closed_payload(value, {"store_summary", "candidates"})
    facts = AnalysisFacts.model_validate(raw)
    if facts.model_dump(mode="json") != raw:
        _safe_error()
    response = AgentAnalysisResponse.model_validate(
        {
            "candidates": [
                {
                    "product_id": candidate.product_id,
                    "rank": rank,
                    "impact_explanation": "该商品经营表现需要关注",
                    "reason": "可信指标显示转化表现偏低",
                    "recommended_action": "建议核验商品标题与详情",
                    "confidence": "0.8000",
                }
                for rank, candidate in enumerate(facts.candidates, start=1)
            ]
        }
    )
    validate_agent_response(facts, response)
    return response.model_dump(mode="json")


def _evidence_is_supplied(
    output: OptimizationProposalOutput,
    allowed_facts: set[str],
    allowed_citations: set[str],
) -> bool:
    evidence_lists = [section.evidence for section in output.description]
    evidence_lists.extend(change.evidence for change in output.changes)
    evidence_lists.extend(item.evidence for item in output.attribute_completions)
    evidence_lists.extend(item.evidence for item in output.price_suggestions)
    evidence_lists.extend(item.evidence for item in output.sku_suggestions)
    return all(
        (item.kind == "fact" and item.value in allowed_facts)
        or (item.kind == "citation" and item.value in allowed_citations)
        for evidence in evidence_lists
        for item in evidence
    ) and all(citation.chunk_id in allowed_citations for citation in output.citations)


def _validated_output(
    value: object,
    trusted: TrustedOptimizationInput,
    required_changes: list[ValidatedRequiredChange],
    allowed_facts: set[str],
    allowed_citations: set[str],
) -> OptimizationProposalOutput:
    output = OptimizationProposalOutput.model_validate(value)
    validate_optimization_response(trusted, required_changes, output)
    if (
        not _evidence_is_supplied(output, allowed_facts, allowed_citations)
        or not validate_optimization_output(trusted, output).passed
    ):
        _safe_error()
    return output


def _declare_change(
    output: dict[str, object],
    trusted: TrustedOptimizationInput,
    field: str,
    evidence: list[dict[str, str]],
) -> None:
    output["changes"] = [
        item for item in output["changes"] if item["field"] != field
    ]
    current_field = "search_keywords" if field == "keywords" else field
    output["changes"].append(
        {
            "field": field,
            "current_value": trusted.model_dump(mode="json")[current_field],
            "suggested_value": output[field],
            "reason": "根据可信事实调整商品文案",
            "evidence": evidence,
        }
    )


def _optimization_content(payload: dict[str, object]) -> dict[str, object]:
    fields = {
        "iteration",
        "trusted_facts",
        "allowed_fact_paths",
        "allowed_rule_chunk_ids",
        "required_changes",
        "response_template",
    }
    _closed_payload(payload, fields)
    if (
        not isinstance(payload["iteration"], int)
        or isinstance(payload["iteration"], bool)
        or not 0 <= payload["iteration"] <= 2
        or not isinstance(payload["required_changes"], list)
        or len(payload["required_changes"]) > 20
    ):
        _safe_error()
    trusted = TrustedOptimizationInput.model_validate(payload["trusted_facts"])
    required_changes = [
        ValidatedRequiredChange.model_validate(item)
        for item in payload["required_changes"]
    ]
    allowed_facts = set(_strings(payload["allowed_fact_paths"], 1000))
    allowed_citations = set(_strings(payload["allowed_rule_chunk_ids"], 50))
    template = _validated_output(
        payload["response_template"],
        trusted,
        required_changes,
        allowed_facts,
        allowed_citations,
    )
    output = template.model_dump(mode="json")

    if not output["title"].startswith(_RESERVED_PREFIX):
        output["title"] = ("优选" + output["title"])[:60]
        _declare_change(
            output,
            trusted,
            "title",
            [{"kind": "fact", "value": "product.title"}],
        )

    for change in required_changes:
        if change.field == "title":
            field = "title"
        elif change.field in {"selling_point", "selling_points"} or change.field.startswith(
            "selling_points["
        ):
            field = "selling_points"
        elif change.field == "description" or change.field.startswith("description["):
            field = "description"
        elif change.field in {"keyword", "keywords"} or change.field.startswith("keywords["):
            field = "keywords"
        else:
            _safe_error()
        if not set(change.citation_chunk_ids) <= allowed_citations:
            _safe_error()
        fact_path = f"product.{field if field != 'keywords' else 'search_keywords'}"
        evidence = (
            [{"kind": "citation", "value": item} for item in change.citation_chunk_ids]
            if change.citation_chunk_ids
            else [{"kind": "fact", "value": fact_path}]
        )
        if not change.citation_chunk_ids and fact_path not in allowed_facts:
            _safe_error()

        suffix = "（已调整）"
        if field == "title":
            base = _RESERVED_PREFIX if output["title"].startswith(_RESERVED_PREFIX) else output["title"]
            output["title"] = base[: 60 - len(suffix)] + suffix
        elif field == "selling_points":
            output[field][0] = output[field][0][: 80 - len(suffix)] + suffix
        elif field == "description":
            suffix = " 已按要求调整。"
            output[field][0]["body"] = output[field][0]["body"][: 1000 - len(suffix)] + suffix
        else:
            output[field][0] = output[field][0][:30] + "优化"
        _declare_change(output, trusted, field, evidence)
        listed = {item["chunk_id"] for item in output["citations"]}
        output["citations"].extend(
            {"chunk_id": item}
            for item in change.citation_chunk_ids
            if item not in listed
        )

    return _validated_output(
        output, trusted, required_changes, allowed_facts, allowed_citations
    ).model_dump(mode="json")


def _compliance_content(payload: dict[str, object]) -> dict[str, object]:
    fields = {
        "iteration",
        "candidate_output",
        "deterministic_result",
        "trusted_facts",
        "canonical_citations",
    }
    _closed_payload(payload, fields)
    if (
        not isinstance(payload["iteration"], int)
        or isinstance(payload["iteration"], bool)
        or not 0 <= payload["iteration"] <= 2
        or not isinstance(payload["canonical_citations"], list)
    ):
        _safe_error()
    trusted = TrustedOptimizationInput.model_validate(payload["trusted_facts"])
    candidate = OptimizationProposalOutput.model_validate(payload["candidate_output"])
    validate_optimization_response(trusted, [], candidate)
    canonical = [
        CanonicalRuleCitation.model_validate(item)
        for item in payload["canonical_citations"]
    ]
    citation_ids = [item.chunk_id for item in canonical]
    if (
        len(citation_ids) != len(set(citation_ids))
        or any(not item.active or not item.applicable for item in canonical)
    ):
        _safe_error()
    deterministic = _closed_payload(
        payload["deterministic_result"],
        {"passed", "violations", "canonical_citation_chunk_ids"},
    )
    if (
        not isinstance(deterministic["passed"], bool)
        or not isinstance(deterministic["violations"], list)
        or not isinstance(deterministic["canonical_citation_chunk_ids"], list)
    ):
        _safe_error()

    if not candidate.title.startswith(_RESERVED_PREFIX):
        response = ComplianceAgentResponse.model_validate(
            {
                "passed": True,
                "risk_level": "low",
                "violations": [],
                "required_changes": [],
                "citations": [{"chunk_id": item} for item in citation_ids],
                "confidence": "0.9000",
                "degraded": False,
            }
        )
    else:
        if not citation_ids:
            _safe_error()
        first = citation_ids[0]
        response = ComplianceAgentResponse.model_validate(
            {
                "passed": False,
                "risk_level": "medium",
                "violations": [
                    {
                        "code": "UNPROVABLE_PROMISE",
                        "field": "title",
                        "message_zh": "标题包含无法证实的承诺",
                        "citation_chunk_ids": [first],
                    }
                ],
                "required_changes": [
                    {
                        "source_track": "semantic",
                        "source_violation_code": "UNPROVABLE_PROMISE",
                        "field": "title",
                        "instruction": "请调整标题中的无法证实承诺",
                        "citation_chunk_ids": [first],
                    }
                ],
                "citations": [{"chunk_id": first}],
                "confidence": "0.9000",
                "degraded": False,
            }
        )
    validate_compliance_response(trusted, response)
    return response.model_dump(mode="json")


def _completion(content: dict[str, object]) -> dict[str, object]:
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        content, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
    }


def create_deterministic_deepseek() -> FastAPI:
    app = FastAPI()
    app.state.calls = []

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(_request, _error) -> JSONResponse:
        return JSONResponse(content=_ERROR, status_code=422)

    @app.get("/health/live")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/chat/completions")
    async def completions(request: CompletionRequest) -> dict[str, object]:
        try:
            if [message.role for message in request.messages] != ["system", "user"]:
                _safe_error()
            payload = json.loads(
                request.messages[1].content,
                parse_constant=lambda _value: _safe_error(),
            )
            if not isinstance(payload, dict):
                _safe_error()
            if set(payload) == {"facts"}:
                kind, content = "analysis", _analysis_content(payload["facts"])
            elif "response_template" in payload and "trusted_facts" in payload:
                kind, content = "optimization", _optimization_content(payload)
            elif "candidate_output" in payload and "canonical_citations" in payload:
                kind, content = "compliance", _compliance_content(payload)
            else:
                _safe_error()
        except HTTPException:
            raise
        except (TypeError, ValueError):
            _safe_error()

        attempt = 1 + sum(call["kind"] == kind for call in app.state.calls)
        app.state.calls.append({"kind": kind, "attempt": attempt})
        return _completion(content)

    return app
