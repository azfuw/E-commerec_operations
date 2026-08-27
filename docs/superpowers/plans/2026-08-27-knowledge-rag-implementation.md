# Knowledge Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` in this development window to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not dispatch subagents.

**Goal:** Deliver a local, versioned knowledge base with recoverable indexing, Milvus dense+sparse retrieval, local reranking, trustworthy citations, and a fixed Chinese demonstration evaluation set.

**Architecture:** Keep the existing FastAPI modular monolith and PostgreSQL as the source of document, version, chunk, lease, and active-state facts. A repository-local command claims one knowledge-version lease at a time, parses and chunks a local upload, produces BGE-M3 dense+sparse vectors, upserts stable chunk IDs to Milvus, then activates the version in a PostgreSQL transaction. Search reloads active version IDs from PostgreSQL before Milvus retrieval, reads canonical chunk text from PostgreSQL, then reranks locally; the existing analysis workflow and Worker remain unchanged.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, SQLAlchemy async, Alembic, PostgreSQL 16, SQLite test fixture, Docker Compose, Milvus Standalone with etcd and MinIO, `pymilvus`, `FlagEmbedding`, `pypdf`, `python-docx`, `python-multipart`, pytest, pytest-asyncio, and `httpx` ASGI transport.

**Spec:** [2026-08-27-knowledge-rag-design.md](../specs/2026-08-27-knowledge-rag-design.md)

## Global Constraints

- Execute later only in `D:\E-commerce_operations`, through `D:\E-commerce_operations\.venv\Scripts\python.exe`, with `superpowers:executing-plans`; this planning turn does not execute any task.
- The first implementation task adds `/model/` to `.gitignore` and verifies the existing `data/uploads/` rule already excludes `data/uploads/knowledge/`; no model weight is inspected, moved, staged, or committed.
- Add only three knowledge business tables: `knowledge_documents`, `knowledge_document_versions`, and `knowledge_chunks`. Do not generalize `workflow_runs` or alter the analysis Worker.
- Use named, non-type-bound SQL checks and named unique constraints in both ORM metadata and Alembic. PostgreSQL is authoritative; Milvus stores only reconstructable vectors and metadata.
- Normal tests use SQLite plus fakes or mocks for Milvus, BGE-M3, and reranker. They do not load weights, make network calls, or call DeepSeek. Real PostgreSQL, Milvus, and local-model tests run only with `RUN_KNOWLEDGE_INTEGRATION=1`.
- All version-worker ownership writes match `id`, `status='processing'`, `lease_owner`, and `lease_expires_at > now()`. Claims use PostgreSQL `now()` and `FOR UPDATE SKIP LOCKED`; no fourth claim exists.
- Every knowledge route receives its current database `User` directly through the existing role dependencies: `Depends(require_roles(UserRole.ADMIN))` for create, list, version, and disable; `Depends(require_roles(UserRole.OPERATOR, UserRole.SUPERVISOR, UserRole.ADMIN))` for search. Knowledge is global demonstration data and has no store-scope parameter.
- Do not add Redis, Celery, Kafka, MCP, microservices, a Compose Worker service, frontend code, real platform access, cloud deployment, generated-model APIs, automatic model downloads, or a product-selection/optimization interface.
- Never log or return upload text, server paths, model weights, vectors, credentials, Authorization values, cookies, prompts, raw dependency responses, or chain-of-thought data.
- All parsing, local-model, and Milvus calls use `Settings.knowledge_dependency_timeout_seconds`; a deadline produces the stable timeout code rather than a partial result. Use only `logging` for safe audit events; do not add an audit table.

## Existing Structure to Reuse

- `backend/database.py` provides `Base`, `async_session_factory`, and `get_session`; `tests/conftest.py` creates the same metadata against SQLite with foreign keys enabled.
- `backend/models.py`, `alembic/versions/0001_initial_schema.py`, and current head `alembic/versions/0002_durable_analysis.py` use string UUIDs, `utc_now`, `native_enum=False`, explicit named checks, and explicit named unique constraints. New revision `0003_knowledge_retrieval.py` follows that convention.
- `backend/common.py` contains `StrEnum` domain states. Add knowledge-version state values there rather than introduce a second enum package.
- `backend/config.py` is the single `Settings` source and `get_settings()` is the existing cached settings accessor. The API must still start without loading local weights.
- `backend/auth.py` provides database-backed `get_current_user()` and `require_roles()`; the new routes use them directly. Existing `get_current_user()` already rejects disabled users and does not trust the JWT role as final authorization.
- `backend/routes.py` is the sole API router. Add all five knowledge endpoints there; do not create another application or router hierarchy. `backend/main.py` is the small app factory and is the correct place for a path-scoped knowledge error envelope handler.
- `backend/analysis_runs.py` demonstrates dialect-specific SQLite/PostgreSQL upsert and owner-guarded lease writes; `backend/analysis_worker.py` and `scripts/run_analysis_worker.py` demonstrate a one-row `run_once()` Worker and a repository-local loop. Reuse the patterns without importing or changing analysis code.
- `backend/seed.py` and `scripts/seed_demo.py` establish deterministic UUID5 data and a narrow reset policy. Knowledge demonstration files are static committed text, not generated product or platform data.
- `tests/test_analysis_api.py` demonstrates FastAPI ASGI tests with a real `get_session` override and real authentication. `tests/test_analysis_postgres.py` demonstrates a default-skipped integration marker, two independent PostgreSQL sessions, exact-row cleanup, and `async_session_factory`.

## Planned File Structure

| File | Responsibility |
|---|---|
| `.gitignore` | Keep local BGE weights and user-uploaded knowledge files out of Git. |
| `pyproject.toml` | Add only parser, upload, Milvus, and local BGE dependencies; register the opt-in knowledge integration marker. |
| `.env.example` | Document non-secret local knowledge paths, Milvus address, collection, lease, and dependency timeout settings. |
| `docker-compose.yml` | Add only Milvus Standalone and its required etcd/MinIO infrastructure beside existing PostgreSQL. |
| `backend/common.py` | Define `KnowledgeVersionStatus`. |
| `backend/config.py` | Define local knowledge settings without loading a model. |
| `backend/models.py` | Map the three knowledge tables and database constraints. |
| `alembic/versions/0003_knowledge_retrieval.py` | Create and reverse only the three knowledge tables and their foreign keys. |
| `backend/knowledge_content.py` | Validate uploads, write controlled local paths, parse supported formats, and create deterministic chunk drafts. |
| `backend/knowledge_runs.py` | Create/list document versions, own PostgreSQL leases, persist chunk metadata, activate versions, and disable documents. |
| `backend/knowledge_index.py` | Load only local BGE-M3/reranker paths and operate the Milvus collection with stable chunk IDs. |
| `backend/knowledge_worker.py` | Claim at most one version, index it recoverably, and apply the version state contract. |
| `backend/knowledge_search.py` | Resolve active versions, perform calibrated hybrid retrieval, fetch canonical chunks, rerank, and return safe result objects. |
| `backend/knowledge_evaluation.py` | Evaluate dense, sparse, hybrid, and reranked results against the fixed Chinese query set. |
| `backend/schemas.py` | Define knowledge request, response, citation, quality, and error-envelope schemas. |
| `backend/routes.py` and `backend/main.py` | Expose the five endpoints and return the knowledge-only uniform error envelope. |
| `scripts/run_knowledge_worker.py` | Run the local index Worker once or in a polling loop; it is not a Compose service. |
| `data/knowledge/demo/platform-neutral-rules.md` | Committed Chinese demonstration rules, explicitly marked platform-neutral and non-official. |
| `data/knowledge/evaluation/queries.json` | Fixed Chinese queries, expected documents, expected versions, and relevant chunk assertions. |
| `data/knowledge/evaluation/calibration-candidates.json` | Committed finite dense/sparse/RRF/rerank candidate grid used by ordinary fake-data selector tests. |
| `data/knowledge/evaluation/calibration.json` | Final selected fusion/threshold configuration, atomically produced only by Task 8's explicitly authorized real local evaluation. |
| `tests/test_knowledge_*.py` | Unit, API, Worker, evaluation, and opt-in integration coverage described in each task. |

---

### Task 1: Local runtime boundary, dependencies, and Milvus infrastructure

**Files:**

- Modify: `.gitignore`
- Modify: `pyproject.toml`
- Modify: `.env.example`
- Modify: `docker-compose.yml`
- Modify: `backend/config.py`
- Create: `tests/test_knowledge_config.py`

**Interfaces:**

```python
# backend/config.py
class Settings(BaseSettings):
    knowledge_upload_dir: Path = Path("data/uploads/knowledge")
    knowledge_embedding_model_path: Path = Path("model/bge-m3")
    knowledge_reranker_model_path: Path = Path("model/bge-reranker-v2-m3")
    milvus_uri: str = "http://localhost:19530"
    milvus_collection: str = "knowledge_chunks"
    knowledge_lease_seconds: int = 60
    knowledge_dependency_timeout_seconds: float = 30.0
```

`Settings` construction and API startup remain side-effect free: no local directory is opened and no Milvus connection or BGE object is created.

- [ ] **Step 1: Establish the Git boundary before any runtime change**

  Add `/model/` to `.gitignore`. Keep the existing `data/uploads/` rule because it already excludes `data/uploads/knowledge/`; do not add a duplicate narrower rule. Verify future paths without reading them:

  ```powershell
  git check-ignore -v model/bge-m3/config.json
  git check-ignore -v data/uploads/knowledge/example.pdf
  ```

  Expected: both hypothetical paths are ignored; no file inside either path is opened.

- [ ] **Step 2: Write failing settings and Compose contract tests**

  Create `tests/test_knowledge_config.py`:

  ```python
  from pathlib import Path

  from backend.config import Settings


  def test_knowledge_settings_have_local_nonsecret_defaults() -> None:
      settings = Settings(_env_file=None, jwt_secret_key="test-only-secret-at-least-32-characters")
      assert settings.knowledge_upload_dir == Path("data/uploads/knowledge")
      assert settings.knowledge_embedding_model_path == Path("model/bge-m3")
      assert settings.knowledge_reranker_model_path == Path("model/bge-reranker-v2-m3")
      assert (settings.milvus_uri, settings.milvus_collection) == (
          "http://localhost:19530", "knowledge_chunks"
      )
      assert settings.knowledge_lease_seconds == 60
      assert settings.knowledge_dependency_timeout_seconds == 30.0
  ```

  Add a text-level assertion that `docker-compose.yml` has `etcd`, `minio`, and `milvus`, but no service named `knowledge-worker`.

- [ ] **Step 3: Run RED**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_config.py -v
  ```

  Expected: FAIL because `Settings` has no knowledge fields and Compose lacks the three named services.

- [ ] **Step 4: Add the minimum runtime declarations**

  Add these production dependencies to `pyproject.toml` only:

  ```toml
  "pymilvus>=2.5,<3",
  "FlagEmbedding>=1.3,<2",
  "pypdf>=5,<6",
  "python-docx>=1.1,<2",
  "python-multipart>=0.0.20,<1",
  ```

  Register `knowledge_integration: requires configured local PostgreSQL, Milvus, and local BGE model paths` in the existing pytest marker list. Add the settings above and matching non-secret `.env.example` entries. Add Compose services `etcd` (`quay.io/coreos/etcd:v3.5.18`), `minio` (`minio/minio:RELEASE.2023-03-20T20-16-18Z` with `server /minio_data`), and `milvus` (`milvusdb/milvus:v2.5.6` with `command: ["milvus", "run", "standalone"]`). Use a named `milvus_data` volume, Milvus port `19530`, health checks, and `milvus` dependency on healthy etcd/MinIO. Do not add a Worker service, an application volume mount, or any Docker API behavior.

  Install only the declared packages into the project virtual environment before the next Python run:

  ```powershell
  .\.venv\Scripts\python.exe -m pip install "pymilvus>=2.5,<3" "FlagEmbedding>=1.3,<2" "pypdf>=5,<6" "python-docx>=1.1,<2" "python-multipart>=0.0.20,<1"
  ```

- [ ] **Step 5: Run GREEN and Compose validation**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_config.py tests/test_health.py -v
  docker compose config
  git diff --check
  ```

  Expected: settings and liveness tests pass, Compose validates with PostgreSQL plus only Milvus infrastructure, and the diff has no whitespace errors.

- [ ] **Step 6: Commit the runtime contract**

  ```powershell
  git add .gitignore pyproject.toml .env.example docker-compose.yml backend/config.py tests/test_knowledge_config.py
  git commit -m "chore: add knowledge retrieval runtime config"
  ```

### Task 2: Knowledge persistence schema and explicit migration

**Files:**

- Modify: `backend/common.py`
- Modify: `backend/models.py`
- Create: `alembic/versions/0003_knowledge_retrieval.py`
- Create: `tests/test_knowledge_models.py`

**Interfaces:**

```python
# backend/common.py
class KnowledgeVersionStatus(StrEnum):
    ACCEPTED = "accepted"
    PROCESSING = "processing"
    ACTIVE = "active"
    FAILED = "failed"
    DISABLED = "disabled"
```

```python
# backend/models.py
class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    # id, name, category, enabled, current_version_id, created_by,
    # idempotency_key, created_at, updated_at

class KnowledgeDocumentVersion(Base):
    __tablename__ = "knowledge_document_versions"
    # id, document_id, version_number, sha256, original_filename, mime_type,
    # storage_path, status, attempt_count, lease_owner, lease_expires_at,
    # parser_version, chunker_version, embedding_version, idempotency_key, error_code,
    # created_at, updated_at

class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    # id is the stable chunk_id, version_id, chunk_index, chunk_hash,
    # canonical_text, chunk_metadata (mapped to database column "metadata"), token_count, created_at
```

`KnowledgeDocument.current_version_id` is nullable and references a version. The service-level activation guard, not a cross-table check, proves it belongs to the same document and is active. `KnowledgeChunk.id` is the globally stable UUID5-derived `chunk_id`; do not add a redundant second ID column.

- [ ] **Step 1: Write failing SQLite constraint tests**

  Create `tests/test_knowledge_models.py`. Insert an administrator, document, version, and chunk through the existing SQLite `session` fixture. Assert a valid processing version persists only with both lease fields, then use independent transactions to assert `IntegrityError` for:

  ```python
  KnowledgeDocumentVersion(
      document_id=document.id,
      version_number=1,
      sha256="a" * 64,
      status=KnowledgeVersionStatus.PROCESSING,
      attempt_count=1,
      lease_owner=None,
      lease_expires_at=None,
  )
  ```

  Assert duplicate `(created_by, idempotency_key)`, duplicate `(document_id, idempotency_key)`, duplicate `(document_id, version_number)`, duplicate `(document_id, sha256)`, `attempt_count=-1`, `attempt_count=4`, an invalid status value, and a non-processing row with a lease all fail. Assert a duplicate `(version_id, chunk_index)` fails, while two chunks with the same `chunk_hash`, distinct indexes, and distinct `chunk_metadata` persist. Assert `KnowledgeChunk.id` is supplied as a stable chunk ID and is globally unique.

- [ ] **Step 2: Run RED**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_models.py -v
  ```

  Expected: collection fails because the enum and three ORM models do not exist.

- [ ] **Step 3: Add the models and revision `0003`**

  Map the three classes with the existing `String(36)` UUID convention, `utc_now` timestamps, `JSON` for `KnowledgeChunk.chunk_metadata`, and explicit constraints. Map its database column with `mapped_column("metadata", JSON, nullable=False)`; no Python class, dataclass, persistence call, search result, or test accesses the reserved attribute name `metadata`.

  ```python
  UniqueConstraint("document_id", "version_number", name="uq_knowledge_document_versions_document_id_version_number")
  UniqueConstraint("document_id", "sha256", name="uq_knowledge_document_versions_document_id_sha256")
  UniqueConstraint("document_id", "idempotency_key", name="uq_knowledge_document_versions_document_id_idempotency_key")
  UniqueConstraint("created_by", "idempotency_key", name="uq_knowledge_documents_created_by_idempotency_key")
  CheckConstraint("attempt_count BETWEEN 0 AND 3", name="ck_knowledge_document_versions_attempt_count")
  CheckConstraint(
      "(status = 'processing' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
      "OR (status != 'processing' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
      name="ck_knowledge_document_versions_lease_state",
  )
  UniqueConstraint("version_id", "chunk_index", name="uq_knowledge_chunks_version_id_chunk_index")
  CheckConstraint("chunk_index >= 0", name="ck_knowledge_chunks_chunk_index")
  CheckConstraint("token_count > 0", name="ck_knowledge_chunks_token_count")
  ```

  Add a named `status IN ('accepted','processing','active','failed','disabled')` check. Create revision `0003` from `0002` with explicit `op.create_table()` calls. Create documents first, versions second, then add the nullable `current_version_id` foreign key with an explicitly named `op.create_foreign_key()` so the document/version cycle is reversible. `downgrade()` drops the current-version foreign key, then chunks, versions, and documents; it does not touch analysis or LangGraph tables.

- [ ] **Step 4: Run GREEN and migration checks**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_models.py tests/test_models.py -v
  $env:JWT_SECRET_KEY = "local-knowledge-plan-verification-secret-at-least-32"
  .\.venv\Scripts\alembic.exe upgrade head
  .\.venv\Scripts\alembic.exe current
  .\.venv\Scripts\alembic.exe check
  ```

  Expected: model tests pass, PostgreSQL reports `0003 (head)`, and Alembic reports no new upgrade operations.

- [ ] **Step 5: Commit the schema boundary**

  ```powershell
  git add backend/common.py backend/models.py alembic/versions/0003_knowledge_retrieval.py tests/test_knowledge_models.py
  git commit -m "feat: add knowledge persistence schema"
  ```

### Task 3: Safe upload processing and deterministic chunks

**Files:**

- Create: `backend/knowledge_content.py`
- Create: `tests/test_knowledge_content.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class ValidatedKnowledgeUpload:
    original_filename: str
    mime_type: str
    sha256: str
    data: bytes

@dataclass(frozen=True)
class StoredKnowledgeUpload(ValidatedKnowledgeUpload):
    storage_path: Path

@dataclass(frozen=True)
class ChunkDraft:
    chunk_index: int
    chunk_id: str
    chunk_hash: str
    canonical_text: str
    chunk_metadata: dict[str, object]
    token_count: int

class KnowledgeContentError(ValueError):
    code: str

async def read_and_validate_upload(upload: UploadFile) -> ValidatedKnowledgeUpload:
    pass

def store_validated_upload(
    upload: ValidatedKnowledgeUpload, *, upload_dir: Path, document_id: str, version_id: str
) -> StoredKnowledgeUpload:
    pass

async def parse_and_chunk(
    stored: StoredKnowledgeUpload, *, token_count: Callable[[str], int], timeout_seconds: float
) -> list[ChunkDraft]:
    pass

def _parse_and_chunk_sync(
    stored: StoredKnowledgeUpload, token_count: Callable[[str], int]
) -> list[ChunkDraft]:
    pass

def stable_chunk_id(version_sha256: str, chunk_index: int, chunk_hash: str) -> str:
    pass
```

`token_count` is a direct callable boundary: ordinary tests pass a deterministic local counter; the production Worker passes the BGE-M3 tokenizer method defined in Task 5. It is not a provider framework.

- [ ] **Step 1: Write failing content-boundary tests**

  Create tests with `UploadFile` backed by `io.BytesIO` and `tmp_path`. Cover accepted UTF-8 `.txt` and `.md`, malformed UTF-8, mismatched extension/MIME, empty input, input larger than 20 MiB, `../name.pdf`, absolute filenames, encrypted or over-200-page PDF, malformed PDF, malformed DOCX ZIP, a ZIP with more than 1,000 entries, an entry or total uncompressed payload over 100 MiB, and empty extracted text. Assert each failure has the exact safe code from the specification. Construct a pre-existing generated document/version directory as a symlink to `tmp_path / "outside"`; `store_validated_upload()` must return `KNOWLEDGE_PATH_INVALID` and write no file outside the controlled upload root.

  Stub the pure parser to block beyond `timeout_seconds`; assert `parse_and_chunk()` raises `KnowledgeContentError("KNOWLEDGE_PARSE_TIMEOUT")`, returns no partial draft list, and leaves no stored chunk result. Feed extracted normalized text longer than 1,000,000 characters and assert `KNOWLEDGE_PARSE_FAILED` before a `ChunkDraft` is produced.

  Use heading and paragraph input with `token_count=lambda text: len(text.split())`; assert chunks preserve heading path, paragraph index, stable order, 512-token maximum, 64-token overlap, deterministic Unicode fallback splitting, and equality across two calls. Assert two equal paragraphs at different positions produce different `chunk_index` and `chunk_id` values while retaining the same `chunk_hash`.

- [ ] **Step 2: Run RED**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_content.py -v
  ```

  Expected: collection fails because `backend.knowledge_content` does not exist.

- [ ] **Step 3: Implement the narrow safe-content module**

  `read_and_validate_upload()` reads at most 20 MiB plus one byte, hashes the bytes with `hashlib.sha256`, and performs every extension/MIME/file-structure check before any write. `store_validated_upload()` is called only after the API has proved no idempotent document/version exists, and writes only to:

  ```text
  data/uploads/knowledge/<document-id>/<version-id>/<server-generated-name>
  ```

  Use `Path.resolve()` and `is_relative_to()` to verify the generated path remains under `upload_dir`, reject a symlink in any generated parent component, and never use the client filename in the path. Validate extension, declared MIME, and file structure before writing. Parse PDFs with `pypdf.PdfReader(BytesIO(data))`; reject encrypted files, more than 200 pages, extraction errors, and no normalized text. Inspect DOCX with `zipfile.ZipFile` before `docx.Document`, applying the stated entry and decompression limits. Decode Markdown/TXT strictly as UTF-8.

  Run the pure parsing/chunking function through `await asyncio.wait_for(asyncio.to_thread(_parse_and_chunk_sync, stored, token_count), timeout_seconds)` and turn only an elapsed deadline into `KNOWLEDGE_PARSE_TIMEOUT`. Build drafts in memory, normalize whitespace, retain title hierarchy/page/paragraph `chunk_metadata`, split by headings then paragraphs, split oversized paragraphs by sentence boundaries with 64-token overlap, and finally use Unicode character boundaries. Reject normalized text over 1,000,000 characters before returning any draft. Derive `chunk_hash` from canonical text and derive `chunk_id` with one fixed UUID5 namespace over `version_sha256`, `chunk_index`, and `chunk_hash`.

- [ ] **Step 4: Run GREEN and parser regression tests**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_content.py -v
  .\.venv\Scripts\python.exe -m compileall backend tests
  ```

  Expected: all content tests pass without loading a model or connecting to Milvus.

- [ ] **Step 5: Commit safe deterministic content handling**

  ```powershell
  git add backend/knowledge_content.py tests/test_knowledge_content.py
  git commit -m "feat: add safe knowledge content processing"
  ```

### Task 4: Version leases, idempotent chunk metadata, and monotonic activation

**Files:**

- Create: `backend/knowledge_runs.py`
- Create: `tests/test_knowledge_runs.py`

**Interfaces:**

```python
async def create_document_version(
    session: AsyncSession, *, document_id: str, version_id: str, created_by: str,
    name: str | None, category: str | None, sha256: str, original_filename: str,
    mime_type: str, storage_path: str, idempotency_key: str | None = None
) -> tuple[KnowledgeDocument, KnowledgeDocumentVersion, bool]:
    pass

async def find_document_version_by_sha(
    session: AsyncSession, *, document_id: str, sha256: str
) -> KnowledgeDocumentVersion | None:
    pass

async def find_document_version_by_idempotency_key(
    session: AsyncSession, *, document_id: str, idempotency_key: str
) -> KnowledgeDocumentVersion | None:
    pass

async def find_document_by_idempotency_key(
    session: AsyncSession, *, created_by: str, idempotency_key: str
) -> KnowledgeDocument | None:
    pass

async def claim_next_knowledge_version(
    session: AsyncSession, *, lease_owner: str, lease_seconds: int
) -> KnowledgeDocumentVersion | None:
    pass

async def renew_knowledge_lease(
    session: AsyncSession, *, version_id: str, lease_owner: str, lease_seconds: int
) -> bool:
    pass

async def return_version_for_retry(
    session: AsyncSession, *, version_id: str, lease_owner: str, error_code: str
) -> bool:
    pass

async def fail_knowledge_version(
    session: AsyncSession, *, version_id: str, lease_owner: str, error_code: str
) -> bool:
    pass

async def upsert_knowledge_chunks(
    session: AsyncSession, *, version_id: str, lease_owner: str, chunks: list[ChunkDraft]
) -> bool:
    pass

async def activate_knowledge_version(
    session: AsyncSession, *, version_id: str, lease_owner: str
) -> bool:
    pass

async def disable_knowledge_document(
    session: AsyncSession, *, document_id: str
) -> bool:
    pass
```

`create_document_version()` returns `created=False` for an existing `(document_id, sha256)` and creates no lease. For initial document creation, a non-empty key remains unique per creator in `KnowledgeDocument`; repeating it with the same SHA returns its first document/version and repeating it with a different SHA returns `KNOWLEDGE_IDEMPOTENCY_CONFLICT` without a write. For `POST /knowledge/documents/{id}/versions`, a non-empty bounded key is stored on `KnowledgeDocumentVersion` and unique by `(document_id, idempotency_key)`: the same key plus the same SHA, or the same document plus SHA without a key match, returns the existing version; the same key plus a different SHA returns `KNOWLEDGE_IDEMPOTENCY_CONFLICT` without a write. `return_version_for_retry()` turns attempts 1 or 2 into `accepted`; at attempt 3 it writes `failed/KNOWLEDGE_ATTEMPTS_EXHAUSTED`. `fail_knowledge_version()` is the non-retryable path. All owner mutations return `False` without committing a stale-owner change.

- [ ] **Step 1: Write failing lease and activation tests**

  Create exact SQLite rows with distinct `created_at` values. Assert claim order is `(created_at, id)`, an accepted row is claimed with attempt 1, an expired attempt-2 row is reclaimed with attempt 3, and an expired attempt-3 row becomes `failed/KNOWLEDGE_ATTEMPTS_EXHAUSTED` without a fourth claim. Assert `failed` and `disabled` rows are never claimed.

  Assert a retryable attempt-1 failure returns to `accepted` with cleared lease, a non-retryable failure reaches `failed`, and an old owner cannot renew, retry, fail, upsert completion metadata, or activate after a replacement owner is installed. Assert a repeated `(created_by, idempotency_key, sha256)` returns the initial document/version and a key with a different SHA returns `KNOWLEDGE_IDEMPOTENCY_CONFLICT`. Separately, assert a repeated `(document_id, version_idempotency_key, sha256)` returns one existing version with no lease, while that same version key plus another SHA returns `KNOWLEDGE_IDEMPOTENCY_CONFLICT`. Persist a `ChunkDraft(chunk_metadata={"heading_path": ["章节"], "paragraph_index": 0})` list twice and assert count remains unchanged by `(version_id, chunk_index)`.

  Create versions 1 and 2 for one document. Activate version 2 first, then call `activate_knowledge_version()` as version 1's valid owner; assert it returns `False`, version 1 becomes `disabled/KNOWLEDGE_VERSION_SUPERSEDED`, version 2 remains `active`, and `document.current_version_id` stays version 2. Disable the document and assert every accepted, processing, and active version becomes disabled with no lease and `current_version_id is None`.

- [ ] **Step 2: Run RED**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_runs.py -v
  ```

  Expected: collection fails because `backend.knowledge_runs` does not exist.

- [ ] **Step 3: Implement database-first state transitions**

  Follow `backend.analysis_runs.py` for SQLite/PostgreSQL `insert(model).on_conflict_do_update()` selection, but operate only on knowledge tables. Use PostgreSQL `func.now()` and `with_for_update(skip_locked=True)` for production. For SQLite tests, retain the current dialect-compatible interval expression used by `analysis_runs._lease_expiry()`.

  Claim in one transaction by first transitioning only expired `processing` rows with `attempt_count=3` to `failed/KNOWLEDGE_ATTEMPTS_EXHAUSTED`, then selecting only `accepted` or expired processing rows with `attempt_count<3`. On each selected row write the owner, future expiry, `status=processing`, and `attempt_count + 1` before commit.

  `upsert_knowledge_chunks()` first locks the version through the same valid owner guard, maps each `ChunkDraft.chunk_metadata` to `KnowledgeChunk.chunk_metadata` (the mapped database column remains `metadata`), and upserts only by `(version_id, chunk_index)`; it never deduplicates canonical text or `chunk_hash`. A lost owner returns `False` after rollback before any chunk row write.

  Implement the activation transaction by locking the document row, checking `enabled=true`, loading the current version number, and applying this exact condition:

  ```python
  current is None or current.version_number < candidate.version_number
  ```

  When true, set candidate active, assign `current_version_id`, and disable the previous active version in the same transaction. When false, owner-guard the candidate to `disabled/KNOWLEDGE_VERSION_SUPERSEDED`; do not change the document current version. `disable_knowledge_document()` locks the document and atomically clears `current_version_id`, disables accepted/processing/active rows, and clears every lease.

  Use `logging.getLogger("backend.knowledge")` to emit one safe state-transition event after each committed create, claim, retry, fail, activation, supersede, and disable result. Its fields are `request_id` (the route-generated ID or `-` for a Worker), `actor_id` (the route user or the document's `created_by`), document/version IDs, transition status, safe error code, and elapsed milliseconds. Add `caplog` assertions for a successful claim and failed retry: both contain the status fields and neither contains canonical text, a storage path, vectors, uploaded bytes, or a credential.

- [ ] **Step 4: Run GREEN and state regression tests**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_runs.py tests/test_knowledge_models.py -v
  ```

  Expected: all lifecycle tests pass; no query touches Milvus or a local model.

- [ ] **Step 5: Commit durable version semantics**

  ```powershell
  git add backend/knowledge_runs.py tests/test_knowledge_runs.py
  git commit -m "feat: add knowledge version leases"
  ```

### Task 5: Local BGE-M3/Milvus indexing Worker

**Files:**

- Create: `backend/knowledge_index.py`
- Create: `backend/knowledge_worker.py`
- Create: `scripts/run_knowledge_worker.py`
- Create: `tests/test_knowledge_worker.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class HybridVector:
    dense: list[float]             # exactly 1024 values
    sparse: dict[int, float]

@dataclass(frozen=True)
class IndexedChunk:
    chunk_id: str
    document_id: str
    version_id: str
    category: str
    vector: HybridVector

class KnowledgeDependencyError(RuntimeError):
    code: str
    retryable: bool

class LocalKnowledgeModels:
    def __init__(
        self, *, embedding_model_path: Path, reranker_model_path: Path, timeout_seconds: float
    ) -> None:
        pass
    def token_count(self, text: str) -> int:
        pass
    async def embed_documents(self, texts: list[str]) -> list[HybridVector]:
        pass
    async def embed_query(self, text: str) -> HybridVector:
        pass
    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        pass

class MilvusKnowledgeIndex:
    def __init__(self, *, uri: str, collection: str, timeout_seconds: float) -> None:
        pass
    async def ensure_collection(self) -> None:
        pass
    async def upsert(self, chunks: list[IndexedChunk]) -> None:
        pass
    async def dense_search(
        self, *, vector: list[float], version_ids: list[str], limit: int
    ) -> list[tuple[str, float]]:
        pass
    async def sparse_search(
        self, *, vector: dict[int, float], version_ids: list[str], limit: int
    ) -> list[tuple[str, float]]:
        pass
    async def existing_chunk_ids(self, *, chunk_ids: list[str]) -> set[str]:
        pass
    async def list_chunk_ids_for_version(self, *, version_id: str) -> set[str]:
        pass
    async def delete_chunk_ids(self, *, chunk_ids: list[str]) -> None:
        pass

async def run_once(
    session_factory: async_sessionmaker[AsyncSession], *, settings: Settings,
    lease_owner: str, models: LocalKnowledgeModels, index: MilvusKnowledgeIndex,
) -> str | None:
    pass
```

`LocalKnowledgeModels` accepts only the two configured local `Path` values and `knowledge_dependency_timeout_seconds`. It verifies each is a directory before constructing `BGEM3FlagModel` and `FlagReranker`; a missing or load-failed path raises `KnowledgeDependencyError("KNOWLEDGE_MODEL_UNAVAILABLE", retryable=True)`, never a download request. Every blocking embedding and reranker call runs through `asyncio.to_thread` plus `asyncio.wait_for`; expiration is `KnowledgeDependencyError("KNOWLEDGE_DEPENDENCY_TIMEOUT", retryable=True)`. `MilvusKnowledgeIndex` represents one fixed collection, not a pluggable provider layer; every collection, upsert, search, exact-key query, and exact-key deletion uses the same configured deadline and maps a deadline to that timeout code and a non-timeout dependency failure to `KNOWLEDGE_MILVUS_UNAVAILABLE`.

- [ ] **Step 1: Write failing Worker tests with real SQLite facts and fake dependencies**

  Create a document/version whose `storage_path` points to a temporary Markdown file. Use a fake `LocalKnowledgeModels` that returns 1024-length dense lists, sparse dictionaries, and a token count, plus a recording `MilvusKnowledgeIndex` fake. Test that one `run_once()`:

  ```python
  processed = await run_once(
      factory, settings=settings, lease_owner="worker-a", models=fake_models, index=fake_index
  )
  assert processed == version.id
  assert fake_index.upserts[0][0].chunk_id == persisted_chunk.id
  ```

  Assert Milvus receives only `chunk_id`, document/version/category metadata, and dense/sparse vectors; canonical text, file path, user ID, and error code are absent. Assert upsert runs before `upsert_knowledge_chunks()` and activation, replaying the Worker does not add vector keys or chunk rows, and only an active version becomes searchable later. Make a fake parser, model, and index each exceed the configured deadline; assert each maps to `KNOWLEDGE_PARSE_TIMEOUT` or `KNOWLEDGE_DEPENDENCY_TIMEOUT`, emits no partial chunks/activation, and does not manufacture a success result.

  Make the fake model raise retryable timeout/unavailable errors on attempts 1 and 2, then assert `accepted` with its safe code; make it fail on attempt 3 and assert `failed/KNOWLEDGE_ATTEMPTS_EXHAUSTED`. Make parsing raise `KnowledgeContentError("KNOWLEDGE_PARSE_FAILED")` and assert terminal failed. Make `upsert_knowledge_chunks()` raise `asyncio.CancelledError` after a successful recording Milvus upsert; assert `run_once()` propagates cancellation, leaves the version `processing` with its existing owner/lease, and calls neither retry nor fail state transitions. Replace the lease owner during renewal and assert no further model call, Milvus upsert, chunk persistence, activation, or overwrite occurs.

- [ ] **Step 2: Run RED**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_worker.py -v
  ```

  Expected: collection fails because the index and Worker modules do not exist.

- [ ] **Step 3: Implement local index operations and one-version Worker**

  `LocalKnowledgeModels` calls local `FlagEmbedding` APIs with the two configured filesystem paths, checks `len(dense) == 1024`, and converts BGE-M3 lexical weights into `dict[int, float]`. `MilvusKnowledgeIndex.ensure_collection()` creates one collection named by `settings.milvus_collection` with primary `chunk_id`, `document_id`, `version_id`, category, `FLOAT_VECTOR` dimension 1024, and `SPARSE_FLOAT_VECTOR`; it creates the documented dense and sparse indexes. `upsert()` uses stable `chunk_id` primary keys. `dense_search()` and `sparse_search()` each receive only service-derived active version IDs, return their own raw `(chunk_id, score)` lists, and do not accept user categories or an arbitrary filter expression. `existing_chunk_ids()` and `delete_chunk_ids()` accept an explicit non-empty stable-ID list only; neither method accepts a document, version, wildcard, or broad collection expression. `list_chunk_ids_for_version()` is read-only integration-test inspection with one server-generated test `version_id` equality filter; it enables exact-set assertion but is never used for cleanup.

  `scripts/run_knowledge_worker.py` constructs exactly one `LocalKnowledgeModels` and one `MilvusKnowledgeIndex` from `Settings` before its loop, then passes both to each `run_once()`; normal tests pass fakes explicitly. `run_once()` claims at most one version using `claim_next_knowledge_version()`. Before parsing, embedding, and Milvus upsert, call `renew_knowledge_lease()`; after parsing returns, renew again before any embedding or persistence. A false result stops immediately. Process in this fixed order:

  ```text
  parse + deterministic chunk -> BGE-M3 dense+sparse -> Milvus stable-ID upsert
  -> PostgreSQL chunk upsert -> PostgreSQL monotonic activation
  ```

  Translate only explicit local-model/Milvus deadline, unavailable, and retryable connection failures to retryable `KnowledgeDependencyError`; translate parsing/security errors to terminal codes; translate any other `Exception` to `KNOWLEDGE_PROCESSING_FAILED`. Never catch `asyncio.CancelledError` or `BaseException`: cancellation simulates process interruption, propagates without retry/fail cleanup, and leaves the valid `processing` lease for expiry recovery. The Worker never deletes Milvus vectors to recover a run. Add a Windows-safe `scripts/run_knowledge_worker.py` command mirroring `scripts/run_analysis_worker.py`, with `--once`, hostname/PID owner, and a one-second idle sleep; it does not print settings or credentials.

- [ ] **Step 4: Run GREEN and Worker regressions**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_worker.py tests/test_knowledge_content.py tests/test_knowledge_runs.py -v
  .\.venv\Scripts\python.exe -m compileall backend scripts tests
  ```

  Expected: all tests use only SQLite and fakes; no BGE weights or Milvus endpoint are loaded.

- [ ] **Step 5: Commit recoverable local indexing**

  ```powershell
  git add backend/knowledge_index.py backend/knowledge_worker.py scripts/run_knowledge_worker.py tests/test_knowledge_worker.py
  git commit -m "feat: add recoverable knowledge indexing worker"
  ```

### Task 6: Demonstration rules, calibrated retrieval, and offline evaluation

**Files:**

- Create: `backend/knowledge_search.py`
- Create: `backend/knowledge_evaluation.py`
- Create: `data/knowledge/demo/platform-neutral-rules.md`
- Create: `data/knowledge/evaluation/queries.json`
- Create: `data/knowledge/evaluation/calibration-candidates.json`
- Create: `tests/test_knowledge_search.py`
- Create: `tests/test_knowledge_evaluation.py`

**Interfaces:**

```python
from collections.abc import Callable
from functools import lru_cache
from typing import TypeAlias

@dataclass(frozen=True)
class KnowledgeSearchHit:
    chunk_id: str
    document_name: str
    version_number: int
    category: str
    canonical_text: str
    chunk_metadata: dict[str, object]
    dense_score: float | None
    sparse_score: float | None
    fusion_score: float | None
    reranker_score: float | None
    final_score: float

RetrievalPath = Literal["dense", "sparse", "hybrid", "hybrid_rerank"]

@dataclass(frozen=True)
class FusedRetrievalScore:
    chunk_id: str
    dense_score: float | None
    sparse_score: float | None
    dense_rank: int | None
    sparse_rank: int | None
    fusion_score: float

KnowledgeSearchDependencies: TypeAlias = tuple[
    LocalKnowledgeModels, MilvusKnowledgeIndex, dict[str, object]
]
KnowledgeSearchLoader: TypeAlias = Callable[[], KnowledgeSearchDependencies]

@dataclass(frozen=True)
class RetrievalQueryOutcome:
    query_id: str
    hits: list[KnowledgeSearchHit]
    elapsed_ms: float

@dataclass(frozen=True)
class RetrievalMetrics:
    recall_at_10: float
    mrr: float
    citation_document_version_accuracy: float
    latency_ms: float

@dataclass(frozen=True)
class KnowledgeSearchOutcome:
    quality_status: Literal["normal", "zero_hit", "low_confidence"]
    hits: list[KnowledgeSearchHit]

async def search_active_knowledge(
    session: AsyncSession, *, query: str, categories: list[str] | None, top_k: int,
    retrieval_path: RetrievalPath, load_dependencies: KnowledgeSearchLoader,
) -> KnowledgeSearchOutcome:
    pass

def fuse_rankings(
    *, dense: list[tuple[str, float]], sparse: list[tuple[str, float]], rrf_k: int
) -> list[FusedRetrievalScore]:
    pass

def evaluate_retrieval(
    queries: list[dict[str, object]],
    outcomes: dict[RetrievalPath, dict[str, RetrievalQueryOutcome]],
) -> dict[RetrievalPath, RetrievalMetrics]:
    pass

def select_calibration(
    candidates: list[dict[str, object]], metrics_by_candidate: dict[str, RetrievalMetrics]
) -> dict[str, object]:
    pass

@lru_cache
def get_knowledge_search_dependencies() -> KnowledgeSearchDependencies:
    pass

def get_knowledge_search_loader() -> KnowledgeSearchLoader:
    pass
```

`select_calibration()` ranks real candidate metrics by Recall@10 descending, MRR descending, lower retrieval candidate count, then lexicographic JSON form. It does not write a file. `get_knowledge_search_loader()` is a cheap, replaceable FastAPI dependency which only returns the cached `get_knowledge_search_dependencies` callable. It neither loads a model, connects Milvus, nor reads `calibration.json`. `search_active_knowledge()` first queries PostgreSQL and returns the no-active-version `zero_hit` before calling that loader. Only its non-empty active-version branch invokes `load_dependencies()`: the cached function then reads `get_settings()`, constructs `LocalKnowledgeModels` and `MilvusKnowledgeIndex`, and reads the final `calibration.json`; later active searches reuse it. Thus an active-version query may load dependencies before it can determine a retrieval-empty `zero_hit`, but API startup and a no-active-version `zero_hit` never do. Ordinary tests call `.cache_clear()` and inject a recording loader. This is a cached dependency function, not a provider or factory framework. The Worker continues to construct and pass its two dependencies explicitly.

`fuse_rankings()` assigns a one-based rank within each raw score list ordered by score descending then `chunk_id` ascending. For a union member, `fusion_score = sum(1 / (rrf_k + rank))` across only its present dense and sparse ranks; absent raw scores and ranks are `None`, never `0`. `dense` sets `final_score=dense_score` and sorts by `final_score DESC, chunk_id ASC`; `sparse` does the analogous sparse rule; `hybrid` sets `final_score=fusion_score` and sorts by `final_score DESC, chunk_id ASC`; `hybrid_rerank` sets `final_score=reranker_score` and sorts by `final_score DESC, fusion_score DESC, chunk_id ASC`. The unused score fields are `None`, so API JSON emits `null` rather than an invented zero.

- [ ] **Step 1: Write failing search and evaluation tests**

  Add a SQLite active document/version/chunk plus an inactive old version and an orphan chunk. Use recording fake models/indexes and a recording `load_dependencies` callable. Assert `search_active_knowledge()` returns the no-active-version `zero_hit` without calling its loader, constructing models, reading calibration, or calling Milvus. With active IDs, assert it calls the loader exactly once, filters optional categories in PostgreSQL before deriving active version IDs, sends only those service-derived IDs to exactly one dense and one sparse Milvus search, reloads canonical text and `chunk_metadata` only from PostgreSQL, rejects a Milvus `chunk_id` outside the active set, reranks only canonical texts, and includes every stage score in deterministic final-score order. Use a category containing quotes and an injection-shaped string; assert it never becomes a Milvus expression or argument and causes no extra index search.

  Make fakes raise `KnowledgeDependencyError("KNOWLEDGE_DEPENDENCY_TIMEOUT", retryable=True)` and `KnowledgeDependencyError("KNOWLEDGE_MILVUS_UNAVAILABLE", retryable=True)`; assert the caller can distinguish timeout from dependency error and no hit is manufactured. Assert dense-only, sparse-only, hybrid, and hybrid-rerank calls each follow the documented final-score formula and tie break. Include a union candidate returned by only one raw search; assert its absent score is `None`, never `0`. Assert `fuse_rankings()` returns `FusedRetrievalScore` values with one-based ranks and creates RRF scores without a third Milvus hybrid request. Test fixed typed outcomes for all four `RetrievalPath` values, each keyed by `query_id` and carrying `elapsed_ms`; assert `evaluate_retrieval()` emits Recall@10, MRR, citation document/version accuracy, and latency for every path. Test candidate metrics where two selector candidates tie on Recall@10 and MRR; assert the lower candidate count wins. Do not infer duplicate vectors from search outcomes; that is an exact primary-key-set check in Task 8.

  Test `get_knowledge_search_dependencies.cache_clear()` with monkeypatched settings and recording constructors: importing the FastAPI app and resolving `get_knowledge_search_loader()` cause neither constructor nor calibration read. The first non-empty-active-version call constructs one model/index pair and reads calibration once; the second reuses those same objects.

- [ ] **Step 2: Run RED**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_search.py tests/test_knowledge_evaluation.py -v
  ```

  Expected: collection fails because search/evaluation modules and committed data files do not exist.

- [ ] **Step 3: Add platform-neutral rules and deterministic retrieval behavior**

  Write one concise Chinese Markdown rule pack. Its heading and every rule section must say `项目演示规则：平台中立、非官方法规或平台规范`. Include title keywords, selling-point/detail evidence, prohibited or exaggerated claims, category attributes, SKU/price/spec consistency, and after-sales/refund wording. Do not state or imply an official policy.

  Add fixed Chinese queries with expected rule section/document/version assertions. Define the finite candidate grid in `calibration-candidates.json` for dense-only, sparse-only, reciprocal-rank-fusion hybrid, and hybrid-plus-rerank evaluation. The candidate file has IDs, candidate limits, `rrf_k`, and thresholds but no selected configuration. It includes no secret, model output, user upload, or platform rule. Do not create or claim a selected `calibration.json` in this task; Task 8 writes it only after the approved real local evaluation meets acceptance targets.

  `search_active_knowledge()` first queries PostgreSQL for enabled documents, optional categories, and active version IDs. An empty active set returns `zero_hit` before calling `load_dependencies()`. A non-empty set invokes the loader, then BGE-M3 query embedding, `dense_search()`, and `sparse_search()` with the same active-ID list; it retains each raw score, applies the selected deterministic RRF in process, fetches chunk text, `chunk_metadata`, and version/document metadata by returned IDs from PostgreSQL, optionally reranks for `hybrid_rerank`, and returns `normal`, `low_confidence`, or `zero_hit`. It does not perform a third Milvus hybrid call. A returned ID outside active versions is discarded. A dependency exception is propagated unchanged for the API to classify; it never becomes a synthetic citation.

- [ ] **Step 4: Run GREEN and retrieval regressions**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_search.py tests/test_knowledge_evaluation.py tests/test_knowledge_worker.py -v
  ```

  Expected: search/evaluation tests pass using SQLite and fakes only; no model, Milvus, or external network access occurs.

- [ ] **Step 5: Commit rules and retrieval core**

  ```powershell
  git add backend/knowledge_search.py backend/knowledge_evaluation.py data/knowledge/demo/platform-neutral-rules.md data/knowledge/evaluation/queries.json data/knowledge/evaluation/calibration-candidates.json tests/test_knowledge_search.py tests/test_knowledge_evaluation.py
  git commit -m "feat: add calibrated knowledge retrieval core"
  ```

### Task 7: Authorized knowledge API and safe envelope

**Files:**

- Modify: `backend/schemas.py`
- Modify: `backend/routes.py`
- Modify: `backend/main.py`
- Create: `tests/test_knowledge_api.py`

**Interfaces:**

```python
class KnowledgeError(BaseModel):
    category: Literal["timeout", "dependency_error", "validation_error", "authorization_error", "not_found"]
    code: str
    message: str

class KnowledgeEnvelope(BaseModel):
    request_id: str
    status: Literal["accepted", "success", "error"]
    data: dict[str, object] | None
    quality: dict[str, str] | None
    error: KnowledgeError | None

class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    categories: list[str] | None = None
    top_k: int = Field(default=10, ge=1, le=20)

class KnowledgeCitation(BaseModel):
    chunk_id: str
    document_name: str
    version_number: int
    category: str
    canonical_text: str
    chunk_metadata: dict[str, object]
    dense_score: float | None
    sparse_score: float | None
    fusion_score: float | None
    reranker_score: float | None
    final_score: float
```

```python
@router.post("/knowledge/documents", status_code=status.HTTP_202_ACCEPTED)
async def create_knowledge_document(
    response: Response,
    name: Annotated[str, Form(min_length=1, max_length=128)],
    category: Annotated[str, Form(min_length=1, max_length=64)],
    file: UploadFile,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
    user: User = Depends(require_roles(UserRole.ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeEnvelope:
    pass

@router.get("/knowledge/documents")
async def list_knowledge_documents(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    category: str | None = None,
    enabled: bool | None = None,
    version_status: KnowledgeVersionStatus | None = None,
    user: User = Depends(require_roles(UserRole.ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeEnvelope:
    pass

@router.post("/knowledge/documents/{document_id}/versions", status_code=status.HTTP_202_ACCEPTED)
async def create_knowledge_document_version(
    response: Response,
    document_id: str,
    file: UploadFile,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
    user: User = Depends(require_roles(UserRole.ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeEnvelope:
    pass

@router.post("/knowledge/documents/{document_id}/disable")
async def disable_knowledge_document_route(
    document_id: str,
    user: User = Depends(require_roles(UserRole.ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeEnvelope:
    pass

@router.post("/knowledge/search")
async def search_knowledge_route(
    request: KnowledgeSearchRequest,
    user: User = Depends(require_roles(UserRole.OPERATOR, UserRole.SUPERVISOR, UserRole.ADMIN)),
    session: AsyncSession = Depends(get_session),
    load_dependencies: KnowledgeSearchLoader = Depends(get_knowledge_search_loader),
) -> KnowledgeEnvelope:
    pass
```

The two upload routes accept `name`/`category` form fields only for document creation and an `UploadFile` field named `file`; later versions inherit document name/category. Both accept a header key of at most 128 characters. List pagination defaults to `page=1`, `page_size=20`, and rejects values outside `1..100`. `Response` changes a new accepted upload to `202` and an existing SHA/key hit to `200`; route decorators document the new-resource default only.

Exact route map: `POST /knowledge/documents`, `GET /knowledge/documents`, `POST /knowledge/documents/{document_id}/versions`, `POST /knowledge/documents/{document_id}/disable`, and `POST /knowledge/search`.

- [ ] **Step 1: Write failing ASGI API and RBAC tests**

  Create `tests/test_knowledge_api.py` using the real FastAPI app, a SQLite `get_session` override, real `create_access_token()`, and actual database role changes. Use a temporary upload directory in a copied `Settings`; override only `get_knowledge_search_loader` with a recording fake loader so no weight, calibration file, or service is loaded.

  Assert an administrator can create a Markdown document and receives a `202` envelope with `accepted`, document/version IDs, and no path. Repeat the creation with the same client idempotency key and SHA and assert a `200` existing version without a second row, upload path, or lease; repeat the key with a different SHA and assert `409/KNOWLEDGE_IDEMPOTENCY_CONFLICT`. Submit an existing document's version with a new bounded key and new SHA, assert `202`, repeat that key plus SHA and assert `200` with no second row/lease, repeat the document+SHA without a key and assert `200`, then reuse the version key with a different SHA and assert `409/KNOWLEDGE_IDEMPOTENCY_CONFLICT`. Assert operator and supervisor receive 403 envelopes for create, list, version, and disable, while all three active roles may search. Remove a user's active status after issuing its JWT and assert 401.

  Assert list responses paginate and omit `storage_path`, `lease_owner`, and upload bytes. Assert an unknown document is 404, a disabled document rejects a new version with 409, repeated disable is 200, and disabled documents disappear from search immediately. For search, assert query length and top-k validation produce a `validation_error` envelope with a non-empty `request_id` that is used consistently within that response; assert no error field contains the request path, invalid raw query, or validation detail. Assert a non-knowledge validation error retains FastAPI's existing response. Assert no active version produces `200/zero_hit` without invoking the recording loader, reading calibration, constructing a model, or connecting Milvus. Assert an active-version request invokes the loader once and returns PostgreSQL text plus document/version/chunk citations and nullable stage scores (`null` for an absent raw or reranker score). A timeout produces exactly `503` with `error.category="timeout"` and `KNOWLEDGE_DEPENDENCY_TIMEOUT`, and an unavailable fake produces exactly `503` with `error.category="dependency_error"` and `KNOWLEDGE_MILVUS_UNAVAILABLE`. Neither error returns hits.

  With `caplog`, assert successful create/list and an upload failure emit request ID, actor ID, document/version ID when known, action/status, safe code, and duration; assert their messages omit upload text, query text, local path, vectors, and credentials.

- [ ] **Step 2: Run RED**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_api.py -v
  ```

  Expected: collection fails because knowledge schemas and routes do not exist.

- [ ] **Step 3: Implement only the five contract routes**

  Extend `backend.schemas` with concrete document, version, citation, search-result, error, and envelope models. Citation score fields are `float | None` for unavailable raw/fusion/reranker stages and `float` for `final_score`; serialize `None` as JSON `null`, never `0`. Each knowledge route injects the current database user directly with `Depends(require_roles(UserRole.ADMIN))` for create/list/version/disable or `Depends(require_roles(UserRole.OPERATOR, UserRole.SUPERVISOR, UserRole.ADMIN))` for search. Do not inject `get_current_user` separately or hand-call `require_roles`. The search route receives only `load_dependencies: KnowledgeSearchLoader = Depends(get_knowledge_search_loader)` and calls `search_active_knowledge(session, query=request.query, categories=request.categories, top_k=request.top_k, retrieval_path="hybrid_rerank", load_dependencies=load_dependencies)`; FastAPI never resolves the cached model/index/calibration tuple itself. Route code first calls `read_and_validate_upload()`, then checks the applicable creation or version idempotency key and existing document SHA before generating IDs or writing a path. Only a new version calls `store_validated_upload()`, followed by `create_document_version()` and one database commit; a transaction failure removes only that exact just-written path. Set `response.status_code = status.HTTP_200_OK` for an existing SHA/key hit, otherwise leave the documented `202`. A repeated key with a different SHA returns `409/KNOWLEDGE_IDEMPOTENCY_CONFLICT` without a write.

  Add two path-scoped handlers in `backend.main.create_app()`. For `/knowledge/` HTTP exceptions, map authentication, authorization, and not-found exceptions to a single generated `KnowledgeEnvelope(status="error", data=None, quality=None, error=KnowledgeError(category=category, code=code, message=message))`; delegate every non-knowledge HTTP exception to FastAPI's existing handler. Separately register a `RequestValidationError` handler that applies the same safe `validation_error/KNOWLEDGE_REQUEST_INVALID` envelope only for `/knowledge/`, with one generated non-empty request ID and no `exc.errors()` path/input/context; delegate every non-knowledge validation exception to FastAPI's existing validation handler. Map `KnowledgeDependencyError` timeout codes to `503/timeout`, and other dependency codes to `503/dependency_error`. Keep `zero_hit` and `low_confidence` as successful search quality values, not exceptions. Log only safe audit fields with stdlib `logging`.

- [ ] **Step 4: Run GREEN API and authentication regressions**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_api.py tests/test_auth_and_scope.py tests/test_analysis_api.py -v
  .\.venv\Scripts\python.exe -m compileall backend tests
  ```

  Expected: knowledge routes enforce real-time database roles, existing analysis/auth routes retain their current responses, and no API request starts a Worker or loads a model before a search with non-empty active version IDs invokes its loader.

- [ ] **Step 5: Commit the protected API**

  ```powershell
  git add backend/schemas.py backend/routes.py backend/main.py tests/test_knowledge_api.py
  git commit -m "feat: add authorized knowledge retrieval API"
  ```

### Task 8: Opt-in PostgreSQL/Milvus/local-model vertical acceptance

**Files:**

- Modify: `backend/knowledge_evaluation.py`
- Create: `scripts/calibrate_knowledge_retrieval.py`
- Create: `data/knowledge/evaluation/calibration.json`
- Create: `tests/test_knowledge_integration.py`

**Interfaces:**

```python
# tests/test_knowledge_integration.py
@pytest.mark.knowledge_integration
@pytest.mark.skipif(
    os.getenv("RUN_KNOWLEDGE_INTEGRATION") != "1",
    reason="explicit PostgreSQL, Milvus, and local-model integration opt-in required",
)
async def test_knowledge_vertical_slice() -> None:
    pass

# backend/knowledge_evaluation.py
def write_selected_calibration(path: Path, selection: dict[str, object]) -> None:
    pass
```

The test uses configured `async_session_factory`, `LocalKnowledgeModels`, and `MilvusKnowledgeIndex`. It creates document/version/chunk rows with test UUID prefixes and deletes only those exact rows and their exact stable Milvus IDs in `finally`; it never drops, truncates, resets, or broadly deletes PostgreSQL, Milvus, uploads, or local models. `write_selected_calibration()` serializes the selected non-secret configuration to a same-directory temporary file, `fsync`s it, and applies `os.replace()` only after all real evaluation thresholds pass; it never writes a partial JSON file.

- [ ] **Step 1: Write the default-skipped vertical acceptance test**

  Create one test that imports the committed demonstration Markdown through the same safe upload and version functions, starts two independent PostgreSQL sessions, and uses `asyncio.gather()` to claim two accepted versions. Assert their claimed IDs are distinct, proving `FOR UPDATE SKIP LOCKED` mutual exclusion.

  Set a test version to expired attempt 2 using PostgreSQL `func.now() - text("interval '1 second'")`; assert a new owner claims attempt 3 and the old owner cannot activate it. Set another test version to expired attempt 3 and assert it becomes `failed/KNOWLEDGE_ATTEMPTS_EXHAUSTED`; assert active, failed, and disabled rows are never claimed.

  Create one accepted test version and monkeypatch `backend.knowledge_worker.upsert_knowledge_chunks` to raise `asyncio.CancelledError` once after the real Milvus upsert returns and before any PostgreSQL chunk persistence or activation. Assert `run_once()` propagates that cancellation, leaves the same version `processing` with its first owner and lease, and does not call retry or fail cleanup. Force only that test version's lease expiry using PostgreSQL `func.now() - text("interval '1 second'")`; restore normal persistence, then run `run_once()` with a second owner so that call itself reclaims and completes the expired version. Afterwards assert the first owner receives `False` from both owner-guarded chunk persistence and activation; no test manually claims a row and then incorrectly asks `run_once()` to process an already-processing row.

  Build `expected_ids` from the first upsert's stable chunk IDs. Read `MilvusKnowledgeIndex.list_chunk_ids_for_version(version_id=version.id)` and the PostgreSQL stable chunk IDs after recovery; assert both exact sets equal `expected_ids`, their counts equal `len(expected_ids)`, and no replay-created vector key exists. Cleanup uses `delete_chunk_ids(chunk_ids=list(expected_ids))` only after that comparison; it performs no version-wide or broad deletion.

  Create version 2, let it activate, then complete version 1 and assert version 1 is `disabled/KNOWLEDGE_VERSION_SUPERSEDED` and never appears in an active-version filtered query. Disable the document and assert its result immediately disappears even if its vectors remain.

  Load the fixed queries and candidate grid. Record dense-only, sparse-only, hybrid, and hybrid+rereank typed per-query outcomes and elapsed times through `evaluate_retrieval()`. Assert the chosen hybrid+rereank configuration has `recall_at_10 >= 0.90` and `citation_document_version_accuracy == 1.0`; record MRR and latency without treating either as a production target. The duplicate-vector acceptance is the preceding real exact-set comparison, not a search-outcome metric.

- [ ] **Step 2: Run RED in the normal environment**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_integration.py -v
  ```

  Expected: the module collects and is skipped because `RUN_KNOWLEDGE_INTEGRATION` is absent; no model path or Milvus service is accessed.

- [ ] **Step 3: Run calibration and vertical acceptance only after explicit authorization**

  Stop before this step until the user explicitly authorizes PostgreSQL, Milvus, and the local model paths. Then run only after PostgreSQL and the three Compose infrastructure services are healthy and local model paths have been intentionally provisioned:

  ```powershell
  docker compose up -d postgres etcd minio milvus
  docker compose ps
  $env:JWT_SECRET_KEY = "local-knowledge-plan-verification-secret-at-least-32"
  .\.venv\Scripts\alembic.exe upgrade head
  .\.venv\Scripts\alembic.exe current
  $env:RUN_KNOWLEDGE_INTEGRATION = "1"
  .\.venv\Scripts\python.exe scripts/calibrate_knowledge_retrieval.py --write-calibration
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_integration.py -m knowledge_integration -v
  Remove-Item Env:RUN_KNOWLEDGE_INTEGRATION
  ```

  The script evaluates each finite candidate across dense, sparse, hybrid, and hybrid+rereank paths, selects deterministically, and atomically writes the final `calibration.json` only if its selected hybrid+rereank metrics meet Recall@10 >= 90% and citation document/version accuracy = 100%. The opt-in test then reloads that exact file and passes the PostgreSQL lease, monotonic activation, interrupted-preactivation replay, exact-key idempotency, citation, and fixed-set metric assertions. It does not access DeepSeek or any external service. If either threshold fails, the script leaves `calibration.json` absent or unchanged, this task must not be committed or marked complete, and execution stops for review.

- [ ] **Step 4: Run the full non-network regression suite after the real gate passes**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest -v
  .\.venv\Scripts\python.exe -m compileall backend scripts tests
  $env:JWT_SECRET_KEY = "local-knowledge-plan-verification-secret-at-least-32"
  .\.venv\Scripts\alembic.exe check
  git diff --check
  git show --check HEAD
  ```

  Expected: ordinary tests pass with `knowledge_integration` skipped, compilation succeeds, Alembic has no drift, and Git checks are clean. Do not reach this commit step if Step 3 has not received explicit authorization or did not write a threshold-valid `calibration.json`.

- [ ] **Step 5: Commit the opt-in acceptance coverage**

  ```powershell
  git add backend/knowledge_evaluation.py scripts/calibrate_knowledge_retrieval.py data/knowledge/evaluation/calibration.json tests/test_knowledge_integration.py
  git commit -m "test: verify hybrid knowledge retrieval"
  ```

## Specification Coverage Matrix

| Approved requirement | Plan coverage |
|---|---|
| `/model/` and upload Git boundary, local offline BGE-M3/reranker paths | Task 1 and Task 5 |
| Milvus Standalone, etcd, MinIO, no Compose Worker | Task 1 |
| Three PostgreSQL tables, `0002` base to `0003_knowledge_retrieval`, named constraints, version idempotency key | Task 2 |
| `chunk_metadata` Python mapping to column `metadata`; duplicate paragraphs retain positions | Tasks 2 through 6 |
| Local PDF/DOCX/Markdown/TXT ingestion, 20 MiB/200-page/1,000,000-character bounds, symlink protection, parse deadline, deterministic chunks | Task 3 |
| Retryable/non-retryable/expired/third-attempt lifecycle, version/creator idempotency, and owner protection | Task 4 |
| Stable IDs, idempotent Milvus upsert, `CancelledError` interrupted-preactivation replay after lease expiry, old-owner rejection, exact-key cleanup, inactive/orphan invisibility | Tasks 4, 5, and 8 |
| Newer-version monotonic activation and disabled superseded version | Tasks 4 and 8 |
| BGE-M3 dense+sparse 1024 vectors, local reranker, configured deadlines, no automatic download | Task 5 |
| Active-version/category filtering in PostgreSQL, separate dense/sparse score collection, nullable absent-stage scores, deterministic client RRF/final-score ordering, canonical citations | Task 6 |
| Cheap replaceable loader before the route body, cached model/index/calibration tuple only after non-empty active IDs, explicit Worker construction, no zero-active `zero_hit` load | Tasks 5 through 7 |
| Platform-neutral Chinese rules, fixed queries, finite candidates without premature selected calibration | Task 6 |
| Administrator imports/list/versions/disable via direct role dependencies, version `Idempotency-Key` 200/202 contract, read-only role search | Task 7 |
| Knowledge-only HTTP and `RequestValidationError` envelopes with safe request ID, stable timeout/dependency/validation categories, non-knowledge preservation | Task 7 |
| Stdlib safe audit logging with request/actor/resource/status/code/duration and no sensitive data | Tasks 4 and 7 |
| Explicitly authorized real four-path evaluation, atomic final calibration, Recall@10/citation gate, exact duplicate-vector-key set | Task 8 |
| Mock-only ordinary tests and explicit PostgreSQL/Milvus/local-model acceptance | Tasks 1 through 8 |
| No analysis Worker change, no queues, microservices, frontend, platform integration, or optimization API | Global Constraints and every task file list |

## Plan Self-Review

- [x] Read the approved knowledge retrieval specification section by section and map every required subsystem, state invariant, API, error category, evaluation target, and exclusion above.
- [x] Check all task files against the repository paths and existing FastAPI, SQLAlchemy, Alembic, authentication, Worker, seed, Compose, and test patterns.
- [x] Verify cross-task signatures: `KnowledgeVersionStatus`, `KnowledgeDocumentVersion.idempotency_key`, `KnowledgeChunk.chunk_metadata`, `ChunkDraft.chunk_metadata`, owner-guarded `upsert_knowledge_chunks`, stable chunk IDs, lease function parameters, deadline behavior, `KnowledgeDependencyError`, separate dense/sparse score methods, `FusedRetrievalScore`, nullable stage scores, typed evaluation outcomes, `KnowledgeSearchLoader`, route dependencies, error categories, and opt-in marker name remain identical.
- [x] Scan this plan for incomplete markers and vague cross-task directions; each task contains named files, executable RED/GREEN commands, expected outcomes, minimal implementation steps, and a standalone commit.
- [x] Confirm the plan adds neither a provider/factory framework nor a future queue/service abstraction, keeps local model creation lazy or explicit as required, and preserves the existing analysis workflow/Worker untouched.

## Full Acceptance Command Set

Run these only while executing the approved plan, after every task-level validation has passed:

```powershell
$env:JWT_SECRET_KEY = "local-knowledge-plan-verification-secret-at-least-32"
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe current
.\.venv\Scripts\alembic.exe check
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m compileall backend scripts tests
docker compose config
git diff --check
git status --short
```

Run `RUN_KNOWLEDGE_INTEGRATION=1` only for Task 8 after explicit local-service/model authorization. No real DeepSeek smoke or other external model request belongs to this plan.

After that explicit authorization, Task 8 additionally requires the successful gate below before staging `calibration.json`:

```powershell
$env:RUN_KNOWLEDGE_INTEGRATION = "1"
.\.venv\Scripts\python.exe scripts/calibrate_knowledge_retrieval.py --write-calibration
.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_integration.py -m knowledge_integration -v
Remove-Item Env:RUN_KNOWLEDGE_INTEGRATION
```

If the chosen real configuration is below Recall@10 90% or citation document/version accuracy 100%, do not stage or commit `calibration.json`.

## Resume and Representation Boundary

This plan ends after the versioned demonstration knowledge API, recoverable local indexing, hybrid retrieval, reranking, citations, deterministic evaluation, and fault routing are verified. It does not start product selection, product optimization, compliance approval, publishing, frontend development, or real platform integration. Resume summaries and resumes may describe only verified local demonstration capabilities, never a production SLA, an official rule corpus, real platform access, or unimplemented work.
