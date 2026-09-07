import os

import pytest


_OPT_IN = pytest.mark.skipif(
    os.getenv("RUN_KNOWLEDGE_INTEGRATION") != "1",
    reason="explicit PostgreSQL, Milvus, and local-model integration opt-in required",
)


@_OPT_IN
def test_direct_calibration_script_bootstraps_the_project_import_path(tmp_path) -> None:
    """Direct execution must reach project imports before any local model load."""
    import os
    from pathlib import Path
    import subprocess
    import sys

    script = Path(__file__).parents[1] / "scripts" / "calibrate_knowledge_retrieval.py"
    command = (
        "import importlib.util; "
        f"path = {str(script)!r}; "
        "spec = importlib.util.spec_from_file_location('calibration_entry', path); "
        "module = importlib.util.module_from_spec(spec); "
        "spec.loader.exec_module(module); "
        "import backend; print(backend.__name__)"
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-c", command],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "backend"


@_OPT_IN
def test_calibration_script_refuses_without_its_explicit_switch(monkeypatch) -> None:
    """The command must not load local dependencies before opt-in."""
    import asyncio

    from scripts.calibrate_knowledge_retrieval import run_calibration

    monkeypatch.delenv("RUN_KNOWLEDGE_INTEGRATION")
    with pytest.raises(RuntimeError, match="RUN_KNOWLEDGE_INTEGRATION=1"):
        asyncio.run(run_calibration(write_calibration=True))


@_OPT_IN
def test_selected_calibration_replaces_a_same_directory_complete_file(monkeypatch, tmp_path) -> None:
    """A partial calibration must never become the selected configuration."""
    import json
    from pathlib import Path

    from backend import knowledge_evaluation

    target = tmp_path / "calibration.json"
    observed: list[tuple[Path, Path]] = []
    original_replace = knowledge_evaluation.os.replace

    def replace(source, destination) -> None:
        source_path, destination_path = Path(source), Path(destination)
        assert source_path.parent == target.parent
        assert source_path.exists()
        observed.append((source_path, destination_path))
        original_replace(source, destination)

    monkeypatch.setattr(knowledge_evaluation.os, "replace", replace)
    knowledge_evaluation.write_selected_calibration(
        target,
        {"candidate_limit": 10, "rrf_k": 60, "threshold": 0.2},
    )

    assert observed == [(observed[0][0], target)]
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "candidate_limit": 10,
        "rrf_k": 60,
        "threshold": 0.2,
    }
    assert not list(tmp_path.glob(".calibration.json.*.tmp"))


@pytest.mark.knowledge_integration
@_OPT_IN
async def test_knowledge_vertical_slice(monkeypatch) -> None:
    """Exercise only UUID-scoped PostgreSQL/Milvus data with local models."""
    import asyncio
    import json
    from datetime import UTC, datetime
    from io import BytesIO
    from pathlib import Path
    from time import perf_counter
    from uuid import uuid4

    from fastapi import UploadFile
    from sqlalchemy import and_, delete, func, or_, select, text, update
    from starlette.datastructures import Headers

    from backend.common import KnowledgeVersionStatus, UserRole
    from backend.config import get_settings
    from backend.database import async_session_factory
    from backend.knowledge_content import ChunkDraft, read_and_validate_upload, store_validated_upload
    from backend.knowledge_evaluation import RetrievalQueryOutcome, evaluate_retrieval
    from backend.knowledge_index import LocalKnowledgeModels, MilvusKnowledgeIndex
    from backend.knowledge_runs import (
        activate_knowledge_version,
        claim_next_knowledge_version,
        create_document_version,
        disable_knowledge_document,
        upsert_knowledge_chunks,
    )
    from backend.knowledge_search import search_active_knowledge
    from backend.knowledge_worker import run_once
    from backend.models import AuditEvent, KnowledgeChunk, KnowledgeDocument, KnowledgeDocumentVersion, User

    settings = get_settings()
    demo_bytes = Path("data/knowledge/demo/platform-neutral-rules.md").read_bytes()
    queries = json.loads(Path("data/knowledge/evaluation/queries.json").read_text(encoding="utf-8"))
    calibration = json.loads(
        Path("data/knowledge/evaluation/calibration.json").read_text(encoding="utf-8")
    )
    assert calibration["retrieval_path"] == "hybrid_rerank"

    actor_id = str(uuid4())
    document_ids: list[str] = []
    version_ids: list[str] = []
    stored_paths: list[Path] = []
    index: MilvusKnowledgeIndex | None = None

    async def create_version(
        *, document_id: str, name: str | None, category: str | None, content: bytes
    ) -> tuple[KnowledgeDocument, KnowledgeDocumentVersion]:
        version_id = str(uuid4())
        upload = UploadFile(
            file=BytesIO(content),
            filename="platform-neutral-rules.md",
            headers=Headers({"content-type": "text/markdown"}),
        )
        validated = await read_and_validate_upload(upload)
        stored = store_validated_upload(
            validated,
            upload_dir=settings.knowledge_upload_dir,
            document_id=document_id,
            version_id=version_id,
        )
        stored_paths.append(stored.storage_path)
        async with async_session_factory() as session:
            document, version, created = await create_document_version(
                session,
                document_id=document_id,
                version_id=version_id,
                created_by=actor_id,
                name=name,
                category=category,
                sha256=stored.sha256,
                original_filename=stored.original_filename,
                mime_type=stored.mime_type,
                storage_path=str(stored.storage_path),
            )
            assert created
            await session.commit()
        if document_id not in document_ids:
            document_ids.append(document_id)
        version_ids.append(version.id)
        return document, version

    async def claim(owner: str) -> KnowledgeDocumentVersion | None:
        async with async_session_factory() as session:
            return await claim_next_knowledge_version(
                session,
                lease_owner=owner,
                lease_seconds=settings.knowledge_lease_seconds,
            )

    try:
        async with async_session_factory() as session:
            claimable = list(
                await session.scalars(
                    select(KnowledgeDocumentVersion.id).where(
                        or_(
                            KnowledgeDocumentVersion.status == KnowledgeVersionStatus.ACCEPTED,
                            and_(
                                KnowledgeDocumentVersion.status
                                == KnowledgeVersionStatus.PROCESSING,
                                KnowledgeDocumentVersion.lease_expires_at < func.now(),
                            ),
                        )
                    )
                )
            )
            assert not claimable, "pre-existing claimable knowledge rows make an isolated claim unsafe"
            session.add(
                User(
                    id=actor_id,
                    username=f"knowledge-integration-{actor_id}",
                    password_hash="integration-only",
                    role=UserRole.ADMIN,
                )
            )
            await session.commit()

        first_document_id, second_document_id = str(uuid4()), str(uuid4())
        _, first = await create_version(
            document_id=first_document_id,
            name="并发领取一",
            category=f"integration-{actor_id}",
            content=demo_bytes + "\n\n并发一。".encode("utf-8"),
        )
        _, second = await create_version(
            document_id=second_document_id,
            name="并发领取二",
            category=f"integration-{actor_id}",
            content=demo_bytes + "\n\n并发二。".encode("utf-8"),
        )
        first_claim, second_claim = await asyncio.gather(claim("claim-one"), claim("claim-two"))
        assert {first_claim.id, second_claim.id} == {first.id, second.id}

        async with async_session_factory() as session:
            await session.execute(
                update(KnowledgeDocumentVersion)
                .where(KnowledgeDocumentVersion.id == first.id)
                .values(
                    attempt_count=2,
                    status=KnowledgeVersionStatus.PROCESSING,
                    lease_owner="old-owner",
                    lease_expires_at=func.now() - text("interval '1 second'"),
                )
            )
            await session.commit()
        reclaimed = await claim("attempt-three-owner")
        assert reclaimed is not None and (reclaimed.id, reclaimed.attempt_count) == (first.id, 3)
        async with async_session_factory() as session:
            assert not await activate_knowledge_version(
                session, version_id=first.id, lease_owner="old-owner"
            )

        exhausted_document_id = str(uuid4())
        _, exhausted = await create_version(
            document_id=exhausted_document_id,
            name="领取耗尽",
            category=f"integration-{actor_id}",
            content=demo_bytes + "\n\n耗尽。".encode("utf-8"),
        )
        async with async_session_factory() as session:
            await session.execute(
                update(KnowledgeDocumentVersion)
                .where(KnowledgeDocumentVersion.id == exhausted.id)
                .values(
                    attempt_count=3,
                    status=KnowledgeVersionStatus.PROCESSING,
                    lease_owner="expired-owner",
                    lease_expires_at=func.now() - text("interval '1 second'"),
                )
            )
            await session.commit()
        assert await claim("no-fourth-claim") is None
        async with async_session_factory() as session:
            exhausted_state = await session.get(KnowledgeDocumentVersion, exhausted.id)
            assert exhausted_state is not None and (
                exhausted_state.status,
                exhausted_state.error_code,
            ) == (KnowledgeVersionStatus.FAILED, "KNOWLEDGE_ATTEMPTS_EXHAUSTED")

            # Finished claim fixtures must not expire and compete with later real-model work.
            await session.execute(
                delete(KnowledgeDocumentVersion).where(
                    KnowledgeDocumentVersion.id.in_((first.id, second.id))
                )
            )
            await session.execute(
                delete(KnowledgeDocument).where(
                    KnowledgeDocument.id.in_((first_document_id, second_document_id))
                )
            )
            await session.commit()

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

        recovery_document_id = str(uuid4())
        _, recovery = await create_version(
            document_id=recovery_document_id,
            name="项目演示规则",
            category=f"integration-{actor_id}",
            content=demo_bytes,
        )
        from backend import knowledge_worker

        original_upsert = knowledge_worker.upsert_knowledge_chunks

        async def cancel_after_milvus(*_args, **_kwargs) -> bool:
            raise asyncio.CancelledError

        monkeypatch.setattr(knowledge_worker, "upsert_knowledge_chunks", cancel_after_milvus)
        with pytest.raises(asyncio.CancelledError):
            await run_once(
                async_session_factory,
                settings=settings,
                lease_owner="recovery-first-owner",
                models=models,
                index=index,
            )
        expected_ids = await index.list_chunk_ids_for_version(version_id=recovery.id)
        assert expected_ids
        async with async_session_factory() as session:
            state = await session.get(KnowledgeDocumentVersion, recovery.id)
            assert state is not None and state.status is KnowledgeVersionStatus.PROCESSING
            assert state.lease_owner == "recovery-first-owner"
            await session.execute(
                update(KnowledgeDocumentVersion)
                .where(KnowledgeDocumentVersion.id == recovery.id)
                .values(lease_expires_at=func.now() - text("interval '1 second'"))
            )
            await session.commit()
        monkeypatch.setattr(knowledge_worker, "upsert_knowledge_chunks", original_upsert)
        assert await run_once(
            async_session_factory,
            settings=settings,
            lease_owner="recovery-second-owner",
            models=models,
            index=index,
        ) == recovery.id
        async with async_session_factory() as session:
            chunks = list(
                await session.scalars(
                    select(KnowledgeChunk).where(KnowledgeChunk.version_id == recovery.id)
                )
            )
            drafts = [
                ChunkDraft(
                    chunk_index=chunk.chunk_index,
                    chunk_id=chunk.id,
                    chunk_hash=chunk.chunk_hash,
                    canonical_text=chunk.canonical_text,
                    chunk_metadata=chunk.chunk_metadata,
                    token_count=chunk.token_count,
                )
                for chunk in chunks
            ]
            persisted_ids = {chunk.id for chunk in chunks}
            assert not await upsert_knowledge_chunks(
                session,
                version_id=recovery.id,
                lease_owner="recovery-first-owner",
                chunks=drafts,
            )
            assert not await activate_knowledge_version(
                session, version_id=recovery.id, lease_owner="recovery-first-owner"
            )
        assert persisted_ids == expected_ids
        assert await index.list_chunk_ids_for_version(version_id=recovery.id) == expected_ids

        monotonic_document_id = str(uuid4())
        monotonic_document, older = await create_version(
            document_id=monotonic_document_id,
            name="单调激活",
            category=f"monotonic-{actor_id}",
            content=demo_bytes + "\n\n较旧版本。".encode("utf-8"),
        )
        older_claim = await claim("slow-older-owner")
        assert older_claim is not None and older_claim.id == older.id
        _, newer = await create_version(
            document_id=monotonic_document.id,
            name=None,
            category=None,
            content=demo_bytes + "\n\n较新版本。".encode("utf-8"),
        )
        assert await run_once(
            async_session_factory,
            settings=settings,
            lease_owner="newer-owner",
            models=models,
            index=index,
        ) == newer.id
        async with async_session_factory() as session:
            await session.execute(
                update(KnowledgeDocumentVersion)
                .where(KnowledgeDocumentVersion.id == older.id)
                .values(lease_expires_at=func.now() - text("interval '1 second'"))
            )
            await session.commit()
        assert await run_once(
            async_session_factory,
            settings=settings,
            lease_owner="older-replay-owner",
            models=models,
            index=index,
        ) == older.id
        async with async_session_factory() as session:
            older_state = await session.get(KnowledgeDocumentVersion, older.id)
            assert older_state is not None and (
                older_state.status,
                older_state.error_code,
            ) == (KnowledgeVersionStatus.DISABLED, "KNOWLEDGE_VERSION_SUPERSEDED")
            loader = lambda: (models, index, calibration)
            visible = await search_active_knowledge(
                session,
                query="标题关键词",
                categories=[f"monotonic-{actor_id}"],
                top_k=10,
                retrieval_path="hybrid_rerank",
                load_dependencies=loader,
            )
            assert visible.hits and {hit.version_number for hit in visible.hits} == {2}
            assert await disable_knowledge_document(session, document_id=monotonic_document.id)
            hidden = await search_active_knowledge(
                session,
                query="标题关键词",
                categories=[f"monotonic-{actor_id}"],
                top_k=10,
                retrieval_path="hybrid_rerank",
                load_dependencies=loader,
            )
            assert hidden.quality_status == "zero_hit" and not hidden.hits

        outcomes = {path: {} for path in ("dense", "sparse", "hybrid", "hybrid_rerank")}
        async with async_session_factory() as session:
            for path in outcomes:
                for query in queries:
                    started = perf_counter()
                    outcome = await search_active_knowledge(
                        session,
                        query=str(query["query"]),
                        categories=[f"integration-{actor_id}"],
                        top_k=10,
                        retrieval_path=path,
                        load_dependencies=lambda: (models, index, calibration),
                    )
                    outcomes[path][str(query["id"])] = RetrievalQueryOutcome(
                        query_id=str(query["id"]),
                        hits=outcome.hits,
                        elapsed_ms=(perf_counter() - started) * 1000,
                    )
        metrics = evaluate_retrieval(queries, outcomes)
        selected_metrics = metrics["hybrid_rerank"]
        assert selected_metrics.recall_at_10 >= 0.90
        assert selected_metrics.citation_document_version_accuracy == 1.0
    finally:
        if index is not None:
            async with async_session_factory() as session:
                database_ids = set(
                    await session.scalars(
                        select(KnowledgeChunk.id).where(KnowledgeChunk.version_id.in_(version_ids))
                    )
                )
            for version_id in version_ids:
                database_ids.update(await index.list_chunk_ids_for_version(version_id=version_id))
            if database_ids:
                await index.delete_chunk_ids(chunk_ids=sorted(database_ids))
        async with async_session_factory() as session:
            if document_ids:
                await session.execute(
                    update(KnowledgeDocument)
                    .where(KnowledgeDocument.id.in_(document_ids))
                    .values(current_version_id=None)
                )
            if version_ids:
                await session.execute(
                    delete(KnowledgeChunk).where(KnowledgeChunk.version_id.in_(version_ids))
                )
                await session.execute(
                    delete(KnowledgeDocumentVersion).where(KnowledgeDocumentVersion.id.in_(version_ids))
                )
            if document_ids:
                await session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id.in_(document_ids)))
            await session.execute(delete(AuditEvent).where(AuditEvent.actor_id == actor_id))
            await session.execute(delete(User).where(User.id == actor_id))
            await session.commit()
        for path in stored_paths:
            if path.exists():
                path.unlink()
            for directory in (path.parent, path.parent.parent):
                try:
                    directory.rmdir()
                except OSError:
                    pass
