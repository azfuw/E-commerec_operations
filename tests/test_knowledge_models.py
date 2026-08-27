from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from backend.common import KnowledgeVersionStatus, UserRole
from backend.models import KnowledgeChunk, KnowledgeDocument, KnowledgeDocumentVersion, User


async def _add_admin(session) -> User:
    admin = User(
        id="knowledge-admin-1",
        username="knowledge-admin",
        password_hash="hash",
        role=UserRole.ADMIN,
    )
    session.add(admin)
    await session.flush()
    return admin


def _document(document_id: str, admin_id: str, **changes: object) -> KnowledgeDocument:
    values: dict[str, object] = {
        "id": document_id,
        "name": "演示规则",
        "category": "演示",
        "created_by": admin_id,
        "idempotency_key": "document-key",
    }
    values.update(changes)
    return KnowledgeDocument(**values)


def _version(version_id: str, document_id: str, **changes: object) -> KnowledgeDocumentVersion:
    values: dict[str, object] = {
        "id": version_id,
        "document_id": document_id,
        "version_number": 1,
        "sha256": "a" * 64,
        "original_filename": "rules.md",
        "mime_type": "text/markdown",
        "storage_path": "data/uploads/knowledge/rules.md",
        "status": KnowledgeVersionStatus.ACCEPTED,
        "idempotency_key": "version-key",
    }
    values.update(changes)
    return KnowledgeDocumentVersion(**values)


def _chunk(chunk_id: str, version_id: str, **changes: object) -> KnowledgeChunk:
    values: dict[str, object] = {
        "id": chunk_id,
        "version_id": version_id,
        "chunk_index": 0,
        "chunk_hash": "b" * 64,
        "canonical_text": "项目演示规则",
        "chunk_metadata": {"heading": "规则"},
        "token_count": 4,
    }
    values.update(changes)
    return KnowledgeChunk(**values)


async def _assert_integrity_error(session, row: object) -> None:
    async with session.begin_nested():
        session.add(row)
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_knowledge_rows_persist_with_stable_chunk_ids_and_metadata(session) -> None:
    admin = await _add_admin(session)
    document = _document("knowledge-document-1", admin.id)
    session.add(document)
    await session.flush()
    version = _version(
        "knowledge-version-1",
        document.id,
        status=KnowledgeVersionStatus.PROCESSING,
        attempt_count=1,
        lease_owner="worker-a",
        lease_expires_at=datetime(2026, 8, 27, tzinfo=UTC),
    )
    session.add(version)
    await session.flush()
    first = _chunk("knowledge-chunk-1", version.id)
    second = _chunk(
        "knowledge-chunk-2",
        version.id,
        chunk_index=1,
        chunk_metadata={"heading": "重复正文"},
    )
    session.add_all([first, second])
    await session.flush()

    assert document.current_version_id is None
    assert version.status is KnowledgeVersionStatus.PROCESSING
    assert version.attempt_count == 1
    assert first.id == "knowledge-chunk-1"
    assert second.chunk_hash == first.chunk_hash
    assert second.chunk_metadata == {"heading": "重复正文"}


async def test_documents_and_versions_reject_duplicate_idempotency_and_version_keys(session) -> None:
    admin = await _add_admin(session)
    document = _document("knowledge-document-1", admin.id)
    session.add(document)
    await session.flush()
    version = _version("knowledge-version-1", document.id)
    session.add(version)
    await session.flush()

    await _assert_integrity_error(
        session,
        _document("knowledge-document-duplicate", admin.id),
    )
    await _assert_integrity_error(
        session,
        _version("knowledge-version-duplicate-number", document.id, sha256="c" * 64),
    )
    await _assert_integrity_error(
        session,
        _version(
            "knowledge-version-duplicate-sha",
            document.id,
            version_number=2,
        ),
    )
    await _assert_integrity_error(
        session,
        _version(
            "knowledge-version-duplicate-key",
            document.id,
            version_number=3,
            sha256="d" * 64,
        ),
    )


@pytest.mark.parametrize(
    ("version_id", "changes"),
    [
        ("knowledge-version-invalid-status", {"status": "unknown"}),
        ("knowledge-version-negative-attempt", {"attempt_count": -1}),
        ("knowledge-version-fourth-attempt", {"attempt_count": 4}),
        (
            "knowledge-version-processing-without-owner",
            {"status": KnowledgeVersionStatus.PROCESSING, "attempt_count": 1},
        ),
        (
            "knowledge-version-accepted-with-lease",
            {
                "lease_owner": "worker-a",
                "lease_expires_at": datetime(2026, 8, 27, tzinfo=UTC),
            },
        ),
    ],
)
async def test_versions_reject_invalid_state_attempt_or_lease(session, version_id, changes) -> None:
    admin = await _add_admin(session)
    document = _document("knowledge-document-1", admin.id)
    session.add(document)
    await session.flush()
    await _assert_integrity_error(session, _version(version_id, document.id, **changes))


async def test_chunks_reject_duplicate_ids_or_indexes_and_invalid_bounds(session) -> None:
    admin = await _add_admin(session)
    document = _document("knowledge-document-1", admin.id)
    session.add(document)
    await session.flush()
    version = _version("knowledge-version-1", document.id)
    session.add(version)
    await session.flush()
    session.add(_chunk("knowledge-chunk-1", version.id))
    await session.flush()

    await _assert_integrity_error(
        session,
        _chunk("knowledge-chunk-duplicate-index", version.id),
    )
    await _assert_integrity_error(
        session,
        _chunk("knowledge-chunk-negative-index", version.id, chunk_index=-1),
    )
    await _assert_integrity_error(
        session,
        _chunk("knowledge-chunk-zero-tokens", version.id, chunk_index=1, token_count=0),
    )
    await _assert_integrity_error(
        session,
        _chunk("knowledge-chunk-1", version.id, chunk_index=2),
    )
