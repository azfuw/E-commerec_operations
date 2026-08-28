from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from io import BytesIO
import json
import os
from pathlib import Path
import sys
from time import perf_counter
from uuid import uuid4


_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_DEMO_PATH = _ROOT / "data" / "knowledge" / "demo" / "platform-neutral-rules.md"
_QUERIES_PATH = _ROOT / "data" / "knowledge" / "evaluation" / "queries.json"
_CANDIDATES_PATH = _ROOT / "data" / "knowledge" / "evaluation" / "calibration-candidates.json"
_CALIBRATION_PATH = _ROOT / "data" / "knowledge" / "evaluation" / "calibration.json"


async def run_calibration(*, write_calibration: bool) -> dict[str, object]:
    if os.getenv("RUN_KNOWLEDGE_INTEGRATION") != "1":
        raise RuntimeError("RUN_KNOWLEDGE_INTEGRATION=1 is required for local calibration")

    from fastapi import UploadFile
    from sqlalchemy import delete, select, update
    from starlette.datastructures import Headers

    from backend.common import UserRole
    from backend.config import get_settings
    from backend.database import async_session_factory
    from backend.knowledge_content import read_and_validate_upload, store_validated_upload
    from backend.knowledge_evaluation import (
        RetrievalQueryOutcome,
        evaluate_retrieval,
        select_calibration,
        write_selected_calibration,
    )
    from backend.knowledge_index import LocalKnowledgeModels, MilvusKnowledgeIndex
    from backend.knowledge_runs import create_document_version
    from backend.knowledge_search import search_active_knowledge
    from backend.knowledge_worker import run_once
    from backend.models import KnowledgeChunk, KnowledgeDocument, KnowledgeDocumentVersion, User

    settings = get_settings()
    actor_id = str(uuid4())
    document_id = str(uuid4())
    version_id = str(uuid4())
    stored_path: Path | None = None
    index: MilvusKnowledgeIndex | None = None
    try:
        demo = _DEMO_PATH.read_bytes()
        queries = json.loads(_QUERIES_PATH.read_text(encoding="utf-8"))
        candidates = json.loads(_CANDIDATES_PATH.read_text(encoding="utf-8"))
        by_path = {str(candidate["retrieval_path"]): candidate for candidate in candidates}
        expected_paths = {"dense", "sparse", "hybrid", "hybrid_rerank"}
        if set(by_path) != expected_paths or len(by_path) != len(candidates):
            raise RuntimeError("calibration candidates must define one finite candidate per retrieval path")

        models = LocalKnowledgeModels(
            embedding_model_path=settings.knowledge_embedding_model_path,
            reranker_model_path=settings.knowledge_reranker_model_path,
            timeout_seconds=settings.knowledge_dependency_timeout_seconds,
        )
        index = MilvusKnowledgeIndex(
            uri=settings.milvus_uri,
            collection=settings.milvus_collection,
            timeout_seconds=settings.knowledge_dependency_timeout_seconds,
        )
        await index.ensure_collection()

        upload = UploadFile(
            file=BytesIO(demo),
            filename=_DEMO_PATH.name,
            headers=Headers({"content-type": "text/markdown"}),
        )
        validated = await read_and_validate_upload(upload)
        stored = store_validated_upload(
            validated,
            upload_dir=settings.knowledge_upload_dir,
            document_id=document_id,
            version_id=version_id,
        )
        stored_path = stored.storage_path
        async with async_session_factory() as session:
            session.add(
                User(
                    id=actor_id,
                    username=f"knowledge-calibration-{actor_id}",
                    password_hash="calibration-only",
                    role=UserRole.ADMIN,
                )
            )
            await session.flush()
            _, version, created = await create_document_version(
                session,
                document_id=document_id,
                version_id=version_id,
                created_by=actor_id,
                name="项目演示规则",
                category=f"calibration-{actor_id}",
                sha256=stored.sha256,
                original_filename=stored.original_filename,
                mime_type=stored.mime_type,
                storage_path=str(stored.storage_path),
            )
            if not created or version.id != version_id:
                raise RuntimeError("calibration version was not created")
            await session.commit()
        if await run_once(
            async_session_factory,
            settings=settings,
            lease_owner=f"calibration-{actor_id}",
            models=models,
            index=index,
        ) != version_id:
            raise RuntimeError("calibration version was not indexed")

        outcomes = {path: {} for path in expected_paths}
        async with async_session_factory() as session:
            for path, candidate in by_path.items():
                for query in queries:
                    started = perf_counter()
                    outcome = await search_active_knowledge(
                        session,
                        query=str(query["query"]),
                        categories=[f"calibration-{actor_id}"],
                        top_k=10,
                        retrieval_path=path,
                        load_dependencies=lambda: (models, index, candidate),
                    )
                    outcomes[path][str(query["id"])] = RetrievalQueryOutcome(
                        query_id=str(query["id"]),
                        hits=outcome.hits,
                        elapsed_ms=(perf_counter() - started) * 1000,
                    )
        metrics = evaluate_retrieval(queries, outcomes)
        hybrid_candidates = [
            candidate for candidate in candidates if candidate["retrieval_path"] == "hybrid_rerank"
        ]
        selected_candidate = select_calibration(
            hybrid_candidates,
            {str(candidate["id"]): metrics["hybrid_rerank"] for candidate in hybrid_candidates},
        )
        selected_metrics = metrics["hybrid_rerank"]
        if (
            selected_metrics.recall_at_10 < 0.90
            or selected_metrics.citation_document_version_accuracy != 1.0
        ):
            raise RuntimeError("calibration acceptance thresholds were not met")
        selection = {
            "candidate_id": selected_candidate["id"],
            "retrieval_path": "hybrid_rerank",
            "candidate_limit": selected_candidate["candidate_limit"],
            "rrf_k": selected_candidate["rrf_k"],
            "threshold": selected_candidate["threshold"],
            "metrics": {path: asdict(value) for path, value in metrics.items()},
        }
        if write_calibration:
            write_selected_calibration(_CALIBRATION_PATH, selection)
        return selection
    finally:
        vector_ids: set[str] = set()
        if index is not None:
            vector_ids = await index.list_chunk_ids_for_version(version_id=version_id)
            if vector_ids:
                await index.delete_chunk_ids(chunk_ids=sorted(vector_ids))
        async with async_session_factory() as session:
            database_ids = set(
                await session.scalars(
                    select(KnowledgeChunk.id).where(KnowledgeChunk.version_id == version_id)
                )
            )
            if database_ids:
                await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.id.in_(database_ids)))
            await session.execute(
                update(KnowledgeDocument)
                .where(KnowledgeDocument.id == document_id)
                .values(current_version_id=None)
            )
            await session.execute(delete(KnowledgeDocumentVersion).where(KnowledgeDocumentVersion.id == version_id))
            await session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id == document_id))
            await session.execute(delete(User).where(User.id == actor_id))
            await session.commit()
        if stored_path is not None:
            if stored_path.exists():
                stored_path.unlink()
            for directory in (stored_path.parent, stored_path.parent.parent):
                try:
                    directory.rmdir()
                except OSError:
                    pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-calibration", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run_calibration(write_calibration=args.write_calibration))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
