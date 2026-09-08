from __future__ import annotations

import json
from decimal import Decimal
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field


_ERROR = {"detail": {"code": "DETERMINISTIC_MODEL_REQUEST_INVALID"}}
_RESERVED_PREFIX = "阶段十连续失败验证"
_OUTPUT_FIELDS = {
    "title",
    "selling_points",
    "description",
    "keywords",
    "attribute_completions",
    "changes",
    "citations",
    "price_suggestions",
    "sku_suggestions",
}


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Message(_ClosedModel):
    role: Literal["system", "user"]
    content: str = Field(min_length=1, max_length=1_000_000)


class CompletionRequest(_ClosedModel):
    model: Literal["deepseek-v4-flash"]
    response_format: dict[Literal["type"], Literal["json_object"]]
    messages: list[_Message] = Field(min_length=2, max_length=2)


class _Evidence(_ClosedModel):
    kind: Literal["fact", "citation"]
    value: str = Field(min_length=1, max_length=128)


class _Description(_ClosedModel):
    heading: str = Field(min_length=1, max_length=40)
    body: str = Field(min_length=1, max_length=1000)
    evidence: list[_Evidence] = Field(min_length=1, max_length=20)


class _Change(_ClosedModel):
    field: Literal["title", "selling_points", "description", "keywords"]
    current_value: str | list[str] | list[_Description]
    suggested_value: str | list[str] | list[_Description]
    reason: str = Field(min_length=1, max_length=500)
    evidence: list[_Evidence] = Field(min_length=1, max_length=20)


class _Attribute(_ClosedModel):
    target_attribute: str = Field(min_length=1, max_length=64)
    current_value: str | None
    suggested_value: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=500)
    evidence: list[_Evidence] = Field(min_length=1, max_length=20)


class _Price(_ClosedModel):
    target_sku_id: str = Field(min_length=1, max_length=36)
    current_price: Decimal
    suggested_price: Decimal
    reason: str = Field(min_length=1, max_length=500)
    evidence: list[_Evidence] = Field(min_length=1, max_length=20)


class _Sku(_ClosedModel):
    target_sku_id: str = Field(min_length=1, max_length=36)
    current_code: str = Field(min_length=1, max_length=64)
    current_spec: dict[str, str] = Field(max_length=50)
    suggested_code: str = Field(min_length=1, max_length=64)
    suggested_spec: dict[str, str] = Field(max_length=50)
    reason: str = Field(min_length=1, max_length=500)
    evidence: list[_Evidence] = Field(min_length=1, max_length=20)


class _Citation(_ClosedModel):
    chunk_id: str = Field(min_length=1, max_length=128)


class _Output(_ClosedModel):
    title: str = Field(min_length=1, max_length=60)
    selling_points: list[str] = Field(min_length=1, max_length=5)
    description: list[_Description] = Field(min_length=1, max_length=10)
    keywords: list[str] = Field(min_length=1, max_length=20)
    attribute_completions: list[_Attribute] = Field(max_length=20)
    changes: list[_Change] = Field(max_length=4)
    citations: list[_Citation] = Field(max_length=20)
    price_suggestions: list[_Price] = Field(max_length=20)
    sku_suggestions: list[_Sku] = Field(max_length=20)


class _Trusted(_ClosedModel):
    store_id: str = Field(min_length=1, max_length=36)
    product_id: str = Field(min_length=1, max_length=36)
    base_product_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=512)
    category: str = Field(min_length=1, max_length=128)
    brand: str = Field(max_length=128)
    selling_points: list[str] = Field(min_length=1, max_length=20)
    description: str = Field(min_length=1, max_length=8000)
    search_keywords: list[str] = Field(min_length=1, max_length=100)
    attributes: dict[str, str] = Field(max_length=50)
    skus: list[dict[str, object]] = Field(max_length=100)
    candidate_metrics: dict[str, object]
    candidate_evidence: list[str] = Field(min_length=1, max_length=50)
    rag_quality: Literal["normal", "zero_hit", "low_confidence"]
    canonical_rule_citations: list[dict[str, object]] = Field(max_length=50)


class _RequiredChange(_ClosedModel):
    source_track: Literal["deterministic", "semantic"]
    source_violation_code: str = Field(min_length=1, max_length=64)
    field: str = Field(min_length=1, max_length=64)
    instruction: str = Field(min_length=1, max_length=240)
    citation_chunk_ids: list[str] = Field(max_length=20)


class _CanonicalCitation(_ClosedModel):
    document_id: str = Field(max_length=36)
    version_id: str = Field(max_length=36)
    chunk_id: str = Field(min_length=1, max_length=128)
    document_name: str = Field(max_length=255)
    version_number: int
    category: str = Field(max_length=64)
    canonical_text: str = Field(max_length=12000)
    active: bool
    applicable: bool


class _AnalysisCandidate(_ClosedModel):
    product_id: str = Field(min_length=1, max_length=128)
    product_code: str = Field(min_length=1, max_length=128)
    anomaly_types: list[str]
    metrics: dict[str, object]
    business_impact: Decimal
    evidence: list[str]


class _AnalysisFacts(_ClosedModel):
    store_summary: dict[str, object]
    candidates: list[_AnalysisCandidate] = Field(min_length=1, max_length=100)


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


def _fact_paths(trusted: _Trusted) -> set[str]:
    paths = {
        "product.title",
        "product.category",
        "product.brand",
        "product.selling_points",
        "product.description",
        "product.search_keywords",
    }
    paths.update(f"product.attributes.{key}" for key in trusted.attributes)
    for sku in trusted.skus:
        sku_id = sku.get("id")
        if not isinstance(sku_id, str):
            _safe_error()
        paths.update(
            f"product.skus.{sku_id}.{field}"
            for field in ("code", "spec", "price", "stock")
        )
    paths.update(
        f"candidate.metrics.{field}"
        for field, value in trusted.candidate_metrics.items()
        if value is not None
    )
    paths.update(
        f"candidate.evidence.{index}"
        for index in range(len(trusted.candidate_evidence))
    )
    return paths


def _validate_output(
    output: _Output,
    trusted: _Trusted,
    allowed_facts: set[str],
    allowed_citations: set[str],
) -> _Output:
    citation_ids = [citation.chunk_id for citation in output.citations]
    if len(citation_ids) != len(set(citation_ids)) or not set(citation_ids) <= allowed_citations:
        _safe_error()
    evidence_lists = [section.evidence for section in output.description]
    evidence_lists += [change.evidence for change in output.changes]
    evidence_lists += [item.evidence for item in output.attribute_completions]
    evidence_lists += [item.evidence for item in output.price_suggestions]
    evidence_lists += [item.evidence for item in output.sku_suggestions]
    if any(
        (item.kind == "fact" and item.value not in allowed_facts)
        or (
            item.kind == "citation"
            and (item.value not in allowed_citations or item.value not in citation_ids)
        )
        for evidence in evidence_lists
        for item in evidence
    ):
        _safe_error()

    current = {
        "title": trusted.title,
        "selling_points": trusted.selling_points,
        "description": trusted.description,
        "keywords": trusted.search_keywords,
    }
    dumped = output.model_dump(mode="json")
    declared: set[str] = set()
    for change in output.changes:
        if (
            change.field in declared
            or change.current_value != current[change.field]
            or change.model_dump(mode="json")["suggested_value"] != dumped[change.field]
        ):
            _safe_error()
        declared.add(change.field)
    if any(dumped[field] != value and field not in declared for field, value in current.items()):
        _safe_error()
    return output


def _analysis_content(value: object) -> dict[str, object]:
    facts = _AnalysisFacts.model_validate(value)
    product_ids = [candidate.product_id for candidate in facts.candidates]
    if len(product_ids) != len(set(product_ids)):
        _safe_error()
    return {
        "candidates": [
            {
                "product_id": product_id,
                "rank": rank,
                "impact_explanation": "该商品经营表现需要关注",
                "reason": "可信指标显示转化表现偏低",
                "recommended_action": "建议核验商品标题与详情",
                "confidence": "0.8000",
            }
            for rank, product_id in enumerate(product_ids, start=1)
        ]
    }


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
    if not isinstance(payload["iteration"], int) or not 0 <= payload["iteration"] <= 2:
        _safe_error()
    trusted = _Trusted.model_validate(payload["trusted_facts"])
    allowed_facts = set(_strings(payload["allowed_fact_paths"], 1000))
    allowed_citations = set(_strings(payload["allowed_rule_chunk_ids"], 50))
    output = _validate_output(
        _Output.model_validate(payload["response_template"]),
        trusted,
        allowed_facts,
        allowed_citations,
    ).model_dump(mode="json")
    if not isinstance(payload["required_changes"], list) or len(payload["required_changes"]) > 20:
        _safe_error()

    for raw_change in payload["required_changes"]:
        change = _RequiredChange.model_validate(raw_change)
        field = change.field
        if field == "title":
            root = "title"
        elif field in {"selling_point", "selling_points"} or field.startswith("selling_points["):
            root = "selling_points"
        elif field == "description" or field.startswith("description["):
            root = "description"
        elif field in {"keyword", "keywords"} or field.startswith("keywords["):
            root = "keywords"
        else:
            _safe_error()
        if len(change.citation_chunk_ids) != len(set(change.citation_chunk_ids)) or not set(
            change.citation_chunk_ids
        ) <= allowed_citations:
            _safe_error()
        fact_path = f"product.{root if root != 'keywords' else 'search_keywords'}"
        evidence = (
            [{"kind": "citation", "value": item} for item in change.citation_chunk_ids]
            if change.citation_chunk_ids
            else [{"kind": "fact", "value": fact_path}]
        )
        if not change.citation_chunk_ids and fact_path not in allowed_facts:
            _safe_error()

        suffix = "（已调整）"
        if root == "title":
            base = _RESERVED_PREFIX if output["title"].startswith(_RESERVED_PREFIX) else output["title"]
            output["title"] = base[: 60 - len(suffix)] + suffix
        elif root == "selling_points":
            output[root][0] = output[root][0][: 80 - len(suffix)] + suffix
        elif root == "description":
            suffix = " 已按要求调整。"
            output[root][0]["body"] = output[root][0]["body"][: 1000 - len(suffix)] + suffix
        else:
            output[root][0] = output[root][0][:30] + "优化"
        output["changes"] = [item for item in output["changes"] if item["field"] != root]
        output["changes"].append(
            {
                "field": root,
                "current_value": getattr(
                    trusted, "search_keywords" if root == "keywords" else root
                ),
                "suggested_value": output[root],
                "reason": "根据合规要求调整字段",
                "evidence": evidence,
            }
        )
        listed = {item["chunk_id"] for item in output["citations"]}
        output["citations"] += [
            {"chunk_id": item} for item in change.citation_chunk_ids if item not in listed
        ]

    return _validate_output(
        _Output.model_validate(output), trusted, allowed_facts, allowed_citations
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
    if not isinstance(payload["iteration"], int) or not 0 <= payload["iteration"] <= 2:
        _safe_error()
    trusted = _Trusted.model_validate(payload["trusted_facts"])
    if not isinstance(payload["canonical_citations"], list):
        _safe_error()
    canonical = [
        _CanonicalCitation.model_validate(item) for item in payload["canonical_citations"]
    ]
    if any(not item.active or not item.applicable for item in canonical):
        _safe_error()
    citation_ids = [item.chunk_id for item in canonical]
    if len(citation_ids) != len(set(citation_ids)):
        _safe_error()
    candidate = _validate_output(
        _Output.model_validate(payload["candidate_output"]),
        trusted,
        _fact_paths(trusted),
        set(citation_ids),
    )
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

    citations = [{"chunk_id": item} for item in citation_ids]
    if not candidate.title.startswith(_RESERVED_PREFIX):
        return {
            "passed": True,
            "risk_level": "low",
            "violations": [],
            "required_changes": [],
            "citations": citations,
            "confidence": "0.9000",
            "degraded": False,
        }
    if not citation_ids:
        _safe_error()
    first = citation_ids[0]
    return {
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
