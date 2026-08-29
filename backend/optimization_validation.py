import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from backend.schemas import CanonicalRuleCitation, OptimizationProposalOutput, TrustedOptimizationInput


@dataclass(frozen=True)
class DeterministicViolation:
    code: str
    field: str
    message_zh: str


@dataclass(frozen=True)
class DeterministicComplianceResult:
    passed: bool
    violations: tuple[DeterministicViolation, ...]
    canonical_citations: tuple[CanonicalRuleCitation, ...]


def validate_optimization_output(
    trusted: TrustedOptimizationInput,
    output: OptimizationProposalOutput,
) -> DeterministicComplianceResult:
    messages = {
        "MISSING_TRUSTED_FACT": "缺少可信商品事实",
        "OUTPUT_BUSINESS_LENGTH": "输出长度不符合项目演示规则",
        "TITLE_LANGUAGE": "标题必须包含中文字符",
        "RESTRICTED_PHRASE": "包含项目演示限制短语",
        "CHANGE_TARGET_DUPLICATE": "文案变更目标重复",
        "CHANGE_CURRENT_MISMATCH": "当前值与可信快照不一致",
        "CHANGE_SUGGESTED_MISMATCH": "建议值与输出不一致",
        "OUTPUT_CHANGE_UNDECLARED": "输出变更未声明",
        "EVIDENCE_MISSING": "缺少有效证据",
        "EVIDENCE_FACT_PATH_INVALID": "事实证据路径无效",
        "EVIDENCE_CITATION_INVALID": "规则证据无效",
        "CITATION_DUPLICATE": "引用重复",
        "CITATION_UNKNOWN": "引用不在可信集合",
        "CITATION_EVIDENCE_UNLISTED": "规则证据未列入引用",
        "ATTRIBUTE_TARGET_DUPLICATE": "属性目标重复",
        "ATTRIBUTE_CURRENT_MISMATCH": "属性当前值不一致",
        "ATTRIBUTE_SOURCE_MISSING": "新增属性缺少规则来源",
        "SKU_TARGET_DUPLICATE": "SKU 目标重复",
        "SKU_UNKNOWN": "SKU 不属于当前商品",
        "SKU_CURRENT_MISMATCH": "SKU 当前值不一致",
        "PRICE_TARGET_DUPLICATE": "价格目标重复",
        "PRICE_SKU_UNKNOWN": "价格 SKU 不属于当前商品",
        "PRICE_CURRENT_MISMATCH": "价格当前值不一致",
        "PRICE_CURRENT_NONPOSITIVE": "当前价格不能产生合规建议",
        "PRICE_NONPOSITIVE": "建议价格必须大于零",
        "PRICE_PRECISION": "价格精度无效",
        "PRICE_RANGE": "建议价格超出允许范围",
        "RAG_QUALITY_INSUFFICIENT": "规则检索质量不足",
    }
    restricted_phrases = (
        "治疗",
        "治愈",
        "疗效",
        "药到病除",
        "100%安全",
        "绝对安全",
        "保证",
        "永久有效",
        "全网最低",
        "第一名",
    )
    violations: list[DeterministicViolation] = []
    seen_violations: set[tuple[str, str]] = set()

    def add(code: str, field: str) -> None:
        key = (field, code)
        if key not in seen_violations:
            seen_violations.add(key)
            violations.append(DeterministicViolation(code, field, messages[code]))

    def valid_fact_path(path: str) -> bool:
        if path in {
            "product.title",
            "product.category",
            "product.brand",
            "product.selling_points",
            "product.description",
            "product.search_keywords",
        }:
            return True
        attribute_prefix = "product.attributes."
        if path.startswith(attribute_prefix):
            return path[len(attribute_prefix) :] in trusted.attributes
        sku_prefix = "product.skus."
        if path.startswith(sku_prefix):
            sku_id, separator, field = path[len(sku_prefix) :].rpartition(".")
            return bool(separator) and sku_id in sku_by_id and field in {
                "code",
                "spec",
                "price",
                "stock",
            }
        parts = path.split(".")
        if len(parts) == 3 and parts[:2] == ["candidate", "metrics"]:
            return (
                parts[2] in trusted.candidate_metrics.__class__.model_fields
                and getattr(trusted.candidate_metrics, parts[2]) is not None
            )
        if len(parts) == 3 and parts[:2] == ["candidate", "evidence"]:
            return parts[2].isdigit() and int(parts[2]) < len(trusted.candidate_evidence)
        return False

    def check_evidence(evidence: list, field: str) -> bool:
        if not evidence:
            add("EVIDENCE_MISSING", field)
            return False
        has_listed_citation = False
        for item in evidence:
            if item.kind == "fact":
                if not valid_fact_path(item.value):
                    add("EVIDENCE_FACT_PATH_INVALID", field)
            elif item.value not in allowed_citations:
                add("EVIDENCE_CITATION_INVALID", field)
            elif item.value not in listed_citations:
                add("CITATION_EVIDENCE_UNLISTED", field)
            else:
                has_listed_citation = True
        return has_listed_citation

    def check_text(field: str, value: str, minimum: int, maximum: int) -> None:
        if not minimum <= len(value) <= maximum:
            add("OUTPUT_BUSINESS_LENGTH", field)
        if any(phrase in value for phrase in restricted_phrases):
            add("RESTRICTED_PHRASE", field)

    def normalized(value: object) -> str:
        if isinstance(value, list):
            value = [
                item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                for item in value
            ]
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    sku_by_id = {sku.id: sku for sku in trusted.skus}
    allowed_citations = {
        citation.chunk_id: citation
        for citation in trusted.canonical_rule_citations
        if citation.active and citation.applicable
    }
    output_citation_ids: set[str] = set()
    listed_citations: set[str] = set()
    for citation in output.citations:
        if citation.chunk_id in output_citation_ids:
            add("CITATION_DUPLICATE", "citations")
        output_citation_ids.add(citation.chunk_id)
        if citation.chunk_id not in allowed_citations:
            add("CITATION_UNKNOWN", "citations")
        else:
            listed_citations.add(citation.chunk_id)
    if len(output.citations) > 20:
        add("OUTPUT_BUSINESS_LENGTH", "citations")

    if not trusted.store_id or not trusted.product_id or trusted.base_product_version < 1:
        add("MISSING_TRUSTED_FACT", "trusted")
    if not trusted.title or not trusted.category:
        add("MISSING_TRUSTED_FACT", "trusted")
    if (
        not trusted.candidate_metrics.product_id
        or not trusted.candidate_metrics.product_code
        or not trusted.candidate_evidence
    ):
        add("MISSING_TRUSTED_FACT", "trusted")
    if not trusted.skus or any(
        not sku.id
        or not sku.code
        or not sku.spec
        or any(not key or not value for key, value in sku.spec.items())
        for sku in trusted.skus
    ):
        add("MISSING_TRUSTED_FACT", "trusted")

    check_text("title", output.title, 1, 60)
    if not any("\u4e00" <= character <= "\u9fff" for character in output.title):
        add("TITLE_LANGUAGE", "title")
    if not 1 <= len(output.selling_points) <= 5:
        add("OUTPUT_BUSINESS_LENGTH", "selling_points")
    for index, point in enumerate(output.selling_points):
        check_text(f"selling_points[{index}]", point, 1, 80)
    if not 1 <= len(output.description) <= 10:
        add("OUTPUT_BUSINESS_LENGTH", "description")
    for index, section in enumerate(output.description):
        check_text(f"description[{index}].heading", section.heading, 1, 40)
        check_text(f"description[{index}].body", section.body, 1, 1000)
        check_evidence(section.evidence, f"description[{index}].evidence")
    if not 1 <= len(output.keywords) <= 20:
        add("OUTPUT_BUSINESS_LENGTH", "keywords")
    for index, keyword in enumerate(output.keywords):
        check_text(f"keywords[{index}]", keyword, 1, 32)
    if len(output.changes) > 4:
        add("OUTPUT_BUSINESS_LENGTH", "changes")
    if len(output.attribute_completions) > 20:
        add("OUTPUT_BUSINESS_LENGTH", "attribute_completions")
    if len(output.price_suggestions) > 20:
        add("OUTPUT_BUSINESS_LENGTH", "price_suggestions")
    if len(output.sku_suggestions) > 20:
        add("OUTPUT_BUSINESS_LENGTH", "sku_suggestions")

    changed_values = {
        "title": (trusted.title, output.title),
        "selling_points": (trusted.selling_points, output.selling_points),
        "description": (trusted.description, output.description),
        "keywords": (trusted.search_keywords, output.keywords),
    }
    declared_changes: set[str] = set()
    for change in output.changes:
        if change.field in declared_changes:
            add("CHANGE_TARGET_DUPLICATE", change.field)
        declared_changes.add(change.field)
        current, suggested = changed_values[change.field]
        if change.field == "description":
            current_matches = normalized(change.current_value) == normalized(current)
            suggested_matches = normalized(change.suggested_value) == normalized(suggested)
        else:
            current_matches = change.current_value == current
            suggested_matches = change.suggested_value == suggested
        if not current_matches:
            add("CHANGE_CURRENT_MISMATCH", change.field)
        if not suggested_matches:
            add("CHANGE_SUGGESTED_MISMATCH", change.field)
        check_text(f"changes.{change.field}.reason", change.reason, 1, 500)
        check_evidence(change.evidence, f"changes.{change.field}.evidence")
    for field, (current, suggested) in changed_values.items():
        if current != suggested and field not in declared_changes:
            add("OUTPUT_CHANGE_UNDECLARED", field)

    attribute_targets: set[str] = set()
    for attribute in output.attribute_completions:
        field = f"attribute_completions.{attribute.target_attribute}"
        if attribute.target_attribute in attribute_targets:
            add("ATTRIBUTE_TARGET_DUPLICATE", field)
        attribute_targets.add(attribute.target_attribute)
        has_citation = check_evidence(attribute.evidence, f"{field}.evidence")
        check_text(f"{field}.suggested_value", attribute.suggested_value, 1, 256)
        check_text(f"{field}.reason", attribute.reason, 1, 500)
        if attribute.target_attribute in trusted.attributes:
            if attribute.current_value != trusted.attributes[attribute.target_attribute]:
                add("ATTRIBUTE_CURRENT_MISMATCH", field)
        else:
            if attribute.current_value is not None:
                add("ATTRIBUTE_CURRENT_MISMATCH", field)
            if not has_citation:
                add("ATTRIBUTE_SOURCE_MISSING", field)

    price_targets: set[str] = set()
    cent = Decimal("0.01")
    for suggestion in output.price_suggestions:
        field = f"price_suggestions.{suggestion.target_sku_id}"
        if suggestion.target_sku_id in price_targets:
            add("PRICE_TARGET_DUPLICATE", field)
        price_targets.add(suggestion.target_sku_id)
        has_citation = check_evidence(suggestion.evidence, f"{field}.evidence")
        if not has_citation:
            add("EVIDENCE_CITATION_INVALID", f"{field}.evidence")
        check_text(f"{field}.reason", suggestion.reason, 1, 500)
        sku = sku_by_id.get(suggestion.target_sku_id)
        if sku is None:
            add("PRICE_SKU_UNKNOWN", field)
            continue
        if suggestion.current_price != sku.price:
            add("PRICE_CURRENT_MISMATCH", field)
        if sku.price <= 0:
            add("PRICE_CURRENT_NONPOSITIVE", field)
        if suggestion.suggested_price <= 0:
            add("PRICE_NONPOSITIVE", field)
        if sku.price.adjusted() >= 25 or suggestion.suggested_price.adjusted() >= 25:
            add("PRICE_RANGE", field)
            continue
        quantized = suggestion.suggested_price.quantize(cent, rounding=ROUND_HALF_UP)
        if suggestion.suggested_price != quantized:
            add("PRICE_PRECISION", field)
        elif sku.price > 0 and suggestion.suggested_price > 0:
            lower = (sku.price * Decimal("0.70")).quantize(cent, rounding=ROUND_HALF_UP)
            upper = (sku.price * Decimal("1.30")).quantize(cent, rounding=ROUND_HALF_UP)
            if not lower <= suggestion.suggested_price <= upper:
                add("PRICE_RANGE", field)

    sku_targets: set[str] = set()
    for suggestion in output.sku_suggestions:
        field = f"sku_suggestions.{suggestion.target_sku_id}"
        if suggestion.target_sku_id in sku_targets:
            add("SKU_TARGET_DUPLICATE", field)
        sku_targets.add(suggestion.target_sku_id)
        has_citation = check_evidence(suggestion.evidence, f"{field}.evidence")
        if not has_citation:
            add("EVIDENCE_CITATION_INVALID", f"{field}.evidence")
        check_text(f"{field}.suggested_code", suggestion.suggested_code, 1, 64)
        check_text(f"{field}.reason", suggestion.reason, 1, 500)
        for key, value in suggestion.suggested_spec.items():
            check_text(f"{field}.suggested_spec.{key}", key, 1, 64)
            check_text(f"{field}.suggested_spec.{key}", value, 1, 128)
        sku = sku_by_id.get(suggestion.target_sku_id)
        if sku is None:
            add("SKU_UNKNOWN", field)
            continue
        if suggestion.current_code != sku.code or suggestion.current_spec != sku.spec:
            add("SKU_CURRENT_MISMATCH", field)

    if trusted.rag_quality in {"zero_hit", "low_confidence"}:
        add("RAG_QUALITY_INSUFFICIENT", "rag_quality")

    ordered_violations = tuple(sorted(violations, key=lambda violation: (violation.field, violation.code)))
    canonical_citations = tuple(
        allowed_citations[chunk_id] for chunk_id in sorted(listed_citations)
    )
    return DeterministicComplianceResult(
        passed=not ordered_violations,
        violations=ordered_violations,
        canonical_citations=canonical_citations,
    )
