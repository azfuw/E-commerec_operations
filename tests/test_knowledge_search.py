import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.common import KnowledgeVersionStatus, UserRole
from backend.knowledge_index import HybridVector, KnowledgeDependencyError
from backend.knowledge_search import (
    KnowledgeSearchHit,
    fuse_rankings,
    get_knowledge_search_dependencies,
    get_knowledge_search_loader,
    search_active_knowledge,
)
from backend.models import KnowledgeChunk, KnowledgeDocument, KnowledgeDocumentVersion, User


class _Models:
    def __init__(self, *, error: Exception | None = None, rerank_scores: list[float] | None = None) -> None:
        self.error = error
        self.rerank_scores = rerank_scores or [0.8]
        self.embed_queries: list[str] = []
        self.rerank_calls: list[tuple[str, list[str]]] = []

    async def embed_query(self, query: str) -> HybridVector:
        self.embed_queries.append(query)
        if self.error is not None:
            raise self.error
        return HybridVector(dense=[0.25] * 1024, sparse={7: 0.5})

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        self.rerank_calls.append((query, texts))
        return self.rerank_scores[: len(texts)]


class _Index:
    def __init__(
        self,
        *,
        dense: list[tuple[str, float]],
        sparse: list[tuple[str, float]],
        error: Exception | None = None,
        dense_error: Exception | None = None,
        sparse_error: Exception | None = None,
    ) -> None:
        self.dense = dense
        self.sparse = sparse
        self.error = error
        self.dense_error = dense_error
        self.sparse_error = sparse_error
        self.dense_calls: list[tuple[list[float], list[str], int]] = []
        self.sparse_calls: list[tuple[dict[int, float], list[str], int]] = []

    async def dense_search(
        self, *, vector: list[float], version_ids: list[str], limit: int
    ) -> list[tuple[str, float]]:
        if self.dense_error is not None:
            raise self.dense_error
        if self.error is not None:
            raise self.error
        self.dense_calls.append((vector, version_ids, limit))
        return self.dense

    async def sparse_search(
        self, *, vector: dict[int, float], version_ids: list[str], limit: int
    ) -> list[tuple[str, float]]:
        if self.sparse_error is not None:
            raise self.sparse_error
        if self.error is not None:
            raise self.error
        self.sparse_calls.append((vector, version_ids, limit))
        return self.sparse


async def _seed(session, *, include_active: bool = True) -> dict[str, str]:
    user = User(
        id="knowledge-search-admin",
        username="knowledge-search-admin",
        password_hash="hash",
        role=UserRole.ADMIN,
    )
    document = KnowledgeDocument(
        id="knowledge-search-document",
        name="项目演示规则",
        category="演示",
        enabled=True,
        created_by=user.id,
    )
    active = KnowledgeDocumentVersion(
        id="knowledge-search-active-version",
        document_id=document.id,
        version_number=2,
        sha256="a" * 64,
        original_filename="rules.md",
        mime_type="text/markdown",
        storage_path="controlled/rules.md",
        status=KnowledgeVersionStatus.ACTIVE if include_active else KnowledgeVersionStatus.DISABLED,
    )
    old = KnowledgeDocumentVersion(
        id="knowledge-search-old-version",
        document_id=document.id,
        version_number=1,
        sha256="b" * 64,
        original_filename="old.md",
        mime_type="text/markdown",
        storage_path="controlled/old.md",
        status=KnowledgeVersionStatus.DISABLED,
    )
    chunks = [
        KnowledgeChunk(
            id="chunk-a",
            version_id=active.id,
            chunk_index=0,
            chunk_hash="c" * 64,
            canonical_text="标题关键词应包含品类和规格。",
            chunk_metadata={"heading_path": ["标题关键词"], "paragraph_index": 0},
            token_count=1,
        ),
        KnowledgeChunk(
            id="chunk-b",
            version_id=active.id,
            chunk_index=1,
            chunk_hash="d" * 64,
            canonical_text="卖点必须基于商品事实。",
            chunk_metadata={"heading_path": ["卖点详情"], "paragraph_index": 1},
            token_count=1,
        ),
        KnowledgeChunk(
            id="chunk-c",
            version_id=active.id,
            chunk_index=2,
            chunk_hash="e" * 64,
            canonical_text="售后退款表述应清晰。",
            chunk_metadata={"heading_path": ["售后退款"], "paragraph_index": 2},
            token_count=1,
        ),
        KnowledgeChunk(
            id="chunk-old",
            version_id=old.id,
            chunk_index=0,
            chunk_hash="f" * 64,
            canonical_text="旧版本内容。",
            chunk_metadata={"heading_path": ["旧版本"], "paragraph_index": 0},
            token_count=1,
        ),
    ]
    session.add(user)
    await session.flush()
    session.add(document)
    await session.flush()
    session.add_all((active, old))
    await session.flush()
    session.add_all(chunks)
    await session.flush()
    if include_active:
        document.current_version_id = active.id
    await session.commit()
    return {"active": active.id, "old": old.id}


async def test_zero_active_versions_return_before_dependency_loader(session) -> None:
    await _seed(session, include_active=False)
    calls = 0

    def loader():
        nonlocal calls
        calls += 1
        raise AssertionError("zero-hit searches must not load dependencies")

    outcome = await search_active_knowledge(
        session,
        query="标题关键词",
        categories=None,
        top_k=3,
        retrieval_path="hybrid",
        load_dependencies=loader,
    )

    assert outcome.quality_status == "zero_hit"
    assert outcome.hits == []
    assert calls == 0


async def test_active_search_uses_only_active_ids_and_canonical_database_data(session) -> None:
    ids = await _seed(session)
    models = _Models()
    index = _Index(
        dense=[("chunk-a", 0.6), ("chunk-old", 0.99), ("orphan", 0.98)],
        sparse=[("chunk-a", 0.7), ("chunk-old", 0.98)],
    )
    calls = 0

    def loader():
        nonlocal calls
        calls += 1
        return models, index, {"candidate_limit": 5, "rrf_k": 60, "threshold": 0.0}

    outcome = await search_active_knowledge(
        session,
        query="标题关键词",
        categories=["演示"],
        top_k=3,
        retrieval_path="hybrid_rerank",
        load_dependencies=loader,
    )

    assert calls == 1 and models.embed_queries == ["标题关键词"]
    assert [call[1] for call in index.dense_calls + index.sparse_calls] == [[ids["active"]], [ids["active"]]]
    assert len(index.dense_calls) == len(index.sparse_calls) == 1
    assert [(hit.chunk_id, hit.document_name, hit.version_number) for hit in outcome.hits] == [
        ("chunk-a", "项目演示规则", 2)
    ]
    hit = outcome.hits[0]
    assert hit.canonical_text == "标题关键词应包含品类和规格。"
    assert hit.chunk_metadata == {"heading_path": ["标题关键词"], "paragraph_index": 0}
    assert (hit.dense_score, hit.sparse_score, hit.reranker_score, hit.final_score) == (0.6, 0.7, 0.8, 0.8)
    assert models.rerank_calls == [("标题关键词", [hit.canonical_text])]
    assert outcome.quality_status == "normal"

    injected = await search_active_knowledge(
        session,
        query="标题关键词",
        categories=['演示" OR 1=1 --'],
        top_k=3,
        retrieval_path="hybrid",
        load_dependencies=loader,
    )
    assert injected.quality_status == "zero_hit"
    assert calls == 1 and len(index.dense_calls) == len(index.sparse_calls) == 1


@pytest.mark.parametrize(
    ("retrieval_path", "expected_ids", "expected_final_scores", "expected_calls"),
    (
        ("dense", ["chunk-a", "chunk-b"], [0.5, 0.5], (1, 0)),
        ("sparse", ["chunk-c", "chunk-b"], [0.9, 0.2], (0, 1)),
        ("hybrid", ["chunk-b", "chunk-a", "chunk-c"], [2 / 12, 1 / 11, 1 / 11], (1, 1)),
        ("hybrid_rerank", ["chunk-c", "chunk-b", "chunk-a"], [0.9, 0.5, 0.5], (1, 1)),
    ),
)
async def test_retrieval_paths_use_documented_scores_and_stable_ties(
    session, retrieval_path, expected_ids, expected_final_scores, expected_calls
) -> None:
    await _seed(session)
    models = _Models(rerank_scores=[0.5, 0.5, 0.9])
    index = _Index(dense=[("chunk-b", 0.5), ("chunk-a", 0.5)], sparse=[("chunk-c", 0.9), ("chunk-b", 0.2)])

    outcome = await search_active_knowledge(
        session,
        query="规则",
        categories=None,
        top_k=3,
        retrieval_path=retrieval_path,
        load_dependencies=lambda: (models, index, {"candidate_limit": 3, "rrf_k": 10, "threshold": 0.0}),
    )

    assert [hit.chunk_id for hit in outcome.hits] == expected_ids
    assert [hit.final_score for hit in outcome.hits] == pytest.approx(expected_final_scores)
    assert (len(index.dense_calls), len(index.sparse_calls)) == expected_calls
    if retrieval_path == "dense":
        assert all(hit.sparse_score is hit.fusion_score is hit.reranker_score is None for hit in outcome.hits)
    elif retrieval_path == "sparse":
        assert all(hit.dense_score is hit.fusion_score is hit.reranker_score is None for hit in outcome.hits)
    elif retrieval_path == "hybrid":
        assert all(hit.reranker_score is None and hit.fusion_score is not None for hit in outcome.hits)
    else:
        assert all(hit.reranker_score is not None and hit.fusion_score is not None for hit in outcome.hits)


@pytest.mark.parametrize(
    ("retrieval_path", "dense_error", "sparse_error", "expected_calls"),
    (
        (
            "dense",
            None,
            KnowledgeDependencyError("KNOWLEDGE_MILVUS_UNAVAILABLE", retryable=True),
            (1, 0),
        ),
        (
            "sparse",
            KnowledgeDependencyError("KNOWLEDGE_MILVUS_UNAVAILABLE", retryable=True),
            None,
            (0, 1),
        ),
    ),
)
async def test_single_path_ignores_an_unavailable_other_index(
    session, retrieval_path, dense_error, sparse_error, expected_calls
) -> None:
    await _seed(session)
    index = _Index(
        dense=[("chunk-a", 0.5)],
        sparse=[("chunk-a", 0.5)],
        dense_error=dense_error,
        sparse_error=sparse_error,
    )

    outcome = await search_active_knowledge(
        session,
        query="规则",
        categories=None,
        top_k=3,
        retrieval_path=retrieval_path,
        load_dependencies=lambda: (
            _Models(),
            index,
            {"candidate_limit": 3, "rrf_k": 60, "threshold": 0.0},
        ),
    )

    assert [hit.chunk_id for hit in outcome.hits] == ["chunk-a"]
    assert (len(index.dense_calls), len(index.sparse_calls)) == expected_calls


async def test_dependency_errors_propagate_without_manufacturing_hits(session) -> None:
    await _seed(session)
    for error in (
        KnowledgeDependencyError("KNOWLEDGE_DEPENDENCY_TIMEOUT", retryable=True),
        KnowledgeDependencyError("KNOWLEDGE_MILVUS_UNAVAILABLE", retryable=True),
    ):
        models = _Models(error=error if error.code == "KNOWLEDGE_DEPENDENCY_TIMEOUT" else None)
        index = _Index(
            dense=[],
            sparse=[],
            error=error if error.code == "KNOWLEDGE_MILVUS_UNAVAILABLE" else None,
        )
        with pytest.raises(KnowledgeDependencyError) as raised:
            await search_active_knowledge(
                session,
                query="规则",
                categories=None,
                top_k=3,
                retrieval_path="hybrid",
                load_dependencies=lambda: (models, index, {"candidate_limit": 3, "rrf_k": 60, "threshold": 0.0}),
            )
        assert raised.value.code == error.code


async def test_real_below_threshold_hit_is_low_confidence(session) -> None:
    await _seed(session)
    outcome = await search_active_knowledge(
        session,
        query="规则",
        categories=None,
        top_k=3,
        retrieval_path="hybrid",
        load_dependencies=lambda: (
            _Models(),
            _Index(dense=[("chunk-a", 0.5)], sparse=[("chunk-a", 0.5)]),
            {"candidate_limit": 3, "rrf_k": 60, "threshold": 0.1},
        ),
    )

    assert outcome.quality_status == "low_confidence"
    assert [hit.chunk_id for hit in outcome.hits] == ["chunk-a"]


def test_fusion_uses_one_based_ranks_and_none_for_absent_stages() -> None:
    fused = fuse_rankings(
        dense=[("chunk-b", 0.5), ("chunk-a", 0.5)],
        sparse=[("chunk-c", 0.9), ("chunk-b", 0.2)],
        rrf_k=10,
    )

    assert [(score.chunk_id, score.dense_rank, score.sparse_rank) for score in fused] == [
        ("chunk-b", 2, 2),
        ("chunk-a", 1, None),
        ("chunk-c", None, 1),
    ]
    by_id = {score.chunk_id: score for score in fused}
    assert by_id["chunk-a"].sparse_score is None
    assert by_id["chunk-c"].dense_score is None
    assert by_id["chunk-b"].fusion_score == pytest.approx(2 / 12)


async def test_loader_is_cheap_until_the_first_active_search(monkeypatch, session) -> None:
    await _seed(session)
    get_knowledge_search_dependencies.cache_clear()
    constructed: list[str] = []

    class ConstructedModels(_Models):
        def __init__(self, **_kwargs) -> None:
            constructed.append("models")
            super().__init__()

    class ConstructedIndex(_Index):
        def __init__(self, **_kwargs) -> None:
            constructed.append("index")
            super().__init__(dense=[("chunk-a", 0.5)], sparse=[("chunk-a", 0.5)])

    from backend import knowledge_search
    from backend.main import create_app

    monkeypatch.setattr(
        knowledge_search,
        "get_settings",
        lambda: SimpleNamespace(
            knowledge_embedding_model_path="unused-embedding",
            knowledge_reranker_model_path="unused-reranker",
            milvus_uri="http://unused",
            milvus_collection="unused",
            knowledge_dependency_timeout_seconds=1.0,
        ),
    )
    monkeypatch.setattr(knowledge_search, "LocalKnowledgeModels", ConstructedModels)
    monkeypatch.setattr(knowledge_search, "MilvusKnowledgeIndex", ConstructedIndex)
    reads = 0

    def read_text(_self, **_kwargs) -> str:
        nonlocal reads
        reads += 1
        return json.dumps({"candidate_limit": 3, "rrf_k": 60, "threshold": 0.0})

    monkeypatch.setattr(knowledge_search.Path, "read_text", read_text)

    create_app()
    loader = get_knowledge_search_loader()
    assert constructed == [] and reads == 0

    for _ in range(2):
        await search_active_knowledge(
            session,
            query="规则",
            categories=None,
            top_k=3,
            retrieval_path="hybrid",
            load_dependencies=loader,
        )
    assert constructed == ["models", "index"] and reads == 1
    get_knowledge_search_dependencies.cache_clear()
