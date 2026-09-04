import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from backend.auth import create_access_token, hash_password
from backend.common import KnowledgeVersionStatus, UserRole, UserStatus
from backend.config import Settings, get_settings
from backend.database import get_session
from backend.knowledge_index import HybridVector, KnowledgeDependencyError
from backend.knowledge_search import get_knowledge_search_loader
from backend.main import create_app
from backend.models import KnowledgeChunk, KnowledgeDocument, KnowledgeDocumentVersion, User, Store, UserStoreScope


def _headers(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


def _markdown_upload(text: bytes = b"# Demo\n\nSafe rule text.") -> dict[str, tuple[str, bytes, str]]:
    return {"file": ("rules.md", text, "text/markdown")}


class _Models:
    async def embed_query(self, query: str) -> HybridVector:
        return HybridVector(dense=[0.25] * 1024, sparse={7: 0.5})

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        return [0.8] * len(texts)


class _Index:
    def __init__(self) -> None:
        self.dense: list[tuple[str, float]] = []
        self.sparse: list[tuple[str, float]] = []

    async def dense_search(
        self, *, vector: list[float], version_ids: list[str], limit: int
    ) -> list[tuple[str, float]]:
        return self.dense

    async def sparse_search(
        self, *, vector: dict[int, float], version_ids: list[str], limit: int
    ) -> list[tuple[str, float]]:
        return self.sparse


@dataclass
class _Loader:
    index: _Index = field(default_factory=_Index)
    calls: int = 0
    error: Exception | None = None

    def __call__(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return _Models(), self.index, {"candidate_limit": 5, "rrf_k": 60, "threshold": 0.0}


@dataclass
class _ApiContext:
    client: AsyncClient
    session: object
    settings: Settings
    users: dict[str, User]
    tokens: dict[str, str]
    loader: _Loader


@pytest_asyncio.fixture
async def knowledge_api(session, tmp_path: Path):
    settings = get_settings().model_copy(update={"knowledge_upload_dir": tmp_path / "uploads"})
    users = {
        role.value: User(
            id=f"knowledge-api-{role.value}",
            username=f"knowledge-api-{role.value}",
            password_hash=hash_password("DemoPass!2026"),
            role=role,
        )
        for role in (UserRole.ADMIN, UserRole.OPERATOR, UserRole.SUPERVISOR)
    }
    session.add_all(users.values())
    session.add(Store(id='knowledge-store',name='Knowledge store',code='knowledge-store'))
    await session.flush()
    session.add_all([UserStoreScope(user_id=user.id,store_id='knowledge-store') for user in users.values()])
    await session.commit()
    loader = _Loader()
    app = create_app()

    async def override_get_session():
        yield session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_knowledge_search_loader] = lambda: loader
    tokens = {name: create_access_token(user, settings) for name, user in users.items()}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield _ApiContext(client, session, settings, users, tokens, loader)
    app.dependency_overrides.clear()


async def _seed_active_document(context: _ApiContext) -> tuple[KnowledgeDocument, KnowledgeDocumentVersion, KnowledgeChunk]:
    document = KnowledgeDocument(
        id="knowledge-api-active-document",
        name="Project demonstration rules",
        category="demo",
        created_by=context.users["admin"].id,
    )
    context.session.add(document)
    await context.session.flush()
    version = KnowledgeDocumentVersion(
        id="knowledge-api-active-version",
        document_id=document.id,
        version_number=1,
        sha256="a" * 64,
        original_filename="rules.md",
        mime_type="text/markdown",
        storage_path="controlled/rules.md",
        status=KnowledgeVersionStatus.ACTIVE,
    )
    context.session.add(version)
    await context.session.flush()
    document.current_version_id = version.id
    chunk = KnowledgeChunk(
        id="knowledge-api-active-chunk",
        version_id=version.id,
        chunk_index=0,
        chunk_hash="b" * 64,
        canonical_text="Canonical citation text.",
        chunk_metadata={"heading_path": ["Demo"], "paragraph_index": 0},
        token_count=3,
    )
    context.session.add(chunk)
    await context.session.commit()
    context.loader.index.dense = [(chunk.id, 0.7)]
    return document, version, chunk


def _assert_error(response, *, category: str, code: str) -> None:
    body = response.json()
    assert body["status"] == "error"
    assert body["data"] is None and body["quality"] is None
    assert body["request_id"]
    assert body["error"]["category"] == category
    assert body["error"]["code"] == code


async def test_administrator_uploads_idempotent_document_and_version(knowledge_api) -> None:
    context = knowledge_api
    headers = _headers(context.tokens["admin"], **{"Idempotency-Key": "document-key"})
    response = await context.client.post(
        "/knowledge/documents",
        data={"name": "Demo rules", "category": "demo"},
        files=_markdown_upload(),
        headers=headers,
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted" and body["error"] is None
    document_id, version_id = body["data"]["document_id"], body["data"]["version_id"]
    assert body["data"]["status"] == "accepted"
    assert "storage_path" not in json.dumps(body)
    initial_uploads = [path for path in context.settings.knowledge_upload_dir.rglob("*") if path.is_file()]
    assert len(initial_uploads) == 1

    repeated = await context.client.post(
        "/knowledge/documents",
        data={"name": "Demo rules", "category": "demo"},
        files=_markdown_upload(),
        headers=headers,
    )
    assert repeated.status_code == 200
    assert repeated.json()["data"] == {"document_id": document_id, "version_id": version_id, "status": "accepted"}
    assert await context.session.scalar(select(func.count(KnowledgeDocumentVersion.id))) == 1
    assert [path for path in context.settings.knowledge_upload_dir.rglob("*") if path.is_file()] == initial_uploads
    initial_version = await context.session.get(KnowledgeDocumentVersion, version_id)
    assert initial_version is not None
    assert initial_version.lease_owner is None and initial_version.lease_expires_at is None

    conflict = await context.client.post(
        "/knowledge/documents",
        data={"name": "Demo rules", "category": "demo"},
        files=_markdown_upload(b"# Different\n\nDifferent text."),
        headers=headers,
    )
    assert conflict.status_code == 409
    _assert_error(conflict, category="validation_error", code="KNOWLEDGE_IDEMPOTENCY_CONFLICT")

    version_headers = _headers(context.tokens["admin"], **{"Idempotency-Key": "version-key"})
    created_version = await context.client.post(
        f"/knowledge/documents/{document_id}/versions",
        files=_markdown_upload(b"# Version two\n\nNew safe rule."),
        headers=version_headers,
    )
    assert created_version.status_code == 202
    version_two_id = created_version.json()["data"]["version_id"]
    assert created_version.json()["data"]["document_id"] == document_id

    repeated_version = await context.client.post(
        f"/knowledge/documents/{document_id}/versions",
        files=_markdown_upload(b"# Version two\n\nNew safe rule."),
        headers=version_headers,
    )
    assert repeated_version.status_code == 200
    assert repeated_version.json()["data"]["version_id"] == version_two_id
    same_sha = await context.client.post(
        f"/knowledge/documents/{document_id}/versions",
        files=_markdown_upload(b"# Version two\n\nNew safe rule."),
        headers=_headers(context.tokens["admin"]),
    )
    assert same_sha.status_code == 200
    assert same_sha.json()["data"]["version_id"] == version_two_id
    version_conflict = await context.client.post(
        f"/knowledge/documents/{document_id}/versions",
        files=_markdown_upload(b"# Version key conflict\n\nOther."),
        headers=version_headers,
    )
    assert version_conflict.status_code == 409
    _assert_error(version_conflict, category="validation_error", code="KNOWLEDGE_IDEMPOTENCY_CONFLICT")
    assert await context.session.scalar(select(func.count(KnowledgeDocumentVersion.id))) == 2


@pytest.mark.parametrize("role", ("operator", "supervisor"))
async def test_non_administrators_cannot_mutate_or_list_knowledge(knowledge_api, role: str) -> None:
    context = knowledge_api
    headers = _headers(context.tokens[role])
    responses = [
        await context.client.post(
            "/knowledge/documents",
            data={"name": "Denied", "category": "demo"},
            files=_markdown_upload(),
            headers=headers,
        ),
        await context.client.get("/knowledge/documents", headers=headers),
        await context.client.post(
            "/knowledge/documents/missing/versions", files=_markdown_upload(), headers=headers
        ),
        await context.client.post("/knowledge/documents/missing/disable", headers=headers),
    ]
    for response in responses:
        assert response.status_code == 403
        _assert_error(response, category="authorization_error", code="KNOWLEDGE_AUTHORIZATION_REQUIRED")


async def test_list_disable_and_current_database_user_state_are_safe(knowledge_api) -> None:
    context = knowledge_api
    created = await context.client.post(
        "/knowledge/documents",
        data={"name": "Listable rules", "category": "demo"},
        files=_markdown_upload(),
        headers=_headers(context.tokens["admin"]),
    )
    document_id = created.json()["data"]["document_id"]
    listed = await context.client.get(
        "/knowledge/documents?page=1&page_size=1&category=demo&enabled=true",
        headers=_headers(context.tokens["admin"]),
    )
    assert listed.status_code == 200
    assert listed.json()["data"]["total"] == 1
    listed_text = json.dumps(listed.json())
    assert "storage_path" not in listed_text and "lease_owner" not in listed_text and "data" in listed_text

    unknown = await context.client.post(
        "/knowledge/documents/missing/versions", files=_markdown_upload(), headers=_headers(context.tokens["admin"])
    )
    assert unknown.status_code == 404
    _assert_error(unknown, category="not_found", code="KNOWLEDGE_DOCUMENT_NOT_FOUND")
    disabled = await context.client.post(
        f"/knowledge/documents/{document_id}/disable", headers=_headers(context.tokens["admin"])
    )
    repeated = await context.client.post(
        f"/knowledge/documents/{document_id}/disable", headers=_headers(context.tokens["admin"])
    )
    assert disabled.status_code == repeated.status_code == 200
    assert repeated.json()["data"] == {"document_id": document_id, "enabled": False}
    rejected = await context.client.post(
        f"/knowledge/documents/{document_id}/versions", files=_markdown_upload(), headers=_headers(context.tokens["admin"])
    )
    assert rejected.status_code == 409
    _assert_error(rejected, category="validation_error", code="KNOWLEDGE_DOCUMENT_DISABLED")

    context.users["operator"].status = UserStatus.DISABLED
    await context.session.commit()
    disabled_user = await context.client.post(
        "/knowledge/search", json={"store_id": "knowledge-store", "query": "rule"}, headers=_headers(context.tokens["operator"])
    )
    assert disabled_user.status_code == 401
    _assert_error(disabled_user, category="authorization_error", code="KNOWLEDGE_AUTHENTICATION_REQUIRED")
    assert disabled_user.headers["www-authenticate"] == "Bearer"


async def test_all_active_read_roles_can_search_and_zero_active_never_loads(knowledge_api) -> None:
    context = knowledge_api
    for role in ("admin", "operator", "supervisor"):
        response = await context.client.post(
            "/knowledge/search", json={"store_id": "knowledge-store", "query": "rule"}, headers=_headers(context.tokens[role])
        )
        assert response.status_code == 200
        assert response.json()["status"] == "success"
        assert response.json()["quality"] == {"status": "zero_hit"}
        assert response.json()["data"] == {"citations": []}
    assert context.loader.calls == 0


async def test_search_returns_canonical_citations_and_maps_dependency_errors(knowledge_api, caplog) -> None:
    context = knowledge_api
    _, version, chunk = await _seed_active_document(context)
    active = await context.client.post(
        "/knowledge/search",
        json={"store_id": "knowledge-store", "query": "rule", "categories": ["demo"], "top_k": 3},
        headers=_headers(context.tokens["operator"]),
    )
    assert active.status_code == 200
    citation = active.json()["data"]["citations"][0]
    assert context.loader.calls == 1
    assert (citation["chunk_id"], citation["version_number"], citation["canonical_text"]) == (
        chunk.id,
        version.version_number,
        chunk.canonical_text,
    )
    assert citation["sparse_score"] is None and citation["reranker_score"] == 0.8

    context.loader.error = KnowledgeDependencyError("KNOWLEDGE_DEPENDENCY_TIMEOUT", retryable=True)
    with caplog.at_level(logging.INFO, logger="backend.knowledge"):
        timeout = await context.client.post(
            "/knowledge/search", json={"store_id": "knowledge-store", "query": "rule"}, headers=_headers(context.tokens["admin"])
        )
    assert timeout.status_code == 503
    _assert_error(timeout, category="timeout", code="KNOWLEDGE_DEPENDENCY_TIMEOUT")
    assert f"request_id={timeout.json()['request_id']}" in "\n".join(
        record.getMessage() for record in caplog.records
    )
    caplog.clear()
    context.loader.error = KnowledgeDependencyError("KNOWLEDGE_MILVUS_UNAVAILABLE", retryable=True)
    with caplog.at_level(logging.INFO, logger="backend.knowledge"):
        unavailable = await context.client.post(
            "/knowledge/search", json={"store_id": "knowledge-store", "query": "rule"}, headers=_headers(context.tokens["admin"])
        )
    assert unavailable.status_code == 503
    _assert_error(unavailable, category="dependency_error", code="KNOWLEDGE_MILVUS_UNAVAILABLE")
    assert f"request_id={unavailable.json()['request_id']}" in "\n".join(
        record.getMessage() for record in caplog.records
    )

    context.loader.error = None
    calls_before_disable = context.loader.calls
    disabled = await context.client.post(
        "/knowledge/documents/knowledge-api-active-document/disable",
        headers=_headers(context.tokens["admin"]),
    )
    zero_hit = await context.client.post(
        "/knowledge/search", json={"store_id": "knowledge-store", "query": "rule"}, headers=_headers(context.tokens["operator"])
    )
    assert disabled.status_code == 200
    assert zero_hit.status_code == 200
    assert zero_hit.json()["quality"] == {"status": "zero_hit"}
    assert zero_hit.json()["data"] == {"citations": []}
    assert context.loader.calls == calls_before_disable


async def test_knowledge_validation_envelope_is_safe_and_other_routes_keep_fastapi_shape(knowledge_api) -> None:
    context = knowledge_api
    invalid_query = "invalid-query-secret-" + ("x" * 500)
    response = await context.client.post(
        "/knowledge/search",
        json={"store_id": "knowledge-store", "query": invalid_query, "top_k": 21},
        headers=_headers(context.tokens["admin"]),
    )
    assert response.status_code == 422
    _assert_error(response, category="validation_error", code="KNOWLEDGE_REQUEST_INVALID")
    encoded = json.dumps(response.json())
    assert invalid_query not in encoded and "/knowledge/search" not in encoded and "loc" not in encoded
    non_knowledge = await context.client.post(
        "/analysis-runs", json={"store_id": "missing"}, headers=_headers(context.tokens["admin"])
    )
    assert non_knowledge.status_code == 422
    assert "detail" in non_knowledge.json() and "request_id" not in non_knowledge.json()


async def test_upload_storage_failure_uses_the_safe_envelope(knowledge_api) -> None:
    context = knowledge_api
    context.settings.knowledge_upload_dir.write_bytes(b"not-a-directory")

    response = await context.client.post(
        "/knowledge/documents",
        data={"name": "Rejected", "category": "demo"},
        files=_markdown_upload(),
        headers=_headers(context.tokens["admin"]),
    )

    assert response.status_code == 400
    _assert_error(response, category="validation_error", code="KNOWLEDGE_PATH_INVALID")


async def test_failed_database_commit_removes_only_new_upload_and_keeps_auditable_request_id(
    knowledge_api, monkeypatch, caplog
) -> None:
    context = knowledge_api
    upload_root = context.settings.knowledge_upload_dir
    upload_root.mkdir()
    sentinel = upload_root / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    async def fail_commit() -> None:
        raise RuntimeError("database commit failed")

    monkeypatch.setattr(context.session, "commit", fail_commit)
    with caplog.at_level(logging.INFO, logger="backend.knowledge"):
        response = await context.client.post(
            "/knowledge/documents",
            data={"name": "Commit failure", "category": "demo"},
            files=_markdown_upload(b"# Commit\n\nNew upload."),
            headers=_headers(context.tokens["admin"]),
        )

    assert response.status_code == 503
    _assert_error(response, category="dependency_error", code="KNOWLEDGE_WRITE_FAILED")
    assert [path for path in upload_root.rglob("*") if path.is_file()] == [sentinel]
    assert await context.session.scalar(select(func.count(KnowledgeDocumentVersion.id))) == 0
    assert f"request_id={response.json()['request_id']}" in "\n".join(
        record.getMessage() for record in caplog.records
    )


async def test_knowledge_api_audit_logs_only_safe_fields(knowledge_api, caplog) -> None:
    context = knowledge_api
    secret_upload_text = b"# Audit\n\nprivate-upload-body-not-for-logs"
    with caplog.at_level(logging.INFO, logger="backend.knowledge"):
        created = await context.client.post(
            "/knowledge/documents",
            data={"name": "Audited", "category": "demo"},
            files=_markdown_upload(secret_upload_text),
            headers=_headers(context.tokens["admin"]),
        )
        listed = await context.client.get("/knowledge/documents", headers=_headers(context.tokens["admin"]))
        failed = await context.client.post(
            "/knowledge/documents",
            data={"name": "Rejected", "category": "demo"},
            files={"file": ("bad.exe", b"bad", "application/octet-stream")},
            headers=_headers(context.tokens["admin"]),
        )
    assert created.status_code == 202
    assert listed.status_code == 200
    assert failed.status_code == 400
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "request_id=" in messages and "actor_id=knowledge-api-admin" in messages
    assert "action=" in messages and "status=" in messages and "elapsed_ms=" in messages
    assert secret_upload_text.decode() not in messages
    assert str(context.settings.knowledge_upload_dir) not in messages
    assert "Authorization" not in messages and "vector" not in messages
