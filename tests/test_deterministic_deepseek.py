import json
from decimal import Decimal

import httpx
import pytest

from backend.analysis_agent import parse_agent_response, validate_agent_response
from backend.common import ComplianceRiskLevel
from backend.compliance_agent import parse_compliance_response, validate_compliance_response
from backend.optimization_agent import (
    parse_optimization_response,
    validate_optimization_response,
)
from backend.optimization_validation import validate_optimization_output
from backend.schemas import (
    AnalysisFacts,
    CanonicalRuleCitation,
    ProductMetrics,
    StoreMetrics,
    TrustedAnalysisCandidate,
    TrustedOptimizationInput,
    TrustedProductSku,
    ValidatedRequiredChange,
)
from tests.support.deterministic_deepseek import create_deterministic_deepseek


RULE_CHUNK = "phase10-rule-chunk-1"
RESERVED_PREFIX = "阶段十连续失败验证"


def _analysis_facts() -> AnalysisFacts:
    candidates = []
    for index in (1, 2):
        product_id = f"phase10-product-{index}"
        product_code = f"PHASE10-{index}"
        metrics = ProductMetrics.from_totals(
            impressions=100,
            clicks=10,
            orders=1,
            units=1,
            revenue=Decimal("100.00"),
            refunds=0,
        ).model_copy(update={"product_id": product_id, "product_code": product_code})
        candidates.append(
            TrustedAnalysisCandidate(
                product_id=product_id,
                product_code=product_code,
                anomaly_types=["低转化"],
                metrics=metrics,
                business_impact=Decimal("100.00"),
                evidence=["近30日点击10次，成交1件"],
            )
        )
    return AnalysisFacts(
        store_summary=StoreMetrics(
            store_id="phase10-store-1",
            impressions=200,
            clicks=20,
            orders=2,
            units=2,
            revenue=Decimal("200.00"),
            refunds=0,
            ctr=Decimal("0.1000"),
            conversion_rate=Decimal("0.1000"),
            refund_rate=Decimal("0.0000"),
            average_order_value=Decimal("100.0000"),
        ),
        candidates=candidates,
    )


def _trusted_input(title: str = "合成桌面收纳盒") -> TrustedOptimizationInput:
    metrics = ProductMetrics.from_totals(
        impressions=100,
        clicks=10,
        orders=1,
        units=1,
        revenue=Decimal("100.00"),
        refunds=0,
    ).model_copy(
        update={"product_id": "phase10-product-1", "product_code": "PHASE10-1"}
    )
    return TrustedOptimizationInput(
        store_id="phase10-store-1",
        product_id="phase10-product-1",
        base_product_version=1,
        title=title,
        category="家居",
        brand="演示品牌",
        selling_points=["分区收纳"],
        description="用于整理桌面小物件，便于分类收纳。",
        search_keywords=["收纳盒", "桌面整理"],
        attributes={"材质": "棉"},
        skus=[
            TrustedProductSku(
                id="phase10-sku-1",
                code="PHASE10-RED",
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
                document_id="phase10-document-1",
                version_id="phase10-version-1",
                chunk_id=RULE_CHUNK,
                document_name="通用商品规则",
                version_number=1,
                category="通用规则",
                canonical_text="商品宣传应如实描述，不得夸大效果。",
                active=True,
                applicable=True,
            )
        ],
    )


def _response_template(trusted: TrustedOptimizationInput) -> dict[str, object]:
    description = [
        {
            "heading": "商品详情",
            "body": trusted.description[:1000],
            "evidence": [{"kind": "fact", "value": "product.description"}],
        }
    ]
    return {
        "title": trusted.title,
        "selling_points": trusted.selling_points,
        "description": description,
        "keywords": trusted.search_keywords,
        "attribute_completions": [],
        "changes": [
            {
                "field": "description",
                "current_value": trusted.description,
                "suggested_value": description,
                "reason": "将可信商品详情整理为分节文案",
                "evidence": [{"kind": "fact", "value": "product.description"}],
            }
        ],
        "citations": [],
        "price_suggestions": [],
        "sku_suggestions": [],
    }


def _allowed_fact_paths() -> list[str]:
    return [
        "product.title",
        "product.selling_points",
        "product.description",
        "product.search_keywords",
    ]


def _optimization_payload(
    trusted: TrustedOptimizationInput,
    required_changes: list[ValidatedRequiredChange] | None = None,
) -> dict[str, object]:
    return {
        "iteration": 0,
        "trusted_facts": trusted.model_dump(mode="json"),
        "allowed_fact_paths": _allowed_fact_paths(),
        "allowed_rule_chunk_ids": [RULE_CHUNK],
        "required_changes": [
            change.model_dump(mode="json") for change in required_changes or []
        ],
        "response_template": _response_template(trusted),
    }


def _compliance_payload(
    trusted: TrustedOptimizationInput, candidate_output: dict[str, object]
) -> dict[str, object]:
    return {
        "iteration": 0,
        "candidate_output": candidate_output,
        "deterministic_result": {
            "passed": True,
            "violations": [],
            "canonical_citation_chunk_ids": [],
        },
        "trusted_facts": trusted.model_dump(mode="json"),
        "canonical_citations": [
            citation.model_dump(mode="json")
            for citation in trusted.canonical_rule_citations
        ],
    }


def _request(payload: object) -> dict[str, object]:
    return {
        "model": "deepseek-v4-flash",
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "只输出 JSON。"},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            },
        ],
    }


@pytest.fixture
def request_body():
    facts = _analysis_facts()
    trusted = _trusted_input()
    template = _response_template(trusted)

    def analysis_validator(content: str) -> bool:
        validate_agent_response(facts, parse_agent_response(content))
        return True

    def optimization_validator(content: str) -> bool:
        output = validate_optimization_response(
            trusted, [], parse_optimization_response(content)
        )
        return validate_optimization_output(trusted, output).passed

    def compliance_validator(content: str) -> bool:
        response = validate_compliance_response(
            trusted, parse_compliance_response(content)
        )
        return (
            response.passed
            and response.risk_level is ComplianceRiskLevel.LOW
            and [citation.chunk_id for citation in response.citations] == [RULE_CHUNK]
        )

    return {
        "analysis": (_request({"facts": facts.model_dump(mode="json")}), analysis_validator),
        "optimization": (_request(_optimization_payload(trusted)), optimization_validator),
        "compliance": (
            _request(_compliance_payload(trusted, template)),
            compliance_validator,
        ),
    }


@pytest.mark.parametrize("kind", ["analysis", "optimization", "compliance"])
async def test_server_returns_a_production_validated_response(kind, request_body) -> None:
    request_json, validator = request_body[kind]
    app = create_deterministic_deepseek()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://model"
    ) as client:
        response = await client.post("/chat/completions", json=request_json)

    assert response.status_code == 200
    content = response.json()["choices"][0]["message"]["content"]
    assert validator(content)
    assert app.state.calls == [{"kind": kind, "attempt": 1}]


@pytest.mark.parametrize(
    "request_json",
    [
        {"model": "unknown-provider-marker"},
        _request("unknown-provider-marker"),
        _request({"unexpected": "unknown-provider-marker"}),
        _request(
            {
                "trusted_facts": {},
                "response_template": {"title": "unknown-provider-marker"},
            }
        ),
        {
            "model": "deepseek-v4-flash",
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "只输出 JSON。"},
                {
                    "role": "user",
                    "content": "unknown-provider-marker" + "x" * 1_000_001,
                },
            ],
        },
    ],
    ids=["unknown-model", "non-object", "unknown-shape", "malformed-shape", "oversized"],
)
async def test_server_rejects_unknown_oversized_or_malformed_shapes_without_echoing_body(
    request_json: dict[str, object],
) -> None:
    app = create_deterministic_deepseek()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://model"
    ) as client:
        response = await client.post("/chat/completions", json=request_json)

    assert response.status_code == 422
    assert response.json() == {
        "detail": {"code": "DETERMINISTIC_MODEL_REQUEST_INVALID"}
    }
    assert "unknown-provider-marker" not in response.text
    assert "unknown-provider-marker" not in repr(app.state.calls)
    assert app.state.calls == []


@pytest.mark.parametrize(
    ("field", "changed_field"),
    [
        ("title", "title"),
        ("selling_points[0]", "selling_points"),
        ("description[0].body", "description"),
        ("keywords[0]", "keywords"),
    ],
)
async def test_required_changes_update_output_and_declare_allowlisted_evidence(
    field: str, changed_field: str
) -> None:
    trusted = _trusted_input()
    change = ValidatedRequiredChange(
        source_track="semantic",
        source_violation_code="SEMANTIC_CONTRADICTION",
        field=field,
        instruction="请按可信依据调整此字段",
        citation_chunk_ids=[RULE_CHUNK],
    )
    payload = _optimization_payload(trusted, [change])
    app = create_deterministic_deepseek()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://model"
    ) as client:
        response = await client.post("/chat/completions", json=_request(payload))

    output = parse_optimization_response(
        response.json()["choices"][0]["message"]["content"]
    )
    validate_optimization_response(trusted, [change], output)
    assert validate_optimization_output(trusted, output).passed
    assert output.model_dump(mode="json")[changed_field] != payload["response_template"][changed_field]
    declaration = next(item for item in output.changes if item.field == changed_field)
    assert {(item.kind, item.value) for item in declaration.evidence} == {
        ("citation", RULE_CHUNK)
    }
    assert [citation.chunk_id for citation in output.citations] == [RULE_CHUNK]


async def test_reserved_title_fails_semantically_after_deterministic_revision() -> None:
    trusted = _trusted_input(f"{RESERVED_PREFIX}原始标题")
    original = _response_template(trusted)
    app = create_deterministic_deepseek()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://model"
    ) as client:
        first = await client.post(
            "/chat/completions", json=_request(_compliance_payload(trusted, original))
        )
        first_review = parse_compliance_response(
            first.json()["choices"][0]["message"]["content"]
        )
        validate_compliance_response(trusted, first_review)
        revised = await client.post(
            "/chat/completions",
            json=_request(_optimization_payload(trusted, first_review.required_changes)),
        )
        revised_output = parse_optimization_response(
            revised.json()["choices"][0]["message"]["content"]
        )
        second = await client.post(
            "/chat/completions",
            json=_request(
                _compliance_payload(trusted, revised_output.model_dump(mode="json"))
            ),
        )
        second_review = parse_compliance_response(
            second.json()["choices"][0]["message"]["content"]
        )

    validate_optimization_response(trusted, first_review.required_changes, revised_output)
    assert validate_optimization_output(trusted, revised_output).passed
    validate_compliance_response(trusted, second_review)
    for review in (first_review, second_review):
        assert review.passed is False
        assert review.risk_level is ComplianceRiskLevel.MEDIUM
        assert [(violation.code, violation.field) for violation in review.violations] == [
            ("UNPROVABLE_PROMISE", "title")
        ]
        assert len(review.required_changes) == 1
        assert review.required_changes[0].citation_chunk_ids == [RULE_CHUNK]
        assert [citation.chunk_id for citation in review.citations] == [RULE_CHUNK]
    assert revised_output.title.startswith(RESERVED_PREFIX)
    assert revised_output.title != original["title"]
    assert app.state.calls == [
        {"kind": "compliance", "attempt": 1},
        {"kind": "optimization", "attempt": 1},
        {"kind": "compliance", "attempt": 2},
    ]


async def test_health_and_counters_expose_only_safe_process_state() -> None:
    app = create_deterministic_deepseek()
    facts = _analysis_facts()
    request_json = _request({"facts": facts.model_dump(mode="json")})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://model"
    ) as client:
        health = await client.get("/health/live")
        await client.post("/chat/completions", json=request_json)
        await client.post("/chat/completions", json=request_json)

    assert health.json() == {"status": "ok"}
    assert app.state.calls == [
        {"kind": "analysis", "attempt": 1},
        {"kind": "analysis", "attempt": 2},
    ]
    assert all(set(call) == {"kind", "attempt"} for call in app.state.calls)
