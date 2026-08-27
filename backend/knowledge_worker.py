import asyncio
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.knowledge_content import KnowledgeContentError, StoredKnowledgeUpload, parse_and_chunk
from backend.knowledge_index import (
    IndexedChunk,
    KnowledgeDependencyError,
    LocalKnowledgeModels,
    MilvusKnowledgeIndex,
)
from backend.knowledge_runs import (
    activate_knowledge_version,
    claim_next_knowledge_version,
    fail_knowledge_version,
    renew_knowledge_lease,
    return_version_for_retry,
    upsert_knowledge_chunks,
)
from backend.models import KnowledgeDocument


class LeaseLostError(RuntimeError):
    pass


async def _renew_or_lose(
    session: AsyncSession, *, version_id: str, lease_owner: str, settings: Settings
) -> None:
    if not await renew_knowledge_lease(
        session,
        version_id=version_id,
        lease_owner=lease_owner,
        lease_seconds=settings.knowledge_lease_seconds,
    ):
        raise LeaseLostError


async def _stored_upload(version) -> StoredKnowledgeUpload:
    try:
        data = await asyncio.to_thread(Path(version.storage_path).read_bytes)
    except OSError as error:
        raise KnowledgeContentError("KNOWLEDGE_PARSE_FAILED") from error
    return StoredKnowledgeUpload(
        original_filename=version.original_filename,
        mime_type=version.mime_type,
        sha256=version.sha256,
        data=data,
        storage_path=Path(version.storage_path),
    )


async def _retry_or_fail(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    version_id: str,
    lease_owner: str,
    error: KnowledgeDependencyError,
) -> None:
    async with session_factory() as session:
        if error.retryable:
            await return_version_for_retry(
                session,
                version_id=version_id,
                lease_owner=lease_owner,
                error_code=error.code,
            )
        else:
            await fail_knowledge_version(
                session,
                version_id=version_id,
                lease_owner=lease_owner,
                error_code=error.code,
            )


async def _fail_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    version_id: str,
    lease_owner: str,
    error_code: str,
) -> None:
    async with session_factory() as session:
        await fail_knowledge_version(
            session,
            version_id=version_id,
            lease_owner=lease_owner,
            error_code=error_code,
        )


async def run_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    lease_owner: str,
    models: LocalKnowledgeModels,
    index: MilvusKnowledgeIndex,
) -> str | None:
    async with session_factory() as session:
        version = await claim_next_knowledge_version(
            session,
            lease_owner=lease_owner,
            lease_seconds=settings.knowledge_lease_seconds,
        )
        if version is None:
            return None
        version_id = version.id
        try:
            document = await session.get(KnowledgeDocument, version.document_id)
            if document is None:
                raise KnowledgeContentError("KNOWLEDGE_PARSE_FAILED")

            await _renew_or_lose(
                session, version_id=version_id, lease_owner=lease_owner, settings=settings
            )
            drafts = await parse_and_chunk(
                await _stored_upload(version),
                token_count=models.token_count,
                timeout_seconds=settings.knowledge_dependency_timeout_seconds,
            )
            await _renew_or_lose(
                session, version_id=version_id, lease_owner=lease_owner, settings=settings
            )
            vectors = await models.embed_documents([draft.canonical_text for draft in drafts])
            if len(vectors) != len(drafts):
                raise KnowledgeDependencyError("KNOWLEDGE_MODEL_UNAVAILABLE", retryable=True)
            await _renew_or_lose(
                session, version_id=version_id, lease_owner=lease_owner, settings=settings
            )
            await index.upsert(
                [
                    IndexedChunk(
                        chunk_id=draft.chunk_id,
                        document_id=document.id,
                        version_id=version_id,
                        category=document.category,
                        vector=vector,
                    )
                    for draft, vector in zip(drafts, vectors, strict=True)
                ]
            )
            await _renew_or_lose(
                session, version_id=version_id, lease_owner=lease_owner, settings=settings
            )
            if not await upsert_knowledge_chunks(
                session, version_id=version_id, lease_owner=lease_owner, chunks=drafts
            ):
                raise LeaseLostError
            await _renew_or_lose(
                session, version_id=version_id, lease_owner=lease_owner, settings=settings
            )
            if not await activate_knowledge_version(
                session, version_id=version_id, lease_owner=lease_owner
            ):
                raise LeaseLostError
        except LeaseLostError:
            return version_id
        except KnowledgeDependencyError as error:
            await _retry_or_fail(
                session_factory,
                version_id=version_id,
                lease_owner=lease_owner,
                error=error,
            )
        except KnowledgeContentError as error:
            await _fail_terminal(
                session_factory,
                version_id=version_id,
                lease_owner=lease_owner,
                error_code=error.code,
            )
        except Exception:
            await _fail_terminal(
                session_factory,
                version_id=version_id,
                lease_owner=lease_owner,
                error_code="KNOWLEDGE_PROCESSING_FAILED",
            )
        return version_id
