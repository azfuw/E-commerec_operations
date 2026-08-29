import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal, TypeAlias

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common import KnowledgeVersionStatus
from backend.config import get_settings
from backend.knowledge_index import (
    HybridVector,
    KnowledgeDependencyError,
    LocalKnowledgeModels,
    MilvusKnowledgeIndex,
)
from backend.models import KnowledgeChunk, KnowledgeDocument, KnowledgeDocumentVersion


@dataclass(frozen=True)
class KnowledgeSearchHit:
    chunk_id: str
    document_name: str
    version_number: int
    category: str
    canonical_text: str
    chunk_metadata: dict[str, object]
    dense_score: float | None
    sparse_score: float | None
    fusion_score: float | None
    reranker_score: float | None
    final_score: float


RetrievalPath = Literal["dense", "sparse", "hybrid", "hybrid_rerank"]


@dataclass(frozen=True)
class FusedRetrievalScore:
    chunk_id: str
    dense_score: float | None
    sparse_score: float | None
    dense_rank: int | None
    sparse_rank: int | None
    fusion_score: float


KnowledgeSearchDependencies: TypeAlias = tuple[
    LocalKnowledgeModels, MilvusKnowledgeIndex, dict[str, object]
]
KnowledgeSearchLoader: TypeAlias = Callable[[], KnowledgeSearchDependencies]

_CALIBRATION_PATH = Path("data/knowledge/evaluation/calibration.json")


def _ranked(scores: list[tuple[str, float]]) -> list[tuple[str, float]]:
    best: dict[str, float] = {}
    for chunk_id, score in scores:
        if chunk_id not in best or score > best[chunk_id]:
            best[chunk_id] = score
    return sorted(best.items(), key=lambda item: (-item[1], item[0]))


def fuse_rankings(
    *, dense: list[tuple[str, float]], sparse: list[tuple[str, float]], rrf_k: int
) -> list[FusedRetrievalScore]:
    dense_ranked = _ranked(dense)
    sparse_ranked = _ranked(sparse)
    dense_values = {chunk_id: (score, rank) for rank, (chunk_id, score) in enumerate(dense_ranked, 1)}
    sparse_values = {chunk_id: (score, rank) for rank, (chunk_id, score) in enumerate(sparse_ranked, 1)}
    fused = []
    for chunk_id in dense_values.keys() | sparse_values.keys():
        dense_score, dense_rank = dense_values.get(chunk_id, (None, None))
        sparse_score, sparse_rank = sparse_values.get(chunk_id, (None, None))
        fusion_score = sum(1 / (rrf_k + rank) for rank in (dense_rank, sparse_rank) if rank is not None)
        fused.append(
            FusedRetrievalScore(
                chunk_id=chunk_id,
                dense_score=dense_score,
                sparse_score=sparse_score,
                dense_rank=dense_rank,
                sparse_rank=sparse_rank,
                fusion_score=fusion_score,
            )
        )
    return sorted(fused, key=lambda score: (-score.fusion_score, score.chunk_id))


async def _active_version_ids(
    session: AsyncSession, categories: list[str] | None
) -> list[str]:
    statement = (
        select(KnowledgeDocumentVersion.id)
        .join(
            KnowledgeDocument,
            KnowledgeDocument.current_version_id == KnowledgeDocumentVersion.id,
        )
        .where(
            KnowledgeDocument.enabled.is_(True),
            KnowledgeDocumentVersion.status == KnowledgeVersionStatus.ACTIVE,
        )
        .order_by(KnowledgeDocumentVersion.id)
    )
    if categories:
        statement = statement.where(KnowledgeDocument.category.in_(categories))
    return list((await session.scalars(statement)).all())


async def _canonical_chunks(
    session: AsyncSession, *, chunk_ids: list[str], version_ids: list[str]
) -> dict[str, tuple[KnowledgeChunk, KnowledgeDocumentVersion, KnowledgeDocument]]:
    if not chunk_ids:
        return {}
    rows = (
        await session.execute(
            select(KnowledgeChunk, KnowledgeDocumentVersion, KnowledgeDocument)
            .join(KnowledgeDocumentVersion, KnowledgeChunk.version_id == KnowledgeDocumentVersion.id)
            .join(KnowledgeDocument, KnowledgeDocument.current_version_id == KnowledgeDocumentVersion.id)
            .where(
                KnowledgeChunk.id.in_(chunk_ids),
                KnowledgeDocumentVersion.id.in_(version_ids),
                KnowledgeDocumentVersion.status == KnowledgeVersionStatus.ACTIVE,
                KnowledgeDocument.enabled.is_(True),
            )
        )
    ).all()
    return {chunk.id: (chunk, version, document) for chunk, version, document in rows}


def _dense_candidates(scores: list[tuple[str, float]]) -> list[FusedRetrievalScore]:
    return [
        FusedRetrievalScore(chunk_id, score, None, rank, None, score)
        for rank, (chunk_id, score) in enumerate(_ranked(scores), 1)
    ]


def _sparse_candidates(scores: list[tuple[str, float]]) -> list[FusedRetrievalScore]:
    return [
        FusedRetrievalScore(chunk_id, None, score, None, rank, score)
        for rank, (chunk_id, score) in enumerate(_ranked(scores), 1)
    ]


def _hits_from_candidates(
    candidates: list[FusedRetrievalScore],
    canonical: dict[str, tuple[KnowledgeChunk, KnowledgeDocumentVersion, KnowledgeDocument]],
    *,
    path: RetrievalPath,
) -> list[KnowledgeSearchHit]:
    hits = []
    for candidate in candidates:
        row = canonical.get(candidate.chunk_id)
        if row is None:
            continue
        chunk, version, document = row
        if path == "dense":
            dense_score = candidate.dense_score
            sparse_score = fusion_score = None
            final_score = dense_score
        elif path == "sparse":
            dense_score = fusion_score = None
            sparse_score = candidate.sparse_score
            final_score = sparse_score
        else:
            dense_score = candidate.dense_score
            sparse_score = candidate.sparse_score
            fusion_score = candidate.fusion_score
            final_score = fusion_score
        if final_score is None:
            continue
        hits.append(
            KnowledgeSearchHit(
                chunk_id=chunk.id,
                document_name=document.name,
                version_number=version.version_number,
                category=document.category,
                canonical_text=chunk.canonical_text,
                chunk_metadata=chunk.chunk_metadata,
                dense_score=dense_score,
                sparse_score=sparse_score,
                fusion_score=fusion_score,
                reranker_score=None,
                final_score=final_score,
            )
        )
    return hits


def _sort_hits(hits: list[KnowledgeSearchHit], path: RetrievalPath) -> list[KnowledgeSearchHit]:
    if path == "hybrid_rerank":
        return sorted(hits, key=lambda hit: (-hit.final_score, -(hit.fusion_score or 0), hit.chunk_id))
    return sorted(hits, key=lambda hit: (-hit.final_score, hit.chunk_id))


async def search_active_knowledge(
    session: AsyncSession,
    *,
    query: str,
    categories: list[str] | None,
    top_k: int,
    retrieval_path: RetrievalPath,
    load_dependencies: KnowledgeSearchLoader,
    before_external_attempt: Callable[[], Awaitable[None]] | None = None,
) -> "KnowledgeSearchOutcome":
    version_ids = await _active_version_ids(session, categories)
    if not version_ids:
        return KnowledgeSearchOutcome("zero_hit", [])

    async def await_before_external() -> None:
        if before_external_attempt is not None:
            await before_external_attempt()

    await await_before_external()
    models, index, calibration = load_dependencies()
    await await_before_external()
    vector = await models.embed_query(query)
    candidate_limit = int(calibration["candidate_limit"])
    if retrieval_path == "dense":
        await await_before_external()
        dense = await index.dense_search(
            vector=vector.dense, version_ids=version_ids, limit=candidate_limit
        )
        candidates = _dense_candidates(dense)
    elif retrieval_path == "sparse":
        await await_before_external()
        sparse = await index.sparse_search(
            vector=vector.sparse, version_ids=version_ids, limit=candidate_limit
        )
        candidates = _sparse_candidates(sparse)
    else:
        await await_before_external()
        dense = await index.dense_search(
            vector=vector.dense, version_ids=version_ids, limit=candidate_limit
        )
        await await_before_external()
        sparse = await index.sparse_search(
            vector=vector.sparse, version_ids=version_ids, limit=candidate_limit
        )
        candidates = fuse_rankings(dense=dense, sparse=sparse, rrf_k=int(calibration["rrf_k"]))
    canonical = await _canonical_chunks(
        session, chunk_ids=[candidate.chunk_id for candidate in candidates], version_ids=version_ids
    )
    hits = _hits_from_candidates(candidates, canonical, path=retrieval_path)
    if retrieval_path == "hybrid_rerank" and hits:
        await await_before_external()
        reranker_scores = await models.rerank(query, [hit.canonical_text for hit in hits])
        if len(reranker_scores) != len(hits):
            raise KnowledgeDependencyError("KNOWLEDGE_MODEL_UNAVAILABLE", retryable=True)
        hits = [
            KnowledgeSearchHit(
                **{
                    **hit.__dict__,
                    "reranker_score": score,
                    "final_score": score,
                }
            )
            for hit, score in zip(hits, reranker_scores, strict=True)
        ]
    hits = _sort_hits(hits, retrieval_path)[:top_k]
    if not hits:
        return KnowledgeSearchOutcome("zero_hit", [])
    quality = "normal" if hits[0].final_score >= float(calibration["threshold"]) else "low_confidence"
    return KnowledgeSearchOutcome(quality, hits)


@dataclass(frozen=True)
class KnowledgeSearchOutcome:
    quality_status: Literal["normal", "zero_hit", "low_confidence"]
    hits: list[KnowledgeSearchHit]


@lru_cache
def get_knowledge_search_dependencies() -> KnowledgeSearchDependencies:
    settings = get_settings()
    try:
        calibration = json.loads(_CALIBRATION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise KnowledgeDependencyError("KNOWLEDGE_CALIBRATION_UNAVAILABLE", retryable=True) from error
    return (
        LocalKnowledgeModels(
            embedding_model_path=settings.knowledge_embedding_model_path,
            reranker_model_path=settings.knowledge_reranker_model_path,
            timeout_seconds=settings.knowledge_dependency_timeout_seconds,
        ),
        MilvusKnowledgeIndex(
            uri=settings.milvus_uri,
            collection=settings.milvus_collection,
            timeout_seconds=settings.knowledge_dependency_timeout_seconds,
        ),
        calibration,
    )


def get_knowledge_search_loader() -> KnowledgeSearchLoader:
    return get_knowledge_search_dependencies
