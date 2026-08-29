# Product Optimization and Compliance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` in this development window to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not dispatch subagents. Every task requires review approval and an independent commit before the next task begins.

**Goal:** Build the complete authorized product-optimization phase: idempotent product selection, a recoverable `optimization` workflow, two independent Agents with at most two automatic revisions, and only `draft_ready` or safe `pending_manual/degraded` outcomes.

**Architecture:** Preserve the modular monolith and existing PostgreSQL fact source. The completed phase atomically selects one analysis candidate, creates one type-isolated optimization run, resumes it through PostgreSQL leases and checkpoints, uses independent optimization and compliance Agents around deterministic checks, and stops after the third iteration. Task 1 is only the first persistence slice; proposal lifecycle facts remain only on the linked optimization workflow row, so no second status machine is introduced.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, SQLAlchemy async, Alembic, PostgreSQL 16, SQLite test fixture, pytest, pytest-asyncio, LangGraph PostgreSQL Checkpointer, existing local Milvus and BGE-M3 RAG dependencies.

**Spec:** `docs/superpowers/specs/2026-08-28-product-optimization-compliance-design.md`

## Global Constraints

- Execute this plan only in `C:\Users\15482\.codex\worktrees\fab9\E-commerce_operations` with `D:\E-commerce_operations_env\python.exe`; do not create or use `D:\E-commerce_operations\.venv`.
- The current development window must use `superpowers:executing-plans`, strict RED then minimal GREEN then regression, a reviewer gate per task, and one commit per task. Do not dispatch subagents.
- Every task uses this fixed Git gate: run `git add -- <allowed files>`; run `git diff --cached --name-only` and verify its output is exactly the task's allowed-file list; run `git diff --cached --check`, including every newly created file; only then commit. After committing, run `git show --check --oneline HEAD` and `git status --short` before reporting the task.
- Keep the FastAPI modular monolith. Do not add Redis, Celery, Kafka, MCP, microservices, provider factories, Compose Worker services, Docker API control, frontend code, real platform access, or local generative models.
- `workflow_runs.workflow_type` is exactly `analysis | optimization`. Analysis dates are both required and ordered; optimization dates are both `NULL`. Analysis and optimization Workers later claim only their own type through PostgreSQL `now()` and `FOR UPDATE SKIP LOCKED`.
- Existing analysis workflow data and `agent_calls` must stay readable. Existing calls receive `iteration=0`; new call uniqueness includes iteration.
- `ProductProposal` holds aggregate identity, foreign keys, `base_product_version`, idempotency hash, and current revision only. It must not contain lifecycle `status`, `quality_status`, or `error_code`.
- Proposal revisions are immutable and limited to iteration `0..2`. A `ComplianceReview` is one-to-one with its revision and is the only storage for deterministic and semantic compliance results.
- `base_product_version` is captured from `products.current_version` by the later selection transaction. Later Workers must reject changed product versions with `PRODUCT_VERSION_CONFLICT`; this task only persists the boundary.
- API routes and business services do not access, record, output, or call the DeepSeek Key. Only a later Worker DeepSeek request boundary reads `SecretStr`.
- Ordinary tests use no network, DeepSeek, model load, Milvus, or model repository. Runtime, caches, and models remain D-drive scoped; never delete, move, or reorganize C-drive personal files.
- Checkpoints, logs, `agent_calls`, APIs, and test output must not persist or print Keys, Authorization, Cookies, full Prompts, raw responses, chain-of-thought, vectors, paths, or upload bytes.

### Unified D-drive development and verification environment

Apply this exact block in the same PowerShell process before every development or verification command in this plan. Every command section below explicitly refers to this one named block; no section defines a competing environment.

```powershell
$env:TEMP = "D:\E-commerce_operations_runtime\tmp"
$env:TMP = "D:\E-commerce_operations_runtime\tmp"
$env:PYTHONPYCACHEPREFIX = "D:\E-commerce_operations_runtime\pycache"
$env:HF_HOME = "D:\E-commerce_operations_runtime\hf"
$env:TRANSFORMERS_CACHE = "D:\E-commerce_operations_runtime\transformers"
$env:TORCH_HOME = "D:\E-commerce_operations_runtime\torch"
Remove-Item Env:RUN_KNOWLEDGE_INTEGRATION -ErrorAction SilentlyContinue
Remove-Item Env:RUN_POSTGRES_INTEGRATION -ErrorAction SilentlyContinue
Remove-Item Env:RUN_DEEPSEEK_SMOKE -ErrorAction SilentlyContinue
$env:DEEPSEEK_API_KEY = ""
```

Ordinary tests must run with all three opt-in variables absent and `DEEPSEEK_API_KEY` blank. A later, explicitly approved PostgreSQL, local-RAG, or DeepSeek smoke command may override only the required opt-in variable in that command's own PowerShell process after first applying this block.

---

## Expected File Responsibilities

| File | Responsibility |
|---|---|
| `backend/common.py` | Workflow type/status and compliance-risk string enums shared by models, services, and API schemas. |
| `backend/models.py` | Typed workflow, audited-call, proposal, immutable revision, and compliance-review ORM mappings with named constraints. |
| `backend/config.py` | Non-secret `optimization_lease_seconds` declaration. |
| `alembic/versions/0004_product_optimization.py` | Explicit transition from schema revision `0003` to typed optimization persistence and its safe reverse order. |
| `tests/test_optimization_models.py` | SQLite persistence and constraint coverage for every new optimization row and type-aware workflow state. |
| `tests/test_analysis_models.py` | Existing analysis workflow and agent-call iteration compatibility regression. |
| `backend/workflow_leases.py` | Later shared database-now lease expiry, owner predicate, and owner-guarded commit primitives. |
| `backend/analysis_runs.py` | Later consumption of shared lease primitives and explicit analysis-only claim/exhaust predicates. |
| `backend/deepseek_runtime.py` | Later direct structured JSON call, bounded retry, safe hash/token/cost audit record, and no raw-response storage. |
| `backend/analysis_agent.py` and `backend/analysis_worker.py` | Later compatibility use of the shared DeepSeek runtime while retaining analysis Prompt, trusted facts, graph, and iteration zero. |
| `backend/proposals.py` | Later atomic select-product transaction, candidate/idempotency checks, `base_product_version` capture, and store-scoped proposal read/aggregation. |
| `backend/optimization_runs.py` | Later optimization lease claim/renewal, owner guards, revision/review/call persistence, and optimization terminal transitions only. It must not absorb proposal-selection or read-aggregation responsibilities. |
| `backend/optimization_agent.py` | Later independent product-optimization Prompt and Pydantic output contract. |
| `backend/compliance_agent.py` | Later independent semantic-compliance Prompt and Pydantic output contract. |
| `backend/optimization_validation.py` | Later deterministic fact, restricted-language, length, SKU, price, and citation validation. |
| `backend/optimization_worker.py` | Task 8's type-isolated, bounded LangGraph Worker. |
| `backend/optimization_trusted_input.py` | Task 9's only product-context-to-local-RAG adapter: it builds one in-memory query, rechecks returned chunks against PostgreSQL, and returns a Task 5 trusted input. |
| `backend/knowledge_search.py` | Existing local retrieval algorithm; Task 9 adds only an optional raising pre-external-attempt callback at its real dependency/model/Milvus/rerank boundaries. |
| `scripts/run_optimization_worker.py` | Task 9's repository-local production command, created only with the real active/applicable RAG trusted-input loader; it is deliberately not created in Task 8. |
| `backend/schemas.py` and `backend/routes.py` | Later select-product and store-scoped read-only proposal endpoints. |
| `tests/test_analysis_worker.py` and `tests/test_analysis_postgres.py` | Later analysis type-isolation regressions for SQLite and opt-in PostgreSQL. |
| `tests/test_optimization_api.py` | Later real-JWT API, role, store-scope, 200/202/409, and idempotency coverage. |
| `tests/test_optimization_worker.py` | Later fake-only iteration ceiling, owner-loss, checkpoint, and degradation coverage. |
| `tests/test_optimization_postgres.py` | Later opt-in PostgreSQL concurrency, recovery, and exact cleanup coverage. |
| `tests/test_optimization_rag.py` | Later opt-in D-drive local RAG citation and safe failure coverage. |
| `tests/test_optimization_deepseek_smoke.py` | Later explicit real double-Agent smoke with one request per Agent and no automatic revision. |

No new dependency, model artifact, Compose entry, queue, or service is expected for this phase.

---

### Task 1: Database and typed optimization domain skeleton

**Files:**

- Modify: `backend/common.py`
- Modify: `backend/models.py`
- Modify: `backend/config.py`
- Create: `alembic/versions/0004_product_optimization.py`
- Create: `tests/test_optimization_models.py`
- Modify: `tests/test_analysis_models.py`

**Interfaces:**

- Consumes: `StrEnum`, `utc_now`, `WorkflowQuality`, `AgentCallType`, `Product.current_version`, existing string UUID mapping convention, explicit named SQL checks, non-native enums, Alembic head `0003`, and the SQLite metadata fixture.
- Produces: `WorkflowType`, expanded `WorkflowStatus`, `ComplianceRiskLevel`, `ProductProposal`, `ProposalRevision`, `ComplianceReview`, `AgentCall.iteration`, `Settings.optimization_lease_seconds`, and migration revision `0004`.

```python
# backend/common.py
class WorkflowType(StrEnum):
    ANALYSIS = "analysis"
    OPTIMIZATION = "optimization"


class WorkflowStatus(StrEnum):
    ACCEPTED = "accepted"
    PROCESSING = "processing"
    AWAITING_SELECTION = "awaiting_selection"
    COMPLETED = "completed"
    DRAFT_READY = "draft_ready"
    PENDING_MANUAL = "pending_manual"
    FAILED = "failed"


class ComplianceRiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# backend/config.py
class Settings(BaseSettings):
    optimization_lease_seconds: int = 60
```

`WorkflowRun.workflow_type` becomes `Mapped[WorkflowType]`. `start_date` and `end_date` become `Mapped[date | None]`. Replace the existing workflow type, state, and date checks with one named type-aware check:

```sql
(
  workflow_type = 'analysis'
  AND start_date IS NOT NULL AND end_date IS NOT NULL
  AND start_date <= end_date
  AND status IN ('accepted','processing','awaiting_selection','completed','failed')
)
OR
(
  workflow_type = 'optimization'
  AND start_date IS NULL AND end_date IS NULL
  AND status IN ('accepted','processing','draft_ready','pending_manual','failed')
)
```

Keep the existing `attempt_count` and processing-lease check intact. `AgentCall.iteration` is non-null integer defaulting to zero with `CHECK(iteration >= 0)`; its named unique key becomes `(workflow_run_id, node_name, call_type, iteration, attempt)`.

```python
# backend/models.py
class ProductProposal(Base):
    __tablename__ = "product_proposals"
    # id, analysis_run_id, analysis_candidate_id, optimization_run_id,
    # store_id, product_id, base_product_version, selection_idempotency_hash,
    # current_revision_id, created_at, updated_at


class ProposalRevision(Base):
    __tablename__ = "proposal_revisions"
    # id, proposal_id, iteration, base_product_version, trusted_fact_hash,
    # proposal_output, citations, created_at


class ComplianceReview(Base):
    __tablename__ = "compliance_reviews"
    # id, proposal_id, proposal_revision_id, iteration, deterministic_checks,
    # semantic_review, passed, risk_level, required_changes, citations,
    # error_code, quality_status, created_at
```

All primary keys use `String(36)` and `utc_now` timestamps. `ProductProposal` has named foreign keys to analysis and optimization `workflow_runs`, `analysis_candidates`, `stores`, and `products`; named unique constraints on `analysis_run_id`, `analysis_candidate_id`, and `optimization_run_id`; `CHECK(base_product_version >= 1)`; and `CHECK(length(selection_idempotency_hash) = 64)` named `ck_product_proposals_selection_idempotency_hash_length`. It deliberately has no lifecycle columns.

`ProposalRevision` has named foreign key `proposal_id`, named `UNIQUE(proposal_id, iteration)`, `CHECK(iteration BETWEEN 0 AND 2)`, `CHECK(base_product_version >= 1)`, `trusted_fact_hash` as non-null `String(64)` with `CHECK(length(trusted_fact_hash) = 64)` named `ck_proposal_revisions_trusted_fact_hash_length`, and non-null JSON `proposal_output` and `citations`. It has no mutable update path in the later service contract.

`ComplianceReview` has named foreign keys to proposal and revision, named `UNIQUE(proposal_revision_id)`, named `UNIQUE(proposal_id, iteration)`, `CHECK(iteration BETWEEN 0 AND 2)`, `passed` non-null Boolean, `risk_level` using `ComplianceRiskLevel` plus an explicit named check, `quality_status` using the existing `WorkflowQuality` plus an explicit named check, and non-null JSON `deterministic_checks`, `semantic_review`, `required_changes`, and `citations`. It is the only table that stores the two compliance tracks.

- [ ] **Step 1: Write failing SQLite persistence and constraint tests**

Create `tests/test_optimization_models.py`. Seed one active operator, store, product at `current_version=7`, analysis workflow/candidate, and optimization workflow through the existing SQLite session fixture. Add the following successful persistence test:

```python
async def test_typed_workflows_proposal_revision_and_review_persist(session) -> None:
    analysis = WorkflowRun(
        id="analysis-1", workflow_type=WorkflowType.ANALYSIS, store_id="store-1",
        created_by="user-1", start_date=date(2026, 8, 1), end_date=date(2026, 8, 2),
        status=WorkflowStatus.AWAITING_SELECTION, quality_status=WorkflowQuality.NORMAL,
    )
    optimization = WorkflowRun(
        id="optimization-1", workflow_type=WorkflowType.OPTIMIZATION, store_id="store-1",
        created_by="user-1", start_date=None, end_date=None,
        status=WorkflowStatus.ACCEPTED, quality_status=WorkflowQuality.NORMAL,
    )
    proposal = ProductProposal(
        id="proposal-1", analysis_run_id=analysis.id, analysis_candidate_id="candidate-1",
        optimization_run_id=optimization.id, store_id="store-1", product_id="product-1",
        base_product_version=7, selection_idempotency_hash="a" * 64,
    )
    revision = ProposalRevision(
        id="revision-1", proposal_id=proposal.id, iteration=0, base_product_version=7,
        trusted_fact_hash="b" * 64, proposal_output={"title": "商品"}, citations=[],
    )
    review = ComplianceReview(
        id="review-1", proposal_id=proposal.id, proposal_revision_id=revision.id, iteration=0,
        deterministic_checks={"passed": True}, semantic_review={"passed": True}, passed=True,
        risk_level=ComplianceRiskLevel.LOW, required_changes=[], citations=[],
        quality_status=WorkflowQuality.NORMAL,
    )
    session.add_all([analysis, optimization, proposal, revision, review])
    await session.flush()
    assert set(ProductProposal.__table__.columns).isdisjoint({"status", "quality_status", "error_code"})
```

In isolated transactions, assert `IntegrityError` for every named constraint family: analysis with either null date; optimization with either date; invalid workflow type/state pair; duplicate proposal `analysis_run_id`, `analysis_candidate_id`, and `optimization_run_id`; base version `0`; selection-idempotency hash lengths of `0` and `63`; a revision iteration `3`; duplicate revision `(proposal_id, iteration)`; trusted-fact hash lengths of `0` and `63`; duplicate review revision; duplicate review proposal/iteration; and invalid review iteration/risk/quality value. Insert two `AgentCall` values that differ only in `iteration` and assert both persist; a duplicate new five-field call key and `iteration=-1` must fail.

Extend `tests/test_analysis_models.py` so existing valid analysis rows use `WorkflowType.ANALYSIS`, still require two dates, and persist `AgentCall.iteration == 0`. Retain all existing analysis candidate, lease, and audit constraint assertions unchanged.

- [ ] **Step 2: Run RED and verify the expected missing contract**

Run:

```powershell
# First apply “Unified D-drive development and verification environment” above.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_optimization_models.py tests/test_analysis_models.py -v
```

Expected: collection fails because the workflow type/risk enums, optimization ORM classes, and `AgentCall.iteration` do not exist. This command uses SQLite only and does not load a model or call DeepSeek. A syntax, dependency, or unrelated fixture error is not an acceptable RED result.

- [ ] **Step 3: Implement the minimal enum, mapping, configuration, and migration boundary**

Add the enums, mapped fields, and named constraints specified above. Keep `native_enum=False`, `create_constraint=False`, and an explicit SQL `CHECK` wherever an enum is stored. Do not create a Worker, route, Prompt, selection service, or external connection in this task.

Create `alembic/versions/0004_product_optimization.py` with `revision = "0004"` and `down_revision = "0003"`. The upgrade operation order is fixed:

```python
# 1. Drop the old analysis-only workflow type/status/date checks.
#    Alter start_date and end_date nullable, then add the one type-aware check.
# 2. Add agent_calls.iteration with server_default="0"; preserve existing rows;
#    make it NOT NULL; replace the old unique key; add its named check and new key.
# 3. Create product_proposals with nullable current_revision_id and no current-revision FK.
# 4. Create proposal_revisions, then compliance_reviews.
# 5. Add fk_product_proposals_current_revision_id from proposal to revision.
```

The downgrade first checks that no `optimization` workflow or proposal row exists and raises a migration error instead of deleting business data. When that guard passes, it drops `fk_product_proposals_current_revision_id`, then `compliance_reviews`, `proposal_revisions`, and `product_proposals`; restores the former agent-call unique key after dropping `iteration`; restores analysis-only workflow checks; and makes both workflow dates non-null again. Name every foreign key, unique key, and check in both directions.

- [ ] **Step 4: Run GREEN, migration, and regression gates**

Run:

```powershell
# First apply “Unified D-drive development and verification environment” above.
$env:JWT_SECRET_KEY = "local-product-optimization-plan-verification-secret-at-least-32"
D:\E-commerce_operations_env\python.exe -m pytest tests/test_optimization_models.py tests/test_analysis_models.py -v
D:\E-commerce_operations_env\python.exe -m alembic upgrade head
D:\E-commerce_operations_env\python.exe -m alembic current
D:\E-commerce_operations_env\python.exe -m alembic check
D:\E-commerce_operations_env\python.exe -m pytest -v
$env:PYTHONPYCACHEPREFIX = "D:\E-commerce_operations_runtime\pycache"
D:\E-commerce_operations_env\python.exe -m compileall backend tests
git diff --check
git status --short
```

Expected: all SQLite persistence tests pass; Alembic reports `0004 (head)` and no model drift; the ordinary full suite retains existing analysis and knowledge behavior without network, model loading, or DeepSeek calls; compilation uses the D-drive `PYTHONPYCACHEPREFIX`, and Git whitespace checks pass.

- [ ] **Step 5: Commit only the typed persistence deliverable**

```powershell
git add -- backend/common.py backend/models.py backend/config.py alembic/versions/0004_product_optimization.py tests/test_optimization_models.py tests/test_analysis_models.py
git diff --cached --name-only
# Expected exact output, in any Git display order:
# backend/common.py
# backend/models.py
# backend/config.py
# alembic/versions/0004_product_optimization.py
# tests/test_optimization_models.py
# tests/test_analysis_models.py
git diff --cached --check
git commit -m "feat: add product optimization persistence"
git show --check --oneline HEAD
git status --short
```

### Task 2: Shared typed workflow lease guards and analysis isolation

**Files:**

- Create: `backend/workflow_leases.py`
- Modify: `backend/analysis_runs.py`
- Create: `tests/test_workflow_leases.py`
- Modify: `tests/test_analysis_worker.py`
- Do not modify: routes, Agents, RAG, migrations, or any optimization claim implementation.

**Interfaces:**

- Consumes: Task 1 `WorkflowType`, `WorkflowStatus`, `WorkflowRun`, the existing SQLite/PostgreSQL database-now lease pattern, and the analysis session fixtures and Worker tests.
- Produces: three typed lease primitives plus analysis-only claim, exhaustion, renewal, and owner-guarded mutations. Later optimization code may consume only these three primitives; analysis persistence, including its multi-table completion transaction, stays in `backend/analysis_runs.py`.

    # backend/workflow_leases.py
    from datetime import datetime

    from sqlalchemy import Update
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

    from backend.common import WorkflowType

    def workflow_lease_expiry(
        session: AsyncSession, lease_seconds: int
    ) -> ColumnElement[datetime]:
        ...

    def owned_workflow_lease(
        workflow_run_id: str, workflow_type: WorkflowType, lease_owner: str
    ) -> tuple[ColumnElement[bool], ...]:
        ...

    async def commit_owned_workflow_update(
        session: AsyncSession, statement: Update
    ) -> bool:
        ...

`workflow_lease_expiry()` returns PostgreSQL `func.now() + func.make_interval(secs=lease_seconds)` for PostgreSQL and `func.datetime(func.now(), f"+{lease_seconds} seconds")` for SQLite, matching the existing analysis lease behavior. `owned_workflow_lease()` returns conditions on `WorkflowRun.id`, `WorkflowRun.workflow_type`, `WorkflowRun.status == WorkflowStatus.PROCESSING`, `WorkflowRun.lease_owner`, and `WorkflowRun.lease_expires_at > func.now()`. `commit_owned_workflow_update()` executes exactly the conditional update, commits only when `result.rowcount > 0`, otherwise rolls back and returns `False`; it returns `True` after the successful commit.

`backend/analysis_runs.py` retains `_insert_for()` and every analysis candidate, result, and audit write. `create_analysis_run()` persists `workflow_type=WorkflowType.ANALYSIS`. Its expired-attempt exhaustion `UPDATE` and eligible `SELECT ... FOR UPDATE SKIP LOCKED` each include `WorkflowRun.workflow_type == WorkflowType.ANALYSIS`. `renew_analysis_lease()`, `update_analysis_step()`, `finalize_analysis_run()`, and `fail_analysis_run()` each build their where conditions with `owned_workflow_lease(run_id, WorkflowType.ANALYSIS, owner)` and use `commit_owned_workflow_update()` for their simple conditional updates. `persist_analysis_completion()` instead keeps its existing locked multi-table transaction: its locking `SELECT` uses `owned_workflow_lease(run_id, WorkflowType.ANALYSIS, owner)`, its candidate/result/audit upserts remain in that transaction, and it retains the final `session.commit()`. When that locking select finds no live owned analysis row, it rolls back the whole transaction and returns `False`; no candidate, result, or audit row may commit. No helper may match an `optimization` row solely because it has the same owner string.

- [ ] **Step 1: Write failing shared-lease and analysis-isolation tests**

Create `tests/test_workflow_leases.py` with the existing SQLite async-session fixture. Seed one live `analysis` processing run and one live `optimization` processing run owned by the same worker. Run each zero-row assertion in a fresh session or reload with `populate_existing=True` so an ORM identity-map value cannot conceal a rollback.

    async def test_owned_analysis_predicate_never_updates_same_owner_optimization_row(session) -> None:
        analysis, optimization = await _add_live_typed_runs(session, owner="worker-a")
        statement = (
            update(WorkflowRun)
            .where(*owned_workflow_lease(analysis.id, WorkflowType.ANALYSIS, "worker-a"))
            .values(current_step="analysis-next")
        )
        assert await commit_owned_workflow_update(session, statement) is True

        stale_statement = (
            update(WorkflowRun)
            .where(*owned_workflow_lease(optimization.id, WorkflowType.ANALYSIS, "worker-a"))
            .values(current_step="must-not-write")
        )
        assert await commit_owned_workflow_update(session, stale_statement) is False

        fresh = await session.get(WorkflowRun, optimization.id, populate_existing=True)
        assert fresh.current_step is None

Add a dialect assertion that a SQLite-bound `workflow_lease_expiry(session, 60)` renders `datetime` and does not render `make_interval`. Add one matching-owner update that commits and one stale-owner update whose rowcount is zero, followed by a fresh-session assertion that the original `current_step` remains unchanged.

Extend the existing analysis-completion test with an expired or reassigned owner before `persist_analysis_completion()`. Assert it returns `False`, then query in a fresh session and assert that the candidate, result, and `agent_calls` audit counts are unchanged. This test fixes the completion boundary: lost ownership rolls back the complete multi-table transaction rather than allowing a partial candidate, result, or audit write.

In `tests/test_analysis_worker.py`, seed one eligible analysis run alongside three same-store optimization rows: one `accepted`, one expired `processing` at attempt `2`, and one expired `processing` at attempt `3`. Run the existing analysis claim/Worker path. Assert the valid analysis run follows its current contract, while every optimization row retains its original status, attempt count, lease owner, lease expiry, current step, and failure fields. The latter two rows cover both the eligible-claim and exhausted-expired predicates.

- [ ] **Step 2: Run RED and verify the missing shared contract and type filters**

Run:

    # First apply “Unified D-drive development and verification environment” above.
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_workflow_leases.py tests/test_analysis_worker.py -v

Expected initial failure: `ModuleNotFoundError` for `backend.workflow_leases` or an explicit missing exported helper. Once the helper exists but before analysis type predicates are added, the isolation assertion must fail because an optimization row is claimed, exhausted, or changed. Syntax errors, fixture setup failures, missing dependencies, and unrelated failures are invalid RED evidence.

- [ ] **Step 3: Implement the minimum shared primitives and explicit analysis filters**

Create `backend/workflow_leases.py` with only the three declared functions. Select the established database-now expression using `session.bind.dialect.name`, falling back to the existing SQLite expression only when the test bind is unavailable. Do not introduce a repository, queue, base Worker, factory, generic state machine, or a fourth convenience helper.

In `backend/analysis_runs.py`, replace only repeated database-now expiry, owned-processing predicate, and simple conditional execute/commit-or-rollback paths with the three shared primitives. Use `commit_owned_workflow_update()` only for `renew_analysis_lease()`, `update_analysis_step()`, `finalize_analysis_run()`, and `fail_analysis_run()`. In `persist_analysis_completion()`, use `owned_workflow_lease(run_id, WorkflowType.ANALYSIS, owner)` only in its existing locking `SELECT`; preserve its multi-table upserts and final `session.commit()`, and call `session.rollback()` with `False` when the owner/type/status/lease guard no longer matches. Add explicit `WorkflowType.ANALYSIS` to `create_analysis_run()`, the expired-attempt failure `UPDATE`, and the eligible claim `SELECT`. Preserve existing analysis status transitions, `_insert_for()`, result storage, error mapping, and attempt-count behavior. Do not add optimization claiming or change routes, Agents, RAG, migrations, or model mappings.

- [ ] **Step 4: Run GREEN and relevant regression gates**

Run:

    # First apply “Unified D-drive development and verification environment” above.
    $env:JWT_SECRET_KEY = "local-product-optimization-plan-verification-secret-at-least-32"
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_workflow_leases.py tests/test_analysis_worker.py tests/test_analysis_models.py -v
    D:\E-commerce_operations_env\python.exe -m pytest -v
    D:\E-commerce_operations_env\python.exe -m compileall backend tests
    D:\E-commerce_operations_env\python.exe -m alembic current
    D:\E-commerce_operations_env\python.exe -m alembic check
    git diff --check
    git status --short

Expected: shared tests prove SQLite expiry compatibility, matching-owner commit, stale-owner rollback, analysis/optimization isolation, and complete rollback of candidate/result/audit writes after completion loses its owner guard; analysis Worker and model behavior remain unchanged; the ordinary full suite stays offline with no model loading, Milvus, model repository access, or DeepSeek call; compilation uses the D-drive pycache configured by the named environment block; Alembic remains at the Task 1 head and reports no drift.

- [ ] **Step 5: Commit only the shared typed-lease refactor**

    git add -- backend/workflow_leases.py backend/analysis_runs.py tests/test_workflow_leases.py tests/test_analysis_worker.py
    git diff --cached --name-only
    # Expected exact output, in any Git display order:
    # backend/workflow_leases.py
    # backend/analysis_runs.py
    # tests/test_workflow_leases.py
    # tests/test_analysis_worker.py
    git diff --cached --check
    git commit -m "refactor: share typed workflow lease guards"
    git show --check --oneline HEAD
    git status --short

### Task 3: Shared DeepSeek JSON runtime with analysis compatibility

**Files:**

- Create: `backend/deepseek_runtime.py`
- Modify: `backend/analysis_agent.py`
- Create: `tests/test_deepseek_runtime.py`
- Modify: `tests/test_analysis_agent.py` only to retain the analysis-specific assertions after moving transport assertions.
- Do not modify: `backend/analysis_worker.py`, `backend/analysis_runs.py`, ORM mappings, migrations, lease SQL, RAG, routes, or `tests/test_deepseek_smoke.py`.

**Interfaces:**

- Consumes: `Settings.deepseek_api_key: SecretStr | None`, `deepseek_base_url`, `deepseek_model`, `deepseek_timeout_seconds`, optional `deepseek_price_per_million_tokens`, `httpx.AsyncClient` and `httpx.MockTransport`, plus the existing analysis response parser and audit record shape.
- Produces: one generic DeepSeek JSON HTTP runtime, safe generic attempt records, generic result values, and an analysis wrapper that preserves `DeepSeekAnalysisClient`, `AgentInvocation`, `AgentCallRecord`, `parse_agent_response()`, `validate_agent_response()`, and both analysis Prompts as public analysis behavior.

    # backend/deepseek_runtime.py
    from collections.abc import Awaitable, Callable, Mapping
    from dataclasses import dataclass
    from decimal import Decimal
    from typing import Generic, TypeVar

    import httpx

    T = TypeVar("T")
    JsonParser = Callable[[str], T]
    BeforeHttpAttempt = Callable[[int], Awaitable[bool]]

    @dataclass(frozen=True)
    class DeepSeekAttemptRecord:
        attempt: int
        model: str
        status: str
        input_hash: str
        prompt_tokens: int
        completion_tokens: int
        total_tokens: int
        duration_ms: int
        estimated_cost: Decimal | None
        error_code: str | None

    @dataclass(frozen=True)
    class DeepSeekJsonResult(Generic[T]):
        response: T | None
        records: list[DeepSeekAttemptRecord]
        error_code: str | None

    class DeepSeekJsonRuntime:
        def __init__(
            self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
        ) -> None:
            ...

        async def request(
            self,
            *,
            system_prompt: str,
            user_payload: Mapping[str, object],
            parse_response: JsonParser[T],
            before_http_attempt: BeforeHttpAttempt | None = None,
            max_attempts: int = 3,
        ) -> DeepSeekJsonResult[T]:
            ...

The runtime accepts `max_attempts` only in the closed interval `1..3` and raises `ValueError` outside it. Production callers use the default of three requests; the final explicit smoke caller passes one, so each Agent cannot make a retry. It owns only the actual `POST /chat/completions` request, `response_format: {"type": "json_object"}` payload assembly, canonical user-payload JSON, SHA-256 input hash, HTTP error classification, bounded retry, duration, usage, and optional cost. It never records a Prompt, raw response, Authorization value, API Key, node name, call type, prompt version, Pydantic model, or business-validation result.

Canonical user payload is `json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)`; `input_hash` is the SHA-256 hex digest of that exact UTF-8 string. The runtime reads `Settings.deepseek_api_key` only inside `request()` at the actual header construction boundary, verifies that `SecretStr.get_secret_value().strip()` is nonempty, and returns `DEEPSEEK_KEY_MISSING` with no record and no HTTP request otherwise. It calls `before_http_attempt(attempt)` immediately before every HTTP POST; a false callback returns `LEASE_LOST` with no additional request or record.

Each response attempt produces only `DeepSeekAttemptRecord` safe fields. A timeout maps to `DEEPSEEK_TIMEOUT`, a transport failure to `DEEPSEEK_TRANSPORT`, status `429` to `DEEPSEEK_RATE_LIMIT`, and `5xx` to `DEEPSEEK_SERVER_ERROR`; those four errors retry until the request cap. `401` maps to `DEEPSEEK_UNAUTHORIZED`, `403` to `DEEPSEEK_FORBIDDEN`, every other non-success response to `DEEPSEEK_HTTP_ERROR`, and malformed completion content or a `ValueError` from `parse_response` to `DEEPSEEK_SCHEMA_INVALID`; none of those errors retry. Missing, non-mapping, boolean, negative, or non-coercible usage values normalize to zero. When price is absent, `estimated_cost` is `None`; otherwise it uses nonnegative price times total tokens divided by one million, quantized to `Decimal("0.000001")`.

`backend/analysis_agent.py` keeps the current `DeepSeekAnalysisClient(settings, transport=None)` constructor and:

    async def request(
        self,
        facts: AnalysisFacts,
        *,
        call_type: AgentCallType,
        before_http_attempt: BeforeHttpAttempt | None = None,
    ) -> AgentInvocation:
        ...

The analysis wrapper selects the existing primary or schema-repair system Prompt, supplies `{"facts": facts.model_dump(mode="json")}` as `user_payload`, passes `parse_agent_response` as the parser, and adapts the current `BeforeHttpAttempt` callback from `(call_type, attempt)` to the runtime's `(attempt)` form. It converts every `DeepSeekAttemptRecord` into the unchanged analysis `AgentCallRecord` by assigning the existing `_node_name(call_type)`, the supplied `call_type`, and `PROMPT_VERSION`. `AgentInvocation` still carries the parsed `AgentAnalysisResponse | None`, mapped records, and the same error codes. `validate_agent_response()` remains solely in the analysis layer, so trusted-fact coverage/rank checks and the decision to run one schema-repair node remain unchanged. Analysis audit persistence continues to use database-default `AgentCall.iteration = 0`; Task 3 does not create optimization or compliance Agents.

- [ ] **Step 1: Write the focused failing runtime and compatibility tests**

Create `tests/test_deepseek_runtime.py` by moving the existing HTTP/transport assertions into compact parameterized cases using only `httpx.MockTransport` and a deterministic JSON parser. Use a synthetic `SecretStr("test-only-transport-token")` supplied through `Settings(_env_file=None, ...)`; tests never inspect a real Key.

    async def test_runtime_retries_only_transient_failures_and_renews_before_each_post() -> None:
        responses = iter([httpx.Response(429), httpx.Response(500), _completion({"ok": True})])
        attempts: list[int] = []
        posts = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal posts
            posts += 1
            assert request.url.path == "/chat/completions"
            assert json.loads(request.content)["response_format"] == {"type": "json_object"}
            return next(responses)

        async def renew(attempt: int) -> bool:
            attempts.append(attempt)
            return True

        result = await _runtime(httpx.MockTransport(handler)).request(
            system_prompt="test prompt",
            user_payload={"facts": {"product_id": "p-1"}},
            parse_response=lambda content: json.loads(content),
            before_http_attempt=renew,
        )
        assert result.response == {"ok": True}
        assert attempts == [1, 2, 3]
        assert posts == len(result.records) == 3

Add compact parameterized assertions for timeout, transport failure, `429`, and `5xx` exhausting at three records; `400`, `401`, `403`, parser `ValueError`, malformed `choices[0].message.content`, and invalid `max_attempts` ending without a retry; missing or blank Key and a false first lease callback causing zero HTTP posts; canonical hashes that match for differently ordered equivalent payload mappings; malformed usage values normalizing to zero; absent price yielding `None`; and a valid nonnegative price yielding a quantized deterministic cost. Assert every generic record omits Prompt, raw response, headers, Authorization, and Key fields.

Reduce `tests/test_analysis_agent.py` to analysis-specific regression: primary versus repair Prompt separation, `call_analysis_agent` versus `validate_and_reconcile` node/call-type mapping, `parse_agent_response()` and `validate_agent_response()` trusted-fact/Pydantic checks, degraded drafts, and the unchanged `DeepSeekAnalysisClient(...).request(facts, call_type=..., before_http_attempt=...)` use. Leave `tests/test_deepseek_smoke.py` untouched as the public-client compatibility consumer.

- [ ] **Step 2: Run RED and verify the new boundary is absent**

Run:

    # First apply “Unified D-drive development and verification environment” above.
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_deepseek_runtime.py tests/test_analysis_agent.py tests/test_deepseek_smoke.py -v

Expected: collection fails because `backend.deepseek_runtime` and its declared runtime types are absent, or runtime assertions fail because the analysis client has not delegated to the shared boundary. The failure must identify the absent shared contract or unchanged duplicate transport loop; syntax failures, fixture errors, dependency failures, and any real-network attempt are invalid RED evidence.

- [ ] **Step 3: Implement the minimum reusable JSON boundary and analysis adapter**

Create `backend/deepseek_runtime.py` with exactly `DeepSeekAttemptRecord`, `DeepSeekJsonResult[T]`, `JsonParser[T]`, `BeforeHttpAttempt`, and `DeepSeekJsonRuntime`. Move the existing `httpx` timeout/client construction, three-attempt transient classification, DeepSeek JSON-object request format, safe usage/cost normalization, and input hashing into it. Use `SecretStr` only at header construction. Catch only `httpx.TimeoutException`, `httpx.TransportError`, and expected response/parser shape errors; do not catch cancellation or broad base exceptions.

Replace the duplicate HTTP loop in `DeepSeekAnalysisClient.request()` with one runtime request. Keep `_canonical_facts()` only if analysis needs it for its existing degraded-record path; do not create a second runtime hash or retry helper. Keep both analysis Prompt strings, `AgentSchemaError`, `parse_agent_response()`, `validate_agent_response()`, `AgentInvocation`, `AgentCallRecord`, and analysis-specific record mapping in `backend/analysis_agent.py`. Do not modify the Worker graph, its retry behavior, `analysis_runs` persistence, model mappings, migrations, routes, RAG, or public smoke tests.

- [ ] **Step 4: Run GREEN and compatibility regression gates**

Run:

    # First apply “Unified D-drive development and verification environment” above.
    $env:JWT_SECRET_KEY = "local-product-optimization-plan-verification-secret-at-least-32"
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_deepseek_runtime.py tests/test_analysis_agent.py tests/test_deepseek_smoke.py -v
    D:\E-commerce_operations_env\python.exe -m pytest -v
    D:\E-commerce_operations_env\python.exe -m compileall backend tests
    D:\E-commerce_operations_env\python.exe -m alembic current
    D:\E-commerce_operations_env\python.exe -m alembic check
    git diff --check
    git status --short

Expected: all transport coverage uses `MockTransport` with zero external requests; the public analysis client and smoke orchestration stay compatible; the default ordinary process leaves `RUN_DEEPSEEK_SMOKE` absent and the real smoke remains skipped; analysis persists only iteration zero calls; all normal tests stay offline without DeepSeek, model loading, Milvus, or model-repository access; compilation uses the D-drive pycache configured by the named environment block; Alembic remains at the Task 1 head without drift.

- [ ] **Step 5: Commit only the DeepSeek JSON runtime refactor**

    git add -- backend/deepseek_runtime.py backend/analysis_agent.py tests/test_deepseek_runtime.py tests/test_analysis_agent.py
    git diff --cached --name-only
    # Expected exact output, in any Git display order:
    # backend/deepseek_runtime.py
    # backend/analysis_agent.py
    # tests/test_deepseek_runtime.py
    # tests/test_analysis_agent.py
    git diff --cached --check
    git commit -m "refactor: share DeepSeek JSON runtime"
    git show --check --oneline HEAD
    git status --short

### Task 4: Atomic product selection and store-scoped proposal API

**Files:**

- Create: `backend/proposals.py`
- Modify: `backend/schemas.py`
- Modify: `backend/routes.py`
- Create: `tests/test_optimization_api.py`
- Keep unchanged: `backend/analysis_runs.py`; its existing workflow-row read already returns the ORM mapping required by the view adjustment.
- Do not modify: global auth semantics, Agents, RAG, Workers, migrations, model mappings, or a second state machine.

**Interfaces:**

- Consumes: Task 1 `WorkflowType`, `WorkflowStatus`, `ProductProposal`, `ProposalRevision`, `ComplianceReview`, typed `WorkflowRun` and `AgentCall`; current `get_current_user` JWT dependency; `User`, `UserStoreScope`, `Store`, `Product`, and `AnalysisCandidate` ORM rows; and FastAPI's existing `HTTPException` response convention.
- Produces: atomic selection and safe proposal-read service functions, stable domain result/error values, select-product and proposal-read routes, type-compatible workflow views, and safe Pydantic response models. The route never commits; `backend.proposals` owns each selection transaction's commit or rollback.

    # backend/proposals.py
    from dataclasses import dataclass
    from typing import Literal

    from sqlalchemy.ext.asyncio import AsyncSession

    @dataclass(frozen=True)
    class ProposalDomainError(Exception):
        code: str
        status_code: int

    @dataclass(frozen=True)
    class ProductSelectionResult:
        proposal: ProductProposal
        optimization_run: WorkflowRun
        created: bool

    @dataclass(frozen=True)
    class ProposalReadResult:
        proposal: ProductProposal
        optimization_run: WorkflowRun
        current_revision: ProposalRevision | None
        current_review: ComplianceReview | None

    async def select_product_for_optimization(
        session: AsyncSession,
        actor_id: str,
        analysis_run_id: str,
        candidate_id: str,
        idempotency_key: str | None,
    ) -> ProductSelectionResult:
        ...

    async def get_proposal_for_actor(
        session: AsyncSession, actor_id: str, proposal_id: str
    ) -> ProposalReadResult:
        ...

`ProposalDomainError` is the only expected domain exception; it carries a stable short code and HTTP status but never a database message, key, Prompt, or row contents. The routes map it to `HTTPException(status_code=error.status_code, detail={"code": error.code})`. `select_product_for_optimization()` catches unanticipated `SQLAlchemyError`, rolls back, and raises `ProposalDomainError("ANALYSIS_SELECTION_UNAVAILABLE", 503)`; `get_proposal_for_actor()` rolls back and raises `ProposalDomainError("PROPOSAL_READ_UNAVAILABLE", 503)`. Neither function catches cancellation or reports database detail.

`select_product_for_optimization()` first validates the header value itself: it accepts only a present `str` whose trimmed form is nonempty and whose character length is `1..128`. It raises `ProposalDomainError("ANALYSIS_IDEMPOTENCY_KEY_INVALID", 400)` otherwise. It computes `hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()` once and stores only that 64-character digest in `ProductProposal.selection_idempotency_hash`; the hash deliberately uses the original, untrimmed UTF-8 string. It neither logs nor returns the source key, including on replay.

In one service-owned transaction, lock and freshly validate in this order: `User` by `actor_id`, analysis `WorkflowRun` by `analysis_run_id`, its `Store`, and exact `UserStoreScope`. Every authorization- or status-sensitive ORM `SELECT` in both exported functions uses `.execution_options(populate_existing=True)` and a database query; locking selects additionally use `.with_for_update()`. This includes the current `User`, `Store`, `UserStoreScope`, run, candidate, and product rows, so no check reads attributes from `get_current_user` or another identity-map-cached ORM object. The current user must be active, exactly `UserRole.OPERATOR`, and have the exact database `UserStoreScope(user_id=actor_id, store_id=run.store_id)`; admin and supervisor never bypass this lookup. The store must be enabled. Unknown resources and missing scope use `ProposalDomainError("ANALYSIS_SELECTION_NOT_FOUND", 404)` so cross-store callers cannot distinguish existence. A current active user with a non-operator role receives `ProposalDomainError("ANALYSIS_SELECTION_FORBIDDEN", 403)`. On normal HTTP requests, `get_current_user` has already re-read the user and returns 401 for a missing or disabled token; the service repeats current status, role, and scope checks so it never trusts JWT claims during a race.

Immediately after those user/run/store/scope checks, query `ProductProposal` by the locked `analysis_run_id`, before checking the analysis type/status or candidate. If a proposal exists with `analysis_candidate_id == candidate_id`, load and validate its linked optimization run, commit the read-only transaction to release locks, and return `ProductSelectionResult(..., created=False)` using the session's `expire_on_commit=False` objects. This is a legal replay even after the analysis run is `COMPLETED/product_selected`, and it preserves the original hash. If an existing proposal names a different candidate, roll back and raise `ProposalDomainError("ANALYSIS_SELECTION_CONFLICT", 409)`. If the linked optimization run is missing, not `WorkflowType.OPTIMIZATION`, or not linked to this proposal, roll back and raise `ANALYSIS_SELECTION_UNAVAILABLE` (503); never return detached or expired ORM data.

Only when no proposal exists must the locked run be `WorkflowType.ANALYSIS` and `WorkflowStatus.AWAITING_SELECTION`; type mismatch returns `ANALYSIS_SELECTION_INVALID_TYPE` (409) and non-ready analysis returns `ANALYSIS_SELECTION_NOT_READY` (409). Then lock `AnalysisCandidate` by `candidate_id` and the workflow-run foreign key, and lock the candidate `Product`. The product must match `candidate.product_id` and `run.store_id`; absence or foreign ownership returns the same 404 code, while an internally inconsistent product association returns `ANALYSIS_PRODUCT_MISMATCH` (409). Capture `product.current_version` only after this product lock.

    optimization_run = WorkflowRun(
        id=optimization_run_id,
        workflow_type=WorkflowType.OPTIMIZATION,
        store_id=run.store_id,
        created_by=actor_id,
        start_date=None,
        end_date=None,
        status=WorkflowStatus.ACCEPTED,
        quality_status=WorkflowQuality.NORMAL,
        input={
            "proposal_id": proposal_id,
            "source_analysis_run_id": run.id,
            "analysis_candidate_id": candidate.id,
            "product_id": product.id,
            "store_id": run.store_id,
        },
    )
    proposal = ProductProposal(
        id=proposal_id,
        analysis_run_id=run.id,
        analysis_candidate_id=candidate.id,
        optimization_run_id=optimization_run.id,
        store_id=run.store_id,
        product_id=product.id,
        base_product_version=product.current_version,
        selection_idempotency_hash=key_hash,
    )
    run.status = WorkflowStatus.COMPLETED
    run.current_step = "product_selected"

Commit only after all four changes are staged. On an `IntegrityError`, roll back, then read the existing proposal and its linked optimization run in a new transaction: same candidate commits that read-only transaction before returning a serializable replay result, another candidate rolls back with `ANALYSIS_SELECTION_CONFLICT`, and a missing/inconsistent proposal/run raises the safe selection 503 code after rollback. This treats Task 1 unique constraints as a concurrency backstop without inventing SQLite concurrency behavior. Every validation failure, conflict, or database failure rolls back the selection transaction; it cannot leave a proposal, optimization run, or completed analysis run by itself.

`get_proposal_for_actor()` freshly reads the current actor and permits only active `operator`, `supervisor`, or `admin`. It obtains the proposal and linked store, then requires an exact `UserStoreScope` for all three roles, including admin. Missing proposal, missing scope, cross-store proposal, or disabled store produces `ProposalDomainError("PROPOSAL_NOT_FOUND", 404)`; a current actor that is inactive or has an unsupported role produces `ProposalDomainError("PROPOSAL_READ_FORBIDDEN", 403)` for the service boundary. Normal HTTP disabled-token requests never reach this function: existing `get_current_user` returns 401 after reading the current database user.

It reads the optimization workflow and, when `ProductProposal.current_revision_id` is non-null, loads `ProposalRevision` with both `ProposalRevision.id == proposal.current_revision_id` and `ProposalRevision.proposal_id == proposal.id`. Missing that exact row is `ProposalDomainError("PROPOSAL_DATA_INCONSISTENT", 503)`. For a loaded revision, query its review with all three conditions `ComplianceReview.proposal_revision_id == revision.id`, `ComplianceReview.proposal_id == proposal.id`, and `ComplianceReview.iteration == revision.iteration`; if any review linked to that revision exists but the three-condition query does not return it, raise the same 503 code. A no-review result is valid only when no review exists for that revision. These checks prevent a broken foreign-key chain from exposing another proposal's revision or review.

    # backend/schemas.py
    class ProductSelectionRequest(BaseModel):
        candidate_id: str = Field(min_length=1, max_length=36)

    class ProductSelectionView(BaseModel):
        proposal_id: str
        optimization_workflow_run_id: str
        status: Literal["accepted"]

    class OptimizationWorkflowSummary(BaseModel):
        id: str
        workflow_type: WorkflowType
        status: WorkflowStatus
        quality_status: WorkflowQuality
        error_code: str | None

    class ProposalRevisionView(BaseModel):
        id: str
        iteration: int
        base_product_version: int
        proposal_output: dict[str, object]
        citations: list[dict[str, object]]

    class ComplianceReviewView(BaseModel):
        id: str
        iteration: int
        deterministic_checks: dict[str, object]
        semantic_review: dict[str, object]
        passed: bool
        risk_level: ComplianceRiskLevel
        quality_status: WorkflowQuality
        required_changes: list[dict[str, object]]
        citations: list[dict[str, object]]
        error_code: str | None

    class ProposalView(BaseModel):
        id: str
        analysis_run_id: str
        analysis_candidate_id: str
        optimization_run_id: str
        store_id: str
        product_id: str
        base_product_version: int
        current_revision_id: str | None
        created_at: datetime
        updated_at: datetime

    class ProposalDetailView(BaseModel):
        proposal: ProposalView
        optimization_run: OptimizationWorkflowSummary
        current_revision: ProposalRevisionView | None
        current_review: ComplianceReviewView | None

`ProposalView` deliberately excludes `selection_idempotency_hash`. None of these responses has lease fields, checkpoint fields, `WorkflowRun.input`, `WorkflowRun.output`, raw prompt/response fields, paths, vectors, or Keys. `proposal_output`, citations, and review JSON are already validated business JSON from Task 1 and use explicit `dict`/`list` response types rather than future schemas.

Change the existing workflow view only as follows:

    class WorkflowRunView(BaseModel):
        model_config = ConfigDict(from_attributes=True)

        id: str
        workflow_type: WorkflowType
        store_id: str
        start_date: date | None
        end_date: date | None
        status: WorkflowStatus
        quality_status: WorkflowQuality
        current_step: str | None
        attempt_count: int
        candidates_ready: bool
        error_code: str | None

The existing `GET /workflow-runs/{id}` maps `workflow_type=run.workflow_type` and sets `candidates_ready` only when `run.workflow_type is WorkflowType.ANALYSIS and run.status is WorkflowStatus.AWAITING_SELECTION`. It no longer hard-codes analysis or assumes non-null dates.

Add exactly these routes, retaining `get_current_user` rather than changing global auth:

    @router.post(
        "/analysis-runs/{analysis_run_id}/select-product",
        response_model=ProductSelectionView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def select_product_route(
        response: Response,
        analysis_run_id: str,
        request: ProductSelectionRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> ProductSelectionView:
        ...

    @router.get("/proposals/{proposal_id}", response_model=ProposalDetailView)
    async def read_proposal_route(
        proposal_id: str,
        user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> ProposalDetailView:
        ...

The select route passes `request.candidate_id`, invokes the service once, and never calls `session.commit()` or `session.rollback()` itself. It sets HTTP `202` for `created=True` and `200` for `created=False`; both responses set `ProductSelectionView.status` to the linked optimization workflow's `accepted` value. The read route maps only safe service fields into the declared models. Both routes return 401 through existing missing/invalid/disabled-token behavior, map only `ProposalDomainError` to the declared 4xx/503 code, and leave unexpected framework errors untouched. They do not log or echo the idempotency Key.

- [ ] **Step 1: Write the failing real-JWT SQLite API tests**

Create `tests/test_optimization_api.py` with the existing real FastAPI app, SQLite `get_session` override, `create_access_token()`, and seeded active users/stores/products/candidates. Do not replace auth dependencies. Seed an analysis run that is `ANALYSIS/AWAITING_SELECTION`, an eligible candidate/product in the target store, and a separate store/candidate/product for no-leak cases.

    async def test_select_product_commits_all_four_selection_changes_once(
        client, operator_token, session
    ) -> None:
        response = await client.post(
            "/analysis-runs/analysis-1/select-product",
            json={"candidate_id": "candidate-1"},
            headers={**_headers(operator_token), "Idempotency-Key": "client-key-1"},
        )

        assert response.status_code == 202
        body = response.json()
        proposal = await session.get(ProductProposal, body["proposal_id"])
        optimization = await session.get(WorkflowRun, body["optimization_workflow_run_id"])
        analysis = await session.get(WorkflowRun, "analysis-1")
        assert proposal is not None and optimization is not None and analysis is not None
        assert len(proposal.selection_idempotency_hash) == 64
        assert proposal.selection_idempotency_hash != "client-key-1"
        assert (optimization.workflow_type, optimization.status, optimization.start_date, optimization.end_date) == (
            WorkflowType.OPTIMIZATION, WorkflowStatus.ACCEPTED, None, None
        )
        assert analysis.status is WorkflowStatus.COMPLETED
        assert analysis.current_step == "product_selected"

Add a replay with the same run/candidate but a different valid key and assert `200`, response JSON serializes the same proposal/run IDs, original key hash unchanged, and one proposal plus one optimization row even though the analysis row is already `COMPLETED/product_selected`. Add a different candidate selection for the same analysis run and assert `409` with `{"detail": {"code": "ANALYSIS_SELECTION_CONFLICT"}}` and no extra row.

Add parameterized missing, whitespace-only, and 129-character key requests that return `400/ANALYSIS_IDEMPOTENCY_KEY_INVALID` with zero writes. Add a 128-character Chinese key that succeeds, proving the length check is character-based while the stored digest remains the original UTF-8 bytes' SHA-256. Assert scope removal and role change after token issuance fail against the current database state; a disabled old token is rejected as 401 by `get_current_user`; supervisor and admin with an exact scope receive `403` for selection. Assert unknown/cross-store runs and candidates return the selected safe `404` code; invalid workflow type, non-ready state, candidate outside the run, and product/candidate/store mismatch return their declared stable code and leave analysis status, proposal count, and optimization-run count unchanged.

Add a same-session identity-map regression for both select and read: first load the target `User`, `Store`, and `UserStoreScope` into the fixture session, then use explicit SQL `UPDATE` statements with `synchronize_session=False` to change user status, user role, or store `enabled`, and an explicit SQL `DELETE` for the scope. Reuse the same token and session for each request. Assert selection/read reject according to the refreshed current state (401 when the existing authentication dependency observes a disabled user, otherwise the declared 403/404 domain result), and assert no selection rows are written. This proves service authorization does not reuse cached attributes after the database changes.

For `GET /proposals/{id}`, assert active operator, supervisor, and admin with an exact scope can read only the proposal's store; admin without a scope, an unknown ID, and a cross-store request all receive the same safe `404`. Assert the response contains workflow status/quality/error, safe proposal IDs/base version, and `null` for both current revision and review when none exists. Seed a linked revision and its single review and assert their declared JSON-safe fields appear. Seed a proposal whose `current_revision_id` points at another proposal's revision, and a revision whose review has another proposal ID or a mismatched iteration; each must return `503/PROPOSAL_DATA_INCONSISTENT` without exposing either foreign object. Assert absent fields include the idempotency hash, lease data, checkpoint, input, output, raw response, Prompt, path, vector, and Key.

Finally, seed an `OPTIMIZATION` run with null dates and assert `GET /workflow-runs/{id}` returns its actual `workflow_type`, `start_date: null`, `end_date: null`, and `candidates_ready: false`. Retain the existing analysis API tests unchanged to show analysis views stay compatible.

- [ ] **Step 2: Run RED and verify routes and service contracts are absent**

Run:

    # First apply “Unified D-drive development and verification environment” above.
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_optimization_api.py tests/test_analysis_api.py -v

Expected: collection or route assertions fail because `backend.proposals`, the body-only `ProductSelectionRequest`, proposal response schemas, and select/read proposal routes do not exist, or because `WorkflowRunView` rejects an optimization type with null dates. The failure must identify those absent Task 4 contracts; a JWT fixture error, syntax failure, network call, model load, or DeepSeek call is invalid RED evidence.

- [ ] **Step 3: Implement the smallest atomic service and route surface**

Create `backend/proposals.py` with exactly the two exported service functions, three frozen result/error dataclasses, and private local mapping helpers needed to build `ProposalReadResult`. It owns all selection `commit()` and `rollback()` calls. For every authorization/status-sensitive read, construct `select(Model).where(...).execution_options(populate_existing=True)` and never inspect an already-loaded `User`, `Store`, `UserStoreScope`, run, candidate, or product object without that refresh; apply `.with_for_update()` to the locked selection rows. Lock/revalidate user, run, store, and exact scope, then query existing proposal before any `AWAITING_SELECTION` or candidate/product check. For legal replay, verify the optimization link and `await session.commit()` before returning `expire_on_commit=False` data; use rollback only for conflict/error paths. Do not invoke `require_store_access()` because its admin bypass conflicts with this phase's policy. Implement the revision/review conjunction checks and the `PROPOSAL_DATA_INCONSISTENT` 503 boundary exactly as declared above. Do not create a repository, service base, RBAC framework, factory, queue, or another lifecycle/status structure.

Add only the listed Pydantic models to `backend/schemas.py` and make the `WorkflowRunView` date/type adjustment. In `backend/routes.py` import the service/models, add the two routes, and map `ProductSelectionResult.created` to 202/200 without an additional transaction call. Preserve existing auth and route behavior. Leave `backend/analysis_runs.py` untouched.

- [ ] **Step 4: Run GREEN and API regression gates**

Run:

    # First apply “Unified D-drive development and verification environment” above.
    $env:JWT_SECRET_KEY = "local-product-optimization-plan-verification-secret-at-least-32"
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_optimization_api.py tests/test_analysis_api.py -v
    D:\E-commerce_operations_env\python.exe -m pytest -v
    D:\E-commerce_operations_env\python.exe -m compileall backend tests
    D:\E-commerce_operations_env\python.exe -m alembic current
    D:\E-commerce_operations_env\python.exe -m alembic check
    git diff --check
    git status --short

Expected: first selection atomically writes the proposal, optimization run, and analysis terminal state; replay after `COMPLETED/product_selected` commits the read-only transaction, serializes safely, does not write, and preserves the original key hash; conflicting reselection cannot modify the original; body-only candidate input, character-based key limits, current-role/status/scope, no-leak, broken revision/review-chain, and same-session stale-identity-map cases use their exact status/code; proposal and optimization workflow responses contain no internal containers or secrets; ordinary tests remain SQLite/Mock-only with no network, DeepSeek, model, Milvus, or model-repository access; compilation uses the D-drive environment; Alembic remains at the Task 1 head without drift.

- [ ] **Step 5: Commit only the selection and proposal API deliverable**

    git add -- backend/proposals.py backend/schemas.py backend/routes.py tests/test_optimization_api.py
    git diff --cached --name-only
    # Expected exact output, in any Git display order:
    # backend/proposals.py
    # backend/schemas.py
    # backend/routes.py
    # tests/test_optimization_api.py
    git diff --cached --check
    git commit -m "feat: add product selection and proposal API"
    git show --check --oneline HEAD
    git status --short

### Task 5: Strict optimization output contract and deterministic compliance

**Files:**

- Modify: `backend/schemas.py`
- Create: `backend/optimization_validation.py`
- Create: `tests/test_optimization_validation.py`
- Do not modify: database mappings, migrations, routes, Agents, RAG, Workers, the DeepSeek runtime, or the existing `KnowledgeCitation` API schema.

**Interfaces:**

- Consumes: Task 1 `ComplianceRiskLevel` and `WorkflowQuality`, server-built product/candidate/RAG facts, and canonical rule citations supplied only after active/applicable knowledge retrieval.
- Produces: strict internal trusted-input and optimization-output Pydantic models, one pure deterministic validator, stable violation/result values, and canonical citations selected solely from trusted input. A later semantic Agent must require both this result's `passed` value and its own result to be true; it cannot override a deterministic failure.

    # backend/schemas.py
    from decimal import Decimal
    from typing import Literal

    from pydantic import BaseModel, ConfigDict, Field

    class CanonicalRuleCitation(BaseModel):
        model_config = ConfigDict(extra="forbid")
        document_id: str = Field(max_length=36)
        version_id: str = Field(max_length=36)
        chunk_id: str = Field(max_length=128)
        document_name: str = Field(max_length=255)
        version_number: int
        category: str = Field(max_length=64)
        canonical_text: str = Field(max_length=12000)
        active: bool
        applicable: bool

    class TrustedProductSku(BaseModel):
        model_config = ConfigDict(extra="forbid")
        id: str = Field(max_length=36)
        code: str = Field(max_length=64)
        spec: dict[str, str] = Field(max_length=50)
        price: Decimal
        stock: int

    class TrustedOptimizationInput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        store_id: str = Field(max_length=36)
        product_id: str = Field(max_length=36)
        base_product_version: int
        title: str = Field(max_length=512)
        category: str = Field(max_length=128)
        brand: str = Field(max_length=128)
        selling_points: list[str] = Field(max_length=20)
        description: str = Field(max_length=8000)
        search_keywords: list[str] = Field(max_length=100)
        attributes: dict[str, str] = Field(max_length=50)
        skus: list[TrustedProductSku] = Field(max_length=100)
        candidate_metrics: ProductMetrics
        candidate_evidence: list[str] = Field(max_length=50)
        rag_quality: Literal["normal", "zero_hit", "low_confidence"]
        canonical_rule_citations: list[CanonicalRuleCitation] = Field(max_length=50)

    class EvidenceRef(BaseModel):
        model_config = ConfigDict(extra="forbid")
        kind: Literal["fact", "citation"]
        value: str = Field(min_length=1, max_length=128)

    class OutputCitation(BaseModel):
        model_config = ConfigDict(extra="forbid")
        chunk_id: str = Field(min_length=1, max_length=128)

    class DescriptionSection(BaseModel):
        model_config = ConfigDict(extra="forbid")
        heading: str = Field(max_length=256)
        body: str = Field(max_length=4000)
        evidence: list[EvidenceRef] = Field(max_length=20)

    class OptimizationChange(BaseModel):
        model_config = ConfigDict(extra="forbid")
        field: Literal["title", "selling_points", "description", "keywords"]
        current_value: str | list[str] | list[DescriptionSection]
        suggested_value: str | list[str] | list[DescriptionSection]
        reason: str = Field(max_length=1024)
        evidence: list[EvidenceRef] = Field(max_length=20)

    class AttributeCompletion(BaseModel):
        model_config = ConfigDict(extra="forbid")
        target_attribute: str = Field(min_length=1, max_length=64)
        current_value: str | None = Field(max_length=512)
        suggested_value: str = Field(max_length=512)
        reason: str = Field(max_length=1024)
        evidence: list[EvidenceRef] = Field(max_length=20)

    class PriceSuggestion(BaseModel):
        model_config = ConfigDict(extra="forbid")
        target_sku_id: str = Field(min_length=1, max_length=36)
        current_price: Decimal
        suggested_price: Decimal
        reason: str = Field(max_length=1024)
        evidence: list[EvidenceRef] = Field(max_length=20)

    class SkuSuggestion(BaseModel):
        model_config = ConfigDict(extra="forbid")
        target_sku_id: str = Field(min_length=1, max_length=36)
        current_code: str = Field(max_length=64)
        current_spec: dict[str, str] = Field(max_length=50)
        suggested_code: str = Field(max_length=64)
        suggested_spec: dict[str, str] = Field(max_length=50)
        reason: str = Field(max_length=1024)
        evidence: list[EvidenceRef] = Field(max_length=20)

    class OptimizationProposalOutput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        title: str = Field(max_length=512)
        selling_points: list[str] = Field(max_length=20)
        description: list[DescriptionSection] = Field(max_length=20)
        keywords: list[str] = Field(max_length=100)
        attribute_completions: list[AttributeCompletion] = Field(max_length=50)
        changes: list[OptimizationChange] = Field(max_length=50)
        citations: list[OutputCitation] = Field(max_length=50)
        price_suggestions: list[PriceSuggestion] = Field(max_length=50)
        sku_suggestions: list[SkuSuggestion] = Field(max_length=50)

All models use `extra="forbid"` and only protective serialization/resource caps above; their strings and lists may be syntactically valid yet violate the business rules below, which the pure validator reports rather than turning into a parser failure. `description` is an ordered list of `DescriptionSection` values: each section has only a plain-Unicode `heading`, a plain-Unicode `body`, and its evidence list. Newlines are permitted in a body; HTML, Markdown fragments, and nested JSON are not part of this contract. Its business section count, heading/body length, and evidence requirements are checked by the validator.

Trusted input is server-built: it contains the exact store/product/base version; current `title`, `category`, `brand`, `selling_points`, `description`, `search_keywords`, and `attributes`; every current SKU's ID/code/spec/price/stock; the existing complete `ProductMetrics` selected-candidate snapshot and deterministic evidence; RAG quality; and canonical rule citations. `CanonicalRuleCitation` is distinct from the existing `KnowledgeCitation` and includes the full `document_id`, `version_id`, `chunk_id`, `document_name`, `version_number`, `category`, and `canonical_text` fields plus server-only `active` and `applicable` facts. The model output never supplies those canonical fields: `OutputCitation` and citation `EvidenceRef` may name only a trusted `chunk_id`, which the validator resolves against a citation with both `active=True` and `applicable=True`. A later Task 9 PostgreSQL boundary rechecks both server facts before it builds this trusted input; Task 5 does not query a database.

    # backend/optimization_validation.py
    from dataclasses import dataclass
    from typing import Literal

    @dataclass(frozen=True)
    class DeterministicViolation:
        code: str
        field: str
        message_zh: str

    @dataclass(frozen=True)
    class DeterministicComplianceResult:
        passed: bool
        violations: tuple[DeterministicViolation, ...]
        canonical_citations: tuple[CanonicalRuleCitation, ...]

    def validate_optimization_output(
        trusted: TrustedOptimizationInput,
        output: OptimizationProposalOutput,
    ) -> DeterministicComplianceResult:
        ...

This is the only function in `backend/optimization_validation.py`. It performs no database work, file access, network call, model invocation, mutation, repair, default filling, or exception-based business control flow. It reads `trusted` and `output`, returns immutable tuples, and never changes either input. It adds only canonical citations found in `trusted.canonical_rule_citations` with both `active=True` and `applicable=True`, ordered by `chunk_id`. Unknown, inactive, inapplicable, or duplicate output citations never appear in the result.

The validator emits `DeterministicViolation(code, field, message_zh)` sorted lexicographically by `field` then `code`. Messages are fixed safe Chinese strings keyed by the following rule families, never interpolated model text: `MISSING_TRUSTED_FACT` “缺少可信商品事实”; `OUTPUT_BUSINESS_LENGTH` “输出长度不符合项目演示规则”; `TITLE_LANGUAGE` “标题必须包含中文字符”; `RESTRICTED_PHRASE` “包含项目演示限制短语”; `CHANGE_TARGET_DUPLICATE` “文案变更目标重复”; `CHANGE_CURRENT_MISMATCH` “当前值与可信快照不一致”; `CHANGE_SUGGESTED_MISMATCH` “建议值与输出不一致”; `OUTPUT_CHANGE_UNDECLARED` “输出变更未声明”; `EVIDENCE_MISSING` “缺少有效证据”; `EVIDENCE_FACT_PATH_INVALID` “事实证据路径无效”; `EVIDENCE_CITATION_INVALID` “规则证据无效”; `CITATION_DUPLICATE` “引用重复”; `CITATION_UNKNOWN` “引用不在可信集合”; `CITATION_EVIDENCE_UNLISTED` “规则证据未列入引用”; `ATTRIBUTE_TARGET_DUPLICATE` “属性目标重复”; `ATTRIBUTE_CURRENT_MISMATCH` “属性当前值不一致”; `ATTRIBUTE_SOURCE_MISSING` “新增属性缺少规则来源”; `SKU_TARGET_DUPLICATE` “SKU 目标重复”; `SKU_UNKNOWN` “SKU 不属于当前商品”; `SKU_CURRENT_MISMATCH` “SKU 当前值不一致”; `PRICE_TARGET_DUPLICATE` “价格目标重复”; `PRICE_SKU_UNKNOWN` “价格 SKU 不属于当前商品”; `PRICE_CURRENT_MISMATCH` “价格当前值不一致”; `PRICE_CURRENT_NONPOSITIVE` “当前价格不能产生合规建议”; `PRICE_NONPOSITIVE` “建议价格必须大于零”; `PRICE_PRECISION` “价格精度无效”; `PRICE_RANGE` “建议价格超出允许范围”; and `RAG_QUALITY_INSUFFICIENT` “规则检索质量不足”.

Business length rules are fixed: title is `1..60` characters and must contain at least one CJK Unified Ideograph (`U+4E00..U+9FFF`), while brand numbers and ASCII text remain otherwise permitted; selling points are `1..5` strings each `1..80` characters; description is `1..10` ordered sections with every heading `1..40` and every body `1..1000` characters; keywords are `1..20` strings each `1..32` characters; reasons are `1..500` characters; attribute suggested values are `1..256` characters; SKU suggested codes are `1..64` characters and every suggested-spec key/value is `1..64`/`1..128` characters; and every `EvidenceRef` list, including each description section, contains at least one item. Changes are at most `4`, attribute completions at most `20`, price suggestions at most `20`, SKU suggestions at most `20`, and output citations at most `20`. Empty business strings and empty evidence lists always yield `OUTPUT_BUSINESS_LENGTH` or `EVIDENCE_MISSING` from the pure validator, not a narrow Pydantic parse failure. Every human-facing output string—title, selling point, description heading/body, keyword, reason, suggested attribute value, suggested SKU code/spec strings—also undergoes the fixed project-demonstration phrase scan. The minimum prohibited/restricted phrase table is `["治疗", "治愈", "疗效", "药到病除", "100%安全", "绝对安全", "保证", "永久有效", "全网最低", "第一名"]`. These are project demonstration rules only; they neither assert coverage of every regulation nor claim to be an official platform policy.

A trusted input fails `MISSING_TRUSTED_FACT` when server store/product IDs, base version, title, category, the complete selected-candidate `ProductMetrics`, candidate evidence, or required SKU identity/code/spec are absent/empty. A `change` target maps exactly to the trusted/output pair `title -> title`, `selling_points -> selling_points`, `description -> description`, and `keywords -> search_keywords/keywords`. For `description`, normalize the trusted current string, the change union values, and the ordered `DescriptionSection` list using `model_dump(mode="json")` followed by `json.dumps(..., ensure_ascii=False, separators=(",", ":"), sort_keys=True)` before comparison; the current side must be the trusted string and the suggested side the output section-list shape. The other targets compare their actual typed values exactly. Each `current_value` must equal the corresponding server snapshot, each `suggested_value` must equal the corresponding output field, targets cannot repeat, and every changed output field requires one matching change declaration. Each change, attribute completion, price suggestion, SKU suggestion, and description section requires at least one valid `EvidenceRef`.

The only valid fact paths are `product.title`, `product.category`, `product.brand`, `product.selling_points`, `product.description`, `product.search_keywords`, `product.attributes.<existing-key>`, `product.skus.<current-sku-id>.code`, `product.skus.<current-sku-id>.spec`, `product.skus.<current-sku-id>.price`, `product.skus.<current-sku-id>.stock`, `candidate.metrics.<existing-ProductMetrics-field>`, and `candidate.evidence.<existing-index>`. Citation evidence must have `kind="citation"`, name a trusted `chunk_id` with both `active=True` and `applicable=True`, and appear in `output.citations`. Every output citation must be unique and name that same allowlist; unknown, inactive, or inapplicable entries use `CITATION_UNKNOWN` or `EVIDENCE_CITATION_INVALID` and the result returns only corresponding trusted canonical citations, never model-provided metadata. This deterministic check proves only server allowlist membership, structural validity, and required linkage; it does not prove that a citation semantically supports a claim. The later independent compliance Agent owns that semantic decision, and release requires both deterministic and semantic tracks to pass.

Attribute target names cannot repeat. An existing attribute completion must exactly match its trusted current value; a new attribute must declare `current_value=None` and include at least one active/applicable citation evidence, otherwise it receives `ATTRIBUTE_SOURCE_MISSING`. Price and SKU suggestions must each contain an active/applicable citation evidence in addition to any fact evidence. Thus `rag_quality in {"zero_hit", "low_confidence"}` always adds `RAG_QUALITY_INSUFFICIENT` and returns `passed=False`; it can never become a deterministic compliance pass, and in particular cannot support a new attribute, price suggestion, SKU suggestion, or later compliance release.

SKU targets cannot repeat and must belong to `trusted.skus`. A SKU suggestion's `current_code` and `current_spec` must equal that SKU snapshot exactly. Price targets cannot repeat and must name a current SKU; `current_price` must exactly equal the trusted price. Quantize all prices with `Decimal("0.01")` and `ROUND_HALF_UP`; a suggested value with any additional fractional unit is `PRICE_PRECISION`. A trusted current price of zero or less yields `PRICE_CURRENT_NONPOSITIVE` for every price suggestion. Otherwise suggested price must be positive and lie inclusively between `(current * Decimal("0.70")).quantize(Decimal("0.01"), ROUND_HALF_UP)` and `(current * Decimal("1.30")).quantize(Decimal("0.01"), ROUND_HALF_UP)`. Price changes are recommendations only: this task never writes `Product` or `ProductSku` and adds no apply, submit, approval, or publish API.

- [ ] **Step 1: Write the failing pure contract and deterministic-validation tests**

Create `tests/test_optimization_validation.py` with a minimal complete server snapshot and legal output. Use one store/product at base version `7`, title `“原商品标题”`, category `“家居”`, one current SKU `sku-1/SKU-RED/{“颜色”: “红”}/Decimal("100.00")/10`, and the existing full `ProductMetrics.from_totals(impressions=100, clicks=10, orders=1, units=1, revenue=Decimal("100.00"), refunds=0).model_copy(update={"product_id": "product-1", "product_code": "HOME-001"})` selected-candidate snapshot, plus candidate evidence `["orders=1"]`, `rag_quality="normal"`, and one `active=True, applicable=True` `CanonicalRuleCitation` named `rule-chunk-1`. Use a legal output with a CJK-bearing declared title change, one structured description section with citation evidence, a matching `OutputCitation(chunk_id="rule-chunk-1")`, a citation-backed new attribute, and an inclusive `70.00` price suggestion.

    def test_valid_output_returns_only_canonical_citations_and_does_not_mutate_inputs() -> None:
        trusted = complete_trusted_input()
        output = complete_legal_output()
        trusted_before = trusted.model_copy(deep=True)
        output_before = output.model_copy(deep=True)

        result = validate_optimization_output(trusted, output)

        assert result.passed is True
        assert result.violations == ()
        assert result.canonical_citations == (trusted.canonical_rule_citations[0],)
        assert trusted == trusted_before
        assert output == output_before

Add `ValidationError` tests for extra fields on every contract boundary, including forged canonical citation metadata in `OutputCitation` and extra fields in `DescriptionSection`. Add pure-validator cases for every deterministic family: prohibited phrase, title with no CJK ideograph, and each business limit/nonempty reason, attribute value, SKU suggestion, section heading/body, and evidence list; missing trusted fact; forged change current value; suggested/output mismatch using the deterministic description JSON shape; undeclared changed output; invalid fact path; unknown/duplicate/inactive/inapplicable output citation; citation evidence missing from `output.citations`; no evidence; no-source or duplicate attribute target; unknown/duplicate SKU target and mismatched code/spec; unknown/duplicate price target, forged current price, non-positive suggestion, zero current price, and price bounds `70.00`/`130.00` passing while `69.99`/`130.01` fail; `zero_hit` and `low_confidence`; duplicate change targets; stable `[(violation.field, violation.code)]` ordering; and input/output deep-equality after both passing and failing calls. Assert canonical results contain only citations with both server facts true and add no test claiming the deterministic path has judged semantic citation support. All tests construct models in memory and use no session, network, model, or Agent.

- [ ] **Step 2: Run RED and verify the missing schema and pure-validator contracts**

Run:

    # First apply “Unified D-drive development and verification environment” above.
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_optimization_validation.py -v

Expected: collection fails because the strict trusted/output models and `backend.optimization_validation` do not exist. Once models exist before the pure function, the legal and violation tests fail because no deterministic result/canonical citation behavior exists. Syntax errors, fixture failures, database access, network access, model loading, or DeepSeek calls are invalid RED evidence.

- [ ] **Step 3: Implement the minimal schemas and one pure validator**

Add exactly the declared `extra="forbid"` internal schema models to `backend/schemas.py`, reusing the existing `ProductMetrics` rather than defining another metrics model and without changing `KnowledgeCitation`. Implement only `DeterministicViolation`, `DeterministicComplianceResult`, and `validate_optimization_output()` in `backend/optimization_validation.py`. Build immutable trusted indexes in local variables, accept canonical citations only when both `active` and `applicable` are true, normalize description comparisons through the declared deterministic JSON representation, append fixed-code violations, sort them by `(field, code)`, resolve canonical citations from that active/applicable server set, and return `passed = not violations`. Use `Decimal` plus `ROUND_HALF_UP` for the stated price policy and a direct CJK Unified Ideograph check for `TITLE_LANGUAGE`. Do not mutate models, persist a result, call a dependency, create a policy engine/rule DSL/repository/factory/plugin, or add a dependency.

- [ ] **Step 4: Run GREEN and focused regression gates**

Run:

    # First apply “Unified D-drive development and verification environment” above.
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_optimization_validation.py -v
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_analysis_agent.py tests/test_deepseek_runtime.py -v
    D:\E-commerce_operations_env\python.exe -m pytest -v
    D:\E-commerce_operations_env\python.exe -m compileall backend tests
    D:\E-commerce_operations_env\python.exe -m alembic current
    D:\E-commerce_operations_env\python.exe -m alembic check
    git diff --check
    git status --short

Expected: the complete legal example passes with a full existing `ProductMetrics` snapshot, structured evidence-backed description sections, and active/applicable canonical trusted citations only; parser extra fields are rejected; every listed deterministic rule yields stable sorted safe violations; CJK-bearing title, nonempty business strings/evidence, inactive/inapplicable citations, deterministic description comparison, zero/low RAG quality, and inclusive quantized price boundaries are covered; no input/output mutation occurs; the validator makes no semantic-support claim beyond allowlist linkage, leaving that judgment to the later compliance Agent; all ordinary tests remain pure/offline with no DeepSeek, network, model, Milvus, or model-repository access; compilation uses the D-drive environment and Alembic stays at the Task 1 head without drift.

- [ ] **Step 5: Commit only the deterministic validation deliverable**

    git add -- backend/schemas.py backend/optimization_validation.py tests/test_optimization_validation.py
    git diff --cached --name-only
    # Expected exact output, in any Git display order:
    # backend/schemas.py
    # backend/optimization_validation.py
    # tests/test_optimization_validation.py
    git diff --cached --check
    git commit -m "feat: validate product optimization output"
    git show --check --oneline HEAD
    git status --short

## Review Slice Boundary

This document currently contains the required plan header, global constraints, expected file responsibilities, and Tasks 1–5. Tasks 1–5 are written but none is authorized for implementation. Tasks 1–5 have passed plan review. Task 6 below awaits review; no later task is authorized.

### Task 6: Independent optimization and compliance Agent clients

**Files:**

- Modify: `backend/schemas.py` only to add `ValidatedRequiredChange`, the one strict contract shared by the compliance response and a later optimization revision.
- Create: `backend/optimization_agent.py`
- Create: `backend/compliance_agent.py`
- Create: `tests/test_optimization_agent.py`
- Create: `tests/test_compliance_agent.py`
- Do not modify: Workers, database mappings, migrations, routes, RAG, `backend/deepseek_runtime.py`, `backend/analysis_agent.py`, or analysis tests.

**Interfaces:**

- Consumes: Task 1 `AgentCallType` and `ComplianceRiskLevel`; Task 3 `BeforeHttpAttempt`, `DeepSeekAttemptRecord`, `DeepSeekJsonRuntime`, and its `1..3` request cap; Task 5 `TrustedOptimizationInput`, `OptimizationProposalOutput`, `OutputCitation`, `CanonicalRuleCitation`, and `DeterministicComplianceResult`; and `Settings.deepseek_model` (whose default remains `"deepseek-v4-flash"`).
- Produces: one independently named optimization client and one independently named compliance client. Each owns its own Prompt constants, Prompt version, parser, validator, invocation, audit-record mapping, and fixed node names. Their only shared HTTP/JSON behavior is the Task 3 runtime; neither creates a provider factory, base Agent, shared business parser, retry loop, Worker step, database write, or model-routing layer.

    # backend/schemas.py
    class ValidatedRequiredChange(BaseModel):
        model_config = ConfigDict(extra="forbid")
        source_track: Literal["deterministic", "semantic"]
        source_violation_code: Literal[
            "MISSING_TRUSTED_FACT",
            "OUTPUT_BUSINESS_LENGTH",
            "TITLE_LANGUAGE",
            "RESTRICTED_PHRASE",
            "CHANGE_TARGET_DUPLICATE",
            "CHANGE_CURRENT_MISMATCH",
            "CHANGE_SUGGESTED_MISMATCH",
            "OUTPUT_CHANGE_UNDECLARED",
            "EVIDENCE_MISSING",
            "EVIDENCE_FACT_PATH_INVALID",
            "EVIDENCE_CITATION_INVALID",
            "CITATION_DUPLICATE",
            "CITATION_UNKNOWN",
            "CITATION_EVIDENCE_UNLISTED",
            "ATTRIBUTE_TARGET_DUPLICATE",
            "ATTRIBUTE_CURRENT_MISMATCH",
            "ATTRIBUTE_SOURCE_MISSING",
            "SKU_TARGET_DUPLICATE",
            "SKU_UNKNOWN",
            "SKU_CURRENT_MISMATCH",
            "PRICE_TARGET_DUPLICATE",
            "PRICE_SKU_UNKNOWN",
            "PRICE_CURRENT_MISMATCH",
            "PRICE_CURRENT_NONPOSITIVE",
            "PRICE_NONPOSITIVE",
            "PRICE_PRECISION",
            "PRICE_RANGE",
            "RAG_QUALITY_INSUFFICIENT",
            "EXAGGERATION",
            "MEDICALIZATION",
            "MISLEADING",
            "SEMANTIC_CONTRADICTION",
            "UNPROVABLE_PROMISE",
            "INSUFFICIENT_EVIDENCE",
        ]
        field: str = Field(min_length=1, max_length=64)
        instruction: str = Field(min_length=1, max_length=240)
        citation_chunk_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
            max_length=20
        )

`ValidatedRequiredChange` is the only common business model added in this task. Its source-code `Literal` is exactly the Task 5 stable deterministic codes plus the six semantic codes declared below; it does not accept arbitrary strings. A model validator permits a deterministic code only with `source_track="deterministic"` and a semantic code only with `source_track="semantic"`; both unknown codes and unknown tracks are Pydantic failures. `citation_chunk_ids` is deliberately `0..20`: an empty list is valid for a factual repair such as `TITLE_LANGUAGE`, `RESTRICTED_PHRASE`, or deleting an unprovable claim. When nonempty, items use `Field(min_length=1, max_length=128)` and a model validator rejects duplicates. The same validator requires `instruction` to contain at least one CJK Unified Ideograph and rejects every Unicode control character; printable ASCII may accompany Chinese text, but all-English text cannot pass. The model never treats that provider instruction as fixed server copy, and local validation errors name only the field/rule without echoing it.

The compliance client can return this model only with `source_track="semantic"`, after it validates nonempty citation IDs against active/applicable trusted citations and matches `(source_violation_code, field)` to a semantic violation. A later Worker creates deterministic instructions only with `source_track="deterministic"`; it may leave the citation list empty when the deterministic rule needs no rule citation. The optimization client accepts only actual server-created `ValidatedRequiredChange` instances from either track and repeats the exact code/track and nonempty-ID allowlist checks before it serializes a revision prompt; a raw provider object, `dict`, response body, or Prompt can never become a revision input. The Prompt requires a concise Chinese `instruction`; the contract preserves only that validated display field and never accepts provider-supplied hidden reasoning.

    # backend/optimization_agent.py
    OPTIMIZATION_PROMPT_VERSION = "product-optimization-v1"
    OPTIMIZATION_PRIMARY_PROMPT: str
    OPTIMIZATION_SCHEMA_REPAIR_PROMPT: str

    @dataclass(frozen=True)
    class OptimizationAgentCallRecord:
        node_name: Literal[
            "call_product_optimization_agent",
            "repair_product_optimization_schema",
        ]
        call_type: AgentCallType
        iteration: int
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
    class OptimizationAgentInvocation:
        response: OptimizationProposalOutput | None
        records: list[OptimizationAgentCallRecord]
        error_code: str | None

    def parse_optimization_response(content: str) -> OptimizationProposalOutput: ...

    def validate_optimization_response(
        trusted: TrustedOptimizationInput,
        required_changes: Sequence[ValidatedRequiredChange],
        response: OptimizationProposalOutput,
    ) -> OptimizationProposalOutput: ...

    class ProductOptimizationAgentClient:
        def __init__(
            self,
            settings: Settings,
            transport: httpx.AsyncBaseTransport | None = None,
        ) -> None: ...

        async def request(
            self,
            trusted: TrustedOptimizationInput,
            *,
            required_changes: Sequence[ValidatedRequiredChange],
            call_type: AgentCallType,
            iteration: int,
            before_http_attempt: BeforeHttpAttempt | None = None,
            max_attempts: int = 3,
        ) -> OptimizationAgentInvocation: ...

`ProductOptimizationAgentClient.request()` rejects `iteration` outside `0..2`, an unsupported `AgentCallType`, a non-`ValidatedRequiredChange` sequence member, a code/track mismatch, a duplicate nonempty required-change citation ID, or a nonempty required-change citation absent from `trusted.canonical_rule_citations` with `active=True` and `applicable=True` before calling the runtime. It accepts valid server-created deterministic and semantic changes, including a citation-free deterministic text correction. It selects `OPTIMIZATION_PRIMARY_PROMPT` for `AgentCallType.PRIMARY` and `OPTIMIZATION_SCHEMA_REPAIR_PROMPT` for `AgentCallType.SCHEMA_REPAIR`; its fixed audit node names are respectively `call_product_optimization_agent` and `repair_product_optimization_schema`.

Primary and schema-repair user payloads have exactly the same five keys: `iteration`, `trusted_facts`, `allowed_fact_paths`, `allowed_rule_chunk_ids`, and `required_changes`. `trusted_facts` is `trusted.model_dump(mode="json")`; `allowed_fact_paths` is the Task 5 fixed fact-path allowlist resolved from that snapshot; `allowed_rule_chunk_ids` includes only active/applicable server chunk IDs; and `required_changes` is the validated model dump. Repair regenerates solely from those trusted inputs, like the analysis schema-repair path; only the system Prompt, `call_type`, and audit node name differ. Both Prompts require JSON matching `OptimizationProposalOutput`, evidence references limited to those allowlists, and explicitly state that price and SKU values are suggestions only: they must not claim to alter a SKU, product, publication, or platform. No filesystem path, vector, authorization header, credential, provider body, or chain-of-thought field belongs in either payload.

`parse_optimization_response()` uses only `OptimizationProposalOutput.model_validate_json()` and maps `ValidationError`/JSON shape failures to `OptimizationAgentSchemaError`. `validate_optimization_response()` repeats only server-bound structural checks available at this layer: it verifies the server-built required-change/citation allowlists and returns the typed response unchanged. It does not run the Task 5 deterministic policy, repair text, infer evidence, change a proposal, or write anything. The later Worker invokes `validate_optimization_output()` and decides whether a semantic review or another bounded revision is appropriate.

    # backend/compliance_agent.py
    COMPLIANCE_PROMPT_VERSION = "product-compliance-v1"
    COMPLIANCE_PRIMARY_PROMPT: str
    COMPLIANCE_SCHEMA_REPAIR_PROMPT: str

    class ComplianceSemanticViolation(BaseModel):
        model_config = ConfigDict(extra="forbid")
        code: Literal[
            "EXAGGERATION",
            "MEDICALIZATION",
            "MISLEADING",
            "SEMANTIC_CONTRADICTION",
            "UNPROVABLE_PROMISE",
            "INSUFFICIENT_EVIDENCE",
        ]
        field: str = Field(min_length=1, max_length=64)
        message_zh: str = Field(min_length=1, max_length=500)
        citation_chunk_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
            max_length=20
        )

    class ComplianceAgentResponse(BaseModel):
        model_config = ConfigDict(extra="forbid")
        passed: bool
        risk_level: ComplianceRiskLevel
        violations: list[ComplianceSemanticViolation] = Field(max_length=20)
        required_changes: list[ValidatedRequiredChange] = Field(max_length=20)
        citations: list[OutputCitation] = Field(max_length=20)
        confidence: Decimal = Field(ge=0, le=1)
        degraded: bool

    @dataclass(frozen=True)
    class ComplianceAgentCallRecord:
        node_name: Literal[
            "call_product_compliance_agent",
            "repair_product_compliance_schema",
        ]
        call_type: AgentCallType
        iteration: int
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
    class ComplianceAgentInvocation:
        response: ComplianceAgentResponse | None
        records: list[ComplianceAgentCallRecord]
        error_code: str | None

    def parse_compliance_response(content: str) -> ComplianceAgentResponse: ...

    def validate_compliance_response(
        trusted: TrustedOptimizationInput,
        response: ComplianceAgentResponse,
    ) -> ComplianceAgentResponse: ...

    class ProductComplianceAgentClient:
        def __init__(
            self,
            settings: Settings,
            transport: httpx.AsyncBaseTransport | None = None,
        ) -> None: ...

        async def request(
            self,
            proposal: OptimizationProposalOutput,
            deterministic: DeterministicComplianceResult,
            trusted: TrustedOptimizationInput,
            *,
            call_type: AgentCallType,
            iteration: int,
            before_http_attempt: BeforeHttpAttempt | None = None,
            max_attempts: int = 3,
        ) -> ComplianceAgentInvocation: ...

`ComplianceSemanticViolation` owns the fixed semantic code set and contains only a field, a provider-produced Chinese explanation that has passed the boundary validator, and citation chunk IDs. `message_zh` and `ValidatedRequiredChange.instruction` are model output, not server-fixed Task 5 messages: each is `1..` its declared upper bound, contains at least one CJK Unified Ideograph, and contains no ASCII or Unicode control character. A semantic violation's `citation_chunk_ids` and a required change's `citation_chunk_ids` may be empty; when nonempty, their `1..128` IDs are unique within the list and must later pass the active/applicable allowlist. This permits an `INSUFFICIENT_EVIDENCE` instruction to delete an unprovable claim without inventing a citation. The compliance parser uses only `ComplianceAgentResponse.model_validate_json()` and turns any Pydantic/JSON-shape failure into `ComplianceAgentSchemaError`, whose safe message identifies the failed contract field without echoing provider text.

`validate_compliance_response()` filters the trusted canonical set to citations with both `active=True` and `applicable=True`, then requires every top-level `response.citations` ID, every nonempty violation citation ID, and every nonempty required-change citation ID to be unique within its own list and to name an ID in that set. Empty violation/change citation lists remain valid. Top-level citations are never fabricated: every returned item must be a real active/applicable allowlist item. It requires every compliance-provided required change to have `source_track="semantic"` and to match an existing semantic `(source_violation_code, field)` violation. It applies these internal-consistency invariants without mutating either input: `degraded=True` requires `passed=False`; `passed=True` requires `risk_level=ComplianceRiskLevel.LOW` with empty violations and required changes; and `passed=False and degraded=False` requires at least one violation and a matching required change for every violation. All provider strings receive the CJK/control/length boundary validation above; safe errors identify only the malformed contract field and never echo provider text. A semantic `passed=True` remains this client's independent result even when `deterministic.passed` is false. The later Worker combines the two tracks, so the client must never overwrite or reinterpret `DeterministicComplianceResult`.

`ProductComplianceAgentClient.request()` enforces the same `iteration` and `AgentCallType` bounds, chooses the independently owned primary or repair Prompt, and maps records to `call_product_compliance_agent` or `repair_product_compliance_schema`. Primary and schema-repair user payloads have exactly the same five keys: `iteration`, `candidate_output`, `deterministic_result`, `trusted_facts`, and `canonical_citations`. Repair regenerates solely from those trusted values; only the system Prompt, `call_type`, and audit node name differ. `deterministic_result` contains only `passed`, stable violation code/field/message, and canonical citation IDs; it omits ORM state and internal containers. `canonical_citations` contains only active/applicable canonical citations from `trusted`. The Prompts require an independent semantic assessment, forbid changing `candidate_output`, insist on the declared six-code schema, and prohibit any claim that deterministic failure is overridden. Payload construction uses `model_dump(mode="json")` and fixed dictionary keys only; it excludes paths, vectors, credentials, headers, provider bodies, and reasoning text.

Both clients construct `DeepSeekJsonRuntime(settings, transport)` and call its `request()` with their own system Prompt, safe user payload, parser callback, `before_http_attempt`, and the unchanged `max_attempts` value. They map only the safe runtime attempt fields into their own audit record with their node name, supplied `call_type`, `iteration`, and Prompt version. Their records never expose Prompt text, payload text, provider body, headers, Authorization, Key, or reasoning. A runtime `DEEPSEEK_KEY_MISSING`, `LEASE_LOST`, timeout, transport, rate-limit, server, authorization, or HTTP error propagates unchanged with the mapped safe records; key absence and lease loss before the first POST produce zero records/zero HTTP requests as supplied by the runtime. A schema-invalid provider completion returns `response=None`, `error_code="DEEPSEEK_SCHEMA_INVALID"`, and safe attempt records only; neither client invents a proposal, compliance review, degraded copy, or required change. The caller may set `max_attempts=1` for the final real smoke so either Agent makes at most one POST.

- [ ] **Step 1: Write focused independent-client contract tests**

Create `tests/test_optimization_agent.py` and `tests/test_compliance_agent.py`. Both use `Settings(_env_file=None, jwt_secret_key=SecretStr("local-product-optimization-agent-test-secret"), deepseek_api_key=SecretStr("test-only-agent-token"))` and `httpx.MockTransport`; neither test reads a real Key nor permits a network request.

In `tests/test_optimization_agent.py`, build the complete Task 5 trusted snapshot plus one active/applicable `rule-chunk-1` citation, an actual semantic `ValidatedRequiredChange(source_track="semantic", source_violation_code="EXAGGERATION", field="title", instruction="删除夸大表述", citation_chunk_ids=["rule-chunk-1"])`, and a citation-free deterministic `ValidatedRequiredChange(source_track="deterministic", source_violation_code="TITLE_LANGUAGE", field="title", instruction="标题补充中文字符", citation_chunk_ids=[])`. Return separately legal primary and schema-repair JSON payloads through the mock handler. Assert the primary invocation at iteration `0` produces an `OptimizationProposalOutput`, record node `call_product_optimization_agent`, Prompt version `product-optimization-v1`, and only safe audit fields; schema repair at iteration `1` accepts the deterministic citation-free change, uses node `repair_product_optimization_schema`, and has exactly the same user-payload keys and values as a primary call over the same trusted input. Include iteration `2` as a valid primary call and assert `-1`/`3` are rejected before a POST. Decode the captured request JSON and assert the five declared keys, exact allowlisted chunk/fact references, suggestion-only instruction, and absence of `storage_path`, `path`, `vector`, `embedding`, `Authorization`, `cookie`, `api_key`, provider-body, and chain-of-thought fields. Assert a raw `dict` in `required_changes`, a required-change citation outside the active/applicable set, a duplicate nonempty citation ID, a source-code/track mismatch, an unknown source code/track rejected by Pydantic, and an extra output field each fail before or through `DEEPSEEK_SCHEMA_INVALID` without returning a proposal. With `max_attempts=1` and a `429` response, assert exactly one HTTP request and one safe failed record. A blank SecretStr and a false first lease callback each cause zero HTTP requests; retain the distinct `DEEPSEEK_KEY_MISSING` and `LEASE_LOST` outcomes.

In `tests/test_compliance_agent.py`, construct the same trusted snapshot and a valid deterministic result. Mock independent legal primary and repair payloads for a `ComplianceAgentResponse` with `passed=False`, a cited `EXAGGERATION` violation, and its matching `source_track="semantic"` required change; assert iteration `0`, `1`, and `2`, distinct `product-compliance-v1` Prompt/version, the two compliance node names, and exactly equal primary/repair payload keys and trusted values. Add direct parser/validator cases for extra fields; unknown, inactive, inapplicable, and duplicate nonempty top-level/violation/required-change citation IDs; legal empty violation/change citation lists; a required change with any non-semantic source track or no matching `(code, field)` violation; `degraded=True, passed=True`; a semantic pass whose risk is not LOW or that contains violations/changes; and a non-degraded failed response without a violation and matching change. Cover provider `message_zh` and required-change `instruction` that are empty, all-English, contain a newline or another Unicode control character, or exceed their stated maximum, asserting the failure never echoes that provider text. Assert fields and citation IDs cannot exceed their stated caps. Supply `DeterministicComplianceResult(passed=False, ...)` with a semantically valid pass response and assert the response remains semantically passed while the deterministic object stays unchanged, proving that combination is not performed in the client. Assert the compliance request has exactly the declared safe keys, omits all path/vector/credential/provider-body/reasoning fields, and uses canonical active/applicable citation metadata only. Cover `max_attempts=1` with one request, missing Key, and lease loss before POST as for the optimization client. Assert every record dataclass's serialized keys omit Prompt, payload, provider body, headers, Authorization, and Key fields.

- [ ] **Step 2: Run RED and verify the independent client contracts are absent**

Run:

    # First apply “Unified D-drive development and verification environment” above.
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_optimization_agent.py tests/test_compliance_agent.py tests/test_deepseek_runtime.py -v

Expected: collection fails because the two client modules and `ValidatedRequiredChange` do not exist, or focused assertions fail because no client delegates to the Task 3 runtime. The RED result must name a missing contract or the absent runtime delegation; syntax failures, fixture failures, real-network attempts, model/RAG loading, or reads of a real DeepSeek key are invalid evidence.

- [ ] **Step 3: Implement the minimum two isolated client boundaries**

Add only `ValidatedRequiredChange` to `backend/schemas.py`, using the exact source-track/code union, CJK/control-safe instruction boundary, optional `0..20` citation list with nested citation string constraints, and duplicate-list/source-track model validation above. Do not move `ComplianceAgentResponse`, the semantic code Literal, or either Agent's parser into `backend/schemas.py`; those remain independent in `backend/compliance_agent.py`.

Create `backend/optimization_agent.py` and `backend/compliance_agent.py` with exactly the declared public classes/functions, separate literal Prompt constants, separate Prompt versions, separate parse/validation functions, and separate safe record/invocation dataclasses. Each class validates inputs before it calls its single `DeepSeekJsonRuntime.request()` invocation, forwards `call_type`, `iteration`, `before_http_attempt`, and `max_attempts` exactly as declared, and maps the generic result without a second retry loop. Primary and repair calls build identical user-payload keys from the same trusted values and regenerate without provider-body retention. Use `ValidationError`/local `ValueError` only to classify provider schema/contract failures as `DEEPSEEK_SCHEMA_INVALID`; propagate runtime error codes unchanged. Do not call the deterministic validator inside either client, modify an ORM object, persist a record, schedule a repair, create a loop, write degraded text, or add a setting/dependency.

- [ ] **Step 4: Run GREEN and isolated-client regression gates**

Run:

    # First apply “Unified D-drive development and verification environment” above.
    $env:JWT_SECRET_KEY = "local-product-optimization-plan-verification-secret-at-least-32"
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_optimization_agent.py tests/test_compliance_agent.py tests/test_deepseek_runtime.py tests/test_analysis_agent.py -v
    D:\E-commerce_operations_env\python.exe -m pytest -v
    D:\E-commerce_operations_env\python.exe -m compileall backend tests
    D:\E-commerce_operations_env\python.exe -m alembic current
    D:\E-commerce_operations_env\python.exe -m alembic check
    git diff --check
    git status --short

Expected: every Agent request is captured by `MockTransport`; primary and repair payload keys are identical and include no provider-body retention; deterministic and semantic required changes obey their exact source-track/code contracts while citation-free factual corrections remain valid; provider display text is bounded, CJK-bearing, and control-character-free without error-text reflection; the two Prompt/schema/node/version/audit mappings remain separate; `max_attempts=1` permits one POST only; ordinary tests make no external DeepSeek request, automatic model download, local-model/RAG load, or Milvus access; the analysis Agent stays compatible with Task 3; compilation uses the named D-drive pycache environment; and Alembic remains at the Task 1 head without model drift.

- [ ] **Step 5: Commit only the independent Agent clients**

    git add -- backend/schemas.py backend/optimization_agent.py backend/compliance_agent.py tests/test_optimization_agent.py tests/test_compliance_agent.py
    git diff --cached --name-only
    # Expected exact output, in any Git display order:
    # backend/schemas.py
    # backend/optimization_agent.py
    # backend/compliance_agent.py
    # tests/test_optimization_agent.py
    # tests/test_compliance_agent.py
    git diff --cached --check
    git commit -m "feat: add optimization and compliance agents"
    git show --check --oneline HEAD
    git status --short

## Review Slice Boundary

Tasks 1–6 are written but none is authorized for implementation. Tasks 1–6 have passed plan review. Task 7 below awaits review; no later task is authorized.

### Task 7: Type-isolated optimization leases and immutable persistence

**Files:**

- Create: `backend/optimization_runs.py`
- Create: `tests/test_optimization_worker.py` for this task's SQLite service and persistence contract; Task 8 alone expands it with the Worker graph.
- Do not modify: Worker graph or CLI, routes, Agents, RAG, schemas, migrations, model mappings, `backend/workflow_leases.py`, or any analysis implementation.

**Interfaces:**

- Consumes: Task 1 `WorkflowType`, `WorkflowStatus`, `WorkflowQuality`, `ProductProposal`, `ProposalRevision`, `ComplianceReview`, and five-field `AgentCall` uniqueness; Task 2 `workflow_lease_expiry()`, `owned_workflow_lease()`, and `commit_owned_workflow_update()` only; Task 4's selected proposal/optimization-run relationship; Task 5 trusted/output/deterministic types; Task 6 safe optimization/compliance audit-record types, semantic response, and `validate_compliance_response()`.
- Produces: a type-isolated optimization claim, simple renewal/step mutations, a safe owned context snapshot, immutable revision/review persistence with exact replay, and guarded optimization terminal transitions. This module owns its own multi-table transactions. It is neither a repository nor a generic Worker/queue/state-machine abstraction, and it does not invoke an Agent, RAG, model, route, checkpoint, or external dependency.

    # backend/optimization_runs.py
    from dataclasses import dataclass
    from typing import Literal, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.common import ComplianceRiskLevel, WorkflowQuality
    from backend.schemas import (
        CanonicalRuleCitation,
        OptimizationProposalOutput,
        ProductMetrics,
        TrustedOptimizationInput,
        TrustedProductSku,
        ValidatedRequiredChange,
    )

    PendingManualErrorCode = Literal[
        "DEEPSEEK_KEY_MISSING",
        "DEEPSEEK_TIMEOUT",
        "DEEPSEEK_TRANSPORT",
        "DEEPSEEK_RATE_LIMIT",
        "DEEPSEEK_SERVER_ERROR",
        "DEEPSEEK_UNAUTHORIZED",
        "DEEPSEEK_FORBIDDEN",
        "DEEPSEEK_HTTP_ERROR",
        "DEEPSEEK_SCHEMA_INVALID",
        "KNOWLEDGE_MODEL_UNAVAILABLE",
        "KNOWLEDGE_DEPENDENCY_TIMEOUT",
        "KNOWLEDGE_DEPENDENCY_ERROR",
        "KNOWLEDGE_ZERO_HIT",
        "KNOWLEDGE_LOW_CONFIDENCE",
        "COMPLIANCE_AGENT_DEGRADED",
        "OPTIMIZATION_ITERATION_LIMIT",
    ]

    OptimizationFailureCode = Literal[
        "OPTIMIZATION_CONTEXT_NOT_FOUND",
        "OPTIMIZATION_CONTEXT_INCONSISTENT",
        "OPTIMIZATION_AUTHORIZATION_CHANGED",
        "PRODUCT_VERSION_CONFLICT",
        "OPTIMIZATION_FACT_ERROR",
        "OPTIMIZATION_DATABASE_ERROR",
        "OPTIMIZATION_CHECKPOINT_ERROR",
        "OPTIMIZATION_REPLAY_CONFLICT",
    ]

    @dataclass(frozen=True)
    class OptimizationClaim:
        workflow_run_id: str
        lease_owner: str
        attempt_count: int

    @dataclass(frozen=True)
    class OwnedOptimizationContext:
        workflow_run_id: str
        proposal_id: str
        analysis_run_id: str
        analysis_candidate_id: str
        store_id: str
        product_id: str
        created_by: str
        base_product_version: int
        title: str
        category: str
        brand: str
        selling_points: tuple[str, ...]
        description: str
        search_keywords: tuple[str, ...]
        attributes: dict[str, str]
        skus: tuple[TrustedProductSku, ...]
        candidate_metrics: ProductMetrics
        candidate_evidence: tuple[str, ...]
        current_revision_id: str | None
        current_revision_iteration: int | None
        current_proposal_output: OptimizationProposalOutput | None
        current_canonical_citations: tuple[CanonicalRuleCitation, ...]
        current_review_id: str | None
        current_review_passed: bool | None
        current_review_quality_status: WorkflowQuality | None
        current_review_error_code: str | None
        current_required_changes: tuple[ValidatedRequiredChange, ...]

    @dataclass(frozen=True)
    class OwnedOptimizationContextResult:
        disposition: Literal["ready", "failed", "lease_lost"]
        context: OwnedOptimizationContext | None
        error_code: OptimizationFailureCode | None

    @dataclass(frozen=True)
    class RevisionPersistenceResult:
        disposition: Literal["created", "replayed", "failed", "lease_lost"]
        revision_id: str | None
        error_code: OptimizationFailureCode | None

    @dataclass(frozen=True)
    class ReviewPersistenceResult:
        disposition: Literal["created", "replayed", "failed", "lease_lost"]
        review_id: str | None
        passed: bool | None
        error_code: OptimizationFailureCode | None

    @dataclass(frozen=True)
    class OptimizationTerminalResult:
        disposition: Literal["draft_ready", "pending_manual", "failed", "lease_lost"]
        error_code: str | None

    async def claim_next_optimization_run(
        session: AsyncSession, *, lease_owner: str, lease_seconds: int
    ) -> OptimizationClaim | None: ...

    async def renew_optimization_lease(
        session: AsyncSession, *, workflow_run_id: str, lease_owner: str, lease_seconds: int
    ) -> bool: ...

    async def update_optimization_step(
        session: AsyncSession, *, workflow_run_id: str, lease_owner: str, current_step: str
    ) -> bool: ...

    async def load_owned_optimization_context(
        session: AsyncSession, *, workflow_run_id: str, lease_owner: str
    ) -> OwnedOptimizationContextResult: ...

    async def persist_optimization_revision(
        session: AsyncSession,
        *,
        workflow_run_id: str,
        lease_owner: str,
        iteration: int,
        trusted: TrustedOptimizationInput,
        output: OptimizationProposalOutput,
        canonical_citations: Sequence[CanonicalRuleCitation],
        calls: Sequence[OptimizationAgentCallRecord],
    ) -> RevisionPersistenceResult: ...

    async def persist_compliance_review(
        session: AsyncSession,
        *,
        workflow_run_id: str,
        lease_owner: str,
        revision_id: str,
        iteration: int,
        deterministic: DeterministicComplianceResult,
        semantic: ComplianceAgentResponse,
        required_changes: Sequence[ValidatedRequiredChange],
        canonical_citations: Sequence[CanonicalRuleCitation],
        calls: Sequence[ComplianceAgentCallRecord],
    ) -> ReviewPersistenceResult: ...

    async def persist_compliance_failure(
        session: AsyncSession,
        *,
        workflow_run_id: str,
        lease_owner: str,
        revision_id: str,
        iteration: int,
        deterministic: DeterministicComplianceResult,
        error_code: PendingManualErrorCode,
        required_changes: Sequence[ValidatedRequiredChange],
        canonical_citations: Sequence[CanonicalRuleCitation],
        calls: Sequence[ComplianceAgentCallRecord],
    ) -> ReviewPersistenceResult: ...

    async def finalize_optimization_draft(
        session: AsyncSession, *, workflow_run_id: str, lease_owner: str
    ) -> OptimizationTerminalResult: ...

    async def defer_optimization_manual(
        session: AsyncSession,
        *,
        workflow_run_id: str,
        lease_owner: str,
        error_code: PendingManualErrorCode,
        iteration: int,
        optimization_calls: Sequence[OptimizationAgentCallRecord] = (),
    ) -> OptimizationTerminalResult: ...

    async def fail_optimization_run(
        session: AsyncSession,
        *,
        workflow_run_id: str,
        lease_owner: str,
        error_code: OptimizationFailureCode,
    ) -> OptimizationTerminalResult: ...

`OptimizationClaim` deliberately contains only the claimed workflow ID, supplied owner, and committed attempt count; callers must load the owned context and never inspect `WorkflowRun.input`. `OwnedOptimizationContext` contains only IDs and the ORM-derived facts that Task 8 later needs to construct `TrustedOptimizationInput`: current product business fields, current SKU ID/code/spec/price/stock snapshots, the selected candidate's full `ProductMetrics`/evidence, and base product version. It also carries only safe, already persisted progress: nullable current revision ID/iteration; `current_proposal_output` revalidated as `OptimizationProposalOutput`; that revision's revalidated `CanonicalRuleCitation` tuple; nullable current review ID/passed/quality/error; and its revalidated `ValidatedRequiredChange` tuple. It excludes API Keys, Prompts, provider body, filesystem paths, vectors, lease values, checkpoint data, and `WorkflowRun.input`/`output`. Task 9 may add active RAG citations; this task neither queries RAG nor manufactures citations.

`claim_next_optimization_run()` first changes only expired `WorkflowType.OPTIMIZATION` rows with `status=PROCESSING`, expired database time, and `attempt_count >= 3` to `FAILED`, clears their lease, sets `current_step="failed"`, and uses `LEASE_ATTEMPTS_EXHAUSTED`. It never evaluates or changes analysis rows. It then locks one optimization row satisfying `(status=ACCEPTED OR status=PROCESSING with expired lease) AND attempt_count < 3`, ordered by `created_at, id`, through PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED`; SQLite retains the established SQLAlchemy lock-compatible path. Only this function transitions the selected row to `PROCESSING`, sets the owner and `workflow_lease_expiry(session, lease_seconds)`, clears its error, sets `current_step="claimed"`, increments `attempt_count` exactly once, and commits. It returns `None` after committing no claim. No persistence or terminal function changes attempt count.

`renew_optimization_lease()` and `update_optimization_step()` are the only simple updates. Each passes `owned_workflow_lease(workflow_run_id, WorkflowType.OPTIMIZATION, lease_owner)` to a conditional `UPDATE`; renewal obtains its value from `workflow_lease_expiry()`, and each delegates commit/rollback to `commit_owned_workflow_update()`. Thus a stale owner, expired lease, processing-state change, or same owner string on an analysis row produces zero writes and `False`. `update_optimization_step()` accepts only a nonempty string of at most 64 characters and raises `ValueError` before database work otherwise.

`load_owned_optimization_context()` locks the current owned optimization run with `owned_workflow_lease(..., WorkflowType.OPTIMIZATION, ...)`, `.execution_options(populate_existing=True)`, and `.with_for_update()`. A missing owned row is `OwnedOptimizationContextResult("lease_lost", None, None)` after rollback and has zero writes. While still holding that transaction, it locks/rechecks `ProductProposal` through `optimization_run_id`, the source analysis run/candidate, store, product, and all product SKUs. It verifies every foreign-key relationship in both directions: proposal/run/store/product IDs agree; the analysis row is `WorkflowType.ANALYSIS`, `COMPLETED/product_selected`; candidate belongs to that analysis row and names that product; every returned SKU belongs to that product; and `proposal.base_product_version == product.current_version`.

When `proposal.current_revision_id` is null, every progress field in `OwnedOptimizationContext` is null or an empty tuple. Otherwise the same locked transaction must load exactly `ProposalRevision.id == proposal.current_revision_id AND ProposalRevision.proposal_id == proposal.id`, copy its iteration, parse `proposal_output` through `OptimizationProposalOutput.model_validate()`, and parse every stored revision citation through `CanonicalRuleCitation.model_validate()`. It requires unique active/applicable canonical citation IDs. It then queries a review with all three links `ComplianceReview.proposal_id == proposal.id`, `ComplianceReview.proposal_revision_id == revision.id`, and `ComplianceReview.iteration == revision.iteration`; if any review for that revision exists but the exact three-link query cannot return it, the chain is inconsistent. For an exact review, it parses every stored `required_changes` item through `ValidatedRequiredChange.model_validate()`, copies only ID/passed/quality/error plus the validated tuple, and does not expose its semantic JSON. A malformed JSON value, duplicate/invalid citation, invalid required change, orphaned revision/review, mismatched iteration, or pointer chain failure is current-owner `FAILED/OPTIMIZATION_CONTEXT_INCONSISTENT` with zero new revision/review/audit rows.

Authorization/status-sensitive `User`, `Store`, and exact `UserStoreScope` reads always use database `SELECT ... execution_options(populate_existing=True)`, never cached ORM attributes. The selected store and product must both be enabled; `created_by` must currently be an active `UserRole.OPERATOR` and possess a matching scope for the proposal store. The selection path uses locks for run, proposal, and product; this context path repeats those locks so it cannot trust selection-era objects. A missing linked row is `OPTIMIZATION_CONTEXT_NOT_FOUND`; a broken relationship/state/SKU mapping is `OPTIMIZATION_CONTEXT_INCONSISTENT`; changed user role/status/scope or disabled store is `OPTIMIZATION_AUTHORIZATION_CHANGED`; a disabled product is `OPTIMIZATION_FACT_ERROR`; and a base-version mismatch is `PRODUCT_VERSION_CONFLICT`. On any such condition, only the still-current owner updates the optimization row to `FAILED`, clears both lease columns, sets `current_step="failed"` and that safe code, then commits and returns `disposition="failed"`; a lost owner rolls back with zero writes. A ready context is copied into plain dataclass/Pydantic values, the read-only transaction commits to release locks, and no ORM instance is returned.

The later Task 8 Worker consumes this snapshot before any external call: a non-null `current_proposal_output` skips the optimization Agent for that iteration; a non-null exact current review skips the compliance Agent; and it starts only the missing persisted node. It never reconstructs a complete output from a checkpoint. This task only returns the snapshot and does not add that Worker behavior.

All multi-table persistence and terminal functions repeat the same private local locked-context sequence rather than relying on a previous context read: fresh run/proposal/product relationship checks, `populate_existing=True` authorization reads, and database-time owner/type/processing/lease predicates. Immediately before returning facts, inserting a revision, inserting a review, or committing a terminal state, they re-read `Product.current_version`. An unequal version wins over any proposed external/degraded transition: the still-current owner atomically clears the lease and writes `FAILED/PRODUCT_VERSION_CONFLICT`, returning its `failed` result; it must not create a revision, review, AgentCall, or make another dependency request. A missing chain/permission fault follows the same guarded failed path. All database exceptions, including cancellation, propagate; this service never catches `CancelledError` or translates a database failure into `pending_manual` or a successful result.

For immutable JSON comparison, the module uses private local canonicalization only: Pydantic values use `model_dump(mode="json")`; safe audit dataclasses are projected field-by-field with `estimated_cost` represented as its JSON Decimal form; then `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)` produces the sole equality representation. `trusted_fact_hash` is `sha256()` of that canonical `TrustedOptimizationInput` JSON, constructed inside `persist_optimization_revision()` and never accepted from an Agent or caller. It stores only validated business fields and safe audit fields, never a Prompt, provider body, header, Key, path, vector, or checkpoint.

`persist_optimization_revision()` rejects iteration outside `0..2`, an optimization audit node outside `{call_product_optimization_agent, repair_product_optimization_schema}`, or a call whose `iteration` differs from the function argument. It reconstructs the complete DB-derived portion of `TrustedOptimizationInput` from the freshly locked `OwnedOptimizationContext` and compares every field exactly before a revision/audit insert: `store_id`, `product_id`, `base_product_version`, title, category, brand, ordered selling points, description, ordered search keywords, attributes, the complete SKU set of ID/code/spec/price/stock values (canonicalized by SKU ID, not input order), the full `ProductMetrics`, and candidate evidence. It never treats matching IDs/version alone as sufficient. Any difference, including a disabled product, is a current-owner `FAILED/OPTIMIZATION_FACT_ERROR` with zero revision/audit inserts; an old owner receives `lease_lost` with zero writes.

RAG quality and canonical citations are supplied by Task 9 rather than reconstructed from the database here. Even so, this service requires `trusted.canonical_rule_citations` to have unique `chunk_id` values with every citation `active=True` and `applicable=True`; `canonical_citations` must have exactly the same canonical JSON values and chunk-ID set, in canonical chunk-ID order. An empty set is valid only when both arguments are empty. Any duplicate, inactive, inapplicable, unknown, missing, reordered-to-different-value, or extra citation is `FAILED/OPTIMIZATION_FACT_ERROR`, not a partial write.

The function reads existing `(proposal_id, iteration)` first. For an exact existing replay, it compares base version, trusted hash, canonical output, canonical citations, and the full set of safe persisted optimization calls for that run/iteration, then returns `RevisionPersistenceResult("replayed", existing.id, None)` without an `UPDATE` even if a higher current pointer or later terminal state now exists. It never moves that pointer backward. For a new row only, iteration `0` requires no revision for the proposal. Iteration `1` or `2` requires the exact `iteration - 1` revision, `proposal.current_revision_id` equal to that prior revision ID, and its exact linked review to be an actionable normal failed review: `passed=False`, `quality_status=WorkflowQuality.NORMAL`, `error_code is None`, and at least one persisted `ValidatedRequiredChange` that has already passed the review coverage rules below. This admits only a safe deterministic/semantic violation correction. It rejects a gap from `0` to `2`, a missing prior review, a prior `passed=True` review, `DEGRADED`/unavailable review, any safe error code, an empty required-change list, or a pointer not on the immediately prior revision as current-owner `FAILED/OPTIMIZATION_REPLAY_CONFLICT` with zero revision/audit/pointer writes. Thus a `persist_compliance_failure()` review or a semantic `degraded=True` review never unlocks another Agent iteration.

Only after those order guards does it insert one immutable `ProposalRevision` using the service hash, canonical output/citations, and exact optimization `AgentCall` values with the Task 1 unique key `(workflow_run_id, node_name, call_type, iteration, attempt)`. It advances `ProductProposal.current_revision_id` only from the required predecessor (or empty pointer for iteration `0`) to the new iteration; it never updates a revision or AgentCall. Any output/call replay difference performs guarded `FAILED/OPTIMIZATION_REPLAY_CONFLICT`. It uses query-existing → compare → insert, not `on_conflict_do_update`. If an `IntegrityError` race occurs, it rolls back and starts a new transaction that first re-locks the owner/type/unexpired lease and repeats the complete context/authorization/product-version guard before it queries the existing row for exact replay. A lost/reassigned/expired owner after that rollback returns `lease_lost` with zero writes; the retry never reads or returns a replay row by bypassing the lease. A still-current owner may return only an exact replay; otherwise the database error propagates.

`persist_compliance_review()` locks the same owned context, verifies that `revision_id` belongs to that proposal and has the exact requested iteration, rechecks product version, and validates that every compliance audit node is in `{call_product_compliance_agent, repair_product_compliance_schema}` with matching iteration. It passes the semantic response through the Task 6 `validate_compliance_response(trusted, semantic)` boundary again (or an exact local invariant equivalent) before persistence; an internally inconsistent semantic response cannot be saved merely because the caller labels it validated. It canonicalizes the Task 5 deterministic result, the revalidated Task 6 semantic response, combined required changes from both source tracks, canonical citations, and safe compliance audit values. It calculates the stored boolean itself as:

    combined_passed = (
        deterministic.passed and semantic.passed and not semantic.degraded
    )

It independently validates every combined `ValidatedRequiredChange`, not merely the caller description. For each unique Task 5 deterministic `(code, field)` violation there must be exactly one `source_track="deterministic"` required change with that pair; no deterministic pair may be omitted, duplicated, or replaced. When `deterministic.passed=True` and its violations are empty, an empty deterministic subset is valid. For a normal semantic review, the semantic-track subset must be exactly the Task 6-validated `semantic.required_changes`: same count and each normalized Pydantic `source_violation_code`, `field`, Chinese `instruction`, and ordered `citation_chunk_ids` equal, with no omission, replacement, or extra semantic change. Each nonempty citation list is duplicate-free and every ID belongs to this invocation's unique active/applicable trusted canonical citation set. It performs the same exact trusted/canonical-citation correspondence rule as revision persistence. Cross-track code/field mismatches, omitted/duplicated/replaced required changes, unknown or duplicated citations, or a semantic response that fails its invariant are current-owner `FAILED/OPTIMIZATION_FACT_ERROR` with zero review/audit insert.

It never accepts a caller-provided pass flag; specifically, a semantic pass paired with deterministic failure persists `passed=False`. Derived review fields are fixed by the service: when `semantic.degraded=True`, store `passed=False`, `quality_status=WorkflowQuality.DEGRADED`, and `error_code="COMPLIANCE_AGENT_DEGRADED"`; when semantic is not degraded, store `quality_status=WorkflowQuality.NORMAL` and `error_code=None`, whether combined pass is true or false. `COMPLIANCE_AGENT_DEGRADED` is an allowed `PendingManualErrorCode` for the later Worker to terminate as `PENDING_MANUAL/DEGRADED`.

It reads the existing unique review for `proposal_revision_id` first. Exact equality across proposal/revision/iteration, deterministic JSON, revalidated semantic JSON, calculated pass, derived risk/quality/error fields, required changes, citations, and every safe compliance `AgentCall` returns `ReviewPersistenceResult("replayed", existing.id, existing.passed, None)` without an update. Otherwise it atomically inserts exactly one immutable `ComplianceReview` plus any missing exact audit rows, or guarded-fails `OPTIMIZATION_REPLAY_CONFLICT`; it does not overwrite a review or audit. Its `IntegrityError` rollback path uses the same fresh owner/type/unexpired lease, context/authorization, and product-version re-guard before a fresh exact-replay query; it never returns a review through a rolled-back stale-owner session.

`persist_compliance_failure()` is the mutually exclusive persistence path when deterministic checking has completed but the compliance Agent cannot yield a valid semantic response. It accepts `PendingManualErrorCode` at the signature boundary but rejects `OPTIMIZATION_ITERATION_LIMIT` and `COMPLIANCE_AGENT_DEGRADED`: those arise after a normal review already exists and the Worker calls only `defer_optimization_manual()`. It accepts only the actual external/RAG/schema subset `DEEPSEEK_KEY_MISSING`, `DEEPSEEK_TIMEOUT`, `DEEPSEEK_TRANSPORT`, `DEEPSEEK_RATE_LIMIT`, `DEEPSEEK_SERVER_ERROR`, `DEEPSEEK_UNAUTHORIZED`, `DEEPSEEK_FORBIDDEN`, `DEEPSEEK_HTTP_ERROR`, `DEEPSEEK_SCHEMA_INVALID`, `KNOWLEDGE_MODEL_UNAVAILABLE`, `KNOWLEDGE_DEPENDENCY_TIMEOUT`, `KNOWLEDGE_DEPENDENCY_ERROR`, `KNOWLEDGE_ZERO_HIT`, and `KNOWLEDGE_LOW_CONFIDENCE`.

It obtains the same fresh owner/type/unexpired lease, proposal/revision/iteration chain, authorization, and product-version guard before doing any review write. It accepts no `ComplianceAgentResponse` and creates none. It validates every compliance audit node/attempt/iteration as the normal review function does. Every failure-review change must use `source_track="deterministic"`; each unique deterministic `(code, field)` violation has exactly one such change, with no omission, duplicate, or replacement. When deterministic passed with no violations, the empty deterministic list is valid. It rejects every semantic-track change. Each nonempty citation ID list is duplicate-free and belongs to the unique active/applicable canonical set passed for this invocation. It persists only server-fixed safe values: `semantic_review={"status":"unavailable","error_code": error_code}`, `passed=False`, `risk_level=ComplianceRiskLevel.HIGH`, `quality_status=WorkflowQuality.DEGRADED`, and `error_code=error_code`, together with canonical deterministic checks, deterministic required changes/citations, and safe audit records. `HIGH` is the conservative no-release value for an unavailable semantic review; it does not claim that the model judged the product high risk.

It queries the one review for `proposal_revision_id` before insert. Only byte-for-byte canonical equality across the fixed unavailable semantic structure, deterministic JSON, derived values, required changes/citations, and every safe audit record may return immutable exact replay. An existing normal review, a different failure code, or any other difference is current-owner `FAILED/OPTIMIZATION_REPLAY_CONFLICT`; it never overwrites the review. Its `IntegrityError` retry repeats the full fresh owner/type/unexpired lease, context, authorization, and product-version guard before exact comparison. If the owner or version changes first, it returns the existing `lease_lost` or version/fact stop result with zero review/audit rows. Only after created/replayed failure review does the later Worker call `defer_optimization_manual()`; this task does not call it itself.

`finalize_optimization_draft()` locks/revalidates the owned context and current product version, then requires that `proposal.current_revision_id` points to a revision belonging to this proposal and that its unique review is linked to the same proposal/revision/iteration with stored `passed=True`. Only then it writes `status=DRAFT_READY`, `quality_status=WorkflowQuality.NORMAL`, `current_step="draft_ready"`, clears the lease, clears `error_code`, and commits. An absent or false/broken review is `OPTIMIZATION_CONTEXT_INCONSISTENT` and follows the guarded failed path, never a draft-ready terminal state.

`defer_optimization_manual()` accepts the declared external/RAG/schema/iteration-limit/degraded code list plus an explicit `iteration` in `0..2` and an optional sequence of Task 6 `OptimizationAgentCallRecord` values. It locks/revalidates a fresh owner/type/processing/unexpired-lease context and product version before any write. That context derives the only legal iteration: no revision permits only `0`; a revision without a review permits only that revision's iteration; an actionable normal failed review at iteration `0` or `1` permits only the next iteration for a trusted-input/optimization failure before that new revision; a degraded/error-bearing review or an iteration-`2` normal failure permits only its current revision iteration; and a passed review rejects defer. A wrong gap, a passed-review defer, or an error code incompatible with that review state is current-owner `FAILED/OPTIMIZATION_REPLAY_CONFLICT` with zero audit/terminal write. In particular, `COMPLIANCE_AGENT_DEGRADED` requires its existing degraded review and `OPTIMIZATION_ITERATION_LIMIT` requires an existing normal failed iteration-`2` review; neither can lower a publishable review to pending manual.

Each supplied record must use only `call_product_optimization_agent` or `repair_product_optimization_schema`, have that exact legal iteration, a nonnegative attempt, and a unique Task 1 `(workflow_run_id, node_name, call_type, iteration, attempt)` key. While that same live-owner transaction remains `PROCESSING`, it projects only safe audit fields and query-compares any existing unique audit row: a field-identical row may be reused only for that in-flight transaction or an `IntegrityError` uniqueness-race retry, while a different row is `FAILED/OPTIMIZATION_REPLAY_CONFLICT` with no overwrite. It then inserts only missing exact rows and sets `status=PENDING_MANUAL`, `quality_status=WorkflowQuality.DEGRADED`, `current_step="pending_manual"`, the stable error code, and cleared lease fields atomically. After a successful terminal write, a later ordinary call must fail the owner/status guard as `lease_lost` with zero writes; this function never bypasses `PROCESSING` or a live lease to offer terminal replay. Thus a pre-revision optimization timeout or schema-repair failure preserves its safe attempts atomically with the terminal state, while a RAG failure passes `optimization_calls=()`. No Prompt, request payload, provider body, header, Key, Authorization, or chain-of-thought enters the audit projection. A version/context/owner change produces the existing guarded failure or `lease_lost` with neither audit insertion nor terminal write. `fail_optimization_run()` accepts only `OptimizationFailureCode` and is reserved for fact, authorization, version, database, checkpoint, and replay failures; after the equivalent owner/type/context check it writes `status=FAILED`, `current_step="failed"`, the code, and cleared lease fields. All three terminal functions use the database-time owner/type/processing guard at the final write; no one can change an expired or reassigned run.

- [ ] **Step 1: Write the failing SQLite optimization persistence tests**

Create `tests/test_optimization_worker.py` with the existing async SQLite session fixture and Task 1/4 typed model factories. Each test creates one complete selected chain: active operator plus exact scope, enabled store, product at current version `7` with two owned SKUs, completed `ANALYSIS/product_selected` run/candidate, `ProductProposal(base_product_version=7)`, and its `OPTIMIZATION/ACCEPTED` run. It supplies only in-memory Task 5/6 values and makes no network, model, RAG, or Agent call.

First demonstrate claim isolation and the one-increment rule:

    async def test_claim_optimization_never_claims_or_exhausts_analysis_rows(session) -> None:
        analysis = await add_expired_processing_run(
            session, workflow_type=WorkflowType.ANALYSIS, attempt_count=3
        )
        optimization = await add_accepted_optimization_chain(session)

        claim = await claim_next_optimization_run(
            session, lease_owner="optimization-worker", lease_seconds=60
        )

        assert claim == OptimizationClaim(optimization.run.id, "optimization-worker", 1)
        fresh_analysis = await get_fresh_run(session, analysis.id)
        fresh_optimization = await get_fresh_run(session, optimization.run.id)
        assert fresh_analysis.status is WorkflowStatus.PROCESSING
        assert fresh_analysis.attempt_count == 3
        assert fresh_optimization.status is WorkflowStatus.PROCESSING
        assert fresh_optimization.attempt_count == 1

Seed an expired optimization `PROCESSING` row at attempt `3` and assert it alone becomes `FAILED/LEASE_ATTEMPTS_EXHAUSTED`; seed one at attempt `2` and assert only a claim changes it to attempt `3`. Assert a same owner string cannot renew/update an analysis row through optimization functions, and stale/expired owners return false without changing `current_step` or lease data. PostgreSQL concurrency is intentionally not simulated here; Task 9 owns the real `SKIP LOCKED` race test.

Create context tests that pre-load user/store/scope into the same session, mutate user status/role or store enabled with explicit SQL `UPDATE(...).execution_options(synchronize_session=False)`, or delete the exact scope, then call `load_owned_optimization_context()`. Assert the refreshed database state writes only the owned optimization run to `FAILED/OPTIMIZATION_AUTHORIZATION_CHANGED`. Set `Product.enabled=False` in the same way and assert `FAILED/OPTIMIZATION_FACT_ERROR`. Independently break proposal/run, analysis/candidate, candidate/product, proposal/store, and SKU/product links; assert the safe context code and no revision/review/audit row. Change `product.current_version` to `8` before each context/facts-return/persist/review/terminal call and assert `FAILED/PRODUCT_VERSION_CONFLICT`, cleared lease, no external action, and zero partial immutable rows. Reassign the lease before each equivalent call and assert `lease_lost` with no write. Use a fresh session after every rollback-sensitive assertion so an identity-map value cannot hide the committed outcome.

Persist a legal revision, reload the owned context in a fresh session, and assert it exposes exactly that revision ID/iteration, revalidated `OptimizationProposalOutput`, and canonical citations while every review field remains null/empty. Persist its review, reload again, and assert the exact review ID/passed/quality/error and revalidated required changes appear with no semantic JSON. This is the recovery contract that lets the later Worker skip the optimization Agent when a revision exists and skip the compliance Agent when its review exists; assert the test needs no checkpoint payload. Corrupt revision output/citation JSON, review required-change JSON, review iteration, or proposal current-revision pointer and assert only `FAILED/OPTIMIZATION_CONTEXT_INCONSISTENT`, never a partially populated snapshot.

Use legal Task 5 trusted/output/canonical citation values and safe Task 6 optimization records to prove immutable revisions:

    async def test_revision_sequence_replay_and_pointer_never_moves_backward(session) -> None:
        chain = await add_claimed_optimization_chain(session, version=7)
        revision_0 = await persist_optimization_revision(
            session,
            workflow_run_id=chain.run.id,
            lease_owner="worker-a",
            iteration=0,
            trusted=legal_trusted_input(chain),
            output=legal_optimization_output(),
            canonical_citations=legal_citations(),
            calls=optimization_records(iteration=0),
        )
        await persist_actionable_normal_failed_review(
            session, chain, revision_0.revision_id, iteration=0
        )
        revision_1 = await persist_optimization_revision(
            session,
            workflow_run_id=chain.run.id,
            lease_owner="worker-a",
            iteration=1,
            trusted=legal_trusted_input(chain),
            output=legal_optimization_output(),
            canonical_citations=legal_citations(),
            calls=optimization_records(iteration=1),
        )
        await persist_actionable_normal_failed_review(
            session, chain, revision_1.revision_id, iteration=1
        )
        revision_2 = await persist_optimization_revision(
            session,
            workflow_run_id=chain.run.id,
            lease_owner="worker-a",
            iteration=2,
            trusted=legal_trusted_input(chain),
            output=legal_optimization_output(),
            canonical_citations=legal_citations(),
            calls=optimization_records(iteration=2),
        )
        replay_1 = await persist_optimization_revision(
            session,
            workflow_run_id=chain.run.id,
            lease_owner="worker-a",
            iteration=1,
            trusted=legal_trusted_input(chain),
            output=legal_optimization_output(),
            canonical_citations=legal_citations(),
            calls=optimization_records(iteration=1),
        )

        assert (revision_0.disposition, revision_1.disposition, revision_2.disposition) == (
            "created", "created", "created"
        )
        assert replay_1.disposition == "replayed"
        assert await current_revision_iteration(chain) == 2

Add order-gate cases: iteration `1` with no revision `0`; iteration `2` after `0` without `1`; iteration `1`/`2` without the immediately prior review; a prior review with `passed=True`; and a pointer not equal to the immediately prior revision. Independently seed an unavailable `persist_compliance_failure()` review, a normal semantic `degraded=True` review, a normal failed review with an error code, and a normal failed review with no required changes, then attempt iteration `+1`. Each reaches only live-owner `FAILED/OPTIMIZATION_REPLAY_CONFLICT` with no new revision/audit/pointer write. Reject `-1` and `3` before an insert. The required valid sequence is exactly `0 → actionable normal failed review → 1 → actionable normal failed review → 2`; replay iteration `1` after pointer `2` and assert it is allowed without pointer regression.

Parameterize a one-field tamper over every DB-derived trusted value—store ID, product ID, base version, title, category, brand, selling-point order/value, description, search-keyword order/value, attributes, each SKU ID/code/spec/price/stock value or set member, every `ProductMetrics` field, and candidate evidence—and assert `FAILED/OPTIMIZATION_FACT_ERROR` with zero revision/audit. Also cover disabled product, duplicate/inactive/inapplicable trusted citation, and each missing/extra/different canonical-citation value; the trusted and passed canonical sets must exactly correspond. Change one output/citation/hash-safe audit field or use an optimization node/call iteration mismatch and assert only the live owner reaches `FAILED/OPTIMIZATION_REPLAY_CONFLICT` or `FAILED/OPTIMIZATION_FACT_ERROR`, with old revision/audit values unchanged. Assert the hash is the service-created SHA-256 of canonical trusted JSON, not an argument field. Pass a semantic/compliance node to the optimization function and vice versa to verify separated node allowlists and the Task 1 five-field audit uniqueness. A duplicate same-key different audit record must conflict; exact replay must neither update nor duplicate it.

Add compliance persistence tests using an exact revision. Verify a deterministic pass plus semantic pass/not-degraded stores `passed=True`, `quality_status=NORMAL`, and `error_code=None`; a deterministic failure plus semantic pass stores `passed=False`, `quality_status=NORMAL`, and `error_code=None`; and semantic `degraded=True` stores `passed=False`, `quality_status=DEGRADED`, and `error_code="COMPLIANCE_AGENT_DEGRADED"`. Assert that this stable error is accepted by `defer_optimization_manual()` and becomes `PENDING_MANUAL/DEGRADED`. An exact replay must return the same immutable review with its derived values.

For the combined required changes, parameterize a deterministic-track code/field absent from `deterministic.violations`, a semantic-track code/field absent from `semantic.violations`, a track/code mismatch, duplicate nonempty citation IDs, and citations outside the active/applicable trusted canonical set. Add required-coverage cases: omit one deterministic violation; duplicate one deterministic `(code, field)`; omit a Task 6-validated semantic required change; replace its instruction or citation IDs; or add an extra semantic change. Each is `FAILED/OPTIMIZATION_FACT_ERROR` with no review/audit row. Add the legal complete deterministic and semantic coverage case, including the deterministic-pass/no-violations empty deterministic subset, and assert it persists. Also pass an internally inconsistent semantic response that Task 6 validation rejects. Change deterministic JSON, semantic JSON, combined required changes/citations, any derived risk/quality/error value, or a safe compliance audit field on replay and assert guarded `OPTIMIZATION_REPLAY_CONFLICT` with no historical update. Assert a wrong revision/proposal/iteration, call-iteration mismatch, version change, or stale owner produces the required fact/failure disposition with no half-written review or audit.

Add `persist_compliance_failure()` tests after a completed deterministic result and before any semantic response: a compliance timeout and a schema-invalid response each create the fixed `semantic_review == {"status": "unavailable", "error_code": stable_code}`, `passed=False`, `risk_level=HIGH`, `quality_status=DEGRADED`, and matching `error_code`, with only complete deterministic required changes/citations and safe compliance audit rows. Assert `HIGH` is a conservative unavailable-review value, not model risk output. Repeat the same input for exact replay, then vary the error code, deterministic JSON, required change/citation, or audit field and assert `OPTIMIZATION_REPLAY_CONFLICT` without overwrite. Verify a failure review cannot accept a semantic-track required change, an omitted/duplicated/replaced deterministic change, duplicate/out-of-allowlist citation, invalid node/iteration, or a `ComplianceAgentResponse` parameter; verify empty deterministic changes are valid only when deterministic passed with no violations. Verify `OPTIMIZATION_ITERATION_LIMIT` and `COMPLIANCE_AGENT_DEGRADED` are rejected by this function; after a normal review they call only `defer_optimization_manual()` and never create a second review. Reassign the owner or change product version before the failure write and assert the existing lease/version stop result with zero review/audit rows.

Finally test terminal gates: a linked current revision with exact combined-passed review reaches only `DRAFT_READY`; absent, mismatched, or combined-failed review cannot. Each declared pending-manual code, including `COMPLIANCE_AGENT_DEGRADED`, reaches `PENDING_MANUAL` with `WorkflowQuality.DEGRADED`; a fact/permission/version/database/checkpoint/replay code is rejected by that function and may only use `fail_optimization_run()`. Add pre-revision terminal-audit tests with an optimization timeout record and a primary-plus-schema-repair failure record: `defer_optimization_manual(iteration=0, optimization_calls=records)` must atomically store the exact safe node/call-type/iteration/attempt/model/hash/token/duration/cost/error fields and the pending-manual terminal, with no Prompt/provider/header/Key field. After this successful terminal transition, call it again with the same records and assert `lease_lost` with zero writes. To test record comparison without bypassing the live guard, pre-seed a still-`PROCESSING`, current-owner row with an exact same-key safe audit record or inject a same-key `IntegrityError` race; assert the exact record can be reused within that transaction without duplication. Pre-seed or race a different same-key field and assert `FAILED/OPTIMIZATION_REPLAY_CONFLICT` without overwriting history. Reassign the owner or change product version before that call and assert both the terminal transition and every supplied audit row are zero. Reject an out-of-range iteration, wrong optimization node, mismatched record iteration, negative attempt, or duplicate call key before a pending-manual write. Add wrong-gap cases, an iteration-`1` pre-revision optimization failure after an actionable revision-`0` review, an iteration-`1` defer after a revision with no review, a passed-review defer, and mismatched `COMPLIANCE_AGENT_DEGRADED`/`OPTIMIZATION_ITERATION_LIMIT` codes; only their exact fresh-context iteration/state combination may terminate. Inject a `SQLAlchemyError` after a revision/review row is staged; assert it propagates rather than producing degraded success, roll back the fixture transaction, and query with a fresh session to prove revision/review/audit counts and pointer are unchanged. For an `IntegrityError` race, reassign/expire the lease before the retry's fresh query and assert it returns `lease_lost` rather than a replay; with the owner still live, assert only fully exact fresh re-guarded record comparison succeeds. No test catches `CancelledError` as a normal outcome.

- [ ] **Step 2: Run RED and verify the optimization lease/persistence boundary is absent**

Run:

    # First apply “Unified D-drive development and verification environment” above.
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_optimization_worker.py tests/test_workflow_leases.py tests/test_analysis_worker.py -v

Expected: collection fails because `backend.optimization_runs` and the declared typed results/functions, including `persist_compliance_failure()` and the audited `defer_optimization_manual()` signature, do not exist. Once a partial module exists, the focused assertions must fail on absent optimization type filters, missing guarded context-progress refresh, non-immutable replay, an unavailable/degraded/empty prior review incorrectly unlocking iteration `+1`, incomplete required-change coverage, an incorrect combined-pass/terminal rule, omitted pre-revision safe optimization audits, audit replay overwrite, or non-fixed failure-review fields. Syntax failures, fixture errors, model/RAG load, external DeepSeek use, or a network request are invalid RED evidence.

- [ ] **Step 3: Implement the minimum type-isolated persistence service**

Create `backend/optimization_runs.py` with only the declared dataclasses, literals, and functions plus private local canonicalization/locking helpers. Import and use exactly the three Task 2 lease primitives; do not copy their database-now, owner predicate, or commit/rollback behavior into a fourth shared layer. Claim explicitly filters `WorkflowType.OPTIMIZATION`; the expired-attempt update, eligible lock query, and post-lock mutation must all have that type. The claim selects with `skip_locked=True` on PostgreSQL and preserves existing SQLite test compatibility.

For simple renewal and step updates, call `commit_owned_workflow_update()` with `owned_workflow_lease(run_id, WorkflowType.OPTIMIZATION, owner)`. For context loading, revision/review persistence, and terminal functions, use a local `SELECT ... FOR UPDATE` transaction with those exact owner/type/processing/unexpired predicates, freshly queried authorization rows, and the full relation chain before one explicit commit. `defer_optimization_manual()` validates its context-derived iteration and optional optimization-call records, query-compares existing safe call rows by the five-field unique key, inserts only missing exact rows, and writes the pending-manual terminal in that same transaction; it must roll back the entire group for any owner/version/context loss or record conflict. On an owned context/fact/replay error, set only the locked optimization run to the declared failed status/code and clear its lease in that same transaction. On a zero-row owner guard, roll back and return `lease_lost`; never write a stale owner's result. Before a revision/review exact-replay retry or a terminal-audit `IntegrityError` record-comparison retry, roll back and repeat that entire owner/type/unexpired lock plus context/authorization/product-version guard in a new transaction; do not query an existing immutable row through the rolled-back session. Do not use `on_conflict_do_update`, update immutable rows, or catch `CancelledError`/broad database exceptions.

Parse the proposal current-revision/review chain into the declared safe context snapshot every time it is loaded; validate revision output/citations and review required changes through their Pydantic models, and guarded-fail malformed JSON or an inconsistent chain. Compute every persisted JSON equality/hash value from the declared canonical representation; compare every DB-derived trusted fact with the fresh context; require exact unique active/applicable trusted/canonical citation correspondence; build `ProposalRevision.trusted_fact_hash` inside the revision service; and build normal-review `ComplianceReview.passed`, quality status, and degraded error code from both tracks inside the review service. For normal reviews, require one-to-one complete deterministic required-change coverage and exact normalized semantic required-change coverage; for failure reviews, require one-to-one complete deterministic coverage and no semantic track. Implement `persist_compliance_failure()` with only the declared actual external/RAG/schema `PendingManualErrorCode` subset and the fixed unavailable semantic JSON/HIGH/DEGRADED values, never a fabricated semantic response. Enforce `0 → actionable normal failed review → 1 → actionable normal failed review → 2` for new revisions: the predecessor must be false-passed, `NORMAL`, error-free, and have nonempty validated required changes; unavailable/degraded/error/empty reviews cannot unlock another iteration. Preserve exact older replay before this order gate and without pointer regression. Query existing immutable rows before insert and compare all safe fields, including the full audit set, before returning an exact replay. Insert only then move the monotonic proposal pointer. Recheck product version before every facts return, immutable write, and terminal commit. Leave Task 8's Worker graph, bounded revision loop, checkpoint behavior, external error capture, and all Agent/RAG calls untouched.

- [ ] **Step 4: Run GREEN and persistence regression gates**

Run:

    # First apply “Unified D-drive development and verification environment” above.
    $env:JWT_SECRET_KEY = "local-product-optimization-plan-verification-secret-at-least-32"
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_optimization_worker.py tests/test_workflow_leases.py tests/test_analysis_worker.py tests/test_optimization_models.py -v
    D:\E-commerce_operations_env\python.exe -m pytest -v
    D:\E-commerce_operations_env\python.exe -m compileall backend tests
    D:\E-commerce_operations_env\python.exe -m alembic current
    D:\E-commerce_operations_env\python.exe -m alembic check
    git diff --check
    git status --short

Expected: claims, exhaustion, and all owner guards isolate optimization from analysis; all authorization/status rows refresh from the database; context reload returns only validated persisted revision/review progress and never relies on checkpoint output; every DB-derived trusted field and active/applicable citation correspondence is rechecked against the locked context; new revisions only follow `0 → actionable normal failed review → 1 → actionable normal failed review → 2`, while unavailable/degraded/error/empty predecessor reviews cannot unlock another Agent call and exact old replays cannot regress the pointer; normal reviews contain complete one-to-one deterministic coverage plus the exact validated semantic required-change subset, while failure reviews contain complete deterministic coverage and no semantic subset; a pre-revision optimization timeout or schema-repair failure atomically retains exact safe optimization calls with its pending-manual terminal, while RAG failures retain no invented call; no stale owner can persist facts, normal reviews, failure reviews, audits, pointers, or terminal state, including after an IntegrityError rollback; version/chain faults atomically fail without partial rows; canonical hash/replay behavior preserves immutable revisions, normal reviews, fixed failure reviews, terminal safe calls, and safe audit records; semantic degradation persists its derived normal-review values and becomes pending-manual only through the declared code; a timeout/schema/RAG failure produces only its fixed unavailable review before the later terminal defer when deterministic work exists; draft-ready requires an actual combined pass; ordinary tests stay offline with no model loading, RAG, Milvus, DeepSeek, or model-repository access; D-drive pycache remains in use; and Alembic stays at the Task 1 head without drift.

- [ ] **Step 5: Commit only the optimization lease persistence deliverable**

    git add -- backend/optimization_runs.py tests/test_optimization_worker.py
    git diff --cached --name-only
    # Expected exact output, in any Git display order:
    # backend/optimization_runs.py
    # tests/test_optimization_worker.py
    git diff --cached --check
    git commit -m "feat: add optimization lease persistence"
    git show --check --oneline HEAD
    git status --short

## Review Slice Boundary

Tasks 1–7 have passed plan review but none is authorized for implementation. Task 8 below awaits re-review of this RAG-quality-gate revision; no later task is authorized.

### Task 8: Bounded recoverable optimization LangGraph Worker

**Files:**

- Create: `backend/optimization_worker.py`
- Modify: `tests/test_optimization_worker.py` to add this task's offline graph and recovery tests to Task 7's SQLite persistence coverage.
- Do not create: `scripts/run_optimization_worker.py`. A production command would need the Task 9 real loader that rechecks active/applicable PostgreSQL rule citations and uses the established local RAG path. Creating a Task 8 command with a fake, empty, or unconditional-failure loader would incorrectly downgrade every accepted production run, so the CLI is deferred to Task 9.
- Do not modify: `backend/optimization_runs.py`, `backend/workflow_leases.py`, models, migrations, routes, Agent clients, DeepSeek runtime, RAG, the analysis Worker/CLI, Compose, or dependencies.

**Interfaces:**

- Consumes: Task 1 `WorkflowQuality` and terminal `WorkflowStatus`; Task 3 `BeforeHttpAttempt`; Task 5 `TrustedOptimizationInput`, `OptimizationProposalOutput`, `ValidatedRequiredChange`, and `validate_optimization_output()`; Task 6 `ProductOptimizationAgentClient`, `ProductComplianceAgentClient`, their safe invocation records, and their fixed primary/schema-repair node identities; and every Task 7 function/result type, especially `load_owned_optimization_context()`, immutable revision/review persistence, and guarded terminal functions.
- Produces: one type-isolated `build_optimization_graph()` and one `run_once()` that claim only optimization runs, use the run ID as the checkpoint thread ID, rebuild all non-safe data from the database, and make at most iterations `0`, `1`, and `2` available. The module contains no queue, repository, generic Worker/state-machine base class, Agent base class, provider factory, CLI, RAG implementation, or dependency registration.

    # backend/optimization_worker.py
    from collections.abc import Awaitable, Callable
    from typing import Literal, TypedDict

    import httpx
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from backend.common import AgentCallType, WorkflowQuality
    from backend.compliance_agent import (
        ComplianceAgentCallRecord,
        ProductComplianceAgentClient,
    )
    from backend.config import Settings
    from backend.optimization_agent import (
        OptimizationAgentCallRecord,
        ProductOptimizationAgentClient,
    )
    from backend.optimization_validation import validate_optimization_output
    from backend.optimization_runs import (
        OptimizationFailureCode,
        OwnedOptimizationContext,
        PendingManualErrorCode,
    )
    from backend.schemas import (
        OptimizationProposalOutput,
        TrustedOptimizationInput,
        ValidatedRequiredChange,
    )

    KnowledgeLoadErrorCode = Literal[
        "KNOWLEDGE_MODEL_UNAVAILABLE",
        "KNOWLEDGE_DEPENDENCY_TIMEOUT",
        "KNOWLEDGE_DEPENDENCY_ERROR",
        "KNOWLEDGE_ZERO_HIT",
        "KNOWLEDGE_LOW_CONFIDENCE",
    ]
    BeforeTrustedInputExternalAttempt = Callable[[], Awaitable[None]]
    TrustedInputLoader = Callable[
        [OwnedOptimizationContext, BeforeTrustedInputExternalAttempt],
        Awaitable[TrustedOptimizationInput],
    ]

    class OptimizationLeaseLost(RuntimeError):
        pass

    class OptimizationCheckpointFailure(RuntimeError):
        pass

    class TrustedInputLoadFailure(RuntimeError):
        def __init__(self, error_code: KnowledgeLoadErrorCode) -> None: ...

    class OptimizationWorkflowState(TypedDict, total=False):
        workflow_run_id: str
        iteration: int
        revision_id: str | None
        review_id: str | None
        review_passed: bool | None
        review_quality_status: Literal["normal", "degraded"] | None
        error_code: PendingManualErrorCode | OptimizationFailureCode | None
        next_node: Literal[
            "load_or_resume",
            "persist_revision",
            "persist_review",
            "finalize_draft",
            "defer_manual",
            "stop",
        ]

    def build_optimization_graph(
        *,
        session: AsyncSession,
        settings: Settings,
        lease_owner: str,
        checkpointer: BaseCheckpointSaver,
        trusted_input_loader: TrustedInputLoader,
        transport: httpx.AsyncBaseTransport | None = None,
        max_agent_attempts: int = 3,
    ): ...

    async def run_once(
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings,
        lease_owner: str,
        checkpointer: BaseCheckpointSaver,
        trusted_input_loader: TrustedInputLoader,
        transport: httpx.AsyncBaseTransport | None = None,
        max_agent_attempts: int = 3,
    ) -> str | None: ...

`TrustedInputLoader` is the only Task 8 injection. It receives only the safe Task 7 `OwnedOptimizationContext` and a `BeforeTrustedInputExternalAttempt` callback that either returns `None` after a successful guarded renewal or raises `OptimizationLeaseLost`; it has no boolean result that a loader can ignore. Before each real RAG/dependency request, the Task 9 loader must `await` that callback and may request only after normal return. It returns a fully typed `TrustedOptimizationInput` or raises `TrustedInputLoadFailure` with only the declared knowledge-dependency code subset. Task 9 maps an existing `KNOWLEDGE_CALIBRATION_UNAVAILABLE` condition to `KNOWLEDGE_DEPENDENCY_ERROR` at its loader boundary; Task 8 does not introduce that undeclared code. Task 8 tests provide a pure fake loader; Task 8 contains no no-op loader, citation synthesis, hard-coded `zero_hit`, model load, Milvus access, network access, or download. Task 9 alone wires the real `knowledge_search` path and active/applicable PostgreSQL citation recheck into this interface.

The persisted graph state has exactly the eight declared business keys: run ID; iteration limited to `0..2`; nullable persisted revision/review IDs; nullable review pass and quality values; one stable error code; and one next-node enum. The graph never writes a complete trusted input, product text, proposal output, canonical citation/text, required-change instruction, Agent record, Prompt, provider response, Key, Authorization, filesystem path, vector, lease, or a copy of a database JSON container to a checkpoint. `workflow_run_id` is always the LangGraph `configurable.thread_id`; a checkpoint value is advisory progress only and cannot be used to reconstruct facts or override the database.

Use a private `_CheckpointBoundary`, shaped only like the existing analysis Worker wrapper, around `aget_tuple`, `aput`, and `aput_writes`. It maps ordinary `Exception` failures to `OptimizationCheckpointFailure` without including an underlying message, while deliberately not catching `BaseException` or `asyncio.CancelledError`. The worker must not copy the analysis Worker's `_record_state()`, `_record_from_state()`, `facts`, `drafts`, candidates, or call-record channels because they violate this Worker’s checkpoint boundary.

`build_optimization_graph()` creates exactly six graph nodes: `load_or_resume`, `persist_revision`, `persist_review`, `finalize_draft`, `defer_manual`, and a terminal `stop`. It constructs the two Task 6 clients with the supplied `transport`; it validates `max_agent_attempts in {1, 2, 3}` before any database or external work and passes it unchanged to each client request. The graph's conditional edges use only `state["next_node"]`; each concrete node immediately reloads the Task 7 owned context, so a resumed edge cannot trust stale checkpoint values.

`load_or_resume` calls `load_owned_optimization_context()` before every initial route and after every completed persistence node. `lease_lost` routes directly to `stop`; the Task 7 `failed` disposition routes to `stop` because that service already made the guarded fact/authorization/version/replay failure transition. For a ready context it overwrites the safe state from database facts only:

```python
if context.current_revision_id is None:
    return safe_state(iteration=0, revision_id=None, review_id=None, next_node="persist_revision")
if context.current_review_id is None:
    return safe_state(iteration=context.current_revision_iteration, next_node="persist_review")
if context.current_review_passed is True:
    return safe_state(iteration=context.current_revision_iteration, next_node="finalize_draft")
if (
    context.current_review_quality_status is WorkflowQuality.NORMAL
    and context.current_review_error_code is None
    and context.current_required_changes
    and context.current_revision_iteration < 2
):
    return safe_state(iteration=context.current_revision_iteration + 1, next_node="persist_revision")
return safe_state(iteration=context.current_revision_iteration, next_node="defer_manual")
```

The last branch is reached for an iteration-2 normal failure, a degraded/unavailable/error-bearing review, or an otherwise unusable persisted review. A valid normal failed review must have Task 7's complete deterministic/semantic required-change coverage; an empty normal failed set is therefore a context inconsistency, not a reason to invent a revision. The persisted context, never checkpoint `state["iteration"]`, derives the target iteration and required changes. No route can return from iteration `2` to `persist_revision`, so the only new-revision sequence is `0 → actionable normal failed review → 1 → actionable normal failed review → 2`.

`persist_revision` first loads the owned context itself and derives one target solely from that fresh database snapshot. With no revision, target iteration is `0` and required changes are empty. With a current revision but no review, that revision is already persisted: it returns its safe ID with `next_node="load_or_resume"`, which routes to `persist_review`, and it makes no optimization request. With a current actionable normal failed review at iteration `0` or `1`, target is exactly current iteration plus one and the Agent receives that review's validated required-change tuple. With an iteration-`2` review, a degraded/unavailable/error-bearing review, a passed review, or an unusable/empty review, it returns to `load_or_resume` for the already declared terminal/failure route. It skips the optimization Agent only when the database already has the derived target iteration's revision; a prior revision is never treated as that target. Thus revision `0` plus an actionable review calls the Agent at iteration `1`, while a database revision `1` with no review skips only the optimization Agent and proceeds to compliance even if a stale checkpoint says iteration `1`; no `load_or_resume ↔ persist_revision` cycle is possible.

For a missing target, call `update_optimization_step()` with a short fixed step value and invoke `trusted_input_loader(context, renew_before_rag)`. `renew_before_rag` has the exact `Callable[[], Awaitable[None]]` contract: it calls `renew_optimization_lease()`, returns `None` only when the guarded update succeeds, and otherwise raises `OptimizationLeaseLost`. The loader must await it immediately before every external request; Task 8's fake loader awaits it without inspecting a value, proving that lease loss cannot be ignored.

For a returned trusted input, apply the pre-revision RAG quality gate **before any optimization-Agent construction or call**:

```python
if trusted.rag_quality == "zero_hit":
    return await defer_optimization_manual(
        session,
        workflow_run_id=workflow_run_id,
        lease_owner=lease_owner,
        error_code="KNOWLEDGE_ZERO_HIT",
        iteration=target_iteration,
        optimization_calls=(),
    )
if trusted.rag_quality == "low_confidence":
    return await defer_optimization_manual(
        session,
        workflow_run_id=workflow_run_id,
        lease_owner=lease_owner,
        error_code="KNOWLEDGE_LOW_CONFIDENCE",
        iteration=target_iteration,
        optimization_calls=(),
    )
```

This branch applies both to an initial iteration and to a next iteration derived from an actionable normal failed review. It writes only Task 7's owner/type/version/context-guarded `PENDING_MANUAL/DEGRADED` terminal; it must make zero DeepSeek requests and create no revision, review, or AgentCall. A persisted revision follows the separate `persist_review` route: that node may rebuild trusted input, rerun the deterministic validator, persist the fixed unavailable review for zero/low RAG quality, and then defer as already specified.

Only `rag_quality="normal"` reaches the Task 6 primary optimization request with `call_type=AgentCallType.PRIMARY`, the derived iteration, `before_http_attempt=renew_before_deepseek`, and `max_attempts=max_agent_attempts`. `renew_before_deepseek` keeps Task 3's actual boolean contract: it receives the attempt number and returns the boolean from `renew_optimization_lease()`; Task 6 makes no POST on `False`, returns `LEASE_LOST`, and the Worker then raises `OptimizationLeaseLost`. If the primary result is `DEEPSEEK_SCHEMA_INVALID`, make exactly one Task 6 `SCHEMA_REPAIR` request with the same trusted input, validated required changes from the prior persisted normal review if any, callback, iteration, and request cap. No raw invalid provider content is passed to repair. A non-schema external error or a repair that still has no typed output occurs before a new revision: call `defer_optimization_manual()` with the stable external/schema code, `iteration=target_iteration`, and the full local safe primary/repair `optimization_calls` sequence. The Task 7 terminal transaction persists those calls atomically with `PENDING_MANUAL/DEGRADED`; it must not fabricate a revision, review, canonical citation, or standalone AgentCall row.

On a typed response, call the Task 5 pure `validate_optimization_output()` locally, retain only its canonical citation tuple, and immediately call `persist_optimization_revision()` with the exact trusted input, typed output, canonical citations, and safe optimization audit records from the successful primary/repair sequence. The Task 7 function rechecks owner/type/version/facts and atomically handles immutable replay; this node never adds a second persistence layer. A `created` or exact `replayed` result returns only its revision ID and `next_node="load_or_resume"`; `failed` and `lease_lost` stop with zero further calls or writes.

`persist_review` also begins by loading the owned context. If an exact current review already exists, it returns only its safe review fields and `next_node="load_or_resume"` without a compliance Agent request. A current revision must be present; its database iteration replaces any stale checkpoint iteration, and the node uses only that database iteration for all Agent and persistence calls. An impossible revision/iteration chain is the Task 7 guarded `OPTIMIZATION_CONTEXT_INCONSISTENT` result and stops. It rebuilds trusted input through the same loader/callback, reruns the pure deterministic validator over the already persisted typed output, and builds deterministic required changes locally in its fixed sorted order:

```python
tuple(
    ValidatedRequiredChange(
        source_track="deterministic",
        source_violation_code=violation.code,
        field=violation.field,
        instruction=violation.message_zh,
        citation_chunk_ids=[],
    )
    for violation in deterministic.violations
)
```

The fixed Task 5 Chinese message is the safe server instruction; no model string, raw response, or inferred citation enters a deterministic change. The exact Task 7 one-to-one coverage guard remains the authority at persistence. If the loader fails before deterministic completion, call `defer_optimization_manual()` with the stable knowledge code, `iteration=current_revision_iteration`, and `optimization_calls=()`, then write no new review. If deterministic completion reports `rag_quality="zero_hit"` or `"low_confidence"`, do not call the compliance Agent: call `persist_compliance_failure()` with respectively `KNOWLEDGE_ZERO_HIT` or `KNOWLEDGE_LOW_CONFIDENCE`, the completed deterministic result, its complete deterministic changes, its canonical citations, and no compliance calls; only its created/exact-replayed result may then call `defer_optimization_manual(iteration=current_revision_iteration, optimization_calls=())`.

Otherwise make a primary Task 6 compliance request, then exactly one schema-repair request only for `DEEPSEEK_SCHEMA_INVALID`, using the same iteration, trusted input, deterministic safe result, and `renew_before_deepseek` callback. Combine deterministic changes with the Task 6-validated semantic required changes only after a typed semantic result exists. Call `persist_compliance_review()` with that exact combined sequence, the trusted canonical citation tuple, and the local safe compliance records. The Task 7 service recomputes combined pass and exact coverage; the Worker never supplies a pass flag. When both compliance attempts fail after deterministic completion, first call `persist_compliance_failure()` with the stable external/schema code, deterministic-only changes, citations, and its safe compliance records, then call `defer_optimization_manual(iteration=current_revision_iteration, optimization_calls=())` only after that immutable result is created or exactly replayed. A valid semantic `degraded=True` response instead uses `persist_compliance_review()` and returns to `load_or_resume`; its stored `COMPLIANCE_AGENT_DEGRADED` review immediately routes to defer and never unlocks another optimization call.

`finalize_draft` and `defer_manual` each load the owned context again before their single Task 7 terminal call. `finalize_draft` is reachable only for an exact current review with `passed=True`; it calls `finalize_optimization_draft()` and never accepts a checkpoint pass flag as proof. `defer_manual` chooses the existing review error code when it is a `PendingManualErrorCode`, chooses `OPTIMIZATION_ITERATION_LIMIT` only for iteration `2` normal failure, and calls `defer_optimization_manual(iteration=current_revision_iteration, optimization_calls=())` only for those declared pending-manual codes. For a persisted non-passed review that is neither actionable nor degraded/error-coded and has no legal pending-manual code, it explicitly calls `fail_optimization_run(..., error_code="OPTIMIZATION_CONTEXT_INCONSISTENT")`; it does not silently stop or label it degraded. Both terminal services retain the owner/type/version predicate; a `lease_lost` result stops silently and a fact/authorization/version/replay failure has already reached guarded `failed` without a degraded transition.

`run_once()` mirrors the existing analysis Worker’s scope, but claims only with `claim_next_optimization_run()`, creates this graph, and invokes it with `{"workflow_run_id": claim.workflow_run_id}` and `{"configurable": {"thread_id": claim.workflow_run_id}}`. It returns `None` when no optimization run is claimable and otherwise returns the claimed run ID after a normal graph stop. After a post-claim `OptimizationCheckpointFailure` or `SQLAlchemyError`, it first rolls back and exits the graph's faulted session; it then opens a new `session_factory()` session and calls `fail_optimization_run()` with respectively `OPTIMIZATION_CHECKPOINT_ERROR` or `OPTIMIZATION_DATABASE_ERROR`. A false guarded terminal result is a silent lost-owner stop; a second database failure from rollback, the fresh session, or `fail_optimization_run()` propagates. It catches `OptimizationLeaseLost` only to stop without a write and does not catch `BaseException`, `asyncio.CancelledError`, arbitrary `RuntimeError`, or provider bodies. Cancellation therefore propagates and retains the current `processing` lease; a later owner reclaims it and uses the database snapshot rather than any missing checkpoint payload.

- [ ] **Step 1: Write the failing offline Worker/recovery tests**

Extend `tests/test_optimization_worker.py` with Task 7's SQLite claimed-proposal helpers, an `InMemorySaver` subclass that records user state channel values, a fake `TrustedInputLoader`, and `httpx.MockTransport` handlers returning only Task 6 JSON fixtures. The fake loader receives `(context, renew_before_external)`, awaits the callback before returning a complete in-memory `TrustedOptimizationInput`, and records that it never loaded a model, touched Milvus, or made a request. It must be the only RAG substitute; do not add a CLI fixture or patch a nonexistent production loader.

```python
async def test_iteration_zero_finalizes_and_checkpoint_contains_only_safe_progress(
    claimed_chain, settings, optimization_and_compliance_transport
) -> None:
    saver = RecordingSaver()
    processed = await optimization_worker.run_once(
        claimed_chain.factory,
        settings=settings,
        lease_owner="worker-a",
        checkpointer=saver,
        trusted_input_loader=trusted_loader_for(claimed_chain),
        transport=optimization_and_compliance_transport,
        max_agent_attempts=1,
    )

    run = await read_run(claimed_chain.factory, claimed_chain.run.id)
    assert processed == claimed_chain.run.id
    assert run.status is WorkflowStatus.DRAFT_READY
    assert await revision_count(claimed_chain) == 1
    assert await review_count(claimed_chain) == 1
    assert saver.thread_ids == {claimed_chain.run.id}
    assert saver.user_state_keys <= {
        "workflow_run_id", "iteration", "revision_id", "review_id",
        "review_passed", "review_quality_status", "error_code", "next_node",
    }
    assert not saver.contains_forbidden_business_payload
```

Add a bounded-loop fixture sequence: iteration `0` normal false review with complete actionable deterministic/semantic changes, iteration `1` with the same result, and iteration `2` still false. Assert exactly three immutable revisions, three immutable reviews, no fourth Agent invocation or audit, and `PENDING_MANUAL/DEGRADED/OPTIMIZATION_ITERATION_LIMIT`. Seed revision `0` plus its actionable normal failed review, then run the graph and assert one real optimization request at iteration `1`, not a skip or a loop. Separately seed database revision `1` without a review while the checkpoint contains stale `iteration=1`; assert the runner makes no optimization request, makes exactly the compliance request for database iteration `1`, and inserts no second revision. Bound each graph invocation with recorded node/invocation counts to prove no `load_or_resume ↔ persist_revision` cycle. Seed an exact review and assert it makes no compliance request. Seed unavailable failure and semantic-degraded reviews, then assert the runner makes no later optimization request and transitions only with their existing pending-manual code. Add a passed review plus a forced defer edge and assert Task 7 rejects it as `OPTIMIZATION_REPLAY_CONFLICT`, never downgrading a draft-ready candidate.

Use a cancellation saver that raises `asyncio.CancelledError` immediately after the revision node commits but before its checkpoint write. Assert `run_once()` propagates cancellation and leaves the first owner `PROCESSING`; expire/reclaim that run under a new owner with the same `thread_id`, run again, and assert database context skips the optimization Agent, does not duplicate revision/audit rows, and performs only the missing compliance work. Separately use failing `aget_tuple` and `aput`/`aput_writes` savers: with a live owner each produces guarded `FAILED/OPTIMIZATION_CHECKPOINT_ERROR` through a fresh failure session; after replacing the owner first, each leaves the new owner's `PROCESSING` row unchanged. Inject a graph-session `SQLAlchemyError` after claim and assert its session is rolled back/closed before a distinct factory session performs the guarded `OPTIMIZATION_DATABASE_ERROR` transition; inject a second database failure in that fresh path and assert it propagates rather than producing pending manual.

For external lease guards, make the fake loader await its `BeforeTrustedInputExternalAttempt` callback without reading a return value, then reassign the lease so that callback raises before its simulated RAG boundary; assert it cannot reach the fake request. Separately make the Task 6 boolean `before_http_attempt` return `False` before a mocked optimization or compliance POST. Each case asserts zero request after the failed renewal, zero new revision/review/audit/terminal write, and no fallback to a pending-manual result. Add a `TrustedInputLoadFailure` for each allowed knowledge code, plus a Task 9-facing calibration-unavailable fixture mapped to `KNOWLEDGE_DEPENDENCY_ERROR`; no other `PendingManualErrorCode` may construct that exception. Seed a `zero_hit` and separately a `low_confidence` trusted input before revision `0` and before a derived iteration `1`; each must call `defer_optimization_manual()` with the exact target iteration and its matching knowledge code, make zero optimization/repair HTTP requests, and create no revision/review/audit. For RAG failure before deterministic completion after an existing revision, directly defer with its stable knowledge code and that current database revision iteration, creating no new review. For an optimization timeout or primary-plus-schema-repair failure before the first revision, assert pending manual and the exact safe optimization attempt records are atomically persisted through `defer_optimization_manual(iteration=0, optimization_calls=...)`, while no revision/review is fabricated. Repeat that test after a revision-`0` actionable review: the Worker must pass `iteration=1` and those records; a wrong gap is a replay conflict with zero call/terminal write. Reassign the owner or change product version before either terminal call and assert both calls and terminal status remain zero. After a successful terminal call, a second Worker-side defer attempt must observe `lease_lost`, not terminal replay. Add compliance timeout/schema failure after completed deterministic work and assert exactly the Task 7 fixed unavailable review is persisted before `PENDING_MANUAL/DEGRADED`.

Mutate the current product version or replace the owner before each load, fake-loader return, optimization persistence, compliance persistence, and terminal edge. Assert Task 7's guarded failure/lost-owner outcome, no external request after the observed transition, and no partial immutable row. For a still-live pre-seeded or uniqueness-raced same-key audit record, assert a field difference is a replay conflict that preserves the original record and leaves the workflow out of pending manual; only an exact in-flight record comparison may be reused. Add a direct `asyncio.CancelledError` from the fake loader and from the mock transport; both must propagate with the run still `PROCESSING` and no terminal update. Finally assert no test starts a real local model, calls a real DeepSeek endpoint, reads a key, touches Milvus, downloads a model, or relies on a checkpoint field beyond the safe state allowlist.

- [ ] **Step 2: Run RED and confirm the bounded Worker contract is absent**

Run:

    # First apply “Unified D-drive development and verification environment” above.
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_optimization_worker.py -v

Expected: collection fails because `backend.optimization_worker`, `TrustedInputLoader`, `build_optimization_graph()`, and `run_once()` do not exist. Once a partial Worker exists, the focused tests must fail on an unsafe checkpoint channel, a non-optimization claim, a revision-0 actionable review that does not invoke target iteration `1`, a persisted target revision that repeats an Agent, a graph cycle, a fourth iteration, an unavailable/degraded review unlocking another optimization call, a `zero_hit`/`low_confidence` input reaching any optimization HTTP call or immutable row before its guarded terminal defer, a missing pre-external renewal, a fabricated pre-revision result, omitted safe pre-revision timeout/schema audit records, a wrong defer iteration or passed-review downgrade, an incorrect fresh-session checkpoint/database failure transition, or swallowed cancellation. Syntax errors, fixture errors, real network/model/RAG/Milvus activity, model download, or a production CLI are invalid RED evidence.

- [ ] **Step 3: Implement the minimal recoverable optimization Worker**

Create only `backend/optimization_worker.py` with the exact exported types and functions above, the private safe checkpoint boundary, a private stable deterministic-change constructor, and narrow private control exceptions. Reuse `StateGraph`, `START`, `END`, `BaseCheckpointSaver`, `httpx.AsyncBaseTransport`, and the existing analysis Worker's `run_once()` shape; do not copy its rich checkpoint state or introduce a common Worker abstraction. Build the six-node graph so every node reloads Task 7 context before taking an action; derive each revision target only from that context; give the RAG loader the raising `Awaitable[None]` renewal callback and DeepSeek the existing boolean callback; pass every pre-revision optimization attempt record into the audited Task 7 defer transaction; and return only `OptimizationWorkflowState` fields.

Use Task 6 clients exactly as declared: primary first, at most one schema-repair call using the same trusted JSON inputs but the independent repair Prompt/node/call type, and no raw invalid response. Before constructing either client or issuing either call, apply the exact pre-revision `zero_hit`/`low_confidence` terminal gate with empty optimization calls and the DB-derived target iteration. Call Task 5 validation only after a typed optimization result, use its fixed messages for deterministic changes, and delegate all immutable/replay/coverage/fact/version/owner verification to Task 7 services. On an unusable persisted review, explicitly use Task 7 `fail_optimization_run(..., OPTIMIZATION_CONTEXT_INCONSISTENT)`; for checkpoint or database exceptions, dispose of the faulted graph session and use one fresh factory session for the guarded failure write. Follow the stated pre-revision, post-deterministic compliance-failure, semantic-degraded, iteration-limit, checkpoint, and database branches exactly. Do not add a CLI, a fallback loader, a RAG query, manual text, a retry loop outside Task 6, a new error code, or broad exception handling.

- [ ] **Step 4: Run GREEN and regression gates**

Run:

    # First apply “Unified D-drive development and verification environment” above.
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_optimization_worker.py tests/test_optimization_validation.py tests/test_optimization_agent.py tests/test_compliance_agent.py -v
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_analysis_worker.py tests/test_analysis_agent.py tests/test_deepseek_runtime.py -v
    D:\E-commerce_operations_env\python.exe -m pytest -v
    D:\E-commerce_operations_env\python.exe -m compileall backend tests
    D:\E-commerce_operations_env\python.exe -m alembic current
    D:\E-commerce_operations_env\python.exe -m alembic check
    docker compose config --quiet
    git diff --check
    git status --short

Expected: the Worker claims only optimization runs and uses the claimed ID as the sole LangGraph thread ID; its checkpoint holds only the declared safe progress keys; database context, not checkpoint payload, skips already persisted revision/review nodes and derives the target iteration without a cycle; the only new-revision path is `0 → actionable normal failed review → 1 → actionable normal failed review → 2`; draft-ready requires an exact double pass and a passed review cannot be downgraded; degraded/unavailable/error reviews cannot unlock another Agent; `zero_hit` and `low_confidence` trusted inputs directly use their guarded pending-manual codes before any optimization Agent call, revision, review, or audit; every fake RAG/DeepSeek boundary renews first and loss prevents subsequent request/write; completed deterministic compliance outages create exactly one fixed unavailable review before pending manual; a pre-revision DeepSeek timeout/schema failure preserves only its safe optimization call records atomically with its one legal pending-manual iteration, while a RAG failure preserves no invented call; a repeated terminal defer is zero-write `lease_lost`, while exact/different audit comparison is covered only under a live owner or uniqueness race; checkpoint and database corruption use a fresh guarded failure session, cancellation propagates, and fact/version/permission/replay cases are not misreported as degraded; all tests remain offline with no model, Milvus, download, real DeepSeek, or production CLI; D-drive pycache remains in use; and analysis, Alembic, Compose, and Git gates remain clean.

- [ ] **Step 5: Commit only the bounded optimization Worker deliverable**

    git add -- backend/optimization_worker.py tests/test_optimization_worker.py
    git diff --cached --name-only
    # Expected exact output, in any Git display order:
    # backend/optimization_worker.py
    # tests/test_optimization_worker.py
    git diff --cached --check
    git commit -m "feat: add recoverable optimization worker"
    git show --check --oneline HEAD
    git status --short

## Review Slice Boundary

Tasks 1–8 have passed plan review but none is authorized for implementation. Task 9 below awaits review; no later task is authorized.

### Task 9: Production trusted-input RAG, PostgreSQL recovery gates, and Worker CLI

**Files:**

- Create: `backend/optimization_trusted_input.py`
- Modify: `backend/knowledge_search.py`
- Modify: `backend/optimization_runs.py`
- Create: `scripts/run_optimization_worker.py`
- Modify: `tests/test_knowledge_search.py`
- Modify: `tests/test_optimization_worker.py`
- Create: `tests/test_optimization_postgres.py`
- Create: `tests/test_optimization_rag.py`
- Do not modify: Agent clients, Prompts, Pydantic schemas, migrations, routes, model mappings, `backend/optimization_worker.py`, `backend/workflow_leases.py`, the analysis Worker/CLI, Compose, or dependencies. The inspected `search_active_knowledge()` and analysis CLI signatures make no other existing-test change necessary.

The file split is intentional and minimal. `backend/optimization_trusted_input.py` is the single product-context-to-RAG adaptation boundary; it does not contain a second search algorithm, model wrapper, cache, or provider layer. `backend/knowledge_search.py` retains its established active-version retrieval and receives only the one callback needed to renew before its existing external boundaries. `backend/optimization_runs.py` stays the sole owner-guarded persistence service, adding only a private PostgreSQL citation fact recheck. The CLI binds those two existing Worker injections with `functools.partial`; it neither creates a service nor supplies a fallback loader.

**Interfaces:**

- Consumes: Task 5 `TrustedOptimizationInput`, `CanonicalRuleCitation`, and canonical citation quality values; Task 7 `OwnedOptimizationContext` plus guarded context/revision/review/failure-review persistence; Task 8's structural raising trusted-input renewal callback and `run_once()`; existing `search_active_knowledge()`, `get_knowledge_search_dependencies()`, `KnowledgeSearchOutcome`, `KnowledgeSearchHit`, `KnowledgeDependencyError`, `AsyncPostgresSaver`, `async_session_factory`, and the analysis CLI's Windows event-loop/owner/`--once` shape.
- Produces: the real `TrustedInputLoader` implementation, an optional per-boundary renewal callback in existing local knowledge search, database-current canonical citations, PostgreSQL-only concurrency/recovery proof, D-drive local-RAG proof, and one runnable optimization Worker command. It adds no queue, generic repository, factory, service, Compose Worker, model download, or second retrieval path.

    # backend/knowledge_search.py
    from collections.abc import Awaitable, Callable

    async def search_active_knowledge(
        session: AsyncSession,
        *,
        query: str,
        categories: list[str] | None,
        top_k: int,
        retrieval_path: RetrievalPath,
        load_dependencies: KnowledgeSearchLoader,
        before_external_attempt: Callable[[], Awaitable[None]] | None = None,
    ) -> KnowledgeSearchOutcome: ...

    # backend/optimization_trusted_input.py
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from backend.config import Settings
    from backend.optimization_runs import OwnedOptimizationContext
    from backend.schemas import TrustedOptimizationInput

    BeforeTrustedInputExternalAttempt = Callable[[], Awaitable[None]]

    async def load_optimization_trusted_input(
        context: OwnedOptimizationContext,
        before_external_attempt: BeforeTrustedInputExternalAttempt,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> TrustedOptimizationInput: ...

`load_optimization_trusted_input()` structurally satisfies Task 8 `TrustedInputLoader`. The `settings` parameter is bound once by the CLI together with `session_factory`; retrieval dependency construction remains the existing cached `get_knowledge_search_dependencies()` call, so model paths, Milvus URI/collection, calibration, timeout handling, and cache ownership remain single-sourced in the existing settings/search layer rather than being duplicated in this adapter.

`search_active_knowledge()` must keep all existing callers source-compatible: `before_external_attempt=None` has exactly current behavior, so the knowledge API and calibration script remain unchanged. It first performs the existing PostgreSQL active-version query; a zero-version result returns `zero_hit` without calling the callback or dependency loader. For a nonempty active-version set, a local private `await_before_external()` awaits the optional callback immediately before each of these existing operations: `load_dependencies()` (local model/Milvus dependency initialization), `models.embed_query()`, every `index.dense_search()`, every `index.sparse_search()`, and `models.rerank()`. Hybrid retrieval therefore renews independently before dense and sparse calls. The callback is not swallowed or translated; in particular `OptimizationLeaseLost` and `asyncio.CancelledError` propagate. It must not alter fusion, canonical chunk filtering, scoring, error types, calibration behavior, or `KnowledgeSearchOutcome` semantics.

The real loader performs exactly this adaptation, entirely in process memory and without logging its query or product text:

```python
_GENERAL_RULE_CATEGORY = "通用规则"
_QUERY_SUFFIX = "标题 卖点 详情 属性 SKU 价格 合规"
_QUERY_MAX_CHARS = 1600
_TOP_K = 10

query = " ".join(
    part.strip()
    for part in (
        context.title,
        context.category,
        context.brand,
        *context.selling_points,
        _QUERY_SUFFIX,
    )
    if part.strip()
)[:_QUERY_MAX_CHARS]
categories = list(dict.fromkeys((context.category, _GENERAL_RULE_CATEGORY)))

async with session_factory() as search_session:
    outcome = await search_active_knowledge(
        search_session,
        query=query,
        categories=categories,
        top_k=_TOP_K,
        retrieval_path="hybrid_rerank",
        load_dependencies=get_knowledge_search_dependencies,
        before_external_attempt=before_external_attempt,
    )
```

The query is deliberately bounded to the current trusted title/category/brand/ordered selling points plus the fixed Chinese terms above. It contains no description body, attributes, SKU values, candidate evidence, ID, path, vector, Prompt, or credential. Categories are exactly the product category plus `通用规则`, deduplicated in that order; no category DSL or inferred applicability rule is introduced.

`KnowledgeSearchHit` deliberately lacks document and version IDs, so no hit becomes a citation directly. After the search session closes, the loader opens a **new** `session_factory()` session and joins `KnowledgeChunk -> KnowledgeDocumentVersion -> KnowledgeDocument` for the returned chunk IDs. It accepts a row only when all of the following database facts hold simultaneously: chunk `id`/`canonical_text` equal the hit; version owns that chunk, is `KnowledgeVersionStatus.ACTIVE`, and has the hit's `version_number`; document owns that version through `current_version_id`, is enabled, and has the hit's name/category; and document category is in the exact two-category list. It constructs every accepted `CanonicalRuleCitation` from database columns with `active=True` and `applicable=True`, carries full document/version/chunk IDs and canonical text, deduplicates by `chunk_id`, and sorts lexicographically by `chunk_id`. An orphan, superseded version, disabled document, non-applicable category, or any mismatched hit field is silently discarded. Quality is deliberately conservative after filtering: an empty accepted set is `zero_hit`; an original `low_confidence` remains `low_confidence`; an original `normal` remains `normal` only when its originally first-ranked hit is in the accepted chunk-ID set, otherwise it becomes `low_confidence`. The loader neither reuses a discarded score nor runs a second retrieval. It then copies every product/SKU/candidate field from `OwnedOptimizationContext` into the exact Task 5 `TrustedOptimizationInput` fields.

The loader translates only expected local-knowledge dependency outcomes into Task 8's restricted `TrustedInputLoadFailure` literal set: `KNOWLEDGE_MODEL_UNAVAILABLE` and `KNOWLEDGE_DEPENDENCY_TIMEOUT` retain their codes; `KNOWLEDGE_CALIBRATION_UNAVAILABLE`, `KNOWLEDGE_MILVUS_UNAVAILABLE`, and every other `KnowledgeDependencyError` map to `KNOWLEDGE_DEPENDENCY_ERROR`; it converts a returned `zero_hit`/`low_confidence` quality into the typed input, not an exception. It never includes a dependency message, local path, provider value, or vector in that exception. Unknown SQLAlchemy/database errors propagate to Task 8 as fact/database failures; `asyncio.CancelledError` and `OptimizationLeaseLost` propagate unchanged.

Task 9 adds a private, DB-only helper inside `backend/optimization_runs.py`, not another exported abstraction:

```python
async def _recheck_canonical_citations(
    session: AsyncSession,
    *,
    product_category: str,
    citations: Sequence[CanonicalRuleCitation],
) -> bool: ...
```

It rejects duplicate chunk IDs and, for every supplied citation, queries the same `KnowledgeChunk -> KnowledgeDocumentVersion -> KnowledgeDocument` relation using the freshly locked context transaction. It ignores supplied `active`/`applicable` booleans as evidence and returns true only if IDs, ownership, document name, version number, category, and canonical text all match database columns; document is enabled; `document.current_version_id == version.id`; version is `ACTIVE`; and category is exactly the current product category or `通用规则`. It performs no model, Milvus, callback, or RAG work. `load_owned_optimization_context()` invokes it before returning a saved revision's citations; `persist_optimization_revision()`, `persist_compliance_review()`, and `persist_compliance_failure()` invoke it before inserting any revision/review/audit row. An inactive, replaced, disabled, non-applicable, malformed, or changed citation is a current-owner guarded `FAILED/OPTIMIZATION_FACT_ERROR`; it creates no new revision, review, AgentCall, draft/pending-manual terminal, or checkpoint data. A lost owner writes nothing. This recheck preserves Task 7's full fresh context/version/authorization guard and its exact immutable replay behavior.

The production CLI must be the smallest usable adaptation of the inspected analysis command:

```python
# scripts/run_optimization_worker.py
import argparse
import asyncio
from functools import partial
import os
import socket

async def main(once: bool) -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from backend.config import get_settings
    from backend.database import async_session_factory
    from backend.optimization_trusted_input import load_optimization_trusted_input
    from backend.optimization_worker import run_once

    settings = get_settings()
    lease_owner = f"{socket.gethostname()}:{os.getpid()}"
    trusted_input_loader = partial(
        load_optimization_trusted_input,
        session_factory=async_session_factory,
        settings=settings,
    )
    async with AsyncPostgresSaver.from_conn_string(settings.langgraph_database_url) as checkpointer:
        await checkpointer.setup()
        while True:
            processed = await run_once(
                async_session_factory,
                settings=settings,
                lease_owner=lease_owner,
                checkpointer=checkpointer,
                trusted_input_loader=trusted_input_loader,
            )
            if once:
                return
            if processed is None:
                await asyncio.sleep(1)
```

Keep the existing `argparse --once`, Windows `asyncio.WindowsSelectorEventLoopPolicy()` before `asyncio.run()`, one-second empty-queue sleep, `AsyncPostgresSaver.setup()`, and `host:pid` owner exactly as the analysis command. The command does not read, print, or validate a DeepSeek Key; it initializes no second client and offers no fake/no-op/unconditional-failure loader.

- [ ] **Step 1: Write the failing focused unit, PostgreSQL, and local-RAG tests**

First extend `tests/test_knowledge_search.py` without duplicating the existing retrieval matrix. Add a recording async callback and recording dependency/model/index fakes to prove this exact chronology for `hybrid_rerank`: `renew, dependency_init, renew, embed, renew, dense, renew, sparse, renew, rerank`. Verify dense-only and sparse-only make callbacks only for their actual boundaries, zero active versions makes none, and a raising callback prevents the immediately following operation. Keep all existing calls without the new keyword to prove `None` compatibility; no test loads a model or opens Milvus.

Create `tests/test_optimization_rag.py` as a mixed normal/opt-in test file, not a module-skipped integration file:

```python
_OPT_IN = pytest.mark.skipif(
    os.getenv("RUN_KNOWLEDGE_INTEGRATION") != "1",
    reason="explicit PostgreSQL, Milvus, and local-model integration opt-in required",
)

async def test_loader_builds_only_bounded_query_and_canonical_trusted_input(...): ...

@_OPT_IN
async def test_real_local_rag_loader_returns_only_current_applicable_citations(...): ...
```

The ordinary tests monkeypatch `backend.optimization_trusted_input.search_active_knowledge` with typed `KnowledgeSearchOutcome`/`KnowledgeSearchHit` fixtures and use SQLite ORM rows in distinct fake session-factory sessions; they never call `get_knowledge_search_dependencies()`, LocalKnowledgeModels, Milvus, or a network. Use a complete Task 7 `OwnedOptimizationContext` and assert the loader sends only the ordered, 1600-character-capped title/category/brand/selling-point/fixed-suffix query and exactly `[context.category, "通用规则"]` after deduplication. Assert every current context product field, full SKU collection, complete `ProductMetrics`, candidate evidence, and database-built canonical citation maps to the exact Task 5 `TrustedOptimizationInput` field; the loader returns no Key, Authorization, Prompt, raw provider body, vector, path, or checkpoint data.

Seed accepted, disabled, superseded, non-applicable, orphan, and hit-field-mismatched SQLite rows. Assert only the accepted current/product-category or `通用规则` hits become canonical citations; all others disappear. Add returned `zero_hit`, returned `low_confidence`, and an originally `normal` result whose first-ranked hit is discarded while a second hit remains: assert respectively `zero_hit`, `low_confidence`, and conservatively downgraded `low_confidence`, without reissuing retrieval. Map `KNOWLEDGE_MODEL_UNAVAILABLE` and `KNOWLEDGE_DEPENDENCY_TIMEOUT` unchanged, map calibration/Milvus/other `KnowledgeDependencyError` values to `KNOWLEDGE_DEPENDENCY_ERROR`, and assert `SQLAlchemyError`, `asyncio.CancelledError`, and `OptimizationLeaseLost` propagate unchanged. Make the fake callback raise before search and assert no subsequent fake dependency boundary runs. Add the smallest CLI import/wiring test: direct `importlib.import_module("scripts.run_optimization_worker")` must not construct dependencies or read a Key; with patched `get_settings`, `AsyncPostgresSaver`, and `optimization_worker.run_once`, await `main(True)` and assert exactly one `run_once()` call received a `functools.partial` of `load_optimization_trusted_input` bound to the supplied session factory/settings. The test never invokes that partial, so no model or Key is accessed.

Extend Task 7's SQLite `tests/test_optimization_worker.py` using real SQLite `KnowledgeDocument`, `KnowledgeDocumentVersion`, and `KnowledgeChunk` rows—not a RAG fake—to cover the persistence helper through its public guarded operations. Seed a current enabled product-category document and `通用规则` document, then assert exact citations allow context reload, revision persistence, normal review persistence, and failure-review persistence. Parameterize citation mutations after the first facts read: document `enabled=False`; `current_version_id` switched to another active version; version status no longer active; category outside `{product.category, "通用规则"}`; changed document name/version number/canonical text; orphan chunk; and supplied `active/applicable=True` despite database failure. Each current-owner case must become `FAILED/OPTIMIZATION_FACT_ERROR` with no new revision/review/call or draft/pending-manual write, while an old owner remains zero-write. Assert a saved revision with an invalidated citation fails during `load_owned_optimization_context()` rather than being handed to a resumed Worker.

Create `tests/test_optimization_postgres.py` with module-level explicit opt-in and no model/RAG/DeepSeek dependency:

```python
pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="explicit PostgreSQL integration opt-in required",
)
```

Use `uuid4()` for every store/user/product/SKU/candidate/run/proposal/revision/review/call/document/version/chunk/checkpoint thread ID and a test-specific string prefix. Keep `owned_workflow_ids` for all rows seeded by this test. Immediately before each claim, query exactly the optimization claim predicate **excluding** `WorkflowRun.id.in_(owned_workflow_ids)` and either `pytest.skip()` before claiming when an external claimable row exists or proceed safely when none exists; never claim, mutate, exhaust, or clean an external row. This exclusion remains in place after the test seeds its own accepted rows, so those rows cannot falsely block their own claim case. In parallel `async_session_factory()` sessions, seed two accepted optimization chains and assert `claim_next_optimization_run()` uses `SKIP LOCKED` to hand different IDs to different owners. Set one owned row to expired attempt 2 and prove claim 3 reacquires it; set a different expired row to attempt 3 and prove only that optimization row becomes `FAILED/LEASE_ATTEMPTS_EXHAUSTED`, with an analysis run untouched. For every context, revision, review, defer/final, and failure operation, make the old owner call after replacement and assert it has zero rows/pointer/terminal changes.

Use a real `AsyncPostgresSaver` and the Task 8 Worker with a fake typed trusted-input loader plus `httpx.MockTransport`; neither fake may load local models. For one exact `workflow_run_id` thread, assert persisted checkpoint channels equal the Task 8 allowlist. Interrupt/checkpoint-fail or cancel after immutable revision persistence, expire its lease, reclaim under a second owner using the same thread ID, and prove context reconstruction skips the optimization Agent, never duplicates revision/review/`agent_calls`, and completes only missing work. Include direct checkpoint get/put failure plus old-owner loss, product version mutation, and citation active/current-version mutation cases; each must retain Task 7/8's guarded failure or zero-write semantics.

The PostgreSQL test owns exact ID collections and always cleans only them in `finally`, in this FK-safe child-to-parent order: delete checkpointer rows by each exact `thread_id`; AgentCalls; ComplianceReviews; set only recorded `ProductProposal.current_revision_id` values to `NULL`; ProposalRevisions; ProductProposals; AnalysisCandidates; optimization and source-analysis WorkflowRuns; ProductSkus; Products; exact UserStoreScopes; Stores; KnowledgeChunks; set only recorded `KnowledgeDocument.current_version_id` values to `NULL`; KnowledgeDocumentVersions; KnowledgeDocuments; Users. Each `DELETE` includes only a recorded ID collection or a subquery restricted to one. The fixture creates no other child rows; before implementation, verify this exact order against `Base.metadata` and, if a test introduces an additional mapped child, record its precise IDs and delete that child before its parent. It never deletes a table, collection, prefix-wide data, or another user's records.

Decorate only the real local-RAG test above with `_OPT_IN`. It uses the existing D-drive BGE-M3/reranker, PostgreSQL, and configured Milvus collection through `get_knowledge_search_dependencies()` and `search_active_knowledge()` only. Create UUID-scoped documents/chunks in PostgreSQL and their exact Milvus chunk IDs: one active current document in the product category, one active current `通用规则`, one active non-applicable category, one disabled document, one superseded old version, and one Milvus orphan. Call the real loader with a recording successful renewal callback and assert returned citations contain only the two active/applicable current database rows, full IDs/names/version numbers/canonical text, `active=True`, `applicable=True`, and stable chunk-ID order. Record callbacks to cover dependency initialization, embedding, dense, sparse, and rerank; inject a lease-loss callback and prove the next local boundary makes no dependency call. Accept either real `normal` or real `low_confidence` quality and assert only the returned typed quality; deterministic unit tests above, not a hardware- and calibration-sensitive natural-language query, prove the low-confidence mapping. Then change a returned citation's current version or document enabled flag and prove Task 7 persistence rejects it with `OPTIMIZATION_FACT_ERROR`.

In the real-RAG test's `finally`, obtain and delete only recorded chunk IDs from Milvus, then only recorded database rows in this exact FK-safe order: checkpoint exact thread IDs; AgentCalls; ComplianceReviews; set only recorded proposal `current_revision_id` values to `NULL`; ProposalRevisions; ProductProposals; AnalysisCandidates; optimization and source-analysis WorkflowRuns; ProductSkus; Products; exact UserStoreScopes; Stores; KnowledgeChunks; set only recorded document `current_version_id` values to `NULL`; KnowledgeDocumentVersions; KnowledgeDocuments; Users. The fixture creates no other child rows; verify this list against `Base.metadata` before implementation and retain any additional created child only with exact-ID cleanup before its parent. Remove only recorded test upload paths after verifying each is beneath the configured D-drive test upload directory. Do not delete a Milvus collection, model directory, calibration file, general upload directory, or any C-drive personal file. The test never calls DeepSeek or downloads a model.

- [ ] **Step 2: Run RED and verify the real RAG/persistence/CLI boundaries are absent**

Run:

    # First apply “Unified D-drive development and verification environment” above.
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_knowledge_search.py tests/test_optimization_worker.py tests/test_optimization_rag.py -v

Expected: the focused suite fails because the optional callback, real `load_optimization_trusted_input()`, current-citation persistence recheck, and optimization CLI do not exist. Once a partial change exists, the focused tests must fail for a missed dependency/model/Milvus/rerank renewal, a callback failure followed by any dependency call, a direct Milvus hit treated as canonical, an incorrectly retained normal quality after its top hit is discarded, a non-applicable/disabled/superseded/orphan citation accepted, a database citation mutation after load accepted at persistence, a side-effectful CLI import/wrong `--once` partial wiring, or a non-RAG test touching a model/Milvus/network. Fixture, syntax, or opt-in-marker failures are not valid RED evidence.

- [ ] **Step 3: Implement the minimum real trusted-input and current-citation boundary**

Create only `backend/optimization_trusted_input.py` with the exact exported loader signature. Reuse `get_knowledge_search_dependencies()` and a single `search_active_knowledge()` call; construct the 1600-character in-memory query and two allowed categories exactly as above; run search and hit recheck in distinct `session_factory()` sessions; derive post-filter quality using the explicit empty/original-low/first-ranked-accepted rule above; map only the declared `KnowledgeDependencyError` cases; build `TrustedOptimizationInput` solely from `OwnedOptimizationContext`, outcome quality, and database-built canonical citations. Do not log the query, cache results, expose a second loader, add a search algorithm, synthesize citations, or catch cancellation/lease loss/database faults.

Modify `backend/knowledge_search.py` only to add the optional callback argument and await it at the five actual boundary kinds above. Reuse one small private await helper inside that function; retain existing search ordering, all score calculations, active-version filtering, errors, and public callers. Modify `backend/optimization_runs.py` only with the private citation fact recheck and its calls in locked context reload plus revision/review/failure-review transactions before their new immutable/audit writes. Database columns, rather than caller booleans or prior loader output, decide active/current/applicable. Keep Task 7's owner/type/lease/version guard, exact replay, and no-`on_conflict_do_update` rule intact.

Create the CLI from the shown analysis-shaped code: bind `load_optimization_trusted_input` by `functools.partial(session_factory=async_session_factory, settings=settings)`, pass it to Task 8 `run_once()`, and use the existing LangGraph PostgreSQL saver/setup/owner/`--once`/sleep/Windows handling. Do not add a CLI client, settings field, provider factory, queue, fallback loader, Compose entry, or key output.

- [ ] **Step 4: Run GREEN, explicit opt-in integrations, and complete regression gates**

Run the ordinary gates only after applying the named D-drive environment block; it clears all three opt-ins and blanks the key, so this suite remains offline:

    # First apply “Unified D-drive development and verification environment” above.
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_knowledge_search.py tests/test_optimization_worker.py tests/test_optimization_rag.py -v
    D:\E-commerce_operations_env\python.exe -m pytest -v
    D:\E-commerce_operations_env\python.exe -m compileall backend tests scripts
    D:\E-commerce_operations_env\python.exe -m alembic current
    D:\E-commerce_operations_env\python.exe -m alembic check
    docker compose config --quiet
    git diff --check
    git status --short

Run the PostgreSQL proof separately and only after the same named block, in a process that explicitly sets its one opt-in while keeping model/cache/key settings unchanged:

    # First apply “Unified D-drive development and verification environment” above.
    $env:RUN_POSTGRES_INTEGRATION = "1"
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_optimization_postgres.py -v

Run the local-RAG proof separately and only after the same named block. Confirm the actual existing `Settings` field names and model directories before the command: `KNOWLEDGE_EMBEDDING_MODEL_PATH` maps to `knowledge_embedding_model_path` and must be `D:\E-commerce_operations\model\bge-m3`; `KNOWLEDGE_RERANKER_MODEL_PATH` maps to `knowledge_reranker_model_path` and must be `D:\E-commerce_operations\model\bge-reranker-v2-m3`. These are local-files-only models; this command must not download anything.

    # First apply “Unified D-drive development and verification environment” above.
    $env:RUN_KNOWLEDGE_INTEGRATION = "1"
    $env:KNOWLEDGE_EMBEDDING_MODEL_PATH = "D:\E-commerce_operations\model\bge-m3"
    $env:KNOWLEDGE_RERANKER_MODEL_PATH = "D:\E-commerce_operations\model\bge-reranker-v2-m3"
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_optimization_rag.py -v

Expected: ordinary tests make no network, DeepSeek, model, Milvus, upload, or model-repository access; PostgreSQL proves type-isolated `SKIP LOCKED`, expired lease behavior, owner guards, same-thread checkpoint recovery, nonduplicated immutable work, exact checkpoint allowlist, version/citation invalidation, and precise cleanup; local RAG proves every returned citation is current/enabled/active/applicable database truth, each local boundary renews first, no raw hit becomes a citation, real quality is safely typed without a threshold-sensitive expectation, fault mapping is safe, and persistence rejects a citation invalidated after retrieval. Compileall, Alembic, Compose, Git whitespace, and status gates remain clean.

- [ ] **Step 5: Commit only the production trusted-input/RAG/CLI deliverable**

    git add -- backend/optimization_trusted_input.py backend/knowledge_search.py backend/optimization_runs.py scripts/run_optimization_worker.py tests/test_knowledge_search.py tests/test_optimization_worker.py tests/test_optimization_postgres.py tests/test_optimization_rag.py
    git diff --cached --name-only
    # Expected exact output, in any Git display order:
    # backend/knowledge_search.py
    # backend/optimization_runs.py
    # backend/optimization_trusted_input.py
    # scripts/run_optimization_worker.py
    # tests/test_knowledge_search.py
    # tests/test_optimization_postgres.py
    # tests/test_optimization_rag.py
    # tests/test_optimization_worker.py
    git diff --cached --check
    git commit -m "feat: wire optimization RAG and worker CLI"
    git show --check --oneline HEAD
    git status --short

## Review Slice Boundary

Tasks 1–9 have passed plan review but none is authorized for implementation. Task 10 below awaits review; no later task is authorized.

### Task 10: Explicit real dual-Agent smoke and final acceptance gates

**Files:**

- Create: `tests/test_optimization_deepseek_smoke.py`
- Do not modify: any production file, `tests/test_deepseek_smoke.py`, RAG/Milvus/database/Worker code, configuration, migrations, Compose, or dependencies. This test is the sole explicit-real-DeepSeek exception and is never an analysis-smoke extension.

**Interfaces:**

- Consumes: Task 1 `AgentCallType`; Task 5 `TrustedOptimizationInput`, `TrustedProductSku`, `CanonicalRuleCitation`, `OptimizationProposalOutput`, `DeterministicComplianceResult`, and `validate_optimization_output()`; Task 6 `ProductOptimizationAgentClient.request()`, `validate_optimization_response()`, `ProductComplianceAgentClient.request()`, `validate_compliance_response()`, their invocation/record types, primary node names, and Prompt versions; the existing `Settings` default model value; and `BeforeHttpAttempt`.
- Produces: one human-authorized, one-file contract smoke that makes at most one real primary request per independent Agent, proves typed/safe boundary handling, and leaves no database, checkpoint, RAG, Milvus, Worker, product, SKU, or platform mutation. It is an acceptance signal only; it is neither a publication, a deployment, nor automatic release authorization.

```python
# tests/test_optimization_deepseek_smoke.py
import os
from dataclasses import asdict
from decimal import Decimal

import pytest

from backend.common import AgentCallType
from backend.compliance_agent import (
    COMPLIANCE_PROMPT_VERSION,
    ProductComplianceAgentClient,
    validate_compliance_response,
)
from backend.config import get_settings
from backend.optimization_agent import (
    OPTIMIZATION_PROMPT_VERSION,
    ProductOptimizationAgentClient,
    validate_optimization_response,
)
from backend.optimization_validation import validate_optimization_output
from backend.schemas import (
    CanonicalRuleCitation,
    ProductMetrics,
    TrustedOptimizationInput,
    TrustedProductSku,
)

pytestmark = pytest.mark.deepseek_smoke

@pytest.mark.skipif(
    os.getenv("RUN_DEEPSEEK_SMOKE") != "1",
    reason="explicit DeepSeek smoke authorization required",
)
async def test_explicit_optimization_and_compliance_primary_contract_smoke() -> None: ...
```

The smoke begins with only stable, non-secret checks:

```python
settings = get_settings()
assert settings.deepseek_model == "deepseek-v4-flash", "DEEPSEEK_SMOKE_MODEL_MISMATCH"
key = settings.deepseek_api_key
if key is None or not key.get_secret_value().strip():
    pytest.fail("DEEPSEEK_SMOKE_KEY_REQUIRED")
```

This fixed failure does not interpolate, print, log, serialize, or otherwise expose the key. With `RUN_DEEPSEEK_SMOKE` absent or different from `1`, pytest skips before settings/client creation and makes zero network requests. The explicit check deliberately fails rather than silently skipping when authorization is present but the key is absent or blank.

Construct the entire trusted input in memory. Use Chinese current product facts, for example title `"家居收纳盒"`, category `"家居"`, brand `"演示品牌"`, one Chinese selling point, Chinese current description/keywords/attributes, and exactly one `TrustedProductSku(id="smoke-sku-1", code="SMOKE-RED", spec={"颜色": "红"}, price=Decimal("100.00"), stock=10)`. Build the existing full `ProductMetrics.from_totals(impressions=100, clicks=10, orders=1, units=1, revenue=Decimal("100.00"), refunds=0).model_copy(update={"product_id": "smoke-product-1", "product_code": "SMOKE-001"})`, one deterministic candidate-evidence string, `rag_quality="normal"`, and one `CanonicalRuleCitation` with synthetic in-memory IDs, category `"通用规则"`, Chinese canonical rule text, `active=True`, and `applicable=True`. No fixture reads a file, opens a session, starts the Worker, initializes RAG/Milvus, captures raw HTTP bodies, or persists an object.

Use two separate narrow counters, each implementing Task 3/6's exact `Callable[[int], Awaitable[bool]]` `BeforeHttpAttempt` contract. The callback gates only an attempt number; it receives no call type and must not be used to infer PRIMARY identity:

```python
def one_primary_attempt_guard():
    calls = 0

    async def before_http_attempt(attempt: int) -> bool:
        nonlocal calls
        calls += 1
        return calls == 1 and attempt == 1

    return before_http_attempt, lambda: calls
```

Instantiate only `ProductOptimizationAgentClient(settings)` and `ProductComplianceAgentClient(settings)`. Do not construct a runtime directly, route through an Agent factory, patch a provider, create a `MockTransport`, launch an optimization Worker, or schedule schema repair. Invoke optimization exactly once:

```python
optimization = await optimization_client.request(
    trusted,
    required_changes=(),
    call_type=AgentCallType.PRIMARY,
    iteration=0,
    before_http_attempt=optimization_guard,
    max_attempts=1,
)
if optimization.response is None:
    pytest.fail("OPTIMIZATION_SMOKE_TYPED_RESPONSE_REQUIRED")
output = validate_optimization_response(trusted, (), optimization.response)
deterministic = validate_optimization_output(trusted, output)
```

Then invoke compliance exactly once and only after the typed optimization response and deterministic result exist:

```python
compliance = await compliance_client.request(
    output,
    deterministic,
    trusted,
    call_type=AgentCallType.PRIMARY,
    iteration=0,
    before_http_attempt=compliance_guard,
    max_attempts=1,
)
if compliance.response is None:
    pytest.fail("COMPLIANCE_SMOKE_TYPED_RESPONSE_REQUIRED")
semantic = validate_compliance_response(trusted, compliance.response)
combined_passed = deterministic.passed and semantic.passed and not semantic.degraded
assert isinstance(combined_passed, bool)
```

Wrap each parser/validator call in a narrow local `try` for its declared schema/validation exception classes and replace it with one of `OPTIMIZATION_SMOKE_CONTRACT_INVALID` or `COMPLIANCE_SMOKE_CONTRACT_INVALID` using `from None`; never include provider text, a Prompt, a request, headers, or an exception representation. Success requires both clients to return a typed response and pass their respective structure/trusted-fact validator boundaries. It does **not** require `deterministic.passed`, `semantic.passed`, or `combined_passed` to be true, and it does not turn a successful contract response into `draft_ready`.

After each call, assert its independent guard count is exactly one; its record list has exactly one safe record; that record has `call_type=PRIMARY`, `iteration=0`, `attempt=1`, `model="deepseek-v4-flash"`, and exactly its own identity:

```python
assert optimization.records[0].node_name == "call_product_optimization_agent"
assert optimization.records[0].call_type is AgentCallType.PRIMARY
assert optimization.records[0].prompt_version == OPTIMIZATION_PROMPT_VERSION
assert compliance.records[0].node_name == "call_product_compliance_agent"
assert compliance.records[0].call_type is AgentCallType.PRIMARY
assert compliance.records[0].prompt_version == COMPLIANCE_PROMPT_VERSION
```

The one-attempt guards plus `max_attempts=1`, one record per client, and absence of any `SCHEMA_REPAIR` invocation prove no retry, repair, automatic revision, second compliance call, or extra POST is allowed. PRIMARY identity comes only from the supplied request `call_type` and the final typed record's `call_type` plus Agent-specific `node_name`, never from callback arguments. Serialize only the two safe record dataclasses with `asdict()`. Their keys may include the required metadata key `prompt_version`, but must not include an exact unsafe field named `prompt`, `payload`, `raw_response`, `header`, `headers`, `authorization`, `api_key`, `cookie`, or `chain_of_thought`; assert that exact forbidden-key set is disjoint from every serialized record's keys. Do not serialize or print a `SecretStr`, request, response body, or environment mapping.

- [ ] **Step 1: Write the opt-in-only real double-Agent smoke test**

Create only `tests/test_optimization_deepseek_smoke.py` with the exact skip marker, safe missing-key failure, in-memory trusted snapshot, separate one-primary guards, two primary-only invocations, validator flow, stable failure strings, final-record PRIMARY identity assertions, and safe-record-key assertion above. The `skipif(RUN_DEEPSEEK_SMOKE != "1")` marker is the complete normal-process gate: the ordinary offline test command proves this real smoke defaults to skipped. Keep the real test physically independent from `tests/test_deepseek_smoke.py`; do not import its helper, its schema-repair orchestration, or any analysis type.

- [ ] **Step 2: Run RED and confirm the smoke file is absent without authorizing a real request**

Run:

    # First apply “Unified D-drive development and verification environment” above.
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_optimization_deepseek_smoke.py -v

Expected: pytest reports the smoke test file is absent. This RED command leaves `RUN_DEEPSEEK_SMOKE` absent and the DeepSeek key blank, so it cannot create a client or network request. After the file exists, the same command must skip the real case; any real call, key echo, raw-response assertion, schema-repair path, retry, Worker/RAG/database construction, or analysis-smoke import is invalid evidence.

- [ ] **Step 3: Implement only the explicit smoke test**

Create no production code. Implement the single test file from the declared imports and exact call sequence. Read `SecretStr` only to test whether it is absent/blank immediately before the authorized request; never format it into an assertion, log, exception, fixture, request argument, command line, test title, or saved artifact. Keep the primary calls in one test and use the two fixed safe failure strings for typed/schema validation failure. Do not make output content, deterministic pass, semantic pass, combined pass, publishing, approval, proposal revision, or database persistence a smoke requirement.

- [ ] **Step 4: Run GREEN, then the final isolated acceptance sequence after explicit human approval**

Run ordinary acceptance first, after applying the named D-drive environment block. This clears all opt-ins and the key, so all real-network tests remain skipped and ordinary tests stay offline:

    # First apply “Unified D-drive development and verification environment” above.
    D:\E-commerce_operations_env\python.exe -m pytest -v
    D:\E-commerce_operations_env\python.exe -m compileall backend tests scripts
    D:\E-commerce_operations_env\python.exe -m alembic current
    D:\E-commerce_operations_env\python.exe -m alembic check
    docker compose config --quiet
    git diff --check
    git status --short

Run the PostgreSQL proof only in a fresh PowerShell process after applying the named D-drive block; do not set the knowledge or DeepSeek opt-ins there:

    # First apply “Unified D-drive development and verification environment” above.
    $env:RUN_POSTGRES_INTEGRATION = "1"
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_optimization_postgres.py -v

Run local RAG only in another fresh PowerShell process after the named block; do not set the PostgreSQL or DeepSeek opt-ins there. The paths remain local-files-only and download is prohibited:

    # First apply “Unified D-drive development and verification environment” above.
    $env:RUN_KNOWLEDGE_INTEGRATION = "1"
    $env:KNOWLEDGE_EMBEDDING_MODEL_PATH = "D:\E-commerce_operations\model\bge-m3"
    $env:KNOWLEDGE_RERANKER_MODEL_PATH = "D:\E-commerce_operations\model\bge-reranker-v2-m3"
    D:\E-commerce_operations_env\python.exe -m pytest tests/test_optimization_rag.py -v

Run the real dual-Agent smoke last and **only after the user explicitly approves that real request**. Execute it in a third fresh PowerShell process. Preserve the existing key only in a task-local variable, then apply the global block (which clears it), check the saved variable without output, restore it only to the process environment for pytest, and remove both values in `finally`:

    $task10_deepseek_key = $env:DEEPSEEK_API_KEY
    # Apply “Unified D-drive development and verification environment” above in this same process.
    if ([string]::IsNullOrWhiteSpace($task10_deepseek_key)) { throw "DEEPSEEK_SMOKE_KEY_REQUIRED" }
    try {
        $env:DEEPSEEK_API_KEY = $task10_deepseek_key
        $env:RUN_DEEPSEEK_SMOKE = "1"
        D:\E-commerce_operations_env\python.exe -m pytest tests/test_optimization_deepseek_smoke.py -v
    }
    finally {
        Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
        Remove-Item Env:RUN_DEEPSEEK_SMOKE -ErrorAction SilentlyContinue
        Remove-Variable task10_deepseek_key -ErrorAction SilentlyContinue
    }

This command never prints, writes, or passes the key on a command line. The expected successful smoke outcome is exactly one primary callback, at most one POST, one safe record, and typed/validated results per Agent; it never promises double-track approval, `draft_ready`, a database write, publication, or deployment.

- [ ] **Step 5: Commit only the explicit smoke test**

    git add -- tests/test_optimization_deepseek_smoke.py
    git diff --cached --name-only
    # Expected exact output:
    # tests/test_optimization_deepseek_smoke.py
    git diff --cached --check
    git commit -m "test: add optimization dual-agent smoke"
    git show --check --oneline HEAD
    git status --short

## Final Plan Self-Review

This is a plan-completeness audit, not implementation evidence. All Task checkboxes remain unchecked; no code, migration, test execution, external request, or commit is authorized by this review.

### Specification coverage

| Approved specification section | Planned implementation coverage | Audit result |
|---|---|---|
| 1. Goal, scope, and exclusions | Header, Global Constraints, Tasks 4, 7–10 | Covered; no human editing, approval, publishing, frontend, platform integration, or local generative model path is planned. |
| 2. Modular flow and typed workflow separation | Tasks 1, 2, 4, 7, 8 | Covered; analysis and optimization claims remain type-isolated and selection atomically creates the optimization run. |
| 3. States and three-iteration ceiling | Tasks 1, 7, 8 | Covered; only iterations 0, 1, and 2 exist, with actionable-normal review gating and guarded terminal outcomes. |
| 4. Persistence and immutable audit schema | Task 1 and Task 7 | Covered; workflow date/type checks, immutable proposal/revision/review rows, safe AgentCall iteration uniqueness, and replay guards are defined. |
| 5. Trusted facts, RAG, and output contract | Tasks 5, 6, 7, 9 | Covered; output validation, active/applicable canonical citations, fresh PostgreSQL recheck, and price/SKU suggestion-only boundaries are explicit. |
| 6. Deterministic plus semantic compliance | Tasks 5, 6, 7, 8, 10 | Covered; two independent clients/validators persist and combine the tracks, while the smoke never turns a contract response into release. |
| 7. Select-product API, read API, RBAC, and idempotency | Task 4 | Covered; current database operator/scope checks, replay/conflict behavior, and safe read aggregation are specified. |
| 8. Lease, checkpoint, and recovery semantics | Tasks 2, 7, 8, 9 | Covered; database-time guards, renewal before every external boundary, safe thread state, reclaim, and zero-write old-owner behavior are specified. |
| 9. Failure classification and safe degradation | Tasks 3, 6, 7, 8, 9 | Covered; DeepSeek/RAG/schema paths become pending manual only under declared safe codes; fact, authorization, version, database, replay, and checkpoint faults fail. |
| 10. Security and audit data boundary | Tasks 3, 6, 8, 9, 10 | Covered; only safe metadata persists, and smoke assertions forbid unsafe record fields or secret/raw output handling. |
| 11. Test matrix and isolation | Tasks 1–10 | Covered; normal suite is fake/Mock-only, PostgreSQL/RAG/smoke each require a separate opt-in process, and D-drive local models never download. |
| 12. Acceptance criteria | Tasks 1–10 and this final gate sequence | Covered; persistence, API, validation, recovery, current citations, and final two-Agent contract evidence each have a focused gate. |
| 13. Existing-stage compatibility and unimplemented boundary | Global Constraints, Tasks 2, 3, 4, 9, 10 | Covered; existing analysis/knowledge public behavior remains compatible and out-of-scope operations have no route, Worker, or CLI path. |

### Consistency and safety results

- Every cross-task callable has one declared producer and matching consumer: Task 2 lease primitives; Task 3/6 `BeforeHttpAttempt = Callable[[int], Awaitable[bool]]`; Task 5 trusted/deterministic models; Task 6 Agent client/validator signatures; Task 7 persistence terminals; Task 8 `TrustedInputLoader`/`run_once`; and Task 9's `functools.partial(load_optimization_trusted_input, session_factory=..., settings=...)` CLI binding. Task 10 uses that exact one-argument callback and obtains PRIMARY identity solely from its final typed records.
- Error boundaries are consistent: Task 9 maps only the declared knowledge subset into Task 8; Task 8 directs pre-revision `zero_hit`/`low_confidence` to their matching pending-manual codes; Task 7 reserves failed transitions for fact/version/authorization/database/checkpoint/replay faults; Task 10 emits only stable local smoke assertions.
- The expected-file table, Task 9 loader, Task 9 cleanup order, Task 8 quality gate, Task 10 smoke file, and every task's explicit allowed-file Git gate agree. Each task ends with cached-name, cached-check, post-commit check, and status verification.
- Ordinary commands clear all opt-ins and the key. PostgreSQL, local-RAG, and real smoke commands run separately with one explicit switch; the smoke key remains only in a task-local process variable and process environment, never output or persisted.
- A prohibited planning-marker scan, Chinese repetition-marker scan, undefined-type/signature search, and code-block review found no unresolved step, missing type, inconsistent error code, unsafe CLI loader wiring, or implementation authorization. No task is authorized to begin until the user explicitly grants execution authority.

## Review Slice Boundary

Tasks 1–10 have passed plan review, but implementation has not received user authorization.
