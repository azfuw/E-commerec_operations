import logging
from time import perf_counter

from sqlalchemy import and_, func, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common import KnowledgeVersionStatus, AuditEventType, AuditOutcome
from backend.audit_events import add_audit_event
from backend.knowledge_content import ChunkDraft
from backend.models import KnowledgeChunk, KnowledgeDocument, KnowledgeDocumentVersion, User


_logger = logging.getLogger("backend.knowledge")


def _lease_expiry(session: AsyncSession, lease_seconds: int):
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        return func.now() + text("make_interval(secs => :lease_seconds)").bindparams(
            lease_seconds=lease_seconds
        )
    return func.datetime(func.now(), literal(f"+{lease_seconds} seconds"))


def _owned_lease(version_id: str, lease_owner: str):
    return (
        KnowledgeDocumentVersion.id == version_id,
        KnowledgeDocumentVersion.status == KnowledgeVersionStatus.PROCESSING,
        KnowledgeDocumentVersion.lease_owner == lease_owner,
        KnowledgeDocumentVersion.lease_expires_at > func.now(),
    )


def _insert_for(session: AsyncSession, model):
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        return postgresql_insert(model)
    return sqlite_insert(model)


def _log_transition(
    *,
    started: float,
    actor_id: str,
    document_id: str,
    version_id: str | None,
    status: KnowledgeVersionStatus,
    error_code: str | None = None,
) -> None:
    _logger.info(
        "knowledge_transition request_id=- actor_id=%s document_id=%s version_id=%s "
        "status=%s error_code=%s elapsed_ms=%d",
        actor_id,
        document_id,
        version_id or "-",
        status,
        error_code or "-",
        int((perf_counter() - started) * 1000),
    )


async def find_document_version_by_sha(
    session: AsyncSession, *, document_id: str, sha256: str
) -> KnowledgeDocumentVersion | None:
    return await session.scalar(
        select(KnowledgeDocumentVersion).where(
            KnowledgeDocumentVersion.document_id == document_id,
            KnowledgeDocumentVersion.sha256 == sha256,
        )
    )


async def find_document_version_by_idempotency_key(
    session: AsyncSession, *, document_id: str, idempotency_key: str
) -> KnowledgeDocumentVersion | None:
    return await session.scalar(
        select(KnowledgeDocumentVersion).where(
            KnowledgeDocumentVersion.document_id == document_id,
            KnowledgeDocumentVersion.idempotency_key == idempotency_key,
        )
    )


async def find_document_by_idempotency_key(
    session: AsyncSession, *, created_by: str, idempotency_key: str
) -> KnowledgeDocument | None:
    return await session.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.created_by == created_by,
            KnowledgeDocument.idempotency_key == idempotency_key,
        )
    )


async def create_document_version(
    session: AsyncSession,
    *,
    document_id: str,
    version_id: str,
    created_by: str,
    name: str | None,
    category: str | None,
    sha256: str,
    original_filename: str,
    mime_type: str,
    storage_path: str,
    idempotency_key: str | None = None,
) -> tuple[KnowledgeDocument, KnowledgeDocumentVersion, bool]:
    started = perf_counter()
    if name is not None and idempotency_key:
        existing_document = await find_document_by_idempotency_key(
            session, created_by=created_by, idempotency_key=idempotency_key
        )
        if existing_document is not None:
            existing_version = await find_document_version_by_sha(
                session, document_id=existing_document.id, sha256=sha256
            )
            if existing_version is None:
                raise ValueError("KNOWLEDGE_IDEMPOTENCY_CONFLICT")
            return existing_document, existing_version, False

    document = await session.get(KnowledgeDocument, document_id)
    new_document = document is None
    if document is None:
        if name is None or category is None:
            raise ValueError("KNOWLEDGE_DOCUMENT_NOT_FOUND")
        document = KnowledgeDocument(
            id=document_id,
            name=name,
            category=category,
            created_by=created_by,
            idempotency_key=idempotency_key,
        )
        session.add(document)
        await session.flush()
        version_number = 1
        version_key = None
    else:
        if idempotency_key:
            existing_version = await find_document_version_by_idempotency_key(
                session, document_id=document_id, idempotency_key=idempotency_key
            )
            if existing_version is not None:
                if existing_version.sha256 != sha256:
                    raise ValueError("KNOWLEDGE_IDEMPOTENCY_CONFLICT")
                return document, existing_version, False
        existing_version = await find_document_version_by_sha(
            session, document_id=document_id, sha256=sha256
        )
        if existing_version is not None:
            return document, existing_version, False
        version_number = (await session.scalar(
            select(func.max(KnowledgeDocumentVersion.version_number)).where(
                KnowledgeDocumentVersion.document_id == document_id
            )
        ) or 0) + 1
        version_key = idempotency_key

    version = KnowledgeDocumentVersion(
        id=version_id,
        document_id=document.id,
        version_number=version_number,
        sha256=sha256,
        original_filename=original_filename,
        mime_type=mime_type,
        storage_path=storage_path,
        status=KnowledgeVersionStatus.ACCEPTED,
        idempotency_key=version_key,
    )
    session.add(version)
    await session.flush()
    actor = await session.get(User, created_by)
    add_audit_event(session,
        event_type=(AuditEventType.KNOWLEDGE_DOCUMENT_CREATED if new_document
                    else AuditEventType.KNOWLEDGE_VERSION_CREATED),
        outcome=AuditOutcome.SUCCESS, store_id=None, actor_id=created_by,
        actor_role=actor.role if actor else None,
        resource_type='knowledge_document' if new_document else 'knowledge_version',
        resource_id=document.id if new_document else version.id,
        details={'document_status': KnowledgeVersionStatus.ACCEPTED.value})
    _log_transition(
        started=started,
        actor_id=created_by,
        document_id=document.id,
        version_id=version.id,
        status=KnowledgeVersionStatus.ACCEPTED,
    )
    return document, version, True


async def claim_next_knowledge_version(
    session: AsyncSession, *, lease_owner: str, lease_seconds: int
) -> KnowledgeDocumentVersion | None:
    started = perf_counter()
    exhausted_rows = (await session.execute(
        update(KnowledgeDocumentVersion)
        .where(
            KnowledgeDocumentVersion.status == KnowledgeVersionStatus.PROCESSING,
            KnowledgeDocumentVersion.lease_expires_at < func.now(),
            KnowledgeDocumentVersion.attempt_count >= 3,
        )
        .values(
            status=KnowledgeVersionStatus.FAILED,
            lease_owner=None,
            lease_expires_at=None,
            error_code="KNOWLEDGE_ATTEMPTS_EXHAUSTED",
        )
        .returning(KnowledgeDocumentVersion.id, KnowledgeDocumentVersion.document_id)
    )).all()
    exhausted_contexts: list[tuple[str, str, str]] = []
    for exhausted_id, document_id in exhausted_rows:
        document = await session.get(KnowledgeDocument, document_id)
        if document is not None:
            exhausted_contexts.append((exhausted_id, document_id, document.created_by))
    eligible = or_(
        KnowledgeDocumentVersion.status == KnowledgeVersionStatus.ACCEPTED,
        and_(
            KnowledgeDocumentVersion.status == KnowledgeVersionStatus.PROCESSING,
            KnowledgeDocumentVersion.lease_expires_at < func.now(),
        ),
    )
    version = await session.scalar(
        select(KnowledgeDocumentVersion)
        .where(eligible, KnowledgeDocumentVersion.attempt_count < 3)
        .order_by(KnowledgeDocumentVersion.created_at, KnowledgeDocumentVersion.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if version is None:
        await session.commit()
    else:
        document = await session.get(KnowledgeDocument, version.document_id)
        version.status = KnowledgeVersionStatus.PROCESSING
        version.lease_owner = lease_owner
        version.lease_expires_at = _lease_expiry(session, lease_seconds)
        version.attempt_count += 1
        version.error_code = None
        await session.commit()
    for exhausted_id, document_id, actor_id in exhausted_contexts:
        _log_transition(
            started=started,
            actor_id=actor_id,
            document_id=document_id,
            version_id=exhausted_id,
            status=KnowledgeVersionStatus.FAILED,
            error_code="KNOWLEDGE_ATTEMPTS_EXHAUSTED",
        )
    if version is None:
        return None
    _log_transition(
        started=started,
        actor_id=document.created_by if document is not None else "-",
        document_id=version.document_id,
        version_id=version.id,
        status=KnowledgeVersionStatus.PROCESSING,
    )
    return version


async def _version_context(
    session: AsyncSession, version_id: str
) -> tuple[str, str] | None:
    row = (await session.execute(
        select(KnowledgeDocument.id, KnowledgeDocument.created_by)
        .join(
            KnowledgeDocumentVersion,
            KnowledgeDocumentVersion.document_id == KnowledgeDocument.id,
        )
        .where(KnowledgeDocumentVersion.id == version_id)
    )).one_or_none()
    return None if row is None else (row[0], row[1])


async def _commit_owned_update(
    session: AsyncSession,
    *,
    version_id: str,
    statement,
    status: KnowledgeVersionStatus,
    error_code: str | None = None,
) -> bool:
    started = perf_counter()
    context = await _version_context(session, version_id)
    result = await session.execute(statement)
    if not result.rowcount:
        await session.rollback()
        return False
    await session.commit()
    if context is not None:
        _log_transition(
            started=started,
            actor_id=context[1],
            document_id=context[0],
            version_id=version_id,
            status=status,
            error_code=error_code,
        )
    return True


async def renew_knowledge_lease(
    session: AsyncSession, *, version_id: str, lease_owner: str, lease_seconds: int
) -> bool:
    return await _commit_owned_update(
        session,
        version_id=version_id,
        statement=(
            update(KnowledgeDocumentVersion)
            .where(*_owned_lease(version_id, lease_owner))
            .values(lease_expires_at=_lease_expiry(session, lease_seconds))
        ),
        status=KnowledgeVersionStatus.PROCESSING,
    )


async def return_version_for_retry(
    session: AsyncSession, *, version_id: str, lease_owner: str, error_code: str
) -> bool:
    accepted = await _commit_owned_update(
        session,
        version_id=version_id,
        statement=(
            update(KnowledgeDocumentVersion)
            .where(*_owned_lease(version_id, lease_owner), KnowledgeDocumentVersion.attempt_count < 3)
            .values(
                status=KnowledgeVersionStatus.ACCEPTED,
                lease_owner=None,
                lease_expires_at=None,
                error_code=error_code,
            )
        ),
        status=KnowledgeVersionStatus.ACCEPTED,
        error_code=error_code,
    )
    if accepted:
        return True
    return await _commit_owned_update(
        session,
        version_id=version_id,
        statement=(
            update(KnowledgeDocumentVersion)
            .where(*_owned_lease(version_id, lease_owner), KnowledgeDocumentVersion.attempt_count == 3)
            .values(
                status=KnowledgeVersionStatus.FAILED,
                lease_owner=None,
                lease_expires_at=None,
                error_code="KNOWLEDGE_ATTEMPTS_EXHAUSTED",
            )
        ),
        status=KnowledgeVersionStatus.FAILED,
        error_code="KNOWLEDGE_ATTEMPTS_EXHAUSTED",
    )


async def fail_knowledge_version(
    session: AsyncSession, *, version_id: str, lease_owner: str, error_code: str
) -> bool:
    return await _commit_owned_update(
        session,
        version_id=version_id,
        statement=(
            update(KnowledgeDocumentVersion)
            .where(*_owned_lease(version_id, lease_owner))
            .values(
                status=KnowledgeVersionStatus.FAILED,
                lease_owner=None,
                lease_expires_at=None,
                error_code=error_code,
            )
        ),
        status=KnowledgeVersionStatus.FAILED,
        error_code=error_code,
    )


async def upsert_knowledge_chunks(
    session: AsyncSession, *, version_id: str, lease_owner: str, chunks: list[ChunkDraft]
) -> bool:
    started = perf_counter()
    version = await session.scalar(
        select(KnowledgeDocumentVersion)
        .where(*_owned_lease(version_id, lease_owner))
        .with_for_update()
    )
    if version is None:
        await session.rollback()
        return False
    context = await _version_context(session, version_id)
    try:
        for chunk in chunks:
            values = {
                "id": chunk.chunk_id,
                "version_id": version_id,
                "chunk_index": chunk.chunk_index,
                "chunk_hash": chunk.chunk_hash,
                "canonical_text": chunk.canonical_text,
                "chunk_metadata": chunk.chunk_metadata,
                "token_count": chunk.token_count,
            }
            insert = _insert_for(session, KnowledgeChunk).values(values)
            await session.execute(
                insert.on_conflict_do_update(
                    index_elements=["version_id", "chunk_index"],
                    set_={
                        "chunk_hash": insert.excluded["chunk_hash"],
                        "canonical_text": insert.excluded["canonical_text"],
                        "metadata": insert.excluded["metadata"],
                        "token_count": insert.excluded["token_count"],
                    },
                )
            )
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    if context is not None:
        _log_transition(
            started=started,
            actor_id=context[1],
            document_id=context[0],
            version_id=version_id,
            status=KnowledgeVersionStatus.PROCESSING,
        )
    return True


async def activate_knowledge_version(
    session: AsyncSession, *, version_id: str, lease_owner: str
) -> bool:
    started = perf_counter()
    document_id = await session.scalar(
        select(KnowledgeDocumentVersion.document_id).where(
            KnowledgeDocumentVersion.id == version_id
        )
    )
    if document_id is None:
        await session.rollback()
        return False
    document = await session.scalar(
        select(KnowledgeDocument)
        .where(KnowledgeDocument.id == document_id)
        .with_for_update()
    )
    if document is None or not document.enabled:
        await session.rollback()
        return False
    candidate = await session.scalar(
        select(KnowledgeDocumentVersion)
        .where(*_owned_lease(version_id, lease_owner))
        .with_for_update()
    )
    if candidate is None:
        await session.rollback()
        return False
    current = (
        await session.get(KnowledgeDocumentVersion, document.current_version_id)
        if document.current_version_id is not None
        else None
    )
    if current is None or current.version_number < candidate.version_number:
        result = await session.execute(
            update(KnowledgeDocumentVersion)
            .where(*_owned_lease(version_id, lease_owner))
            .values(
                status=KnowledgeVersionStatus.ACTIVE,
                lease_owner=None,
                lease_expires_at=None,
                error_code=None,
            )
        )
        if not result.rowcount:
            await session.rollback()
            return False
        if current is not None:
            await session.execute(
                update(KnowledgeDocumentVersion)
                .where(
                    KnowledgeDocumentVersion.id == current.id,
                    KnowledgeDocumentVersion.status == KnowledgeVersionStatus.ACTIVE,
                )
                .values(status=KnowledgeVersionStatus.DISABLED)
            )
        await session.execute(
            update(KnowledgeDocument)
            .where(KnowledgeDocument.id == document.id)
            .values(current_version_id=version_id)
        )
        await session.commit()
        _log_transition(
            started=started,
            actor_id=document.created_by,
            document_id=document.id,
            version_id=version_id,
            status=KnowledgeVersionStatus.ACTIVE,
        )
        return True

    result = await session.execute(
        update(KnowledgeDocumentVersion)
        .where(*_owned_lease(version_id, lease_owner))
        .values(
            status=KnowledgeVersionStatus.DISABLED,
            lease_owner=None,
            lease_expires_at=None,
            error_code="KNOWLEDGE_VERSION_SUPERSEDED",
        )
    )
    if not result.rowcount:
        await session.rollback()
        return False
    await session.commit()
    _log_transition(
        started=started,
        actor_id=document.created_by,
        document_id=document.id,
        version_id=version_id,
        status=KnowledgeVersionStatus.DISABLED,
        error_code="KNOWLEDGE_VERSION_SUPERSEDED",
    )
    return False


async def disable_knowledge_document(session: AsyncSession, *, document_id: str, actor_id: str | None = None) -> bool:
    started = perf_counter()
    document = await session.scalar(
        select(KnowledgeDocument)
        .where(KnowledgeDocument.id == document_id)
        .with_for_update()
    )
    if document is None:
        await session.rollback()
        return False
    await session.execute(
        update(KnowledgeDocument)
        .where(KnowledgeDocument.id == document_id)
        .values(enabled=False, current_version_id=None)
    )
    await session.execute(
        update(KnowledgeDocumentVersion)
        .where(
            KnowledgeDocumentVersion.document_id == document_id,
            KnowledgeDocumentVersion.status.in_(
                (
                    KnowledgeVersionStatus.ACCEPTED,
                    KnowledgeVersionStatus.PROCESSING,
                    KnowledgeVersionStatus.ACTIVE,
                )
            ),
        )
        .values(
            status=KnowledgeVersionStatus.DISABLED,
            lease_owner=None,
            lease_expires_at=None,
        )
    )
    actor = await session.get(User, actor_id or document.created_by)
    add_audit_event(session, event_type=AuditEventType.KNOWLEDGE_DOCUMENT_DISABLED,
        outcome=AuditOutcome.SUCCESS, store_id=None, actor_id=actor.id if actor else None,
        actor_role=actor.role if actor else None, resource_type='knowledge_document',
        resource_id=document_id, details={'document_status': KnowledgeVersionStatus.DISABLED.value})
    await session.commit()
    _log_transition(
        started=started,
        actor_id=actor_id or document.created_by,
        document_id=document_id,
        version_id=None,
        status=KnowledgeVersionStatus.DISABLED,
    )
    return True
