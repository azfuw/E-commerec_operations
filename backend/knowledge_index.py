import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Callable, TypeVar


_T = TypeVar("_T")


@dataclass(frozen=True)
class HybridVector:
    dense: list[float]
    sparse: dict[int, float]


@dataclass(frozen=True)
class IndexedChunk:
    chunk_id: str
    document_id: str
    version_id: str
    category: str
    vector: HybridVector


class KnowledgeDependencyError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


async def _with_deadline(
    callback: Callable[[], _T], *, timeout_seconds: float, unavailable_code: str
) -> _T:
    try:
        return await asyncio.wait_for(asyncio.to_thread(callback), timeout_seconds)
    except TimeoutError as error:
        raise KnowledgeDependencyError("KNOWLEDGE_DEPENDENCY_TIMEOUT", retryable=True) from error
    except KnowledgeDependencyError:
        raise
    except Exception as error:
        raise KnowledgeDependencyError(unavailable_code, retryable=True) from error


class LocalKnowledgeModels:
    def __init__(
        self, *, embedding_model_path: Path, reranker_model_path: Path, timeout_seconds: float
    ) -> None:
        if not embedding_model_path.is_dir() or not reranker_model_path.is_dir():
            raise KnowledgeDependencyError("KNOWLEDGE_MODEL_UNAVAILABLE", retryable=True)
        try:
            import torch
            from FlagEmbedding import BGEM3FlagModel, FlagReranker

            use_fp16 = torch.cuda.is_available()
            self._embedding = BGEM3FlagModel(
                str(embedding_model_path), use_fp16=use_fp16, local_files_only=True
            )
            self._reranker = FlagReranker(
                str(reranker_model_path), use_fp16=use_fp16, local_files_only=True
            )
        except Exception as error:
            raise KnowledgeDependencyError("KNOWLEDGE_MODEL_UNAVAILABLE", retryable=True) from error
        self._timeout_seconds = timeout_seconds

    def token_count(self, text: str) -> int:
        return len(self._embedding.tokenizer.encode(text, add_special_tokens=False))

    def _embed_sync(self, texts: list[str]) -> list[HybridVector]:
        encoded = self._embedding.encode(
            texts, return_dense=True, return_sparse=True, return_colbert_vecs=False
        )
        dense_vectors = encoded["dense_vecs"]
        sparse_vectors = encoded["lexical_weights"]
        vectors = [
            HybridVector(
                dense=[float(value) for value in dense],
                sparse={int(key): float(value) for key, value in sparse.items()},
            )
            for dense, sparse in zip(dense_vectors, sparse_vectors, strict=True)
        ]
        if len(vectors) != len(texts) or any(len(vector.dense) != 1024 for vector in vectors):
            raise ValueError
        return vectors

    async def embed_documents(self, texts: list[str]) -> list[HybridVector]:
        return await _with_deadline(
            lambda: self._embed_sync(texts),
            timeout_seconds=self._timeout_seconds,
            unavailable_code="KNOWLEDGE_MODEL_UNAVAILABLE",
        )

    async def embed_query(self, text: str) -> HybridVector:
        return (await self.embed_documents([text]))[0]

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        def score() -> list[float]:
            values = self._reranker.compute_score([[query, text] for text in texts])
            return [float(value) for value in values] if isinstance(values, list) else [float(values)]

        return await _with_deadline(
            score,
            timeout_seconds=self._timeout_seconds,
            unavailable_code="KNOWLEDGE_MODEL_UNAVAILABLE",
        )


class MilvusKnowledgeIndex:
    def __init__(self, *, uri: str, collection: str, timeout_seconds: float) -> None:
        try:
            from pymilvus import MilvusClient

            self._client = MilvusClient(uri=uri, timeout=timeout_seconds)
        except Exception as error:
            raise KnowledgeDependencyError("KNOWLEDGE_MILVUS_UNAVAILABLE", retryable=True) from error
        self._collection = collection
        self._timeout_seconds = timeout_seconds

    async def _call(self, callback: Callable[[], _T]) -> _T:
        return await _with_deadline(
            callback,
            timeout_seconds=self._timeout_seconds,
            unavailable_code="KNOWLEDGE_MILVUS_UNAVAILABLE",
        )

    async def ensure_collection(self) -> None:
        def ensure() -> None:
            if self._client.has_collection(
                collection_name=self._collection, timeout=self._timeout_seconds
            ):
                return
            from pymilvus import DataType

            schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field(field_name="chunk_id", datatype=DataType.VARCHAR, is_primary=True, max_length=36)
            schema.add_field(field_name="document_id", datatype=DataType.VARCHAR, max_length=36)
            schema.add_field(field_name="version_id", datatype=DataType.VARCHAR, max_length=36)
            schema.add_field(field_name="category", datatype=DataType.VARCHAR, max_length=256)
            schema.add_field(field_name="dense_vector", datatype=DataType.FLOAT_VECTOR, dim=1024)
            schema.add_field(field_name="sparse_vector", datatype=DataType.SPARSE_FLOAT_VECTOR)
            indexes = self._client.prepare_index_params()
            indexes.add_index(field_name="dense_vector", index_type="AUTOINDEX", metric_type="COSINE")
            indexes.add_index(
                field_name="sparse_vector", index_type="SPARSE_INVERTED_INDEX", metric_type="IP"
            )
            self._client.create_collection(
                collection_name=self._collection,
                schema=schema,
                index_params=indexes,
                timeout=self._timeout_seconds,
            )

        await self._call(ensure)

    async def upsert(self, chunks: list[IndexedChunk]) -> None:
        if any(len(chunk.vector.dense) != 1024 for chunk in chunks):
            raise ValueError("Knowledge vectors must have 1024 dimensions")
        rows = [
            {
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "version_id": chunk.version_id,
                "category": chunk.category,
                "dense_vector": chunk.vector.dense,
                "sparse_vector": chunk.vector.sparse,
            }
            for chunk in chunks
        ]
        if rows:
            await self._call(
                lambda: self._client.upsert(
                    collection_name=self._collection, data=rows, timeout=self._timeout_seconds
                )
            )

    @staticmethod
    def _ids_filter(field: str, values: list[str]) -> str:
        if not values or any(not value for value in values):
            raise ValueError("An explicit non-empty stable-ID list is required")
        return f"{field} in {json.dumps(sorted(set(values)))}"

    async def _search(
        self, *, field: str, vector: list[float] | dict[int, float], version_ids: list[str], limit: int
    ) -> list[tuple[str, float]]:
        if not version_ids:
            return []
        rows = await self._call(
            lambda: self._client.search(
                collection_name=self._collection,
                data=[vector],
                filter=self._ids_filter("version_id", version_ids),
                limit=limit,
                anns_field=field,
                output_fields=["chunk_id"],
                timeout=self._timeout_seconds,
            )
        )
        return [
            (str(hit.get("entity", {}).get("chunk_id") or hit["id"]), float(hit["distance"]))
            for hit in rows[0]
        ]

    async def dense_search(
        self, *, vector: list[float], version_ids: list[str], limit: int
    ) -> list[tuple[str, float]]:
        if len(vector) != 1024:
            raise ValueError("Knowledge vectors must have 1024 dimensions")
        return await self._search(
            field="dense_vector", vector=vector, version_ids=version_ids, limit=limit
        )

    async def sparse_search(
        self, *, vector: dict[int, float], version_ids: list[str], limit: int
    ) -> list[tuple[str, float]]:
        return await self._search(
            field="sparse_vector", vector=vector, version_ids=version_ids, limit=limit
        )

    async def existing_chunk_ids(self, *, chunk_ids: list[str]) -> set[str]:
        identifiers = self._ids_filter("chunk_id", chunk_ids)
        rows = await self._call(
            lambda: self._client.query(
                collection_name=self._collection,
                filter=identifiers,
                output_fields=["chunk_id"],
                timeout=self._timeout_seconds,
            )
        )
        return {str(row["chunk_id"]) for row in rows}

    async def list_chunk_ids_for_version(self, *, version_id: str) -> set[str]:
        if not version_id:
            raise ValueError("An explicit version ID is required")
        rows = await self._call(
            lambda: self._client.query(
                collection_name=self._collection,
                filter=f"version_id == {json.dumps(version_id)}",
                output_fields=["chunk_id"],
                timeout=self._timeout_seconds,
            )
        )
        return {str(row["chunk_id"]) for row in rows}

    async def delete_chunk_ids(self, *, chunk_ids: list[str]) -> None:
        identifiers = self._ids_filter("chunk_id", chunk_ids)
        await self._call(
            lambda: self._client.delete(
                collection_name=self._collection,
                filter=identifiers,
                timeout=self._timeout_seconds,
            )
        )
