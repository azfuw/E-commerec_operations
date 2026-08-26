# Durable Analysis Agent Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the authorized, durable analysis path from an accepted API request through a PostgreSQL-leased LangGraph Worker to trustworthy candidates in `awaiting_selection`.

**Architecture:** Keep FastAPI and the Worker as two processes in this repository. FastAPI only creates and reads workflow facts after existing server-side JWT and store-scope checks; the Worker claims one PostgreSQL lease at a time, runs a checkpointed LangGraph with `workflow_run.id` as its thread ID, then writes idempotent candidates and safe call metadata. Deterministic analytics remain the fact source, while DeepSeek supplies only bounded explanatory fields that the server validates and reconciles.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, SQLAlchemy async, Alembic, PostgreSQL 16, SQLite test fixture, LangGraph with its PostgreSQL checkpointer, `httpx`, pytest, pytest-asyncio, and `httpx.MockTransport`.

**Spec:** `docs/superpowers/specs/2026-08-26-durable-analysis-agent-design.md`

## Global Constraints

- Work only in `D:\E-commerce_operations` and use `D:\E-commerce_operations\.venv\Scripts\python.exe` for all Python commands.
- Add exactly three application business tables: `workflow_runs`, `analysis_candidates`, and `agent_calls`; only the LangGraph PostgreSQL integration creates its own checkpoint tables.
- Do not introduce Redis, Celery, Kafka, MCP, microservices, frontend code, Milvus/RAG, local models, real commerce-platform integration, Docker API usage, or a Compose Worker service.
- Do not create product-selection, optimization, approval, compliance, publishing, knowledge-base, or RAG interfaces. This slice ends at `awaiting_selection`.
- Reuse `get_current_user()` and `require_store_access()` for every new API access. JWT claims never replace the current database `User`, scope, or enabled-store check.
- `workflow_runs.status` is limited to `accepted`, `processing`, `awaiting_selection`, and `failed`; its quality schema permits `normal`, `partial`, and `degraded`, while this implementation writes only `normal` or `degraded`.
- Lease selection, expiry, renewal, and stale-owner protection use PostgreSQL `now()` plus `FOR UPDATE SKIP LOCKED`; an accepted or expired processing run is claimed at most three times and terminal rows are never claimed.
- Call all five existing read-only functions in `backend.analytics`: `get_store_summary`, `find_anomalous_products`, `get_product_metrics`, `compare_store_products`, and `get_inventory_risk`. Their already-tested store and date behavior remains unchanged.
- DeepSeek is called directly with `httpx`; the default model is exactly `deepseek-v4-flash`, and `DEEPSEEK_MODEL` may override it. The API must start without a DeepSeek key; only the Worker call node checks for it.
- No database row, API response, test assertion output, or log message may contain an API key, `Authorization` value, password, complete token, complete prompt, complete model response, or chain of thought.
- Every normal LLM test uses `httpx.MockTransport`; the sole real `deepseek-v4-flash` smoke test is skipped by default and runs only through its explicit opt-in command.
- Use manual, named SQL `CheckConstraint`s for every application enum as in the existing schema; do not rely on type-bound enum checks or hide Alembic drift in `alembic/env.py`.

## Existing Structure to Reuse

- `backend/auth.py` already decodes only the configured JWT algorithm, reloads the active `User` from the database, and exposes `require_store_access(store_id, user, session)`. New routes call these directly.
- `backend/database.py` supplies `Base`, `async_session_factory`, and `get_session`; `tests/conftest.py` builds the same metadata against SQLite with foreign keys enabled.
- `backend/routes.py` has the single project router and current dependency style. Add the three analysis endpoints here instead of creating another FastAPI application or router hierarchy.
- `backend/models.py` and `alembic/versions/0001_initial_schema.py` use explicit tables, named constraints, string UUIDs, and non-native enums with separately named checks. The new `0002` migration follows that pattern.
- `backend/analytics.py` already provides the deterministic five-tool fact layer. `find_anomalous_products(..., limit=5)` supplies the server-selected candidate IDs, anomaly types, impacts, and evidence.
- `backend/schemas.py` already carries Pydantic response models and decimal metrics. Extend it for request, workflow, trusted-fact, draft, and candidate representations rather than introducing a second schema package.
- `backend/seed.py` deterministically produces 5 top candidate facts for the flagship store over `2026-07-26` through `2026-08-24`; use it for integration acceptance data.

---

### Task 1: Durable workflow persistence and explicit migration

**Files:**

- Modify: `backend/common.py`
- Modify: `backend/models.py`
- Create: `alembic/versions/0002_durable_analysis.py`
- Create: `tests/test_analysis_models.py`

**Interfaces:**

```python
# backend/common.py
class WorkflowStatus(StrEnum):
    ACCEPTED = "accepted"
    PROCESSING = "processing"
    AWAITING_SELECTION = "awaiting_selection"
    FAILED = "failed"

class WorkflowQuality(StrEnum):
    NORMAL = "normal"
    PARTIAL = "partial"
    DEGRADED = "degraded"

class AgentCallType(StrEnum):
    PRIMARY = "primary"
    SCHEMA_REPAIR = "schema_repair"
```

```python
# backend/models.py
class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    # id, workflow_type, store_id, created_by, start_date, end_date,
    # status, quality_status, attempt_count, lease_owner,
    # lease_expires_at, current_step, input, output, quality,
    # error_code, created_at, updated_at

class AnalysisCandidate(Base):
    __tablename__ = "analysis_candidates"
    # id, workflow_run_id, product_id, rank, product_code, anomaly_types,
    # metrics, business_impact, evidence, impact_explanation, reason,
    # recommended_action, confidence, created_at, updated_at

class AgentCall(Base):
    __tablename__ = "agent_calls"
    # id, workflow_run_id, node_name, call_type, attempt, model,
    # prompt_version, status, input_hash, prompt_tokens,
    # completion_tokens, total_tokens, duration_ms, estimated_cost,
    # error_code, created_at
```

`WorkflowRun.input`, `output`, and `quality` are JSON values. Use `Numeric(14, 2)` for candidate `business_impact`, `Numeric(5, 4)` for candidate `confidence`, and `Numeric(14, 6)` for nullable `estimated_cost`. `AgentCall.attempt=0` is reserved for a deterministic degradation decision; external `primary` and `schema_repair` attempts start at 1. This permits `agent_calls.call_type` to remain exactly `primary|schema_repair` while recording the degradation decision under `call_type=primary, attempt=0`.

- [ ] **Step 1: Write the failing persistence tests**

Create `tests/test_analysis_models.py`. First insert the referenced store, user, and product through the existing SQLite `session` fixture, then insert a valid run, candidate, and call and assert defaults and valid values:

```python
async def test_workflow_and_candidate_constraints(session) -> None:
    store = Store(id="store-1", name="旗舰店", code="flagship")
    user = User(id="user-1", username="operator", password_hash="hash", role=UserRole.OPERATOR)
    product = Product(
        id="product-1", store_id=store.id, code="FLAGSHIP-001", title="商品", category="数码"
    )
    session.add_all([store, user, product])
    await session.flush()
    run = WorkflowRun(
        id="run-1", workflow_type="analysis", store_id="store-1", created_by="user-1",
        start_date=date(2026, 7, 26), end_date=date(2026, 8, 24),
        status=WorkflowStatus.ACCEPTED, quality_status=WorkflowQuality.NORMAL,
    )
    session.add(run)
    await session.flush()
    session.add(AnalysisCandidate(
        id="candidate-1", workflow_run_id=run.id, product_id="product-1", rank=1,
        product_code="FLAGSHIP-001", anomaly_types=["low_conversion"], metrics={},
        business_impact=Decimal("1.00"), evidence=["clicks=300"],
        impact_explanation="影响说明", reason="原因", recommended_action="建议",
        confidence=Decimal("0.8000"),
    ))
    await session.flush()
```

In fresh transactions, assert `IntegrityError` for a second candidate with the same run/product, a second candidate at the same rank, rank below 1, confidence below 0 and above 1, invalid workflow status, invalid quality status, `attempt_count=-1`, `attempt_count=4`, an invalid call type, and a duplicate `AgentCall` `(workflow_run_id, node_name, call_type, attempt)` key. Assert terminal runs have no lease owner or expiry, and a `processing` run requires both lease fields.

- [ ] **Step 2: Run the model tests and observe RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_analysis_models.py -v`

Expected: collection fails because `WorkflowRun`, `AnalysisCandidate`, and `AgentCall` do not yet exist.

- [ ] **Step 3: Add enums, ORM rows, and one explicit Alembic revision**

Add the three string enums to `backend/common.py`. Add the three mapped classes at the end of `backend/models.py`, with string UUID defaults matching existing rows, UTC timestamps, and explicit named checks:

```python
CheckConstraint("workflow_type = 'analysis'", name="ck_workflow_runs_type")
CheckConstraint(
    "status IN ('accepted', 'processing', 'awaiting_selection', 'failed')",
    name="ck_workflow_runs_status",
)
CheckConstraint(
    "quality_status IN ('normal', 'partial', 'degraded')",
    name="ck_workflow_runs_quality_status",
)
CheckConstraint("start_date <= end_date", name="ck_workflow_runs_dates")
CheckConstraint("attempt_count BETWEEN 0 AND 3", name="ck_workflow_runs_attempt_count")
CheckConstraint(
    "(status = 'processing' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
    "OR (status != 'processing' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
    name="ck_workflow_runs_lease_state",
)
```

Add named checks `rank >= 1`, `confidence >= 0 AND confidence <= 1`, `call_type IN ('primary', 'schema_repair')`, `attempt >= 0`, token counts `>= 0`, and duration `>= 0`; add named unique constraints for both candidate keys and the call key. Keep enums `native_enum=False`, `create_constraint=False`, and pair them with these non-type-bound checks.

Create revision `0002` with `down_revision = "0001"`. Use explicit `op.create_table()` calls and the exact column types, foreign keys, named checks, and unique constraints above. Its `downgrade()` drops only `agent_calls`, `analysis_candidates`, and `workflow_runs`, in reverse foreign-key order. Do not add LangGraph checkpoint tables to this migration.

- [ ] **Step 4: Run GREEN tests and migration validation**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_analysis_models.py -v
$env:JWT_SECRET_KEY = "local-plan-verification-secret-at-least-32"
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe check
```

Expected: model tests pass, the database reaches revision `0002`, and Alembic reports no new upgrade operations.

- [ ] **Step 5: Commit the durable data contract**

```powershell
git add backend/common.py backend/models.py alembic/versions/0002_durable_analysis.py tests/test_analysis_models.py
git commit -m "feat: add durable analysis persistence"
```

### Task 2: Authorized analysis-run API contract

**Files:**

- Create: `backend/analysis_runs.py`
- Modify: `backend/schemas.py`
- Modify: `backend/routes.py`
- Create: `tests/test_analysis_api.py`

**Interfaces:**

```python
# backend/analysis_runs.py
async def create_analysis_run(
    session: AsyncSession,
    *,
    store_id: str,
    created_by: str,
    start_date: date,
    end_date: date,
) -> WorkflowRun: ...

async def get_workflow_run(
    session: AsyncSession, workflow_run_id: str
) -> WorkflowRun | None: ...

async def list_analysis_candidates(
    session: AsyncSession, workflow_run_id: str
) -> list[AnalysisCandidate]: ...
```

```python
# backend/schemas.py
class AnalysisRunRequest(BaseModel):
    store_id: str = Field(min_length=1, max_length=36)
    start_date: date
    end_date: date

class AnalysisRunAccepted(BaseModel):
    workflow_run_id: str
    status: Literal["accepted"]

class WorkflowRunView(BaseModel):
    id: str
    workflow_type: Literal["analysis"]
    store_id: str
    start_date: date
    end_date: date
    status: WorkflowStatus
    quality_status: WorkflowQuality
    current_step: str | None
    attempt_count: int
    candidates_ready: bool
    error_code: str | None

class AnalysisCandidateView(BaseModel):
    product_id: str
    rank: int
    product_code: str
    anomaly_types: list[str]
    metrics: ProductMetrics
    business_impact: Decimal
    evidence: list[str]
    impact_explanation: str
    reason: str
    recommended_action: str
    confidence: Decimal
```

`create_analysis_run()` creates `workflow_type="analysis"`, `status=accepted`, `quality_status=normal`, `attempt_count=0`, no lease, and JSON `input={"store_id": store_id, "start_date": start_date.isoformat(), "end_date": end_date.isoformat()}`. It does not call analytics, LangGraph, or DeepSeek. Route handlers own authorization and transaction commit; service functions do not accept a JWT or a role claim.

- [ ] **Step 1: Write the failing API tests with database-backed authorization changes**

Create `tests/test_analysis_api.py` with its own SQLite user, stores, scopes, ASGI client, and `get_session` override following `tests/test_auth_and_scope.py`. Add these executable tests:

```python
async def test_authorized_user_creates_an_accepted_run(client, operator_token) -> None:
    response = await client.post(
        "/analysis-runs",
        json={"store_id": "flagship", "start_date": "2026-07-26", "end_date": "2026-08-24"},
        headers={"Authorization": f"Bearer {operator_token}"},
    )
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
```

Assert a one-day range and a 90-day range are accepted; reversed dates and 91 days return 422. Assert missing token returns 401, a user without `UserStoreScope` receives 403, and an admin cannot create against a disabled store because `require_store_access()` returns the existing 404.

Create a valid row, then remove the token subject's scope after token issuance; `GET /workflow-runs/{id}` and `GET /analysis-runs/{id}/candidates` must return 403, proving current database scope is authoritative. Assert a creator can read only its scoped run, no lease owner is exposed, a nonterminal (`accepted`, `processing`, or `failed`) candidates request always has status 409 and `{"detail": {"code": "ANALYSIS_NOT_READY"}}`, and an `awaiting_selection` row returns rank-ordered candidate views. Assert unknown run IDs return 404 only after authentication succeeds.

- [ ] **Step 2: Run the API tests and observe RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_analysis_api.py -v`

Expected: collection fails because the request and workflow response schemas, analysis-run functions, and routes do not yet exist.

- [ ] **Step 3: Implement the narrow service functions, schemas, and routes**

Implement the three `backend.analysis_runs` functions using normal async SQLAlchemy statements. `list_analysis_candidates()` orders `AnalysisCandidate.rank` ascending. It has no authorization branch and no Worker behavior.

Add Pydantic schemas with `model_config = ConfigDict(from_attributes=True)` where an ORM response is built directly. Validate the request's inclusive duration in a model validator:

```python
days = (self.end_date - self.start_date).days + 1
if not 1 <= days <= 90:
    raise ValueError("date range must contain 1 to 90 days")
```

Add these route signatures to the existing `router`:

```python
@router.post("/analysis-runs", response_model=AnalysisRunAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_analysis_run_route(
    request: AnalysisRunRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AnalysisRunAccepted: ...

@router.get("/workflow-runs/{workflow_run_id}", response_model=WorkflowRunView)
async def read_workflow_run(
    workflow_run_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WorkflowRunView: ...

@router.get("/analysis-runs/{workflow_run_id}/candidates", response_model=list[AnalysisCandidateView])
async def read_analysis_candidates(
    workflow_run_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AnalysisCandidateView]: ...
```

For each route, load the run, call `require_store_access(run.store_id, user, session)`, then return only its safe schema. The candidates route performs that authorization before the status check, emits the stable 409 detail shown in the test for every non-ready status, and never returns a candidate for a non-ready run. Commit the create transaction only after successful validation and authorization.

- [ ] **Step 4: Run GREEN API and regression tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_analysis_api.py -v
.\.venv\Scripts\python.exe -m pytest tests/test_auth_and_scope.py tests/test_models.py -v
```

Expected: new API tests and existing JWT/store-scope tests pass; no route starts a Worker or makes an LLM request.

- [ ] **Step 5: Commit the API slice**

```powershell
git add backend/analysis_runs.py backend/schemas.py backend/routes.py tests/test_analysis_api.py
git commit -m "feat: add analysis run API"
```

### Task 3: Deterministic facts and bounded DeepSeek client

**Files:**

- Modify: `pyproject.toml`
- Modify: `.env.example`
- Modify: `backend/config.py`
- Modify: `backend/schemas.py`
- Create: `backend/analysis_agent.py`
- Create: `tests/test_analysis_agent.py`

**Interfaces:**

```python
# backend/schemas.py
class TrustedAnalysisCandidate(BaseModel):
    product_id: str
    product_code: str
    anomaly_types: list[str]
    metrics: ProductMetrics
    business_impact: Decimal
    evidence: list[str]

class AnalysisFacts(BaseModel):
    store_summary: StoreMetrics
    candidates: list[TrustedAnalysisCandidate]

class AgentCandidateDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str
    rank: int
    impact_explanation: str
    reason: str
    recommended_action: str
    confidence: Decimal = Field(ge=0, le=1)

class AgentAnalysisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[AgentCandidateDraft]
```

```python
# backend/analysis_agent.py
class AgentSchemaError(ValueError):
    pass

BeforeHttpAttempt = Callable[[AgentCallType, int], Awaitable[bool]]

@dataclass(frozen=True)
class AgentCallRecord:
    node_name: Literal["call_analysis_agent", "validate_and_reconcile"]
    call_type: AgentCallType
    attempt: int
    model: str
    prompt_version: str
    status: str
    input_hash: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    duration_ms: int
    estimated_cost: Decimal | None
    error_code: str | None

@dataclass(frozen=True)
class AgentInvocation:
    response: AgentAnalysisResponse | None
    records: list[AgentCallRecord]
    error_code: str | None

async def collect_analysis_facts(
    session: AsyncSession, store_id: str, start_date: date, end_date: date
) -> AnalysisFacts: ...

def parse_agent_response(content: str) -> AgentAnalysisResponse: ...

def validate_agent_response(
    facts: AnalysisFacts, response: AgentAnalysisResponse
) -> list[AgentCandidateDraft]: ...

def build_degraded_drafts(facts: AnalysisFacts) -> list[AgentCandidateDraft]: ...

class DeepSeekAnalysisClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None: ...

    async def request(
        self,
        facts: AnalysisFacts,
        *,
        call_type: AgentCallType,
        before_http_attempt: BeforeHttpAttempt | None = None,
    ) -> AgentInvocation: ...
```

`DeepSeekAnalysisClient.request()` sets `AgentCallRecord.node_name="call_analysis_agent"` for every `primary` transport record and `"validate_and_reconcile"` for every `schema_repair` record. The Worker creates the deterministic attempt-0 degradation record with `node_name="validate_and_reconcile"` and `call_type=primary`.

Add runtime dependency `httpx>=0.28,<1` to `[project].dependencies` and remove its duplicate from the `test` extra. Add `deepseek_api_key: SecretStr | None = None`, `deepseek_model: str = "deepseek-v4-flash"`, `deepseek_base_url: str = "https://api.deepseek.com"`, `deepseek_timeout_seconds: float = 30.0`, and `deepseek_price_per_million_tokens: Decimal | None = None` to `Settings`. Add matching non-secret names and defaults to `.env.example`; leave `DEEPSEEK_API_KEY=` blank. `estimated_cost` is `None` unless the price setting is non-null, then is `(total_tokens * price / 1_000_000)` quantized to six decimal places.

- [ ] **Step 1: Write MockTransport-only tests for fact provenance, output validation, retry, and degradation**

Create `tests/test_analysis_agent.py`. Seed the existing deterministic fixture and use `httpx.MockTransport` handlers; do not make an external request in this file. Include these focused tests:

```python
async def test_collect_facts_calls_all_existing_tools_and_preserves_server_values(
    session, monkeypatch
) -> None:
    # Wrap each imported analytics function with AsyncMock(wraps=original),
    # then assert all five were awaited and the resulting five candidates
    # retain the deterministic product_code, metrics, anomaly_types, impact, and evidence.
    facts = await collect_analysis_facts(session, flagship_id, date(2026, 7, 26), date(2026, 8, 24))
    assert len(facts.candidates) == 5
```

Use a valid structured mock response whose product IDs and ranks match `facts`; after `validate_agent_response()`, assert the only accepted model values are the four explanation fields and confidence. Compare the eventual trusted candidate construction against `facts` so mock-provided product codes, metrics, anomaly types, impact, and evidence cannot overwrite server values.

Parameterize raw invalid JSON, extra-field/schema responses, and `confidence=-0.0001` or `confidence=1.0001`; `parse_agent_response()` must raise `AgentSchemaError` for each because Pydantic validates `AgentCandidateDraft` bounds, while `DeepSeekAnalysisClient.request()` catches that exception and returns `AgentInvocation(response=None, error_code="DEEPSEEK_SCHEMA_INVALID")`. Parameterize schema-valid responses for an unknown ID, duplicate ID, missing ID, duplicate rank, and non-contiguous rank; only `validate_agent_response()` must raise `AgentSchemaError` for those trusted-set inconsistencies. Verify two 429 responses followed by 200 produce exactly three `primary` records; independently cover timeout, transport error, and 5xx with three attempts. Pass a recording `before_http_attempt` callback and assert it is awaited immediately before every attempt with `[(AgentCallType.PRIMARY, 1), (AgentCallType.PRIMARY, 2), (AgentCallType.PRIMARY, 3)]`. Assert primary records use `node_name="call_analysis_agent"`; make a `schema_repair` request and assert its record uses `node_name="validate_and_reconcile"`. Verify 401 and 403 create exactly one safe record, and no key makes zero HTTP requests with error code `DEEPSEEK_KEY_MISSING`.

Assert the fixed Chinese degraded drafts cover exactly the trusted product IDs and ranks, contain the Chinese statement `模型解释暂不可用，请人工核验。`, and carry no invented product facts. Assert every `AgentCallRecord` has a 64-character input hash and no attributes for a request header, prompt body, raw response, key, or authorization value. With no price setting, assert `estimated_cost is None`. Clear `get_settings`' cache around the settings tests, assert the default is `deepseek-v4-flash`, and assert `DEEPSEEK_MODEL` overrides only the model value.

- [ ] **Step 2: Run the agent tests and observe RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_analysis_agent.py -v`

Expected: collection fails because the fact schemas and `backend.analysis_agent` do not exist.

- [ ] **Step 3: Implement the fact collector and direct client without a provider layer**

`collect_analysis_facts()` calls the existing functions in this fixed sequence: one `get_store_summary`, one `find_anomalous_products(..., limit=5)`, then for the selected candidate IDs one `get_product_metrics` per ID, one `compare_store_products` for the full selected ID list, and one `get_inventory_risk` per ID. Construct `TrustedAnalysisCandidate` solely from their results; preserve the anomaly function's candidate order and evidence. Raise a normal Python exception if any deterministic call fails so the Worker can classify it as a fact failure.

Build a canonical, non-secret JSON representation of `AnalysisFacts` with sorted object keys and compute `sha256(...).hexdigest()` for `input_hash`. Send the JSON request body only to the compatible Chat Completions endpoint with `POST /chat/completions`; it contains the approved facts and a response schema asking only for `product_id`, `rank`, `impact_explanation`, `reason`, `recommended_action`, and `confidence`, excludes credentials, and is neither persisted nor logged.

Import `Awaitable` and `Callable` from `collections.abc`. `DeepSeekAnalysisClient.request()` uses one local `httpx.AsyncClient` with the configured base URL, timeout, transient bearer header, and `POST /chat/completions`. Before the initial request and before every retry, it awaits `before_http_attempt(call_type, attempt)`. If the callback returns `False`, it sends no HTTP request, records no new external attempt, and returns `AgentInvocation(response=None, error_code="LEASE_LOST")`; the Worker then stops without fallback or writes. It retries only `httpx.TimeoutException`, `httpx.TransportError`, HTTP 429, and HTTP 500 through 599: initial request plus two retries. It records safe metadata for every sent request. It makes no retry for missing key, 401, or 403. It calls `parse_agent_response()` on only the model content JSON and catches `AgentSchemaError`, returning `AgentInvocation(response=None, error_code="DEEPSEEK_SCHEMA_INVALID")`; caller-visible errors are otherwise stable safe codes such as `DEEPSEEK_TIMEOUT`, `DEEPSEEK_TRANSPORT`, `DEEPSEEK_RATE_LIMIT`, `DEEPSEEK_SERVER_ERROR`, `DEEPSEEK_UNAUTHORIZED`, and `DEEPSEEK_FORBIDDEN`.

`validate_agent_response()` requires exact product-ID coverage, unique product IDs, rank set exactly `1..N`, and the already bounded confidence values. Its `AgentSchemaError`, or `DEEPSEEK_SCHEMA_INVALID` from `request()`, makes the Worker issue exactly one schema-repair request. `build_degraded_drafts()` derives rank and all four permitted model fields from candidate order using the fixed Chinese message; the Worker reconciles those drafts to trusted facts by product ID. Do not add a DeepSeek SDK, vendor factory, prompt persistence, response persistence, or logging of request/response bodies.

- [ ] **Step 4: Run GREEN agent tests and existing analytics regression**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_analysis_agent.py -v
.\.venv\Scripts\python.exe -m pytest tests/test_analytics.py -v
```

Expected: all agent tests use `MockTransport`, the default model assertion is `deepseek-v4-flash`, and the five original deterministic tools still pass their regression suite.

- [ ] **Step 5: Commit the deterministic/LLM boundary**

```powershell
git add pyproject.toml .env.example backend/config.py backend/schemas.py backend/analysis_agent.py tests/test_analysis_agent.py
git commit -m "feat: add bounded analysis agent client"
```
### Task 4: Checkpointed PostgreSQL lease Worker and idempotent persistence

**Files:**

- Modify: `pyproject.toml`
- Modify: `backend/config.py`
- Modify: `backend/analysis_runs.py`
- Create: `backend/analysis_worker.py`
- Create: `scripts/run_analysis_worker.py`
- Create: `tests/test_analysis_worker.py`

**Interfaces:**

```python
# backend/config.py
class Settings(BaseSettings):
    langgraph_database_url: str = "postgresql://ecommerce:ecommerce@localhost:5434/ecommerce"
    analysis_lease_seconds: int = 60
```

```python
# backend/analysis_runs.py
async def claim_next_analysis_run(
    session: AsyncSession, *, lease_owner: str, lease_seconds: int
) -> WorkflowRun | None: ...

async def renew_analysis_lease(
    session: AsyncSession, *, workflow_run_id: str, lease_owner: str, lease_seconds: int
) -> bool: ...

async def update_analysis_step(
    session: AsyncSession, *, workflow_run_id: str, lease_owner: str, current_step: str
) -> bool: ...

async def persist_analysis_completion(
    session: AsyncSession,
    *,
    workflow_run_id: str,
    lease_owner: str,
    candidates: list[AnalysisCandidateView],
    calls: list[AgentCallRecord],
    quality_status: WorkflowQuality,
    quality: dict[str, object],
) -> bool: ...

async def fail_analysis_run(
    session: AsyncSession, *, workflow_run_id: str, lease_owner: str, error_code: str
) -> bool: ...
```

```python
# backend/analysis_worker.py
class LeaseLostError(RuntimeError):
    pass

class CheckpointFailure(RuntimeError):
    pass

class AnalysisWorkflowState(TypedDict, total=False):
    workflow_run_id: str
    store_id: str
    start_date: str
    end_date: str
    facts: dict[str, object]
    drafts: list[dict[str, object]]
    candidates: list[dict[str, object]]
    call_records: list[dict[str, object]]
    quality_status: str
    error_code: str | None

def build_analysis_graph(
    *,
    session: AsyncSession,
    settings: Settings,
    lease_owner: str,
    checkpointer: BaseCheckpointSaver,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CompiledStateGraph: ...

async def run_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    lease_owner: str,
    checkpointer: BaseCheckpointSaver,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str | None: ...
```

Add runtime dependencies `langgraph>=0.6,<1`, `langgraph-checkpoint-postgres>=2,<3`, and `psycopg[binary,pool]>=3.2,<4`. `scripts/run_analysis_worker.py` is the only process entry point. It uses the saver-owned async context `async with AsyncPostgresSaver.from_conn_string(settings.langgraph_database_url) as checkpointer:`, calls `await checkpointer.setup()` inside that context, then loops calling `run_once()`; `--once` executes one call and exits. Do not construct a bare `AsyncConnection`; the saver context owns the driver configuration required for checkpoint persistence. It does not modify `docker-compose.yml`.

- [ ] **Step 1: Write failing Worker, lease, reconciliation, and failure-policy tests**

Create `tests/test_analysis_worker.py` with a SQLite session factory for graph/unit behavior and `langgraph.checkpoint.memory.InMemorySaver` for the ordinary test path. Use only `httpx.MockTransport` for every LLM outcome.

Add tests for these exact outcomes:

```python
async def test_run_once_processes_one_accepted_run_with_trusted_candidates(
    worker_factory, worker_session, settings, valid_transport
) -> None:
    run = await create_analysis_run(...)
    processed_id = await run_once(
        factory,
        settings=settings,
        lease_owner="worker-a",
        checkpointer=InMemorySaver(),
        transport=valid_transport,
    )
    assert processed_id == run.id
    refreshed = await session.get(WorkflowRun, run.id)
    assert (refreshed.status, refreshed.quality_status, refreshed.attempt_count) == (
        WorkflowStatus.AWAITING_SELECTION, WorkflowQuality.NORMAL, 1
    )
```

Assert exactly five persisted candidates and verify each persisted `product_code`, `metrics`, `anomaly_types`, `business_impact`, and `evidence` equals the trusted fact source even when the mock response tries to provide a different value. Assert the response's explanations, reason, action, and confidence are preserved.

Test one `run_once()` claims no more than one accepted run; `accepted` is claimed before expired `processing` in creation order; terminal rows return `None`; a stale `processing` row with `attempt_count=3` becomes `failed/LEASE_ATTEMPTS_EXHAUSTED` without a fourth claim; and an expired row with count 2 is reclaimed once with count 3. Directly replace `lease_owner` in the test database, then assert `renew_analysis_lease`, `persist_analysis_completion`, and `fail_analysis_run` return `False` for the old owner and do not change the replacement owner's row.

Use a recording wrapper around `renew_analysis_lease` and a `MockTransport` sequence `429, 429, 200` to prove the Worker invokes the `before_http_attempt` callback, receives `True`, and then sends each `primary` attempt in the exact order `renew(1), post(1), renew(2), post(2), renew(3), post(3)`. Repeat with a primary schema error and a valid repair response to prove `renew(AgentCallType.SCHEMA_REPAIR, 1)` occurs before the repair POST. Make the callback return `False` after a replacement owner takes the lease; assert the old Worker sends no further HTTP request and neither persists candidates/calls nor changes the replacement owner's row.

Parameterize model outcomes for timeout, transport error, 429, 5xx, missing key, 401, 403, and `DEEPSEEK_SCHEMA_INVALID` from both primary and schema-repair requests. Each LLM failure except `LEASE_LOST` must end `awaiting_selection/degraded` with five Chinese fallback candidates, safe error metadata, and no more than three transport attempts per logical request. Assert exactly one `schema_repair` logical request follows a schema error and no repair follows non-schema errors. Inject an exception from deterministic fact collection; it must result in `failed` with a safe fact error code, never a degradation. Inject `CheckpointFailure` from the checkpointer during `graph.ainvoke()`; while the lease remains valid, assert outer `run_once()` handling calls `fail_analysis_run(..., error_code="CHECKPOINT_ERROR")` and yields `failed`. Repeat after replacing the lease owner and assert `fail_analysis_run()` returns `False`, no terminal overwrite occurs, and the old Worker stops.

Run normal completion twice against the same checkpoint and call `persist_analysis_completion()` twice with the same data. Assert candidate count and `agent_calls` count remain unchanged, and the latter's unique keys are `(workflow_run_id, node_name, call_type, attempt)`. Assert `estimated_cost is None` when the configured price is absent and inspect only persisted column names/values for the absence of credential, authorization, prompt, raw-response, and chain-of-thought fields.

For a degradation scenario, assert the attempt-0 `AgentCall` has `node_name="validate_and_reconcile"`, `call_type=primary`, and the deterministic degradation status. The same test asserts every primary transport call row has `node_name="call_analysis_agent"` and every repair call row has `node_name="validate_and_reconcile"`.

- [ ] **Step 2: Run Worker tests and observe RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_analysis_worker.py -v`

Expected: collection fails because LangGraph dependencies, lease functions, Worker entry points, and `run_once()` do not exist.

- [ ] **Step 3: Implement server-timed leases and ownership-guarded writes**

In `claim_next_analysis_run()`, first atomically fail only expired `processing` rows whose `attempt_count >= 3`, clearing both lease fields and setting `LEASE_ATTEMPTS_EXHAUSTED`. Then select one eligible row with this PostgreSQL shape inside one transaction:

```python
eligible = or_(
    WorkflowRun.status == WorkflowStatus.ACCEPTED,
    and_(
        WorkflowRun.status == WorkflowStatus.PROCESSING,
        WorkflowRun.lease_expires_at < func.now(),
    ),
)
run = await session.scalar(
    select(WorkflowRun)
    .where(eligible, WorkflowRun.attempt_count < 3)
    .order_by(WorkflowRun.created_at, WorkflowRun.id)
    .with_for_update(skip_locked=True)
    .limit(1)
)
```

For a selected run, set `status=processing`, `lease_owner`, `lease_expires_at=func.now() + text("make_interval(secs => :lease_seconds)")`, increment `attempt_count`, and commit before any graph invocation. Use a SQLite-compatible bound duration only in unit-test execution while preserving `func.now()` as the production clock; do not call application time for PostgreSQL lease comparison.

Every renewal, node-step update, completion, and failure update filters `id`, `status=processing`, `lease_owner`, and `lease_expires_at > func.now()`. Completion first locks and verifies that ownership in one transaction, upserts candidates by `(workflow_run_id, product_id)` and call records by `(workflow_run_id, node_name, call_type, attempt)`, then changes status to `awaiting_selection`, clears the lease, and writes `current_step="persist_results"`. Use the database dialect's `insert(...).on_conflict_do_update()` for SQLite and PostgreSQL; do not delete candidates before writing them. `fail_analysis_run()` uses the same owner guard, clears the lease, and changes only fact/input/checkpoint failures to `failed`.

- [ ] **Step 4: Implement the five-node graph and testable Worker**

`build_analysis_graph()` creates one `StateGraph(AnalysisWorkflowState)` with this exact linear sequence and compiles it with the supplied checkpointer:

```text
START -> load_run -> collect_facts -> call_analysis_agent -> validate_and_reconcile -> persist_results -> END
```

`load_run` reads the lease-owned run, verifies `workflow_type="analysis"`, persisted date strings, and `processing` state, then calls `update_analysis_step(..., current_step="load_run")`. `collect_facts` sets `current_step="collect_facts"`, calls `collect_analysis_facts()`, and stores only normalized trusted facts in graph state. `call_analysis_agent` creates a minimal closure `before_http_attempt(call_type, attempt) -> bool` that calls `renew_analysis_lease(session, workflow_run_id=workflow_run.id, lease_owner=lease_owner, lease_seconds=settings.analysis_lease_seconds)` using PostgreSQL `now()`. It passes that closure to `DeepSeekAnalysisClient.request(..., call_type=AgentCallType.PRIMARY)` so every initial and retried POST renews immediately beforehand. A `False` callback result becomes `LeaseLostError`; the old Worker stops without further external calls, completion, degradation, or failure writes. The node stores only parsed draft fields plus safe `AgentCallRecord`s in state.

`validate_and_reconcile` calls `update_analysis_step(..., current_step="validate_and_reconcile")`. It validates exact coverage/ranks/confidence. On `AgentSchemaError` from validation or `AgentInvocation.error_code="DEEPSEEK_SCHEMA_INVALID"` from parsing, it makes exactly one `SCHEMA_REPAIR` request with the same per-attempt renewal closure, then validates once more. A false repair callback raises `LeaseLostError` and stops the old Worker. Missing key, 401, 403, exhausted transport failure, and a remaining schema error use `build_degraded_drafts()` and set `quality_status=degraded`; they are not `failed`. It merges each approved or degraded draft onto the matching trusted candidate by product ID, so all fact fields originate from `AnalysisFacts`.

`persist_results` renews before its database work, calls `persist_analysis_completion()`, and refuses to overwrite if it lost the lease. Add a safe attempt-0 `AgentCallRecord` for a degradation decision with `node_name="validate_and_reconcile"` and `call_type=primary`. Deterministic input/fact errors exit through `fail_analysis_run()` with a stable code; no LLM call can convert one of these failures to degraded. Configure the checkpointer boundary to normalize read, save, and consistency failures into `CheckpointFailure`, preserving the original exception only in controlled local diagnostics rather than a workflow row.

Implement `run_once()` so it claims at most one run using a short-lived async session, invokes the graph with an outer `try`/`except`, and uses:

```python
config = {"configurable": {"thread_id": workflow_run.id}}
await graph.ainvoke({"workflow_run_id": workflow_run.id}, config=config)
```

On `CheckpointFailure` from `graph.ainvoke()`, open a fresh session and call `fail_analysis_run(..., error_code="CHECKPOINT_ERROR")`. That function's owner-and-unexpired-lease predicate decides the outcome: commit `failed` only when it returns `True`; when it returns `False`, stop without any state overwrite. On `LeaseLostError`, stop without calling `fail_analysis_run()`. It returns `None` for no claim and the claimed ID otherwise. The script supplies the production `AsyncPostgresSaver`; tests supply `InMemorySaver`. The script creates a process-unique owner such as `f"{socket.gethostname()}:{os.getpid()}"`, never prints settings or secrets, and sleeps only when a non-`--once` poll finds no row.

- [ ] **Step 5: Run GREEN Worker tests and static verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_analysis_worker.py -v
.\.venv\Scripts\python.exe -m compileall backend scripts tests
$env:JWT_SECRET_KEY = "local-plan-verification-secret-at-least-32"
.\.venv\Scripts\alembic.exe check
```

Expected: Worker tests pass with no external network access, all modules compile, and Alembic has no drift.

- [ ] **Step 6: Commit the durable Worker**

```powershell
git add pyproject.toml backend/config.py backend/analysis_runs.py backend/analysis_worker.py scripts/run_analysis_worker.py tests/test_analysis_worker.py
git commit -m "feat: add durable analysis worker"
```
### Task 5: PostgreSQL vertical acceptance and explicit DeepSeek smoke test

**Files:**

- Modify: `pyproject.toml`
- Create: `tests/test_analysis_postgres.py`
- Create: `tests/test_deepseek_smoke.py`

**Interfaces:**

```python
# pyproject.toml
[tool.pytest.ini_options]
markers = [
  "postgres_integration: requires the configured local PostgreSQL service",
  "deepseek_smoke: explicit real deepseek-v4-flash smoke test",
]

# tests/test_analysis_postgres.py
@pytest.mark.postgres_integration
async def test_postgres_analysis_vertical_slice() -> None: ...

# tests/test_deepseek_smoke.py
@pytest.mark.deepseek_smoke
async def test_explicit_deepseek_v4_flash_schema_smoke() -> None: ...
```

The PostgreSQL test is skipped unless `RUN_POSTGRES_INTEGRATION=1`; it uses `async_session_factory`, `AsyncPostgresSaver`, and the existing deterministic seed data. It deletes only workflow, candidate, and call rows it created by exact test UUIDs during cleanup. The DeepSeek test is skipped unless `RUN_DEEPSEEK_SMOKE=1`; it obtains the configured optional key only inside `DeepSeekAnalysisClient`, never prints it, and does not store a prompt or response.

- [ ] **Step 1: Write the failing PostgreSQL acceptance and default-skipped smoke tests**

Create `tests/test_analysis_postgres.py` using `pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", ...)`. In a seeded flagship store over `2026-07-26` through `2026-08-24`, create accepted runs and use two independent async sessions concurrently:

```python
claimed = await asyncio.gather(
    claim_next_analysis_run(session_a, lease_owner="worker-a", lease_seconds=60),
    claim_next_analysis_run(session_b, lease_owner="worker-b", lease_seconds=60),
)
assert {run.id for run in claimed if run is not None} == {first_run.id, second_run.id}
```

Set one test row's lease expiry with PostgreSQL `now() - interval '1 second'`, assert a new owner reclaims it, then prove the old owner cannot finish it. Create an expired attempt-3 row and assert it reaches `failed` rather than a fourth claim. Confirm terminal `awaiting_selection` and `failed` rows never appear in `claim_next_analysis_run()`.

Use the real PostgreSQL `renew_analysis_lease()` through a recording wrapper and a `MockTransport` sequence `429, 429, 200`; assert each POST follows a successful server-timed renewal for attempts 1, 2, and 3. Repeat for a primary schema error followed by a valid repair response, asserting the repair POST follows its own successful renewal. After a second session replaces the lease owner, make the next callback return `False`; assert the original Worker sends no later POST and cannot persist or finish the run.

For checkpoint recovery, compile a graph with `AsyncPostgresSaver` and invoke it with `interrupt_after=["collect_facts"]` and `thread_id=workflow_run.id`. Read the checkpoint tuple and assert safe facts exist but no key, header, prompt, or raw response does. Compile a fresh graph with the same saver/thread ID and resume it; assert it reaches `awaiting_selection` without duplicate candidates or calls. Inject `CheckpointFailure` from checkpointer read, save, and consistency paths around `graph.ainvoke()`: with an unexpired matching lease, assert outer `run_once()` writes `failed/CHECKPOINT_ERROR`; after a different owner reclaims the lease, assert that old invocation stops with no overwrite. The LLM transport for this entire test is a valid `MockTransport` response.

Add the vertical assertion that `run_once()` completes exactly one seeded run to `awaiting_selection/normal`, creates five candidates, calls all five deterministic tools through the graph, retains database-derived fact fields, and writes only the permitted safe `agent_calls` columns. Query candidate and call counts after a second completion/persistence attempt and assert they are unchanged.

Create `tests/test_deepseek_smoke.py` with default skip:

```python
def minimal_trusted_facts() -> AnalysisFacts:
    metrics = ProductMetrics.from_totals(
        impressions=10, clicks=1, orders=1, units=1, revenue=Decimal("1.00"), refunds=0
    ).model_copy(update={"product_id": "smoke-product", "product_code": "SMOKE-001"})
    return AnalysisFacts(
        store_summary=StoreMetrics(
            store_id="smoke-store", impressions=10, clicks=1, orders=1, units=1,
            revenue=Decimal("1.00"), refunds=0, ctr=Decimal("0.1000"),
            conversion_rate=Decimal("1.0000"), refund_rate=Decimal("0.0000"),
            average_order_value=Decimal("1.0000"),
        ),
        candidates=[TrustedAnalysisCandidate(
            product_id="smoke-product", product_code="SMOKE-001",
            anomaly_types=["smoke"], metrics=metrics, business_impact=Decimal("1.00"),
            evidence=["smoke=1"],
        )],
    )

@pytest.mark.deepseek_smoke
@pytest.mark.skipif(os.getenv("RUN_DEEPSEEK_SMOKE") != "1", reason="explicit opt-in required")
async def test_explicit_deepseek_v4_flash_schema_smoke() -> None:
    settings = get_settings()
    if settings.deepseek_api_key is None:
        pytest.skip("DeepSeek key is not configured")
    client = DeepSeekAnalysisClient(settings)
    result = await _run_smoke(client, minimal_trusted_facts())
    assert result.response is not None
    assert 1 <= len(result.records) <= 2
    assert result.records[0].call_type is AgentCallType.PRIMARY
    assert all(record.model == "deepseek-v4-flash" for record in result.records)
```

`_run_smoke()` is a test-only orchestrator that reuses `DeepSeekAnalysisClient`, `AgentCallType`, and `validate_agent_response()`. It makes the primary request first, performs exactly one `schema_repair` only when the primary result is `DEEPSEEK_SCHEMA_INVALID` or fails trusted-set validation, and validates the repair once without recursion. A shared `before_http_attempt` callback permits no more than two total POSTs across the smoke run, so transient client retries cannot exceed the test's external-call budget. `minimal_trusted_facts()` contains one local synthetic product and no user credentials. Default-run MockTransport tests prove invalid-primary/valid-repair, valid-primary/no-repair, invalid-repair/no-recursion, and the two-POST cap; the smoke test asserts only parsed schema and safe record metadata and neither prints nor writes the result body.

- [ ] **Step 2: Run the new tests and observe RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_analysis_postgres.py tests/test_deepseek_smoke.py -v
```

Expected: collection fails because the markers and test modules do not exist. After their creation, the same command passes with both integration suites skipped unless their explicit flags are set.

- [ ] **Step 3: Implement only test registration and acceptance fixtures**

Register the two markers in `pyproject.toml`; do not add a test database service or new environment-file values. Build the PostgreSQL fixture from the configured application session factory, run `await checkpointer.setup()` before the graph, and clean up only records created by test IDs. Use `seed_demo_data(session)` for prerequisite facts and do not reset, drop, truncate, or broadly delete project data.

The real smoke test retains the exact `deepseek-v4-flash` assertion. One opt-in pytest smoke run makes a primary POST and, only on `DEEPSEEK_SCHEMA_INVALID`, one `schema_repair` POST; no valid primary triggers repair and no invalid repair triggers another repair. Its shared callback caps all external POSTs, including transient client retries, at two. The key remains in the operator's already-configured secret source and is never read, echoed, asserted, or persisted by test code.

- [ ] **Step 4: Run the required PostgreSQL closed loop and the full normal suite**

Run:

```powershell
$env:JWT_SECRET_KEY = "local-plan-verification-secret-at-least-32"
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe scripts/seed_demo.py --reset --seed 20260825
$env:RUN_POSTGRES_INTEGRATION = "1"
.\.venv\Scripts\python.exe -m pytest tests/test_analysis_postgres.py -m postgres_integration -v
Remove-Item Env:RUN_POSTGRES_INTEGRATION
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m compileall backend scripts tests
.\.venv\Scripts\alembic.exe check
git diff --check
git show --check HEAD
```

Expected: the PostgreSQL test proves `SKIP LOCKED` mutual exclusion, expired-lease recovery, stale-owner protection, three-attempt exhaustion, checkpoint resume, idempotent candidates/calls, and a five-candidate seeded closure. The complete normal suite passes without network access; `test_deepseek_smoke.py` is skipped by default.

- [ ] **Step 5: Perform the final, explicitly authorized real DeepSeek smoke test**

Only after Step 4 passes and an operator has deliberately configured a DeepSeek key in their private secret source, run:

```powershell
$env:RUN_DEEPSEEK_SMOKE = "1"
$env:DEEPSEEK_MODEL = "deepseek-v4-flash"
.\.venv\Scripts\python.exe -m pytest tests/test_deepseek_smoke.py -m deepseek_smoke -v
Remove-Item Env:RUN_DEEPSEEK_SMOKE
Remove-Item Env:DEEPSEEK_MODEL
```

Expected: one real smoke run passes using `deepseek-v4-flash` after a primary response or one valid `schema_repair`; it sends no more than two external POSTs and command output contains no key, authorization value, prompt, or response body. If the operator has not authorized this step or no private key is configured, leave the test skipped and report that the explicit external acceptance was not performed rather than fabricating a result.

- [ ] **Step 6: Commit the acceptance coverage**

```powershell
git add pyproject.toml tests/test_analysis_postgres.py tests/test_deepseek_smoke.py
git commit -m "test: verify durable analysis slice"
```

## Plan Self-Review Checklist

- [x] Read the approved spec section by section and confirm the mapping below.
- [x] Search this plan for placeholder language and replace each match with executable detail.
- [x] Reconcile every later interface against its preceding definitions: workflow status/quality enum names, `AgentCallType`, request schemas, `AnalysisFacts`, lease function parameters, graph state keys, and `run_once()` parameters.
- [x] Confirm `git diff --check` is clean before the implementation-plan handoff commit.

## Approved-spec Coverage Map

| Approved requirement | Implementation task coverage |
|---|---|
| Three business tables, checks, unique keys, explicit migration, safe audit columns | Task 1 |
| POST/GET API contract, 1–90 inclusive days, 202, real-time store authorization, stable 409 | Task 2 |
| Five deterministic tools, DeepSeek direct `httpx`, exact default model, output schema, retries, repairs, Chinese degradation, fact reattachment | Tasks 3 and 4 |
| PostgreSQL leases, `SKIP LOCKED`, three claims, stale-owner protection, `run_once`, state transitions | Tasks 4 and 5 |
| LangGraph PostgreSQL checkpointer, thread ID, checkpoint resume, candidate/call idempotency | Tasks 4 and 5 |
| Mock-only ordinary tests, PostgreSQL seeded five-candidate closure, explicit real smoke | Task 5 |
| Excluded Worker service, queues, RAG, selection endpoint, frontend, and platform integration | Global Constraints and every task's file list |
