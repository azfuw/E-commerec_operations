import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.common import KnowledgeVersionStatus, UserRole
from backend.config import Settings
from backend.database import Base
from backend.knowledge_content import ChunkDraft, KnowledgeContentError
from backend.knowledge_index import (
    HybridVector,
    IndexedChunk,
    KnowledgeDependencyError,
    LocalKnowledgeModels,
    MilvusKnowledgeIndex,
)
from backend.knowledge_worker import run_once
from backend.models import KnowledgeChunk, KnowledgeDocument, KnowledgeDocumentVersion, User


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite://")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        jwt_secret_key="test-only-secret-at-least-32-characters",
        knowledge_lease_seconds=60,
        knowledge_dependency_timeout_seconds=0.01,
    )


def _draft() -> ChunkDraft:
    return ChunkDraft(
        chunk_index=0,
        chunk_id="62b4d1ec-ae1a-5f92-8661-3f959a3e65e5",
        chunk_hash="a" * 64,
        canonical_text="项目演示规则正文",
        chunk_metadata={"heading_path": ["规则"], "paragraph_index": 0, "page_number": None},
        token_count=4,
    )


class _FakeModels:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.embed_calls = 0

    def token_count(self, text: str) -> int:
        return max(1, len(text.split()))

    async def embed_documents(self, texts: list[str]) -> list[HybridVector]:
        self.embed_calls += 1
        if self.error is not None:
            raise self.error
        return [HybridVector(dense=[0.25] * 1024, sparse={1: 0.5}) for _ in texts]


class _RecordingIndex:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.upserts: list[list[IndexedChunk]] = []
        self.chunk_ids: set[str] = set()

    async def upsert(self, chunks: list[IndexedChunk]) -> None:
        if self.error is not None:
            raise self.error
        self.upserts.append(chunks)
        self.chunk_ids.update(chunk.chunk_id for chunk in chunks)


async def _seed_version(
    session_factory: async_sessionmaker,
    tmp_path: Path,
    *,
    attempt_count: int = 0,
) -> tuple[str, str]:
    path = tmp_path / "rules.md"
    path.write_text("# 项目演示规则\n\n正文", encoding="utf-8")
    async with session_factory() as session:
        user = User(
            id="knowledge-worker-admin",
            username="knowledge-worker-admin",
            password_hash="hash",
            role=UserRole.ADMIN,
        )
        session.add(user)
        await session.flush()
        document = KnowledgeDocument(
            id="knowledge-worker-document",
            name="项目演示规则",
            category="演示",
            enabled=True,
            created_by=user.id,
        )
        version = KnowledgeDocumentVersion(
            id="knowledge-worker-version",
            document_id=document.id,
            version_number=1,
            sha256="a" * 64,
            original_filename="rules.md",
            mime_type="text/markdown",
            storage_path=str(path),
            status=KnowledgeVersionStatus.ACCEPTED,
            attempt_count=attempt_count,
        )
        session.add_all((document, version))
        await session.commit()
    return document.id, version.id


async def _version_state(session_factory: async_sessionmaker, version_id: str):
    async with session_factory() as session:
        version = await session.get(KnowledgeDocumentVersion, version_id)
        document = await session.get(KnowledgeDocument, version.document_id)
        chunk_count = await session.scalar(
            select(func.count(KnowledgeChunk.id)).where(KnowledgeChunk.version_id == version_id)
        )
        return version, document, chunk_count


async def test_run_once_indexes_only_safe_metadata_then_persists_and_activates(
    session_factory, tmp_path, monkeypatch
) -> None:
    document_id, version_id = await _seed_version(session_factory, tmp_path)
    events: list[str] = []
    models = _FakeModels()
    index = _RecordingIndex()

    async def fake_parse(*_args, **_kwargs) -> list[ChunkDraft]:
        events.append("parse")
        return [_draft()]

    from backend import knowledge_worker

    original_chunks = knowledge_worker.upsert_knowledge_chunks
    original_activate = knowledge_worker.activate_knowledge_version

    async def record_chunks(*args, **kwargs):
        events.append("chunks")
        return await original_chunks(*args, **kwargs)

    async def record_activate(*args, **kwargs):
        events.append("activate")
        return await original_activate(*args, **kwargs)

    original_embed = models.embed_documents
    original_upsert = index.upsert

    async def record_embed(texts: list[str]) -> list[HybridVector]:
        events.append("embed")
        return await original_embed(texts)

    async def record_upsert(chunks: list[IndexedChunk]) -> None:
        events.append("milvus")
        await original_upsert(chunks)

    models.embed_documents = record_embed
    index.upsert = record_upsert
    monkeypatch.setattr(knowledge_worker, "parse_and_chunk", fake_parse)
    monkeypatch.setattr(knowledge_worker, "upsert_knowledge_chunks", record_chunks)
    monkeypatch.setattr(knowledge_worker, "activate_knowledge_version", record_activate)

    assert await run_once(
        session_factory,
        settings=_settings(),
        lease_owner="worker-a",
        models=models,
        index=index,
    ) == version_id
    version, document, chunk_count = await _version_state(session_factory, version_id)
    assert events == ["parse", "embed", "milvus", "chunks", "activate"]
    assert version.status is KnowledgeVersionStatus.ACTIVE
    assert document.current_version_id == version_id
    assert chunk_count == 1
    indexed = index.upserts[0][0]
    assert indexed.chunk_id == _draft().chunk_id
    assert (indexed.document_id, indexed.version_id, indexed.category) == (
        document_id,
        version_id,
        "演示",
    )
    assert len(indexed.vector.dense) == 1024 and indexed.vector.sparse == {1: 0.5}
    assert set(indexed.__dict__) == {"chunk_id", "document_id", "version_id", "category", "vector"}

    assert await run_once(
        session_factory,
        settings=_settings(),
        lease_owner="worker-a",
        models=models,
        index=index,
    ) is None
    _, _, repeated_chunk_count = await _version_state(session_factory, version_id)
    assert len(index.upserts) == 1
    assert index.chunk_ids == {_draft().chunk_id}
    assert repeated_chunk_count == 1


@pytest.mark.parametrize(
    ("stage_error", "attempt_count", "expected_status", "expected_code"),
    (
        (
            KnowledgeContentError("KNOWLEDGE_PARSE_TIMEOUT"),
            0,
            KnowledgeVersionStatus.FAILED,
            "KNOWLEDGE_PARSE_TIMEOUT",
        ),
        (
            KnowledgeDependencyError("KNOWLEDGE_DEPENDENCY_TIMEOUT", retryable=True),
            0,
            KnowledgeVersionStatus.ACCEPTED,
            "KNOWLEDGE_DEPENDENCY_TIMEOUT",
        ),
        (
            KnowledgeDependencyError("KNOWLEDGE_MILVUS_UNAVAILABLE", retryable=True),
            2,
            KnowledgeVersionStatus.FAILED,
            "KNOWLEDGE_ATTEMPTS_EXHAUSTED",
        ),
        (
            KnowledgeContentError("KNOWLEDGE_PARSE_FAILED"),
            0,
            KnowledgeVersionStatus.FAILED,
            "KNOWLEDGE_PARSE_FAILED",
        ),
    ),
)
async def test_run_once_maps_parse_and_dependency_failures_without_partial_results(
    session_factory,
    tmp_path,
    monkeypatch,
    stage_error: Exception,
    attempt_count: int,
    expected_status: KnowledgeVersionStatus,
    expected_code: str,
) -> None:
    _, version_id = await _seed_version(session_factory, tmp_path, attempt_count=attempt_count)
    models = _FakeModels(
        error=stage_error if isinstance(stage_error, KnowledgeDependencyError) else None
    )
    index = _RecordingIndex(
        error=stage_error
        if isinstance(stage_error, KnowledgeDependencyError)
        and stage_error.code == "KNOWLEDGE_MILVUS_UNAVAILABLE"
        else None
    )

    from backend import knowledge_worker

    if isinstance(stage_error, KnowledgeContentError):
        async def fail_parse(*_args, **_kwargs):
            raise stage_error

        monkeypatch.setattr(knowledge_worker, "parse_and_chunk", fail_parse)
    else:
        async def fake_parse(*_args, **_kwargs) -> list[ChunkDraft]:
            return [_draft()]

        monkeypatch.setattr(knowledge_worker, "parse_and_chunk", fake_parse)

    assert await run_once(
        session_factory,
        settings=_settings(),
        lease_owner="worker-a",
        models=models,
        index=index,
    ) == version_id
    version, document, chunk_count = await _version_state(session_factory, version_id)
    assert (version.status, version.error_code) == (expected_status, expected_code)
    assert document.current_version_id is None
    assert chunk_count == 0
    assert not index.chunk_ids


async def test_run_once_propagates_cancellation_after_milvus_upsert_without_state_cleanup(
    session_factory, tmp_path, monkeypatch
) -> None:
    _, version_id = await _seed_version(session_factory, tmp_path)
    models = _FakeModels()
    index = _RecordingIndex()
    from backend import knowledge_worker

    async def fake_parse(*_args, **_kwargs) -> list[ChunkDraft]:
        return [_draft()]

    async def interrupted_persist(*_args, **_kwargs) -> bool:
        raise asyncio.CancelledError

    monkeypatch.setattr(knowledge_worker, "parse_and_chunk", fake_parse)
    monkeypatch.setattr(knowledge_worker, "upsert_knowledge_chunks", interrupted_persist)

    with pytest.raises(asyncio.CancelledError):
        await run_once(
            session_factory,
            settings=_settings(),
            lease_owner="worker-a",
            models=models,
            index=index,
        )
    version, document, chunk_count = await _version_state(session_factory, version_id)
    assert version.status is KnowledgeVersionStatus.PROCESSING
    assert version.lease_owner == "worker-a" and version.lease_expires_at is not None
    assert document.current_version_id is None and chunk_count == 0
    assert index.chunk_ids == {_draft().chunk_id}


async def test_run_once_stops_before_embedding_when_renewal_loses_owner(
    session_factory, tmp_path, monkeypatch
) -> None:
    _, version_id = await _seed_version(session_factory, tmp_path)
    models = _FakeModels()
    index = _RecordingIndex()
    from backend import knowledge_worker

    async def fake_parse(*_args, **_kwargs) -> list[ChunkDraft]:
        return [_draft()]

    original_renew = knowledge_worker.renew_knowledge_lease
    renewals = 0

    async def replace_owner(session, **kwargs) -> bool:
        nonlocal renewals
        renewals += 1
        if renewals == 2:
            await session.execute(
                update(KnowledgeDocumentVersion)
                .where(KnowledgeDocumentVersion.id == version_id)
                .values(
                    lease_owner="worker-b",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                )
            )
            await session.commit()
        return await original_renew(session, **kwargs)

    monkeypatch.setattr(knowledge_worker, "parse_and_chunk", fake_parse)
    monkeypatch.setattr(knowledge_worker, "renew_knowledge_lease", replace_owner)

    assert await run_once(
        session_factory,
        settings=_settings(),
        lease_owner="worker-a",
        models=models,
        index=index,
    ) == version_id
    version, document, chunk_count = await _version_state(session_factory, version_id)
    assert renewals == 2 and models.embed_calls == 0 and not index.upserts
    assert version.lease_owner == "worker-b" and version.status is KnowledgeVersionStatus.PROCESSING
    assert document.current_version_id is None and chunk_count == 0


async def test_index_exact_key_operations_reject_empty_lists_without_a_client() -> None:
    index = object.__new__(MilvusKnowledgeIndex)
    with pytest.raises(ValueError):
        await index.existing_chunk_ids(chunk_ids=[])
    with pytest.raises(ValueError):
        await index.delete_chunk_ids(chunk_ids=[])


def test_local_models_reject_missing_local_directories_without_loading_weights(tmp_path) -> None:
    with pytest.raises(KnowledgeDependencyError) as error:
        LocalKnowledgeModels(
            embedding_model_path=tmp_path / "missing-embedding",
            reranker_model_path=tmp_path / "missing-reranker",
            timeout_seconds=0.01,
        )
    assert (error.value.code, error.value.retryable) == ("KNOWLEDGE_MODEL_UNAVAILABLE", True)


@pytest.mark.parametrize(("cuda_available", "expected_fp16"), [(True, True), (False, False)])
def test_local_models_select_fp16_only_when_cuda_is_available(
    tmp_path, monkeypatch, cuda_available, expected_fp16
) -> None:
    calls: list[dict[str, object]] = []

    class FakeEmbedding:
        def __init__(self, *_args, **kwargs) -> None:
            calls.append(kwargs)

    class FakeReranker:
        def __init__(self, *_args, **kwargs) -> None:
            calls.append(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "FlagEmbedding",
        SimpleNamespace(BGEM3FlagModel=FakeEmbedding, FlagReranker=FakeReranker),
    )
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: cuda_available)),
    )
    embedding_path = tmp_path / "embedding"
    reranker_path = tmp_path / "reranker"
    embedding_path.mkdir()
    reranker_path.mkdir()

    LocalKnowledgeModels(
        embedding_model_path=embedding_path,
        reranker_model_path=reranker_path,
        timeout_seconds=1.0,
    )

    assert [call["use_fp16"] for call in calls] == [expected_fp16, expected_fp16]
    assert all(call["local_files_only"] is True for call in calls)


async def test_local_models_use_local_bge_vectors_with_exactly_1024_dense_values(
    tmp_path, monkeypatch
) -> None:
    class FakeEmbedding:
        def __init__(self, *_args, **kwargs) -> None:
            assert kwargs["local_files_only"] is True
            self.tokenizer = SimpleNamespace(encode=lambda *_args, **_kwargs: [1, 2])

        def encode(self, _texts, **_kwargs):
            return {"dense_vecs": [[0.5] * 1024], "lexical_weights": [{"7": 0.25}]}

    class FakeReranker:
        def __init__(self, *_args, **kwargs) -> None:
            assert kwargs["local_files_only"] is True

    monkeypatch.setitem(
        sys.modules,
        "FlagEmbedding",
        SimpleNamespace(BGEM3FlagModel=FakeEmbedding, FlagReranker=FakeReranker),
    )
    embedding_path = tmp_path / "embedding"
    reranker_path = tmp_path / "reranker"
    embedding_path.mkdir()
    reranker_path.mkdir()
    models = LocalKnowledgeModels(
        embedding_model_path=embedding_path,
        reranker_model_path=reranker_path,
        timeout_seconds=1.0,
    )

    assert await models.embed_documents(["本地文本"]) == [
        HybridVector(dense=[0.5] * 1024, sparse={7: 0.25})
    ]
    assert models.token_count("本地文本") == 2


async def test_local_models_normalize_a_single_list_reranker_score(tmp_path, monkeypatch) -> None:
    class FakeEmbedding:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    class FakeReranker:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def compute_score(self, _pairs):
            return [0.75]

    monkeypatch.setitem(
        sys.modules,
        "FlagEmbedding",
        SimpleNamespace(BGEM3FlagModel=FakeEmbedding, FlagReranker=FakeReranker),
    )
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)),
    )
    embedding_path = tmp_path / "embedding"
    reranker_path = tmp_path / "reranker"
    embedding_path.mkdir()
    reranker_path.mkdir()
    models = LocalKnowledgeModels(
        embedding_model_path=embedding_path,
        reranker_model_path=reranker_path,
        timeout_seconds=1.0,
    )

    assert await models.rerank("查询", ["候选内容"]) == [0.75]


async def test_local_model_blocking_call_maps_deadline_to_retryable_timeout(tmp_path, monkeypatch) -> None:
    class SleepingEmbedding:
        def __init__(self, *_args, **_kwargs) -> None:
            self.tokenizer = SimpleNamespace(encode=lambda *_args, **_kwargs: [1])

        def encode(self, _texts, **_kwargs):
            time.sleep(0.05)
            return {"dense_vecs": [[0.5] * 1024], "lexical_weights": [{"7": 0.25}]}

    class FakeReranker:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    monkeypatch.setitem(
        sys.modules,
        "FlagEmbedding",
        SimpleNamespace(BGEM3FlagModel=SleepingEmbedding, FlagReranker=FakeReranker),
    )
    embedding_path = tmp_path / "embedding"
    reranker_path = tmp_path / "reranker"
    embedding_path.mkdir()
    reranker_path.mkdir()
    models = LocalKnowledgeModels(
        embedding_model_path=embedding_path,
        reranker_model_path=reranker_path,
        timeout_seconds=0.005,
    )

    with pytest.raises(KnowledgeDependencyError) as error:
        await models.embed_documents(["本地文本"])
    assert (error.value.code, error.value.retryable) == ("KNOWLEDGE_DEPENDENCY_TIMEOUT", True)


class _RecordingMilvusClient:
    def __init__(self, **kwargs) -> None:
        self.init_kwargs = kwargs
        self.has_calls: list[dict[str, object]] = []
        self.create_calls: list[dict[str, object]] = []
        self.upsert_rows: list[dict[str, object]] = []
        self.search_calls: list[dict[str, object]] = []
        self.query_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []

    def has_collection(self, **kwargs) -> bool:
        self.has_calls.append(kwargs)
        return False

    def create_collection(self, **kwargs) -> None:
        self.create_calls.append(kwargs)

    def upsert(self, **kwargs) -> None:
        self.upsert_rows.extend(kwargs["data"])

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return [[{"id": "chunk-1", "distance": 0.75, "entity": {"chunk_id": "chunk-1"}}]]

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        return [{"chunk_id": "chunk-1"}]

    def delete(self, **kwargs) -> None:
        self.delete_calls.append(kwargs)


async def test_milvus_adapter_forwards_deadline_and_uses_only_safe_exact_operations(monkeypatch) -> None:
    client = _RecordingMilvusClient()
    schema = SimpleNamespace(fields=[])
    indexes = SimpleNamespace(indexes=[])
    schema.add_field = lambda **field: schema.fields.append(field)
    indexes.add_index = lambda **index: indexes.indexes.append(index)
    client.create_schema = lambda **_kwargs: schema
    client.prepare_index_params = lambda: indexes

    def make_client(**kwargs):
        client.init_kwargs = kwargs
        return client

    monkeypatch.setitem(
        sys.modules,
        "pymilvus",
        SimpleNamespace(
            MilvusClient=make_client,
            DataType=SimpleNamespace(
                VARCHAR="VARCHAR", FLOAT_VECTOR="FLOAT_VECTOR", SPARSE_FLOAT_VECTOR="SPARSE_FLOAT_VECTOR"
            ),
        ),
    )
    index = MilvusKnowledgeIndex(uri="http://unused", collection="knowledge_chunks", timeout_seconds=0.25)

    await index.ensure_collection()
    await index.upsert(
        [
            IndexedChunk(
                chunk_id="chunk-1",
                document_id="document-1",
                version_id="version-1",
                category="演示",
                vector=HybridVector(dense=[0.5] * 1024, sparse={7: 0.25}),
            )
        ]
    )
    assert await index.dense_search(
        vector=[0.5] * 1024, version_ids=["version-2", "version-1"], limit=3
    ) == [("chunk-1", 0.75)]
    assert await index.existing_chunk_ids(chunk_ids=["chunk-2", "chunk-1"]) == {"chunk-1"}
    await index.delete_chunk_ids(chunk_ids=["chunk-2", "chunk-1"])

    assert client.init_kwargs == {"uri": "http://unused", "timeout": 0.25}
    assert client.has_calls == [{"collection_name": "knowledge_chunks", "timeout": 0.25}]
    assert client.create_calls[0]["timeout"] == 0.25
    assert {field["field_name"] for field in schema.fields} == {
        "chunk_id",
        "document_id",
        "version_id",
        "category",
        "dense_vector",
        "sparse_vector",
    }
    assert schema.fields[0]["is_primary"] is True and schema.fields[4]["dim"] == 1024
    assert {entry["field_name"] for entry in indexes.indexes} == {"dense_vector", "sparse_vector"}
    assert set(client.upsert_rows[0]) == {
        "chunk_id",
        "document_id",
        "version_id",
        "category",
        "dense_vector",
        "sparse_vector",
    }
    assert client.search_calls[0]["filter"] == 'version_id in ["version-1", "version-2"]'
    assert client.query_calls[0]["filter"] == 'chunk_id in ["chunk-1", "chunk-2"]'
    assert client.delete_calls[0]["filter"] == 'chunk_id in ["chunk-1", "chunk-2"]'


async def test_milvus_search_reads_entity_chunk_id_without_a_top_level_id() -> None:
    class EntityOnlyClient:
        def search(self, **_kwargs):
            return [[{"entity": {"chunk_id": "canonical-chunk"}, "distance": 0.75}]]

    index = object.__new__(MilvusKnowledgeIndex)
    index._client = EntityOnlyClient()
    index._collection = "knowledge_chunks"
    index._timeout_seconds = 1.0

    assert await index.dense_search(
        vector=[0.5] * 1024, version_ids=["version-1"], limit=1
    ) == [("canonical-chunk", 0.75)]
    assert await index.sparse_search(
        vector={7: 0.25}, version_ids=["version-1"], limit=1
    ) == [("canonical-chunk", 0.75)]


async def test_milvus_blocking_call_maps_deadline_to_retryable_timeout(monkeypatch) -> None:
    class SleepingClient:
        def __init__(self, **_kwargs) -> None:
            pass

        def upsert(self, **_kwargs) -> None:
            time.sleep(0.05)

    monkeypatch.setitem(sys.modules, "pymilvus", SimpleNamespace(MilvusClient=SleepingClient))
    index = MilvusKnowledgeIndex(uri="http://unused", collection="knowledge_chunks", timeout_seconds=0.005)

    with pytest.raises(KnowledgeDependencyError) as error:
        await index.upsert(
            [
                IndexedChunk(
                    chunk_id="chunk-1",
                    document_id="document-1",
                    version_id="version-1",
                    category="演示",
                    vector=HybridVector(dense=[0.5] * 1024, sparse={7: 0.25}),
                )
            ]
        )
    assert (error.value.code, error.value.retryable) == ("KNOWLEDGE_DEPENDENCY_TIMEOUT", True)
