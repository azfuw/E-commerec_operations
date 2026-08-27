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
- Search and administration reload the current database `User`. Only administrators change knowledge; operator, supervisor, and administrator may read search. Knowledge is global demonstration data and has no store-scope parameter.
- Do not add Redis, Celery, Kafka, MCP, microservices, a Compose Worker service, frontend code, real platform access, cloud deployment, generated-model APIs, automatic model downloads, or a product-selection/optimization interface.
- Never log or return upload text, server paths, model weights, vectors, credentials, Authorization values, cookies, prompts, raw dependency responses, or chain-of-thought data.

## Existing Structure to Reuse

- `backend/database.py` provides `Base`, `async_session_factory`, and `get_session`; `tests/conftest.py` creates the same metadata against SQLite with foreign keys enabled.
- `backend/models.py` and `alembic/versions/0001_initial_schema.py` use string UUIDs, `utc_now`, `native_enum=False`, explicit named checks, and explicit named unique constraints. Revision `0003` follows that convention.
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
| `data/knowledge/evaluation/calibration.json` | Versioned deterministic fusion/threshold selection grid and selected evaluation result metadata. |
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
    # parser_version, chunker_version, embedding_version, error_code,
    # created_at, updated_at

class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    # id is the stable chunk_id, version_id, chunk_index, chunk_hash,
    # canonical_text, metadata, token_count, created_at
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

  Assert duplicate `(created_by, idempotency_key)`, duplicate `(document_id, version_number)`, duplicate `(document_id, sha256)`, `attempt_count=-1`, `attempt_count=4`, an invalid status value, and a non-processing row with a lease all fail. Assert a duplicate `(version_id, chunk_index)` fails, while two chunks with the same `chunk_hash` and different indexes persist. Assert `KnowledgeChunk.id` is supplied as a stable chunk ID and is globally unique.

- [ ] **Step 2: Run RED**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_models.py -v
  ```

  Expected: collection fails because the enum and three ORM models do not exist.

- [ ] **Step 3: Add the models and revision `0003`**

  Map the three classes with the existing `String(36)` UUID convention, `utc_now` timestamps, `JSON` for chunk metadata, and explicit constraints:

  ```python
  UniqueConstraint("document_id", "version_number", name="uq_knowledge_document_versions_document_id_version_number")
  UniqueConstraint("document_id", "sha256", name="uq_knowledge_document_versions_document_id_sha256")
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
    metadata: dict[str, object]
    token_count: int

class KnowledgeContentError(ValueError):
    code: str

async def read_and_validate_upload(upload: UploadFile) -> ValidatedKnowledgeUpload:
    pass

def store_validated_upload(
    upload: ValidatedKnowledgeUpload, *, upload_dir: Path, document_id: str, version_id: str
) -> StoredKnowledgeUpload:
    pass

def parse_and_chunk(
    stored: StoredKnowledgeUpload, *, token_count: Callable[[str], int]
) -> list[ChunkDraft]:
    pass

def stable_chunk_id(version_sha256: str, chunk_index: int, chunk_hash: str) -> str:
    pass
```

`token_count` is a direct callable boundary: ordinary tests pass a deterministic local counter; the production Worker passes the BGE-M3 tokenizer method defined in Task 5. It is not a provider framework.

- [ ] **Step 1: Write failing content-boundary tests**

  Create tests with `UploadFile` backed by `io.BytesIO` and `tmp_path`. Cover accepted UTF-8 `.txt` and `.md`, malformed UTF-8, mismatched extension/MIME, empty input, input larger than 20 MiB, `../name.pdf`, absolute filenames, encrypted or over-200-page PDF, malformed PDF, malformed DOCX ZIP, a ZIP with more than 1,000 entries, an entry or total uncompressed payload over 100 MiB, and empty extracted text. Assert each failure has the exact safe code from the specification.

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

  Use `Path.resolve()` and `is_relative_to()` to verify the generated path remains under `upload_dir`; never use the client filename in the path. Validate extension, declared MIME, and file structure before writing. Parse PDFs with `pypdf.PdfReader(BytesIO(data))`; reject encrypted files, more than 200 pages, extraction errors, and no normalized text. Inspect DOCX with `zipfile.ZipFile` before `docx.Document`, applying the stated entry and decompression limits. Decode Markdown/TXT strictly as UTF-8.

  Normalize whitespace, retain title hierarchy/page/paragraph metadata, split by headings then paragraphs, split oversized paragraphs by sentence boundaries with 64-token overlap, and finally use Unicode character boundaries. Derive `chunk_hash` from canonical text and derive `chunk_id` with one fixed UUID5 namespace over `version_sha256`, `chunk_index`, and `chunk_hash`.

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
    session: AsyncSession, *, version_id: str, chunks: list[ChunkDraft]
) -> None:
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

`create_document_version()` returns `created=False` for an existing `(document_id, sha256)` and creates no lease. A non-empty creation idempotency key is unique per creator; repeating it with the same content returns its first document/version and repeating it with a different SHA returns `KNOWLEDGE_IDEMPOTENCY_CONFLICT` without a write. `return_version_for_retry()` turns attempts 1 or 2 into `accepted`; at attempt 3 it writes `failed/KNOWLEDGE_ATTEMPTS_EXHAUSTED`. `fail_knowledge_version()` is the non-retryable path. All owner mutations return `False` without committing a stale-owner change.

- [ ] **Step 1: Write failing lease and activation tests**

  Create exact SQLite rows with distinct `created_at` values. Assert claim order is `(created_at, id)`, an accepted row is claimed with attempt 1, an expired attempt-2 row is reclaimed with attempt 3, and an expired attempt-3 row becomes `failed/KNOWLEDGE_ATTEMPTS_EXHAUSTED` without a fourth claim. Assert `failed` and `disabled` rows are never claimed.

  Assert a retryable attempt-1 failure returns to `accepted` with cleared lease, a non-retryable failure reaches `failed`, and an old owner cannot renew, retry, fail, upsert completion metadata, or activate after a replacement owner is installed. Assert a repeated `(created_by, idempotency_key, sha256)` returns the initial document/version and a key with a different SHA returns `KNOWLEDGE_IDEMPOTENCY_CONFLICT`. Persist the same `ChunkDraft` list twice and assert count remains unchanged by `(version_id, chunk_index)`.

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

  Implement the activation transaction by locking the document row, checking `enabled=true`, loading the current version number, and applying this exact condition:

  ```python
  current is None or current.version_number < candidate.version_number
  ```

  When true, set candidate active, assign `current_version_id`, and disable the previous active version in the same transaction. When false, owner-guard the candidate to `disabled/KNOWLEDGE_VERSION_SUPERSEDED`; do not change the document current version. `disable_knowledge_document()` locks the document and atomically clears `current_version_id`, disables accepted/processing/active rows, and clears every lease.

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
    def token_count(self, text: str) -> int:
        pass
    def embed_documents(self, texts: list[str]) -> list[HybridVector]:
        pass
    def embed_query(self, text: str) -> HybridVector:
        pass
    def rerank(self, query: str, texts: list[str]) -> list[float]:
        pass

class MilvusKnowledgeIndex:
    def ensure_collection(self) -> None:
        pass
    def upsert(self, chunks: list[IndexedChunk]) -> None:
        pass
    def hybrid_search(
        self, *, vector: HybridVector, version_ids: list[str], categories: list[str] | None,
        limit: int, fusion: dict[str, object]
    ) -> list[dict[str, object]]:
        pass

async def run_once(
    session_factory: async_sessionmaker[AsyncSession], *, settings: Settings,
    lease_owner: str, models: LocalKnowledgeModels | None = None,
    index: MilvusKnowledgeIndex | None = None,
) -> str | None:
    pass
```

`LocalKnowledgeModels` accepts only the two configured local `Path` values. It verifies that each is a directory before constructing `BGEM3FlagModel` and `FlagReranker`; a missing or load-failed path raises `KnowledgeDependencyError("KNOWLEDGE_MODEL_UNAVAILABLE", retryable=True)`, never a download request. `MilvusKnowledgeIndex` represents one fixed collection, not a pluggable provider layer.

- [ ] **Step 1: Write failing Worker tests with real SQLite facts and fake dependencies**

  Create a document/version whose `storage_path` points to a temporary Markdown file. Use a fake `LocalKnowledgeModels` that returns 1024-length dense lists, sparse dictionaries, and a token count, plus a recording `MilvusKnowledgeIndex` fake. Test that one `run_once()`:

  ```python
  processed = await run_once(
      factory, settings=settings, lease_owner="worker-a", models=fake_models, index=fake_index
  )
  assert processed == version.id
  assert fake_index.upserts[0][0].chunk_id == persisted_chunk.id
  ```

  Assert Milvus receives only `chunk_id`, document/version/category metadata, and dense/sparse vectors; canonical text, file path, user ID, and error code are absent. Assert upsert runs before `upsert_knowledge_chunks()` and activation, replaying the Worker does not add vector keys or chunk rows, and only an active version becomes searchable later.

  Make the fake model raise retryable timeout/unavailable errors on attempts 1 and 2, then assert `accepted` with its safe code; make it fail on attempt 3 and assert `failed/KNOWLEDGE_ATTEMPTS_EXHAUSTED`. Make parsing raise `KnowledgeContentError("KNOWLEDGE_PARSE_FAILED")` and assert terminal failed. Replace the lease owner during renewal and assert no further model call, Milvus upsert, chunk persistence, activation, or overwrite occurs.

- [ ] **Step 2: Run RED**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_worker.py -v
  ```

  Expected: collection fails because the index and Worker modules do not exist.

- [ ] **Step 3: Implement local index operations and one-version Worker**

  `LocalKnowledgeModels` calls local `FlagEmbedding` APIs with the two configured filesystem paths, checks `len(dense) == 1024`, and converts BGE-M3 lexical weights into `dict[int, float]`. `MilvusKnowledgeIndex.ensure_collection()` creates one collection named by `settings.milvus_collection` with primary `chunk_id`, `document_id`, `version_id`, category, `FLOAT_VECTOR` dimension 1024, and `SPARSE_FLOAT_VECTOR`; it creates the documented dense and sparse indexes. `upsert()` uses stable `chunk_id` primary keys.

  `run_once()` claims at most one version using `claim_next_knowledge_version()`. Before parsing, embedding, and Milvus upsert, call `renew_knowledge_lease()`; a false result stops immediately. Process in this fixed order:

  ```text
  parse + deterministic chunk -> BGE-M3 dense+sparse -> Milvus stable-ID upsert
  -> PostgreSQL chunk upsert -> PostgreSQL monotonic activation
  ```

  Translate only explicit local-model/Milvus deadline, unavailable, and retryable connection failures to retryable `KnowledgeDependencyError`; translate parsing/security errors to terminal codes; translate any other processing exception to `KNOWLEDGE_PROCESSING_FAILED`. The Worker never deletes Milvus vectors to recover a run. Add a Windows-safe `scripts/run_knowledge_worker.py` command mirroring `scripts/run_analysis_worker.py`, with `--once`, hostname/PID owner, and a one-second idle sleep; it does not print settings or credentials.

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
- Create: `data/knowledge/evaluation/calibration.json`
- Create: `tests/test_knowledge_search.py`
- Create: `tests/test_knowledge_evaluation.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class KnowledgeSearchHit:
    chunk_id: str
    document_name: str
    version_number: int
    category: str
    canonical_text: str
    dense_score: float
    sparse_score: float
    fusion_score: float
    reranker_score: float
    final_score: float

@dataclass(frozen=True)
class KnowledgeSearchOutcome:
    quality_status: Literal["normal", "zero_hit", "low_confidence"]
    hits: list[KnowledgeSearchHit]

async def search_active_knowledge(
    session: AsyncSession, *, query: str, categories: list[str] | None, top_k: int,
    models: LocalKnowledgeModels, index: MilvusKnowledgeIndex,
    calibration: dict[str, object],
) -> KnowledgeSearchOutcome:
    pass

def evaluate_retrieval(
    queries: list[dict[str, object]], outcomes: dict[str, KnowledgeSearchOutcome]
) -> dict[str, object]:
    pass

def select_calibration(candidates: list[dict[str, object]]) -> dict[str, object]:
    pass
```

`select_calibration()` ranks candidate configurations by Recall@10 descending, MRR descending, lower retrieval candidate count, then lexicographic JSON form. This deterministic selector is the only way `calibration.json` receives its selected fusion strategy and threshold; no copied weight is accepted.

- [ ] **Step 1: Write failing search and evaluation tests**

  Add a SQLite active document/version/chunk plus an inactive old version and an orphan chunk. Use recording fake models/indexes. Assert `search_active_knowledge()` returns `zero_hit` without calling Milvus when no active IDs exist; otherwise it sends only active version IDs and optional categories to hybrid search, reloads canonical text only from PostgreSQL, rejects a Milvus `chunk_id` outside the active set, reranks only canonical texts, and includes every stage score in descending final-score order.

  Make fakes raise `KnowledgeDependencyError("KNOWLEDGE_DEPENDENCY_TIMEOUT", retryable=True)` and `KnowledgeDependencyError("KNOWLEDGE_MILVUS_UNAVAILABLE", retryable=True)`; assert the caller can distinguish timeout from dependency error and no hit is manufactured. Test fixed labeled outcomes where two calibration candidates tie on Recall@10 and MRR; assert the lower candidate count wins. Assert evaluation emits exact `recall_at_10`, `mrr`, `citation_document_version_accuracy`, `duplicate_vector_keys`, and latency fields.

- [ ] **Step 2: Run RED**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_search.py tests/test_knowledge_evaluation.py -v
  ```

  Expected: collection fails because search/evaluation modules and committed data files do not exist.

- [ ] **Step 3: Add platform-neutral rules and deterministic retrieval behavior**

  Write one concise Chinese Markdown rule pack. Its heading and every rule section must say `项目演示规则：平台中立、非官方法规或平台规范`. Include title keywords, selling-point/detail evidence, prohibited or exaggerated claims, category attributes, SKU/price/spec consistency, and after-sales/refund wording. Do not state or imply an official policy.

  Add fixed Chinese queries with expected rule section/document/version assertions. Define a finite calibration grid in `calibration.json` for dense-only, sparse-only, reciprocal-rank-fusion hybrid, and hybrid-plus-rerank evaluation; write the selector's chosen values and a `config_version` only through `select_calibration()`. The file includes no secret, model output, user upload, or platform rule.

  `search_active_knowledge()` first queries PostgreSQL for enabled documents and active version IDs. It invokes BGE-M3 query embedding, calls `MilvusKnowledgeIndex.hybrid_search()` with the selected configuration, fetches chunk text and version/document metadata by returned IDs from PostgreSQL, reranks locally, and returns `normal`, `low_confidence`, or `zero_hit`. An empty active set does not invoke Milvus. A returned ID outside active versions is discarded. A dependency exception is propagated unchanged for the API to classify; it never becomes a synthetic citation.

- [ ] **Step 4: Run GREEN and retrieval regressions**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_search.py tests/test_knowledge_evaluation.py tests/test_knowledge_worker.py -v
  ```

  Expected: search/evaluation tests pass using SQLite and fakes only; no model, Milvus, or external network access occurs.

- [ ] **Step 5: Commit rules and retrieval core**

  ```powershell
  git add backend/knowledge_search.py backend/knowledge_evaluation.py data/knowledge/demo/platform-neutral-rules.md data/knowledge/evaluation/queries.json data/knowledge/evaluation/calibration.json tests/test_knowledge_search.py tests/test_knowledge_evaluation.py
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
```

```python
@router.post("/knowledge/documents", status_code=status.HTTP_202_ACCEPTED)
async def create_knowledge_document(
    name: Annotated[str, Form(min_length=1, max_length=128)],
    category: Annotated[str, Form(min_length=1, max_length=64)],
    file: UploadFile,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeEnvelope:
    pass

@router.post("/knowledge/documents/{document_id}/versions", status_code=status.HTTP_202_ACCEPTED)
async def create_knowledge_document_version(
    document_id: str,
    file: UploadFile,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeEnvelope:
    pass

@router.post("/knowledge/documents/{document_id}/disable")
async def disable_knowledge_document_route(
    document_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeEnvelope:
    pass

@router.post("/knowledge/search")
async def search_knowledge_route(
    request: KnowledgeSearchRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeEnvelope:
    pass
```

The two upload routes accept `name`/`category` form fields only for document creation and an `UploadFile` field named `file`; later versions inherit document name/category. List pagination defaults to `page=1`, `page_size=20`, and rejects values outside `1..100`.

Exact route map: `POST /knowledge/documents`, `GET /knowledge/documents`, `POST /knowledge/documents/{document_id}/versions`, `POST /knowledge/documents/{document_id}/disable`, and `POST /knowledge/search`.

- [ ] **Step 1: Write failing ASGI API and RBAC tests**

  Create `tests/test_knowledge_api.py` using the real FastAPI app, a SQLite `get_session` override, real `create_access_token()`, and actual database role changes. Use a temporary upload directory in a copied `Settings`; monkeypatch only `LocalKnowledgeModels` and `MilvusKnowledgeIndex` so no weight or service is loaded.

  Assert an administrator can create a Markdown document and receives a `202` envelope with `accepted`, document/version IDs, and no path. Repeat the creation with the same client idempotency key and SHA and assert it returns the existing version without a second row, upload path, or lease; repeat the key with a different SHA and assert `409/KNOWLEDGE_IDEMPOTENCY_CONFLICT`. Repeat a version SHA for an existing document and assert it returns that existing version without a second row or lease. Assert operator and supervisor receive 403 envelopes for all three modifying routes, while all three active roles may search. Remove a user's active status after issuing its JWT and assert 401.

  Assert list responses paginate and omit `storage_path`, `lease_owner`, and upload bytes. Assert an unknown document is 404, a disabled document rejects a new version with 409, repeated disable is 200, and disabled documents disappear from search immediately. For search, assert query length and top-k validation produce `validation_error`, no active version produces `200/zero_hit`, active fake results return PostgreSQL text plus document/version/chunk citations and stage scores, a timeout produces exactly `503` with `error.category="timeout"` and `KNOWLEDGE_DEPENDENCY_TIMEOUT`, and an unavailable fake produces exactly `503` with `error.category="dependency_error"` and `KNOWLEDGE_MILVUS_UNAVAILABLE`. Neither error returns hits.

- [ ] **Step 2: Run RED**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_api.py -v
  ```

  Expected: collection fails because knowledge schemas and routes do not exist.

- [ ] **Step 3: Implement only the five contract routes**

  Extend `backend.schemas` with concrete document, version, citation, search-result, error, and envelope models. In `backend.routes`, use `Depends(get_current_user)` then `require_roles(UserRole.ADMIN)` for mutations; search accepts `OPERATOR`, `SUPERVISOR`, or `ADMIN` through the current database `User`. Route code first calls `read_and_validate_upload()`, then checks the creation idempotency key or existing document SHA before generating IDs or writing a path. Only a new version calls `store_validated_upload()`, followed by `create_document_version()` and one database commit; a transaction failure removes only that exact just-written path. A repeated key with a different SHA returns `409/KNOWLEDGE_IDEMPOTENCY_CONFLICT` without a write.

  Add a path-scoped HTTP exception handler in `backend.main.create_app()`: for paths beginning `/knowledge/`, map authentication, authorization, and not-found exceptions to `KnowledgeEnvelope(status="error", data=None, quality=None, error=KnowledgeError(category=category, code=code, message=message))`; delegate every non-knowledge path to FastAPI's existing HTTP exception handler. Map `KnowledgeDependencyError` timeout codes to `503/timeout`, and other dependency codes to `503/dependency_error`. Keep `zero_hit` and `low_confidence` as successful search quality values, not exceptions.

- [ ] **Step 4: Run GREEN API and authentication regressions**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_api.py tests/test_auth_and_scope.py tests/test_analysis_api.py -v
  .\.venv\Scripts\python.exe -m compileall backend tests
  ```

  Expected: knowledge routes enforce real-time database roles, existing analysis/auth routes retain their current responses, and no API request starts a Worker or loads a model until search's explicit dependency is invoked.

- [ ] **Step 5: Commit the protected API**

  ```powershell
  git add backend/schemas.py backend/routes.py backend/main.py tests/test_knowledge_api.py
  git commit -m "feat: add authorized knowledge retrieval API"
  ```

### Task 8: Opt-in PostgreSQL/Milvus/local-model vertical acceptance

**Files:**

- Create: `tests/test_knowledge_integration.py`

**Interfaces:**

```python
@pytest.mark.knowledge_integration
@pytest.mark.skipif(
    os.getenv("RUN_KNOWLEDGE_INTEGRATION") != "1",
    reason="explicit PostgreSQL, Milvus, and local-model integration opt-in required",
)
async def test_knowledge_vertical_slice() -> None:
    pass
```

The test uses configured `async_session_factory`, `LocalKnowledgeModels`, and `MilvusKnowledgeIndex`. It creates document/version/chunk rows with test UUID prefixes and deletes only those exact rows and their exact stable Milvus IDs in `finally`; it never drops, truncates, resets, or broadly deletes PostgreSQL, Milvus, uploads, or local models.

- [ ] **Step 1: Write the default-skipped vertical acceptance test**

  Create one test that imports the committed demonstration Markdown through the same safe upload and version functions, starts two independent PostgreSQL sessions, and uses `asyncio.gather()` to claim two accepted versions. Assert their claimed IDs are distinct, proving `FOR UPDATE SKIP LOCKED` mutual exclusion.

  Set a test version to expired attempt 2 using PostgreSQL `func.now() - text("interval '1 second'")`; assert a new owner claims attempt 3 and the old owner cannot activate it. Set another test version to expired attempt 3 and assert it becomes `failed/KNOWLEDGE_ATTEMPTS_EXHAUSTED`; assert active, failed, and disabled rows are never claimed.

  Run the real local Worker on a test version, assert its Milvus entries use stable IDs, and run it again without creating additional chunk rows or vector primary keys. Create version 2, let it activate, then complete version 1 and assert version 1 is `disabled/KNOWLEDGE_VERSION_SUPERSEDED` and never appears in an active-version filtered query. Disable the document and assert its result immediately disappears even if its vectors remain.

  Load the fixed queries, compare dense-only, sparse-only, hybrid, and hybrid+rereank outcomes through `evaluate_retrieval()`, and assert `recall_at_10 >= 0.90`, `citation_document_version_accuracy == 1.0`, and `duplicate_vector_keys == 0`. Record MRR and index/query latency in the test report without treating them as a production target.

- [ ] **Step 2: Run RED in the normal environment**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_integration.py -v
  ```

  Expected: the module collects and is skipped because `RUN_KNOWLEDGE_INTEGRATION` is absent; no model path or Milvus service is accessed.

- [ ] **Step 3: Run the explicitly authorized vertical acceptance**

  Run only after PostgreSQL and the three Compose infrastructure services are healthy and local model paths have been intentionally provisioned:

  ```powershell
  docker compose up -d postgres etcd minio milvus
  docker compose ps
  $env:JWT_SECRET_KEY = "local-knowledge-plan-verification-secret-at-least-32"
  .\.venv\Scripts\alembic.exe upgrade head
  .\.venv\Scripts\alembic.exe current
  $env:RUN_KNOWLEDGE_INTEGRATION = "1"
  .\.venv\Scripts\python.exe -m pytest tests/test_knowledge_integration.py -m knowledge_integration -v
  Remove-Item Env:RUN_KNOWLEDGE_INTEGRATION
  ```

  Expected: the opt-in test passes the PostgreSQL lease, monotonic activation, idempotent-vector, citation, and fixed-set metrics assertions. It does not access DeepSeek or any external service.

- [ ] **Step 4: Run the full non-network regression suite**

  Run:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest -v
  .\.venv\Scripts\python.exe -m compileall backend scripts tests
  $env:JWT_SECRET_KEY = "local-knowledge-plan-verification-secret-at-least-32"
  .\.venv\Scripts\alembic.exe check
  git diff --check
  git show --check HEAD
  ```

  Expected: ordinary tests pass with `knowledge_integration` skipped, compilation succeeds, Alembic has no drift, and Git checks are clean.

- [ ] **Step 5: Commit the opt-in acceptance coverage**

  ```powershell
  git add tests/test_knowledge_integration.py
  git commit -m "test: verify hybrid knowledge retrieval"
  ```

## Specification Coverage Matrix

| Approved requirement | Plan coverage |
|---|---|
| Local PDF/DOCX/Markdown/TXT ingestion, secure paths, parsing, deterministic chunks | Tasks 1 and 3 |
| `/model/` and upload Git boundary, local offline BGE-M3/reranker paths | Task 1 and Task 5 |
| Milvus Standalone, etcd, MinIO, no Compose Worker | Task 1 |
| Three PostgreSQL tables, exact status/lease checks, explicit migration | Task 2 |
| Retryable/non-retryable/expired/third-attempt lifecycle and owner protection | Task 4 |
| Stable IDs, idempotent Milvus upsert, inactive/orphan invisibility | Tasks 4, 5, and 8 |
| Newer-version monotonic activation and disabled superseded version | Tasks 4 and 8 |
| BGE-M3 dense+sparse 1024 vectors, hybrid retrieval, local reranker | Tasks 5 and 6 |
| Active-version filtering, canonical PostgreSQL citations, calibrated quality routing | Task 6 |
| Administrator imports/list/versions/disable, read-only role search, real-time RBAC | Task 7 |
| Envelope with request ID, status, data, quality, stable timeout/dependency categories | Task 7 |
| Platform-neutral Chinese rules, fixed query set, Recall@10/citation/duplicate-vector evaluation | Tasks 6 and 8 |
| Mock-only ordinary tests and explicit PostgreSQL/Milvus/local-model acceptance | Tasks 1 through 8 |
| No analysis Worker change, no queues, microservices, frontend, platform integration, or optimization API | Global Constraints and every task file list |

## Plan Self-Review

- [x] Read the approved knowledge retrieval specification section by section and map every required subsystem, state invariant, API, error category, evaluation target, and exclusion above.
- [x] Check all task files against the repository paths and existing FastAPI, SQLAlchemy, Alembic, authentication, Worker, seed, Compose, and test patterns.
- [x] Verify cross-task signatures: `KnowledgeVersionStatus`, `KnowledgeDocumentVersion`, `ChunkDraft`, stable chunk IDs, lease function parameters, `KnowledgeDependencyError`, search result fields, error categories, and opt-in marker name remain identical.
- [x] Scan this plan for incomplete markers and vague cross-task directions; each task contains named files, executable RED/GREEN commands, expected outcomes, minimal implementation steps, and a standalone commit.
- [x] Confirm the plan adds neither a provider factory nor a future queue/service abstraction and preserves the existing analysis workflow/Worker untouched.

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

## Resume and Representation Boundary

This plan ends after the versioned demonstration knowledge API, recoverable local indexing, hybrid retrieval, reranking, citations, deterministic evaluation, and fault routing are verified. It does not start product selection, product optimization, compliance approval, publishing, frontend development, or real platform integration. Resume summaries and resumes may describe only verified local demonstration capabilities, never a production SLA, an official rule corpus, real platform access, or unimplemented work.
