from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.common import KnowledgeVersionStatus
from backend.config import Settings
from backend.knowledge_index import KnowledgeDependencyError
from backend.knowledge_search import (
    KnowledgeSearchHit,
    get_knowledge_search_dependencies,
    search_active_knowledge,
)
from backend.models import KnowledgeChunk, KnowledgeDocument, KnowledgeDocumentVersion
from backend.optimization_runs import OwnedOptimizationContext
from backend.optimization_worker import OptimizationLeaseLost, TrustedInputLoadFailure
from backend.schemas import CanonicalRuleCitation, TrustedOptimizationInput


BeforeTrustedInputExternalAttempt = Callable[[], Awaitable[None]]

_GENERAL_RULE_CATEGORY = "通用规则"
_QUERY_SUFFIX = "标题 卖点 详情 属性 SKU 价格 合规"
_QUERY_MAX_CHARS = 1600
_TOP_K = 10


def _query(context: OwnedOptimizationContext) -> str:
    return " ".join(
        part.strip()
        for part in (
            context.title,
            context.category,
            context.brand,
            *context.selling_points,
            _QUERY_SUFFIX,
        )
        if part.strip()
    )[:_QUERY_MAX_CHARS]


async def _canonical_citations(
    session: AsyncSession,
    *,
    hits: list[KnowledgeSearchHit],
    categories: list[str],
) -> tuple[CanonicalRuleCitation, ...]:
    if not hits:
        return ()
    rows = (
        await session.execute(
            select(KnowledgeChunk, KnowledgeDocumentVersion, KnowledgeDocument)
            .join(KnowledgeDocumentVersion, KnowledgeChunk.version_id == KnowledgeDocumentVersion.id)
            .join(
                KnowledgeDocument,
                KnowledgeDocument.current_version_id == KnowledgeDocumentVersion.id,
            )
            .where(
                KnowledgeChunk.id.in_([hit.chunk_id for hit in hits]),
                KnowledgeChunk.version_id == KnowledgeDocumentVersion.id,
                KnowledgeDocumentVersion.document_id == KnowledgeDocument.id,
                KnowledgeDocument.enabled.is_(True),
                KnowledgeDocumentVersion.status == KnowledgeVersionStatus.ACTIVE,
                KnowledgeDocument.category.in_(categories),
            )
        )
    ).all()
    canonical = {chunk.id: (chunk, version, document) for chunk, version, document in rows}
    accepted: dict[str, CanonicalRuleCitation] = {}
    for hit in hits:
        row = canonical.get(hit.chunk_id)
        if row is None:
            continue
        chunk, version, document = row
        if (
            chunk.canonical_text != hit.canonical_text
            or document.name != hit.document_name
            or version.version_number != hit.version_number
            or document.category != hit.category
        ):
            continue
        accepted.setdefault(
            chunk.id,
            CanonicalRuleCitation(
                document_id=document.id,
                version_id=version.id,
                chunk_id=chunk.id,
                document_name=document.name,
                version_number=version.version_number,
                category=document.category,
                canonical_text=chunk.canonical_text,
                active=True,
                applicable=True,
            ),
        )
    return tuple(accepted[chunk_id] for chunk_id in sorted(accepted))


async def load_optimization_trusted_input(
    context: OwnedOptimizationContext,
    before_external_attempt: BeforeTrustedInputExternalAttempt,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> TrustedOptimizationInput:
    del settings
    categories = list(dict.fromkeys((context.category, _GENERAL_RULE_CATEGORY)))
    try:
        async with session_factory() as search_session:
            outcome = await search_active_knowledge(
                search_session,
                query=_query(context),
                categories=categories,
                top_k=_TOP_K,
                retrieval_path="hybrid_rerank",
                load_dependencies=get_knowledge_search_dependencies,
                before_external_attempt=before_external_attempt,
            )
    except OptimizationLeaseLost:
        raise
    except KnowledgeDependencyError as error:
        code = error.code if error.code in {
            "KNOWLEDGE_MODEL_UNAVAILABLE", "KNOWLEDGE_DEPENDENCY_TIMEOUT"
        } else "KNOWLEDGE_DEPENDENCY_ERROR"
        raise TrustedInputLoadFailure(code) from None
    async with session_factory() as citation_session:
        citations = await _canonical_citations(
            citation_session, hits=outcome.hits, categories=categories
        )
    if not citations:
        quality = "zero_hit"
    elif outcome.quality_status == "low_confidence":
        quality = "low_confidence"
    elif outcome.quality_status == "normal" and outcome.hits[0].chunk_id in {
        citation.chunk_id for citation in citations
    }:
        quality = "normal"
    else:
        quality = "low_confidence"
    return TrustedOptimizationInput(
        store_id=context.store_id,
        product_id=context.product_id,
        base_product_version=context.base_product_version,
        title=context.title,
        category=context.category,
        brand=context.brand,
        selling_points=list(context.selling_points),
        description=context.description,
        search_keywords=list(context.search_keywords),
        attributes=context.attributes,
        skus=list(context.skus),
        candidate_metrics=context.candidate_metrics,
        candidate_evidence=list(context.candidate_evidence),
        rag_quality=quality,
        canonical_rule_citations=list(citations),
    )
