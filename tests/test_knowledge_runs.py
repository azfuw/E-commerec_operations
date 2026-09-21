from datetime import UTC, datetime, timedelta
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy import and_, func, select, update

from backend.common import KnowledgeVersionStatus, UserDepartment, UserRole, UserStatus
from backend.knowledge_content import ChunkDraft
from backend.knowledge_runs import (
    activate_knowledge_version,
    claim_next_knowledge_version,
    create_document_version,
    disable_knowledge_document,
    fail_knowledge_version,
    find_document_by_idempotency_key,
    find_document_version_by_idempotency_key,
    find_document_version_by_sha,
    renew_knowledge_lease,
    return_version_for_retry,
    upsert_knowledge_chunks,
)
from backend.models import KnowledgeChunk, KnowledgeDocument, KnowledgeDocumentVersion, User


async def _add_admin(session) -> User:
    admin = User(
        id="knowledge-runs-admin",
        username="knowledge-runs-admin",
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
    }
    values.update(changes)
    return KnowledgeDocument(**values)


def _version(
    version_id: str, document_id: str, version_number: int, **changes: object
) -> KnowledgeDocumentVersion:
    values: dict[str, object] = {
        "id": version_id,
        "document_id": document_id,
        "version_number": version_number,
        "sha256": f"{version_number:x}" * 64,
        "original_filename": "rules.md",
        "mime_type": "text/markdown",
        "storage_path": "data/uploads/knowledge/private/rules.md",
        "status": KnowledgeVersionStatus.ACCEPTED,
    }
    values.update(changes)
    return KnowledgeDocumentVersion(**values)


def _draft(index: int) -> ChunkDraft:
    return ChunkDraft(
        chunk_index=index,
        chunk_id=f"knowledge-run-chunk-{index}",
        chunk_hash="a" * 64,
        canonical_text="项目演示规则正文",
        chunk_metadata={"heading_path": ["章节"], "paragraph_index": index},
        token_count=4,
    )


class _ActivationStatementSpy:
    def __init__(
        self,
        *,
        document: KnowledgeDocument,
        candidate: KnowledgeDocumentVersion | None,
        pre_read_document_id: str | None = None,
    ) -> None:
        self.document = document
        self.candidate = candidate
        self.pre_read_document_id = pre_read_document_id
        self.scalar_statements = []
        self.executed_statements = []
        self.commit_count = 0
        self.rollback_count = 0

    async def scalar(self, statement):
        self.scalar_statements.append(statement)
        description = statement.column_descriptions[0]
        if description["name"] == "actor_id":
            return None
        if description["name"] == "created_by":
            return self.document.created_by
        if description["entity"] is User:
            return User(id=self.document.created_by, username="admin", password_hash="unused",
                role=UserRole.ADMIN, department=UserDepartment.OPERATIONS, status=UserStatus.ACTIVE)
        if description["name"] == "document_id":
            return self.pre_read_document_id
        if description["entity"] is KnowledgeDocument:
            return self.document
        return self.candidate

    async def get(self, *_args, **_kwargs):
        return None

    async def execute(self, statement):
        self.executed_statements.append(statement)
        return SimpleNamespace(rowcount=1)

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1


async def test_create_document_version_distinguishes_both_idempotency_keys(session) -> None:
    admin = await _add_admin(session)
    document, initial, created = await create_document_version(
        session,
        document_id="knowledge-runs-document-1",
        version_id="knowledge-runs-version-1",
        created_by=admin.id,
        name="演示规则",
        category="演示",
        sha256="a" * 64,
        original_filename="rules.md",
        mime_type="text/markdown",
        storage_path="data/uploads/knowledge/private/one.md",
        idempotency_key="document-key",
    )
    assert created and initial.status is KnowledgeVersionStatus.ACCEPTED
    assert initial.lease_owner is None and initial.lease_expires_at is None

    repeated_document, repeated_initial, created = await create_document_version(
        session,
        document_id="different-document-id",
        version_id="different-version-id",
        created_by=admin.id,
        name="ignored",
        category="ignored",
        sha256="a" * 64,
        original_filename="rules.md",
        mime_type="text/markdown",
        storage_path="data/uploads/knowledge/private/ignored.md",
        idempotency_key="document-key",
    )
    assert not created
    assert (repeated_document.id, repeated_initial.id) == (document.id, initial.id)
    assert await find_document_by_idempotency_key(
        session, created_by=admin.id, idempotency_key="document-key"
    ) == document
    with pytest.raises(ValueError, match="KNOWLEDGE_IDEMPOTENCY_CONFLICT"):
        await create_document_version(
            session,
            document_id="conflicting-document-id",
            version_id="conflicting-version-id",
            created_by=admin.id,
            name="冲突",
            category="演示",
            sha256="b" * 64,
            original_filename="rules.md",
            mime_type="text/markdown",
            storage_path="data/uploads/knowledge/private/conflict.md",
            idempotency_key="document-key",
        )

    _, second, created = await create_document_version(
        session,
        document_id=document.id,
        version_id="knowledge-runs-version-2",
        created_by=admin.id,
        name=None,
        category=None,
        sha256="b" * 64,
        original_filename="rules-v2.md",
        mime_type="text/markdown",
        storage_path="data/uploads/knowledge/private/two.md",
        idempotency_key="version-key",
    )
    assert created and second.version_number == 2
    assert await find_document_version_by_sha(session, document_id=document.id, sha256="b" * 64) == second
    assert await find_document_version_by_idempotency_key(
        session, document_id=document.id, idempotency_key="version-key"
    ) == second
    _, repeated_second, created = await create_document_version(
        session,
        document_id=document.id,
        version_id="ignored-version-id",
        created_by=admin.id,
        name=None,
        category=None,
        sha256="b" * 64,
        original_filename="rules-v2.md",
        mime_type="text/markdown",
        storage_path="data/uploads/knowledge/private/ignored-v2.md",
        idempotency_key="version-key",
    )
    assert not created and repeated_second == second
    _, same_sha_without_key, created = await create_document_version(
        session,
        document_id=document.id,
        version_id="ignored-sha-id",
        created_by=admin.id,
        name=None,
        category=None,
        sha256="b" * 64,
        original_filename="rules-v2.md",
        mime_type="text/markdown",
        storage_path="data/uploads/knowledge/private/ignored-sha.md",
    )
    assert not created and same_sha_without_key == second
    with pytest.raises(ValueError, match="KNOWLEDGE_IDEMPOTENCY_CONFLICT"):
        await create_document_version(
            session,
            document_id=document.id,
            version_id="conflicting-version-id",
            created_by=admin.id,
            name=None,
            category=None,
            sha256="c" * 64,
            original_filename="rules-v3.md",
            mime_type="text/markdown",
            storage_path="data/uploads/knowledge/private/conflicting-v3.md",
            idempotency_key="version-key",
        )
    assert await session.scalar(select(func.count(KnowledgeDocumentVersion.id))) == 2


async def test_claim_orders_reclaims_expired_and_exhausts_without_a_fourth_attempt(session) -> None:
    admin = await _add_admin(session)
    document = _document("knowledge-runs-document-1", admin.id)
    session.add(document)
    await session.flush()
    accepted = _version("knowledge-runs-version-accepted", document.id, 1)
    expired = _version(
        "knowledge-runs-version-expired",
        document.id,
        2,
        status=KnowledgeVersionStatus.PROCESSING,
        attempt_count=2,
        lease_owner="old-owner",
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    exhausted = _version(
        "knowledge-runs-version-exhausted",
        document.id,
        3,
        status=KnowledgeVersionStatus.PROCESSING,
        attempt_count=3,
        lease_owner="old-owner",
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    failed = _version(
        "knowledge-runs-version-failed",
        document.id,
        4,
        status=KnowledgeVersionStatus.FAILED,
    )
    disabled = _version(
        "knowledge-runs-version-disabled",
        document.id,
        5,
        status=KnowledgeVersionStatus.DISABLED,
    )
    for second, version in enumerate((accepted, expired, exhausted, failed, disabled)):
        version.created_at = datetime(2026, 8, 27, 10, 0, second, tzinfo=UTC)
    session.add_all((accepted, expired, exhausted, failed, disabled))
    await session.commit()

    first = await claim_next_knowledge_version(session, lease_owner="worker-a", lease_seconds=60)
    second = await claim_next_knowledge_version(session, lease_owner="worker-b", lease_seconds=60)
    third = await claim_next_knowledge_version(session, lease_owner="worker-c", lease_seconds=60)

    assert first is not None and first.id == accepted.id and first.attempt_count == 1
    assert second is not None and second.id == expired.id and second.attempt_count == 3
    assert third is None
    exhausted = await session.get(
        KnowledgeDocumentVersion, exhausted.id, populate_existing=True
    )
    assert exhausted is not None
    assert (exhausted.status, exhausted.error_code, exhausted.lease_owner) == (
        KnowledgeVersionStatus.FAILED,
        "KNOWLEDGE_ATTEMPTS_EXHAUSTED",
        None,
    )
    assert await session.get(KnowledgeDocumentVersion, failed.id) == failed
    assert await session.get(KnowledgeDocumentVersion, disabled.id) == disabled


async def test_retry_and_nonretryable_fail_follow_exact_terminal_rules(session) -> None:
    admin = await _add_admin(session)
    document = _document("knowledge-runs-document-1", admin.id)
    retryable = _version(
        "knowledge-runs-version-retryable",
        document.id,
        1,
        status=KnowledgeVersionStatus.PROCESSING,
        attempt_count=1,
        lease_owner="worker-a",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    retryable_id = retryable.id
    exhausted = _version(
        "knowledge-runs-version-exhausted",
        document.id,
        2,
        status=KnowledgeVersionStatus.PROCESSING,
        attempt_count=3,
        lease_owner="worker-b",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    exhausted_id = exhausted.id
    terminal = _version(
        "knowledge-runs-version-terminal",
        document.id,
        3,
        status=KnowledgeVersionStatus.PROCESSING,
        attempt_count=1,
        lease_owner="worker-c",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    terminal_id = terminal.id
    session.add_all((document, retryable, exhausted, terminal))
    await session.commit()

    assert await return_version_for_retry(
        session,
        version_id=retryable_id,
        lease_owner="worker-a",
        error_code="KNOWLEDGE_MILVUS_UNAVAILABLE",
    )
    assert await return_version_for_retry(
        session,
        version_id=exhausted_id,
        lease_owner="worker-b",
        error_code="KNOWLEDGE_MILVUS_UNAVAILABLE",
    )
    assert await fail_knowledge_version(
        session,
        version_id=terminal_id,
        lease_owner="worker-c",
        error_code="KNOWLEDGE_PARSE_FAILED",
    )
    retryable = await session.get(KnowledgeDocumentVersion, retryable_id, populate_existing=True)
    exhausted = await session.get(KnowledgeDocumentVersion, exhausted_id, populate_existing=True)
    terminal = await session.get(KnowledgeDocumentVersion, terminal_id, populate_existing=True)
    assert retryable is not None and exhausted is not None and terminal is not None
    assert (retryable.status, retryable.error_code, retryable.lease_owner) == (
        KnowledgeVersionStatus.ACCEPTED,
        "KNOWLEDGE_MILVUS_UNAVAILABLE",
        None,
    )
    assert (exhausted.status, exhausted.error_code, exhausted.lease_owner) == (
        KnowledgeVersionStatus.FAILED,
        "KNOWLEDGE_ATTEMPTS_EXHAUSTED",
        None,
    )
    assert (terminal.status, terminal.error_code, terminal.lease_owner) == (
        KnowledgeVersionStatus.FAILED,
        "KNOWLEDGE_PARSE_FAILED",
        None,
    )


async def test_owner_guard_blocks_stale_writes_and_chunk_replay_is_index_idempotent(session) -> None:
    admin = await _add_admin(session)
    document = _document("knowledge-runs-document-1", admin.id)
    version = _version("knowledge-runs-version-1", document.id, 1)
    version_id = version.id
    session.add_all((document, version))
    await session.commit()
    claimed = await claim_next_knowledge_version(session, lease_owner="worker-a", lease_seconds=60)
    assert claimed is not None
    drafts = [_draft(0), _draft(1)]
    assert await upsert_knowledge_chunks(
        session, version_id=version_id, lease_owner="worker-a", chunks=drafts
    )
    assert await upsert_knowledge_chunks(
        session, version_id=version_id, lease_owner="worker-a", chunks=drafts
    )
    persisted = list(
        await session.scalars(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.version_id == version_id)
            .order_by(KnowledgeChunk.chunk_index)
        )
    )
    assert [(chunk.chunk_index, chunk.chunk_hash, chunk.chunk_metadata) for chunk in persisted] == [
        (0, "a" * 64, {"heading_path": ["章节"], "paragraph_index": 0}),
        (1, "a" * 64, {"heading_path": ["章节"], "paragraph_index": 1}),
    ]

    await session.execute(
        update(KnowledgeDocumentVersion)
        .where(KnowledgeDocumentVersion.id == version_id)
        .values(
            lease_owner="replacement-worker",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        )
    )
    await session.commit()
    assert not await renew_knowledge_lease(
        session, version_id=version_id, lease_owner="worker-a", lease_seconds=60
    )
    assert not await return_version_for_retry(
        session,
        version_id=version_id,
        lease_owner="worker-a",
        error_code="KNOWLEDGE_MILVUS_UNAVAILABLE",
    )
    assert not await fail_knowledge_version(
        session,
        version_id=version_id,
        lease_owner="worker-a",
        error_code="KNOWLEDGE_PARSE_FAILED",
    )
    assert not await upsert_knowledge_chunks(
        session, version_id=version_id, lease_owner="worker-a", chunks=[_draft(2)]
    )
    assert not await activate_knowledge_version(
        session, version_id=version_id, lease_owner="worker-a"
    )
    assert await session.scalar(select(func.count(KnowledgeChunk.id))) == 2
    refreshed = await session.get(KnowledgeDocumentVersion, version_id)
    assert refreshed is not None
    assert (refreshed.status, refreshed.lease_owner) == (
        KnowledgeVersionStatus.PROCESSING,
        "replacement-worker",
    )


async def test_activation_locks_document_before_the_owner_guarded_candidate() -> None:
    document = _document(
        "knowledge-runs-document-1", "knowledge-runs-admin", enabled=True
    )
    candidate = _version(
        "knowledge-runs-version-1",
        document.id,
        1,
        status=KnowledgeVersionStatus.PROCESSING,
        attempt_count=1,
        lease_owner="worker-a",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    session = _ActivationStatementSpy(
        document=document,
        candidate=candidate,
        pre_read_document_id=document.id,
    )

    assert await activate_knowledge_version(
        session, version_id=candidate.id, lease_owner="worker-a"
    )

    assert session.scalar_statements[0]._for_update_arg is None
    pre_read, document_lock, candidate_lock = session.scalar_statements[:3]
    assert pre_read.column_descriptions[0]["name"] == "document_id"
    assert pre_read.column_descriptions[0]["entity"] is KnowledgeDocumentVersion
    assert pre_read._for_update_arg is None
    assert document_lock.column_descriptions[0]["entity"] is KnowledgeDocument
    assert document_lock._for_update_arg is not None
    assert candidate_lock.column_descriptions[0]["entity"] is KnowledgeDocumentVersion
    assert candidate_lock._for_update_arg is not None
    actor_lock = session.scalar_statements[-1]
    assert actor_lock.column_descriptions[0]["entity"] is User
    assert actor_lock._for_update_arg is not None
    assert candidate_lock.whereclause.compare(
        and_(
            KnowledgeDocumentVersion.id == candidate.id,
            KnowledgeDocumentVersion.status == KnowledgeVersionStatus.PROCESSING,
            KnowledgeDocumentVersion.lease_owner == "worker-a",
            KnowledgeDocumentVersion.lease_expires_at > func.now(),
        )
    )


@pytest.mark.parametrize("case", ("missing", "disabled", "lost_owner"))
async def test_activation_rolls_back_without_writes_when_preconditions_change(case: str) -> None:
    document = _document(
        "knowledge-runs-document-1",
        "knowledge-runs-admin",
        enabled=case != "disabled",
    )
    candidate = _version(
        "knowledge-runs-version-1",
        document.id,
        1,
        status=KnowledgeVersionStatus.PROCESSING,
        attempt_count=1,
        lease_owner="worker-a",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    session = _ActivationStatementSpy(
        document=document,
        candidate=None if case == "lost_owner" else candidate,
        pre_read_document_id=None if case == "missing" else document.id,
    )

    assert not await activate_knowledge_version(
        session, version_id=candidate.id, lease_owner="worker-a"
    )
    assert session.executed_statements == []
    assert session.commit_count == 0
    assert session.rollback_count == 1


async def test_activation_is_monotonic_and_disable_clears_all_visible_version_states(session) -> None:
    admin = await _add_admin(session)
    document = _document("knowledge-runs-document-1", admin.id)
    first = _version(
        "knowledge-runs-version-1",
        document.id,
        1,
        status=KnowledgeVersionStatus.PROCESSING,
        attempt_count=1,
        lease_owner="worker-one",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    second = _version(
        "knowledge-runs-version-2",
        document.id,
        2,
        status=KnowledgeVersionStatus.PROCESSING,
        attempt_count=1,
        lease_owner="worker-two",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    session.add_all((document, first, second))
    await session.commit()

    assert await activate_knowledge_version(
        session, version_id=second.id, lease_owner="worker-two"
    )
    assert not await activate_knowledge_version(
        session, version_id=first.id, lease_owner="worker-one"
    )
    current = await session.get(KnowledgeDocument, document.id)
    superseded = await session.get(KnowledgeDocumentVersion, first.id)
    active = await session.get(KnowledgeDocumentVersion, second.id)
    assert current is not None and current.current_version_id == second.id
    assert superseded is not None and (superseded.status, superseded.error_code) == (
        KnowledgeVersionStatus.DISABLED,
        "KNOWLEDGE_VERSION_SUPERSEDED",
    )
    assert active is not None and active.status is KnowledgeVersionStatus.ACTIVE

    accepted = _version("knowledge-runs-version-3", document.id, 3)
    processing = _version(
        "knowledge-runs-version-4",
        document.id,
        4,
        status=KnowledgeVersionStatus.PROCESSING,
        attempt_count=1,
        lease_owner="worker-three",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    session.add_all((accepted, processing))
    await session.commit()
    assert await disable_knowledge_document(session, document_id=document.id)
    document = await session.get(KnowledgeDocument, document.id)
    versions = list(
        await session.scalars(
            select(KnowledgeDocumentVersion)
            .where(KnowledgeDocumentVersion.document_id == document.id)
            .order_by(KnowledgeDocumentVersion.version_number)
        )
    )
    assert document is not None and not document.enabled and document.current_version_id is None
    assert all(version.status is KnowledgeVersionStatus.DISABLED for version in versions)
    assert all(version.lease_owner is None and version.lease_expires_at is None for version in versions)


async def test_safe_logs_include_transitions_without_content_or_storage_values(caplog, session) -> None:
    admin = await _add_admin(session)
    document = _document("knowledge-runs-document-1", admin.id)
    version = _version("knowledge-runs-version-1", document.id, 1)
    exhausted = _version(
        "knowledge-runs-version-exhausted",
        document.id,
        2,
        status=KnowledgeVersionStatus.PROCESSING,
        attempt_count=3,
        lease_owner="old-worker",
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    session.add_all((document, version, exhausted))
    await session.commit()
    caplog.set_level(logging.INFO, logger="backend.knowledge")

    claimed = await claim_next_knowledge_version(session, lease_owner="worker-a", lease_seconds=60)
    assert claimed is not None
    assert await return_version_for_retry(
        session,
        version_id=version.id,
        lease_owner="worker-a",
        error_code="KNOWLEDGE_MILVUS_UNAVAILABLE",
    )

    messages = caplog.text
    assert "request_id=-" in messages
    assert f"actor_id={admin.id}" in messages
    assert f"document_id={document.id}" in messages
    assert f"version_id={version.id}" in messages
    assert f"version_id={exhausted.id}" in messages
    assert "status=processing" in messages
    assert "status=accepted" in messages
    assert "error_code=KNOWLEDGE_MILVUS_UNAVAILABLE" in messages
    assert "error_code=KNOWLEDGE_ATTEMPTS_EXHAUSTED" in messages
    assert "private/rules.md" not in messages
    assert "项目演示规则正文" not in messages
    assert "credential" not in messages
