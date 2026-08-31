from dataclasses import dataclass

from backend.schemas import ManualRevisionRequest, OptimizationProposalOutput


@dataclass(frozen=True)
class ManualReviewDomainError(Exception):
    code: str
    status_code: int


def compose_manual_output(
    request: ManualRevisionRequest,
    parent: OptimizationProposalOutput,
) -> OptimizationProposalOutput:
    values = request.model_dump(exclude={"parent_revision_id", "base_product_version"})
    values.update(
        parent.model_dump(include={"citations", "price_suggestions", "sku_suggestions"})
    )
    return OptimizationProposalOutput.model_validate(values)
