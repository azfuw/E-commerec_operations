# Manual Review, Approval, and Local Publish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Track every checkbox, use strict RED → minimal GREEN → focused regression, do not dispatch subagents, and stop for review after each commit.

**Goal:** Extend the existing product proposal flow with trusted manual revisions, an independent recoverable manual-review workflow, supervisor/admin approval, one-time local PostgreSQL Listing publication, and append-only safe audit records.

**Architecture:** Keep the FastAPI modular monolith and the existing proposal's optimization workflow as the lifecycle fact source. A manual edit creates one immutable revision plus a new type-isolated `manual_review` workflow. That Worker reuses the existing lease predicates, trusted-input retrieval, deterministic validator, compliance client, and PostgreSQL checkpointer. Approval locks the proposal, submitted revision/review, original optimization run, and Product in one transaction, then updates only permitted Listing fields and records the simulated publish once.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, SQLAlchemy async, Alembic, PostgreSQL 16, SQLite test fixtures, pytest/pytest-asyncio, LangGraph PostgreSQL Checkpointer, and the repository's existing compliance/RAG clients.

**Spec:** [`docs/superpowers/specs/2026-08-31-manual-review-approval-publish-design.md`](../specs/2026-08-31-manual-review-approval-publish-design.md)

## Global Constraints

- Execute only in `C:\Users\15482\.codex\worktrees\fab9\E-commerce_operations` with `D:\E-commerce_operations_env\python.exe`.
- Begin implementation from the current `codex/manual-review-approval-publish` branch. Before Task 1, verify its history contains the approved specification commit `9fd526103cf52147ed219a64e4a65acf12142319` and the latest committed version of this plan; never reset or check out a state that discards the plan. Do not modify `D:\E-commerce_operations`, merge, push, or remove either worktree.
- Use the existing FastAPI app, SQLAlchemy session factory, JWT/RBAC dependencies, `workflow_leases` predicates, `load_optimization_trusted_input`, `validate_optimization_output`, `ProductComplianceAgentClient`, and LangGraph saver. Add no dependency, service, queue, provider factory, shared Worker base class, validator duplicate, or Compose service.
- The API process never reads or calls DeepSeek. Ordinary tests use `httpx.MockTransport`; this phase adds no real DeepSeek smoke and never runs the existing smoke with its opt-in enabled.
- Preserve automatic optimization iteration `0..2`. Agent revisions retain integer `iteration`; manual revisions and their reviews use `iteration=NULL`. `revision_number`, not `iteration`, is the proposal-wide display and parent-chain sequence.
- Manual input may replace only title, selling points, structured description, keywords, and attribute completions. Citations, price suggestions, and SKU suggestions are inherited and revalidated server-side. Publishing never changes price, SKU code/spec, current stock, inventory snapshots, orders, or traffic.
- Every new POST write endpoint introduced by this phase requires a trimmed `Idempotency-Key` of `1..128` characters. Store only SHA-256 hashes. This requirement does not alter unrelated existing POST endpoints.
- All new write services use one replay order: validate the key; fresh-load and validate the active actor, enabled Store, exact `UserStoreScope`, target resource visibility, and ownership chain; canonicalize the request and query the immutable result by action/actor/resource/key hash; return it only when the request hash and the first successful write's immutable terminal chain are exact. A different request hash returns `409/IDEMPOTENCY_REPLAY_CONFLICT`. Only when no prior result exists may the service apply first-write state, pointer, version, active-run, review, and eligibility gates. Replay never reruns those pre-write gates: a manual-revision replay may return after current revision/pointer advancement with an active run, submit may replay in `pending_approval` or after a later legal action, reject may replay in `rejected`, request-changes may replay in `pending_manual` after `submitted_revision_id` is cleared, and approve may replay in `completed` after Product version `base+1`.
- Every security/state/fact query uses fresh database state (`populate_existing=True`), exact actor/store scope, resource ownership, and row locks where the transaction mutates state. No success/replay decision may rely on a stale identity-map object.
- Immutable rows use insert-only behavior. On a unique-key `IntegrityError`, rollback first, open a fresh transaction, repeat key validation plus the complete fresh actor/store/scope/resource-ownership guard, then perform the same prior-result lookup and terminal-chain replay contract above. Only a missing prior result re-enters first-write state/version guards. A still-current Worker owner with a missing or non-exact immutable race result receives the original database error or the specified stable replay conflict; never use upsert/update.
- Checkpoints, logs, responses, business tables, and audit details must not contain Keys, Authorization/Cookie values, full Prompts, raw provider responses, chain-of-thought, exception text, filesystem/model paths, vectors, original idempotency keys, or request/trusted-fact hashes.
- Every task ends with the exact file whitelist Git gate shown in that task. `git diff --cached --name-only` must match it exactly and `git diff --cached --check` must pass before the task commit.

### Unified ordinary offline environment

Apply this block in the same PowerShell process before every ordinary RED, GREEN, or regression command:

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
Remove-Item Env:RUN_TASK10_DEEPSEEK_SMOKE -ErrorAction SilentlyContinue
$env:DEEPSEEK_API_KEY = ""
$env:JWT_SECRET_KEY = "manual-review-plan-local-jwt-secret-at-least-32"
```

PostgreSQL integration runs begin in a new PowerShell process with this block and set only `RUN_POSTGRES_INTEGRATION=1`. They do not set knowledge or DeepSeek opt-ins. No command in this plan loads local embedding/reranker models or calls a model provider.

## Expected File Responsibilities

| File | Responsibility |
|---|---|
| `backend/common.py` | Add only the shared workflow/status, revision-origin, approval-action, and audit closed sets. |
| `backend/models.py` | Map lossless revision extensions, nullable manual review iteration, manual runs, approval actions, publish records, audit events, and proposal pointers with named constraints. |
| `alembic/versions/0005_manual_review_approval_publish.py` | Upgrade `0004` without data loss, backfill agent history, create new tables/indexes/FKs, and refuse a destructive downgrade when stage-five facts exist. |
| `backend/schemas.py` | Strict manual-edit/write request types and safe proposal/approval/audit/publish response types. |
| `backend/manual_reviews.py` | Compose trusted manual output and atomically create one revision/workflow/manual run with idempotent replay. |
| `backend/audit_events.py` | Enforce the exact audit-details allowlist and provide append-only insertion plus scoped bounded reads. |
| `backend/manual_review_runs.py` | Manual-only claim/renew/context, immutable review/call persistence, terminal transitions, and owner/version/replay guards. |
| `backend/manual_review_worker.py` | Independent six-node LangGraph orchestration; no optimization Agent import or call. |
| `scripts/run_manual_review_worker.py` | Import-safe production CLI binding the existing trusted-input loader, saver, settings, and one-shot/loop behavior. |
| `backend/approvals.py` | Submit, reject, request-changes, approve, exact replay, single-transaction local publish, and list/read domain services. |
| `backend/proposals.py` | Extend the existing safe proposal aggregate read only; keep selection behavior unchanged. |
| `backend/routes.py` | Bind the approved write/read endpoints to services and translate only stable domain codes. |
| `tests/test_manual_review_models.py` | ORM constraints, agent-history compatibility, migration helper, and downgrade-refusal tests. |
| `tests/test_manual_review_api.py` | Strict manual input, inherited readonly facts, RBAC, idempotency, atomic creation, and safe response tests. |
| `tests/test_manual_review_worker.py` | Type-isolated lease/persistence/graph/recovery matrix using in-memory saver and MockTransport. |
| `tests/test_approval_api.py` | Submit/approval actions, self-approval, atomic publish, replay/concurrency, rollback, safe views, and audit tests. |
| `tests/test_manual_review_postgres.py` | Explicit opt-in PostgreSQL claim/race/recovery/locking/migration proof with exact-ID cleanup. |
| `tests/test_manual_review_flow.py` | Offline vertical success, request-changes re-entry, reject, immutability, and audit acceptance flows. |

No frontend, real platform adapter, price/SKU mutation service, price role, new Prompt/client, or real-provider test belongs in this phase.

## Fixed Persistence and Error Contracts

The implementation does not choose names or shapes during execution. Task 1 implements this fixed database contract:

- `workflow_runs`: add `manual_review` to `workflow_type`; add `pending_approval` and `rejected` to optimization statuses; allow manual-review rows only `accepted|processing|completed|failed` with both dates `NULL`; retain attempt, quality, and processing-lease checks; add `ix_workflow_runs_type_status_lease_created(workflow_type,status,lease_expires_at,created_at,id)`.
- `proposal_revisions`: add `revision_number Integer NOT NULL`, `origin String(16) NOT NULL`, `created_by String(36) NOT NULL FK users.id`, and `parent_revision_id String(36) NULL FK proposal_revisions.id`; make `iteration` nullable. Add `UNIQUE(proposal_id,revision_number)`, revision number `>=1`, origin closed set, agent/manual iteration pairing, first/non-first parent requirement, and non-self-parent checks. Retain `UNIQUE(proposal_id,iteration)` for non-null automatic iterations.
- `compliance_reviews`: make `iteration` nullable and replace its check with `iteration IS NULL OR iteration BETWEEN 0 AND 2`; retain unique `proposal_revision_id` and `(proposal_id,iteration)`.
- `product_proposals`: add nullable `active_manual_review_run_id FK manual_review_runs.id`, nullable `submitted_revision_id FK proposal_revisions.id`, nullable unique active-run pointer, and `ix_product_proposals_submitted_revision_id`.
- `manual_review_runs`: `id String(36) PK`; required unique `workflow_run_id FK workflow_runs.id`; required `proposal_id FK product_proposals.id`; required unique `proposal_revision_id FK proposal_revisions.id`; required `submitted_by FK users.id`; required 64-character `idempotency_key_hash` and `request_hash`; timezone-aware required `created_at/updated_at`. Add `UNIQUE(proposal_id,submitted_by,idempotency_key_hash)`, both exact hash-length checks, and `ix_manual_review_runs_proposal_created(proposal_id,created_at,id)`.
- `approval_actions`: `id String(36) PK`; required proposal/revision/store/actor FKs; required `actor_role String(16)`, `action String(32)`, both 64-character hashes, and timezone-aware `created_at`; nullable `comment String(500)`. Add `UNIQUE(proposal_id,actor_id,action,idempotency_key_hash)`, action/role/hash checks, the exact comment rule, and proposal/store created-order indexes.
- `publish_records`: `id String(36) PK`; unique required proposal/revision/approval-action FKs; required product/store/approver FKs; unique required 64-character `publish_idempotency_hash`; required before/after JSON snapshots, base/published versions, and timezone-aware `published_at`. Add base `>=1`, published `= base+1`, hash-length checks, and store/product published-order indexes.
- `audit_events`: `id String(36) PK`; required event type/outcome/store/details/created time; nullable actor/role/proposal/revision/workflow/action/publish/request/error fields with their named FKs; event/outcome/role checks; store/proposal/workflow/actor created-order indexes. `details` defaults to `{}`, accepts only the global allowlist, and canonical UTF-8 JSON is at most 4096 bytes.

Constraint and index names are fixed: `uq_proposal_revisions_proposal_revision_number`, `ck_proposal_revisions_revision_number`, `ck_proposal_revisions_origin`, `ck_proposal_revisions_origin_iteration`, `ck_proposal_revisions_parent`, `ck_proposal_revisions_not_self_parent`; `fk_product_proposals_active_manual_review_run_id`, `fk_product_proposals_submitted_revision_id`, `uq_product_proposals_active_manual_review_run_id`, `ix_product_proposals_submitted_revision_id`; `uq_manual_review_runs_workflow_run_id`, `uq_manual_review_runs_proposal_revision_id`, `uq_manual_review_runs_proposal_actor_key`, `ck_manual_review_runs_idempotency_hash_length`, `ck_manual_review_runs_request_hash_length`, `ix_manual_review_runs_proposal_created`; `uq_approval_actions_proposal_actor_action_key`, `ck_approval_actions_action`, `ck_approval_actions_actor_role`, `ck_approval_actions_idempotency_hash_length`, `ck_approval_actions_request_hash_length`, `ck_approval_actions_comment`, `ix_approval_actions_proposal_created`, `ix_approval_actions_store_created`; `uq_publish_records_proposal_id`, `uq_publish_records_proposal_revision_id`, `uq_publish_records_approval_action_id`, `uq_publish_records_publish_idempotency_hash`, `ck_publish_records_base_version`, `ck_publish_records_version_increment`, `ck_publish_records_idempotency_hash_length`, `ix_publish_records_store_published`, `ix_publish_records_product_published`; and `ck_audit_events_event_type`, `ck_audit_events_outcome`, `ck_audit_events_actor_role`, `ix_audit_events_store_created`, `ix_audit_events_proposal_created`, `ix_audit_events_workflow_created`, `ix_audit_events_actor_created`. New FKs use `fk_<table>_<column>` consistently.

`ck_approval_actions_comment` means reject/request-changes rows have a non-null comment whose trimmed length is `1..500`, while submit/approve rows require `comment IS NULL`. Snapshot JSON contains only `title`, `selling_points`, rendered Text `description`, `search_keywords`, `attributes`, and `current_version`.

Stable API codes are exactly `IDEMPOTENCY_KEY_INVALID`, `PROPOSAL_ACTION_FORBIDDEN`, `PROPOSAL_NOT_FOUND`, `IDEMPOTENCY_REPLAY_CONFLICT`, `MANUAL_REVIEW_ACTIVE`, `PROPOSAL_EDIT_FORBIDDEN`, `PROPOSAL_NOT_SUBMITTABLE`, `APPROVAL_STATE_CONFLICT`, `APPROVAL_ACTION_CONFLICT`, `PRODUCT_VERSION_CONFLICT`, `PUBLISH_REPLAY_CONFLICT`, `MANUAL_REVISION_INVALID`, `TRUSTED_EVIDENCE_INVALID`, `APPROVAL_COMMENT_INVALID`, and `PROPOSAL_DATA_INCONSISTENT`, with the HTTP statuses fixed by the approved spec.

Manual Worker fact failures are exactly `MANUAL_REVIEW_CONTEXT_NOT_FOUND`, `MANUAL_REVIEW_CONTEXT_INCONSISTENT`, `MANUAL_REVIEW_AUTHORIZATION_CHANGED`, `PRODUCT_VERSION_CONFLICT`, `MANUAL_REVIEW_FACT_ERROR`, `MANUAL_REVIEW_DATABASE_ERROR`, `MANUAL_REVIEW_CHECKPOINT_ERROR`, and `MANUAL_REVIEW_REPLAY_CONFLICT`. `LEASE_ATTEMPTS_EXHAUSTED` remains the repository's shared safe lease-operations code and is not added to that manual fact-failure closed set. Dependency degradation reuses only the existing DeepSeek/knowledge/compliance safe code closed set named in the spec; arbitrary exceptions never become stored or returned codes.

---

### Task 1: Lossless schema and automatic-optimization compatibility

**Files:**

- Modify: `backend/common.py`
- Modify: `backend/models.py`
- Modify: `backend/optimization_runs.py`
- Create: `alembic/versions/0005_manual_review_approval_publish.py`
- Create: `tests/test_manual_review_models.py`
- Modify: `tests/test_optimization_models.py`
- Modify: `tests/test_optimization_api.py`
- Modify: `tests/test_optimization_worker.py`

**Interfaces:**

- Consumes: migration head `0004`, `WorkflowRun`, `ProductProposal`, `ProposalRevision`, `ComplianceReview`, `AgentCall`, existing UUID/timestamp/JSON mapping conventions, named checks, and existing optimization immutable-write races.
- Produces: migration `0005`, `WorkflowType.MANUAL_REVIEW`, `WorkflowStatus.PENDING_APPROVAL`, `WorkflowStatus.REJECTED`, `ProposalRevisionOrigin`, `ApprovalActionType`, `AuditEventType`, `AuditOutcome`, extended proposal/revision/review mappings, and `ManualReviewRun`, `ApprovalAction`, `PublishRecord`, `AuditEvent`.

```python
# backend/common.py
class ProposalRevisionOrigin(StrEnum):
    AGENT = "agent"
    MANUAL = "manual"


class ApprovalActionType(StrEnum):
    SUBMIT = "submit"
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"
```

`AuditEventType` contains exactly `manual_revision_created`, `manual_review_claimed`, `manual_review_completed`, `manual_review_failed`, `proposal_submitted`, `proposal_approved`, `proposal_rejected`, `proposal_changes_requested`, `simulated_publish_completed`, and `authorization_denied`. `AuditOutcome` contains exactly `success`, `failed`, and `denied`.

- [ ] **Step 1: Write failing model and migration-contract tests**

Create `tests/test_manual_review_models.py` and update direct `ProposalRevision` fixtures in the three existing optimization suites. Assert the new workflow type/status matrix, agent/manual origin-iteration rules, proposal-wide revision uniqueness, parent/self-parent checks, nullable manual compliance iteration, proposal pointers, every new FK/UNIQUE/CHECK/index, hash length, comment rule, publish version increment, audit closed sets, and `details` default. Assert existing agent revisions remain `iteration=0..2`, get `revision_number=iteration+1`, preserve their immutable output/review/calls, and cannot be inserted as `origin=manual` with an integer iteration.

Add focused tests around migration helpers using transaction-local tables: the backfill maps existing iterations `0,1,2` to numbers `1,2,3`, creator from `optimization_run.created_by`, and exact prior parent; a missing/duplicate/cross-proposal parent raises before constraints tighten. The downgrade guard permits a pure `0004` data shape and refuses when any manual revision, `manual_review` workflow, manual run, approval action, publish record, or audit event exists.

- [ ] **Step 2: Run RED for the missing schema**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_models.py tests/test_optimization_models.py tests/test_optimization_api.py tests/test_optimization_worker.py -v
```

Expected: collection or the first focused assertions fail because `0005`, the new enums/models/columns, and nullable manual iteration contract do not exist. No syntax, fixture, network, or environment failure is an acceptable RED.

- [ ] **Step 3: Implement the minimal mappings and ordered migration**

In `backend/models.py`, add the spec's exact columns, lengths, named keys/checks, and indexes. Keep `ProductProposal` free of a duplicated lifecycle status. Use `use_alter=True` for the circular proposal/manual-run pointer and name every FK explicitly.

In `0005`, perform this fixed upgrade order:

1. Drop and recreate `ck_workflow_runs_type_status_dates` for analysis, optimization, and manual-review rows; add `ix_workflow_runs_type_status_lease_created`.
2. Add revision columns nullable, run guarded backfill, validate nulls/duplicates/parent ownership, make required columns non-null, make `iteration` nullable, replace its check, and add revision number/origin/creator/parent constraints.
3. Make `compliance_reviews.iteration` nullable and replace its check without rewriting existing values.
4. Add the two nullable proposal pointer columns without their circular manual-run FK.
5. Create `manual_review_runs`, `approval_actions`, `publish_records`, and `audit_events` plus all exact indexes.
6. Add `fk_product_proposals_active_manual_review_run_id`, its nullable unique key, and the submitted-revision FK/index last.

The downgrade begins with the explicit business-fact guard. When safe, drop circular FKs/indexes first, then new tables in reverse FK order, restore review/revision constraints and non-null agent iteration, remove the added columns/enums/index, and restore the exact `0004` workflow check. It raises instead of deleting stage-five facts.

Update `backend/optimization_runs.py` so the existing automatic insert path sets `revision_number=iteration+1`, `origin=agent`, `created_by=run.created_by`, and the exact prior revision as parent. Its load/replay guards must refresh and verify those values; they must continue to reject malformed automatic chains and must not reinterpret iteration.

- [ ] **Step 4: Run GREEN and compatibility gates**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_models.py tests/test_optimization_models.py tests/test_optimization_api.py tests/test_optimization_worker.py -v
D:\E-commerce_operations_env\python.exe -m alembic upgrade head
D:\E-commerce_operations_env\python.exe -m alembic current
D:\E-commerce_operations_env\python.exe -m alembic check
D:\E-commerce_operations_env\python.exe -m compileall backend tests alembic
git diff --check
```

Expected: focused suites pass; Alembic reports `0005 (head)` with no drift; existing optimization tests retain their prior outcomes; compile and diff checks exit zero.

- [ ] **Step 5: Commit the schema slice**

```powershell
git add -- backend/common.py backend/models.py backend/optimization_runs.py alembic/versions/0005_manual_review_approval_publish.py tests/test_manual_review_models.py tests/test_optimization_models.py tests/test_optimization_api.py tests/test_optimization_worker.py
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add manual review approval schema"
git show --check --oneline HEAD
git status --short
```

Expected cached names are exactly the eight paths above.

---

### Task 2: Strict manual input and trusted readonly composition

**Files:**

- Modify: `backend/schemas.py`
- Create: `backend/manual_reviews.py`
- Create: `tests/test_manual_review_api.py`

**Interfaces:**

- Consumes: `DescriptionSection`, `AttributeCompletion`, `OptimizationChange`, `OptimizationProposalOutput`, `CanonicalRuleCitation`, `TrustedOptimizationInput`, and `validate_optimization_output`.
- Produces:

```python
# backend/schemas.py
class ManualRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent_revision_id: str = Field(min_length=1, max_length=36)
    base_product_version: int = Field(ge=1)
    title: str
    selling_points: list[str]
    description: list[DescriptionSection]
    keywords: list[str]
    attribute_completions: list[AttributeCompletion]
    changes: list[OptimizationChange]


class ManualRevisionAccepted(BaseModel):
    revision_id: str
    manual_review_workflow_run_id: str
    status: Literal["accepted"]
```

`ManualRevisionRequest` enforces `base_product_version >= 1`, every resource ID as a non-empty string of at most 36 characters, and the approved business caps: title `1..60` with Chinese text; selling points `1..5` of `1..80`; description `1..10` with heading `1..40` and body `1..1000`; keywords `1..20` of `1..32`; attribute completions `0..20`; changes `0..4`, with their field closed set unchanged. It reuses the existing nested types and adds one request-level validator for the narrower heading/body limits. It has no client fields for citations, price suggestions, or SKU suggestions.

```python
# backend/manual_reviews.py
@dataclass(frozen=True)
class ManualReviewDomainError(Exception):
    code: str
    status_code: int


def compose_manual_output(
    request: ManualRevisionRequest,
    parent: OptimizationProposalOutput,
) -> OptimizationProposalOutput
```

- [ ] **Step 1: Write failing strict-boundary tests**

Test valid minimum/maximum requests, `base_product_version` values `0` and `1`, empty and 37-character resource IDs, every content length bound, required Chinese title, duplicate targets delegated to the existing validator, and `extra="forbid"`. Prove client `citations`, `price_suggestions`, and `sku_suggestions` produce 422-level Pydantic errors. Assert `compose_manual_output` takes only the five editable groups from the request and copies the parent's output citations, price suggestions, and SKU suggestions byte-equivalently.

Build a trusted input with current SKU/citation facts and prove the composed `OptimizationProposalOutput` passes Pydantic plus the existing deterministic allowlist when facts match. Parameterize unknown fact paths, forged current values, duplicate targets, missing evidence, unknown citations, unknown SKU, stale price/code/spec, and untrusted new attributes as trust-boundary failures. Keep business-review violations such as restricted copy as deterministic results for the Worker rather than claiming API approval.

- [ ] **Step 2: Run RED**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_api.py -v
```

Expected: collection fails only because the new request/accepted schemas and composition function are absent.

- [ ] **Step 3: Implement only the strict request and pure composition**

Reuse existing nested schemas; add field constraints plus one request-level nested-length validator where the manual business cap is narrower. `compose_manual_output` constructs one `OptimizationProposalOutput` and copies the three readonly groups from the parsed parent. Do not access the database, create a workflow, hash a request, call an Agent, or introduce a second output validator in this task.

- [ ] **Step 4: Run GREEN**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_api.py tests/test_optimization_agent.py tests/test_compliance_agent.py -v
D:\E-commerce_operations_env\python.exe -m compileall backend tests
git diff --check
```

Expected: manual schema/composition tests and existing Agent contract tests pass offline; no HTTP request occurs.

- [ ] **Step 5: Commit the input slice**

```powershell
git add -- backend/schemas.py backend/manual_reviews.py tests/test_manual_review_api.py
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: validate trusted manual revisions"
git show --check --oneline HEAD
git status --short
```

Expected cached names are exactly the three paths above.

---

### Task 3: Idempotent manual-revision transaction and route

**Files:**

- Create: `backend/audit_events.py`
- Modify: `backend/manual_reviews.py`
- Modify: `backend/routes.py`
- Modify: `tests/test_manual_review_api.py`

**Interfaces:**

```python
# backend/audit_events.py
AUDIT_DETAIL_KEYS: frozenset[str]

def add_audit_event(
    session: AsyncSession,
    *,
    event_type: AuditEventType,
    outcome: AuditOutcome,
    store_id: str,
    actor_id: str | None = None,
    actor_role: UserRole | None = None,
    proposal_id: str | None = None,
    proposal_revision_id: str | None = None,
    workflow_run_id: str | None = None,
    approval_action_id: str | None = None,
    publish_record_id: str | None = None,
    request_id: str | None = None,
    error_code: str | None = None,
    details: dict[str, object] | None = None,
) -> AuditEvent
```

`AUDIT_DETAIL_KEYS` is exactly `from_status`, `to_status`, `revision_number`, `origin`, `workflow_type`, `quality_status`, `current_step`, `changed_fields`, `review_passed`, `risk_level`, `published_from_version`, and `published_to_version`. Canonical UTF-8 JSON must be at most 4096 bytes; any unknown key or unsafe value is rejected before `session.add`.

```python
# backend/manual_reviews.py
@dataclass(frozen=True)
class ManualRevisionResult:
    revision_id: str
    workflow_run_id: str
    created: bool


async def create_manual_revision(
    session: AsyncSession,
    *,
    actor_id: str,
    proposal_id: str,
    request: ManualRevisionRequest,
    idempotency_key: str | None,
    request_id: str,
) -> ManualRevisionResult
```

- [ ] **Step 1: Extend tests with route-level RED cases**

Using the existing JWT/client/session fixtures, test operator, supervisor, and admin success with exact scope; disabled user/store, missing scope, cross-store, wrong resource chain, and unsupported role behavior; admin must not bypass scope. Test empty/37-character path and body resource IDs, `base_product_version=0`, missing/blank/129-character key, exact replay (`202` then `200`), same-key/different-body conflict, different active request conflict, and a controlled unique-key race. After the first success, advance the current revision and leave a different active manual run; the exact original key/request must still return its immutable original revision/workflow result without pointer or state mutation.

For success, assert one new manual revision (`origin=manual`, `iteration=NULL`, next consecutive number, exact parent/creator and SHA-256 of canonical fresh trusted input), one `WorkflowRun(type=manual_review,status=accepted)`, one `ManualReviewRun`, one safe audit event, current/active pointers, and original optimization run `pending_manual/normal/manual_review_pending`. Assert all six mutations commit together. Inject a flush/commit error at each boundary and prove zero partial revision/workflow/manual-run/pointer/audit writes.

Use same-session stale identity-map regressions for actor role/status, scope, store enabled, parent/current pointer, Product version/SKU facts, and canonical citation current-version ownership. Cross-owned document-version pointers must return `TRUSTED_EVIDENCE_INVALID` with zero writes.

- [ ] **Step 2: Run RED for the missing transaction/route**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_api.py -v
```

Expected: schema/composition tests remain green; route/service tests fail because the endpoint, transaction, audit insert, and replay handling are absent.

- [ ] **Step 3: Implement the locked creation path**

Add `POST /proposals/{id}/manual-revision` with `id: Annotated[str, Path(min_length=1, max_length=36)]` bound to the service's `proposal_id`. Follow the global replay order: validate the key; in stable lock order refresh actor, Store, exact `UserStoreScope`, proposal and ownership chain; compute hashes and look up the existing `ManualReviewRun` plus immutable revision/workflow/audit chain. Exact replay returns that original result even when current revision/pointers have advanced or another active run exists. A hash mismatch returns `IDEMPOTENCY_REPLAY_CONFLICT`. Only when no prior result exists may the service lock/refetch the original optimization run, Product, current SKUs, parent revision/review and canonical citations, require `draft_ready|pending_manual`, reject an active manual pointer, and require request parent/current revision plus `base_product_version >= 1` to match current facts.

Parse the immutable parent output, call `compose_manual_output`, inherit canonical citations from the revision, rebuild `TrustedOptimizationInput` from current database facts, and run `validate_optimization_output`. Reject trust-boundary codes as `422/TRUSTED_EVIDENCE_INVALID`; use `422/MANUAL_REVISION_INVALID` for request/output structure. Do not reject reviewable copy violations that the Worker must record.

Canonicalize action, actor, proposal, parent revision, base version, and request body for `request_hash`; hash the original key separately. Insert the six success facts in one transaction. An `IntegrityError` rollback repeats the global replay order in a fresh transaction: fresh actor/store/scope/resource ownership first, then exact key/request hash and immutable result-chain comparison, without current-pointer/active-run/first-write state gates. Same key with non-exact hash returns `IDEMPOTENCY_REPLAY_CONFLICT`; only a request with no existing result reaches `MANUAL_REVIEW_ACTIVE` or the other first-write guards.

- [ ] **Step 4: Run GREEN and safe-response checks**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_api.py tests/test_optimization_api.py -v
D:\E-commerce_operations_env\python.exe -m compileall backend tests
git diff --check
```

Expected: first create is 202, exact replay is 200, every invalid/race case is atomic, and response/log capture contains no hashes, inherited raw JSON, lease fields, or sensitive values.

- [ ] **Step 5: Commit the creation API**

```powershell
git add -- backend/audit_events.py backend/manual_reviews.py backend/routes.py tests/test_manual_review_api.py
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: create idempotent manual review runs"
git show --check --oneline HEAD
git status --short
```

Expected cached names are exactly the four paths above.

---

### Task 4: Type-isolated manual-review lease and fresh context

**Files:**

- Create: `backend/manual_review_runs.py`
- Create: `tests/test_manual_review_worker.py`
- Modify: `tests/test_workflow_leases.py`

**Interfaces:**

```python
# backend/manual_review_runs.py
@dataclass(frozen=True)
class ManualReviewClaim:
    workflow_run_id: str
    manual_review_run_id: str
    lease_owner: str
    attempt_count: int


@dataclass(frozen=True)
class OwnedManualReviewContext:
    workflow_run_id: str
    manual_review_run_id: str
    proposal_id: str
    proposal_revision_id: str
    revision_number: int
    store_id: str
    product_id: str
    submitted_by: str
    base_product_version: int
    proposal_output: OptimizationProposalOutput
    canonical_citations: tuple[CanonicalRuleCitation, ...]
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
    current_review_id: str | None


@dataclass(frozen=True)
class OwnedManualReviewContextResult:
    disposition: Literal["ready", "failed", "lease_lost"]
    context: OwnedManualReviewContext | None
    error_code: str | None


async def claim_next_manual_review_run(
    session: AsyncSession, *, lease_owner: str, lease_seconds: int
) -> ManualReviewClaim | None

async def renew_manual_review_lease(
    session: AsyncSession, *, workflow_run_id: str, lease_owner: str, lease_seconds: int
) -> bool

async def load_owned_manual_review_context(
    session: AsyncSession, *, workflow_run_id: str, lease_owner: str
) -> OwnedManualReviewContextResult
```

`OwnedManualReviewContextResult.disposition` is exactly `ready|failed|lease_lost`; its failure code is from the spec's manual Worker fact-error closed set.

- [ ] **Step 1: Write failing lease/context tests**

Test `FOR UPDATE SKIP LOCKED` query shape through the established SQLite-compatible claim behavior: only `manual_review` accepted/expired-processing rows are eligible; analysis and optimization rows with identical status/owner are untouched. Claim increments attempts once, sets server-time lease/current step, and writes `manual_review_claimed` audit. For an expired manual-review workflow already at attempt `3`, assert one locked transaction refreshes and locks its `ManualReviewRun`, proposal, original optimization run, and active pointer; sets the manual workflow to `status=failed`, `quality_status=degraded`, `current_step=failed`, `error_code=LEASE_ATTEMPTS_EXHAUSTED` and clears its lease; sets the original optimization run to `status=failed`, `quality_status=degraded`, `current_step=manual_review_failed`, `error_code=LEASE_ATTEMPTS_EXHAUSTED`; clears `active_manual_review_run_id`; and inserts exactly one safe `manual_review_failed` audit carrying that code. Assert zero changes to an otherwise identical analysis/optimization row and no dangling proposal pointer after success or rollback fault injection. Renew and simple updates require type, processing status, owner, and unexpired lease.

Context tests fresh-load active actor and current role, exact scope, enabled Store, manual run/proposal/revision/original run/Product/candidate/SKU ownership, active pointer, `origin=manual`, `iteration=NULL`, parent/number chain, base product version, immutable output, and current canonical citations. Parameterize every broken link and same-session stale cache. Actor/scope/version/fact changes return the exact stable fact code; owner loss returns `lease_lost` and writes nothing.

- [ ] **Step 2: Run RED**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_worker.py tests/test_workflow_leases.py -v
```

Expected: new tests fail because the manual claim/context module is absent while existing shared lease tests stay green.

- [ ] **Step 3: Implement the minimal manual-only persistence boundary**

Build claim and renewal from `workflow_lease_expiry`, `owned_workflow_lease`, and `commit_owned_workflow_update`, always passing `WorkflowType.MANUAL_REVIEW`. Before claiming an eligible row, select each expired attempt-three manual workflow and its manual/proposal/original-run chain with row locks in stable order, validate that the proposal active pointer names that run, then apply the complete two-run failure, pointer clear, lease clear, and one audit insertion in the same transaction. Use the shared safe operations code `LEASE_ATTEMPTS_EXHAUSTED`; do not add it to the manual fact-error closed set. Any lock, chain, audit, or commit error rolls back the whole exhaustion transition. Do not change `backend/workflow_leases.py` and do not add a generic claim service.

The context loader uses locked, `populate_existing=True` queries and returns an immutable dataclass. It must expose the same product/candidate field names consumed by `load_optimization_trusted_input`; pass this structurally compatible context directly later rather than create a second RAG adapter. It never calls RAG or an Agent.

- [ ] **Step 4: Run GREEN**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_worker.py tests/test_workflow_leases.py tests/test_optimization_worker.py -v
D:\E-commerce_operations_env\python.exe -m compileall backend tests
git diff --check
```

Expected: manual claims and atomic exhaustion are type-isolated, no exhausted chain leaves an active pointer, and existing analysis/optimization lease behavior is unchanged.

- [ ] **Step 5: Commit the lease/context slice**

```powershell
git add -- backend/manual_review_runs.py tests/test_manual_review_worker.py tests/test_workflow_leases.py
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add manual review lease context"
git show --check --oneline HEAD
git status --short
```

Expected cached names are exactly the three paths above.

---

### Task 5: Immutable two-track review and owner-guarded terminal state

**Files:**

- Modify: `backend/manual_review_runs.py`
- Modify: `tests/test_manual_review_worker.py`

**Interfaces:**

```python
# backend/manual_review_runs.py
@dataclass(frozen=True)
class ManualReviewPersistenceResult:
    disposition: Literal["created", "replayed", "failed", "lease_lost"]
    review_id: str | None
    passed: bool | None
    quality_status: WorkflowQuality | None
    error_code: str | None


@dataclass(frozen=True)
class ManualReviewTerminalResult:
    disposition: Literal["draft_ready", "pending_manual", "failed", "lease_lost"]
    error_code: str | None


async def persist_manual_compliance_review(
    session: AsyncSession,
    *,
    workflow_run_id: str,
    lease_owner: str,
    trusted: TrustedOptimizationInput,
    deterministic: DeterministicComplianceResult,
    response: ComplianceAgentResponse | None,
    calls: Sequence[ComplianceAgentCallRecord],
    error_code: str | None,
) -> ManualReviewPersistenceResult

async def finalize_manual_review(
    session: AsyncSession, *, workflow_run_id: str, lease_owner: str
) -> ManualReviewTerminalResult

async def fail_manual_review_run(
    session: AsyncSession, *, workflow_run_id: str, lease_owner: str, error_code: str
) -> ManualReviewTerminalResult
```

- [ ] **Step 1: Write failing immutable persistence tests**

Cover a normal pass, normal deterministic/semantic non-pass, and degraded dependency failure. The persisted `ComplianceReview` must reference the manual revision, use `iteration=NULL`, store both tracks, validate every deterministic violation and semantic required change through `ValidatedRequiredChange`, keep exact one-to-one deterministic coverage and exact semantic subset coverage, and store only safe `AgentCall` fields with call-local `iteration=0`.

Test exact replay for review and one/two call rows; non-exact review/call replay; malformed stored JSON; existing review for a different revision/proposal; stale Product/version/citation/actor/scope/active pointer; old owner; and same-session stale identity rows. For controlled `IntegrityError` races, rollback first and use a distinct fresh transaction to repeat all guards before querying the immutable winner. Exact winners replay; owner loss is `lease_lost`; current owner with missing/non-exact winner propagates the original database error or returns `MANUAL_REVIEW_REPLAY_CONFLICT` as specified. Assert no partial calls/review/pointer/terminal/audit remain.

Terminal tests lock manual run, proposal, original optimization run, revision/review, Product, and active pointer. Prove:

- two tracks pass → manual run `completed/normal`, original run `draft_ready/normal/manual_review_passed`;
- valid non-pass → manual run `completed/normal`, original run `pending_manual/normal/manual_review_changes_required`;
- dependency failure review → manual run `completed/degraded`, original run `pending_manual/degraded/manual_review_degraded` with the exact compatible dependency code;
- fatal fact/database/checkpoint/replay code → both runs failed/degraded with `manual_review_failed` on the original run;
- every terminal path clears `active_manual_review_run_id` and adds one exact safe audit event in the same transaction.

- [ ] **Step 2: Run RED**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_worker.py -v
```

Expected: lease/context cases stay green; new immutable review and terminal cases fail because these functions do not exist.

- [ ] **Step 3: Implement the insert-only review and atomic terminal paths**

Use the current optimization persistence as the proven race pattern, but keep manual semantics local to `manual_review_runs.py`; do not refactor the existing optimization module or create a shared repository layer. Complete all comparison queries before adding missing call rows so autoflush cannot escape the controlled flush boundary. Narrow `IntegrityError` handling to review/call/audit insert flushes, never context lookup or terminal commits.

Revalidate typed persisted JSON and current database citations before comparison or write. A failure review has fixed degraded quality, `passed=False`, a compatible safe error code, and no invented semantic content. The terminal transaction derives state from the stored review, not caller-provided booleans.

- [ ] **Step 4: Run GREEN and regression**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_worker.py tests/test_optimization_worker.py -v
D:\E-commerce_operations_env\python.exe -m compileall backend tests
git diff --check
```

Expected: the full race/replay matrix passes, with zero regression in automatic optimization persistence.

- [ ] **Step 5: Commit immutable persistence**

```powershell
git add -- backend/manual_review_runs.py tests/test_manual_review_worker.py
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: persist manual compliance reviews"
git show --check --oneline HEAD
git status --short
```

Expected cached names are exactly the two paths above.

---

### Task 6: Independent six-node recoverable Manual Review Worker

**Files:**

- Create: `backend/manual_review_worker.py`
- Modify: `tests/test_manual_review_worker.py`

**Interfaces:**

```python
# backend/manual_review_worker.py
TrustedInputLoader = Callable[
    [OwnedManualReviewContext, BeforeTrustedInputExternalAttempt],
    Awaitable[TrustedOptimizationInput],
]


class ManualReviewWorkflowState(TypedDict, total=False):
    workflow_run_id: str
    manual_review_run_id: str
    proposal_id: str
    revision_id: str
    review_id: str | None
    review_passed: bool | None
    review_quality_status: Literal["normal", "degraded"] | None
    error_code: str | None
    next_node: Literal[
        "load_or_resume",
        "load_trusted_input",
        "run_deterministic_checks",
        "call_compliance_agent",
        "persist_manual_review",
        "finalize_manual_review",
        "stop",
    ]


def build_manual_review_graph(
    *,
    session: AsyncSession,
    settings: Settings,
    lease_owner: str,
    checkpointer: BaseCheckpointSaver,
    trusted_input_loader: TrustedInputLoader,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CompiledStateGraph


async def run_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    lease_owner: str,
    checkpointer: BaseCheckpointSaver,
    trusted_input_loader: TrustedInputLoader,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str | None
```

- [ ] **Step 1: Write the Worker RED matrix**

Test the exact six nodes and safe checkpoint user-channel equality: only workflow/manual/proposal/revision/review IDs, review passed/quality, safe error code, and next node. No Product text, trusted input, canonical text, model payload, raw response, headers, paths, or hashes may be checkpointed.

Cover fresh pass, deterministic non-pass, semantic non-pass, `zero_hit`, `low_confidence`, each allowed knowledge dependency code, primary timeout/transport/HTTP/auth/provider code, primary schema-invalid plus one repair success/failure, and `CancelledError` from loader/transport/checkpointer. RAG non-normal paths make zero DeepSeek requests. Normal paths make one PRIMARY and only schema-invalid may make one SCHEMA_REPAIR; each uses `max_attempts=1`, call-local iteration zero, and a lease callback before every HTTP attempt. The module must never import or instantiate `ProductOptimizationAgentClient`.

Add recovery cases for checkpoint get/put/put-writes failures, review committed then checkpoint cancellation, expired reclaim with the same `thread_id`, exact existing review/calls/audit, stale/non-exact replay, owner replacement at every node, Product version/auth/citation mutation between nodes, and a database error inside the graph. Live-owner checkpoint/database failure is recorded through a distinct fresh session; owner replacement writes nothing; a second failure in that fresh path propagates unchanged. Revision already exists by design and is never inserted or repeated by this Worker.

- [ ] **Step 2: Run RED**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_worker.py -v
```

Expected: persistence tests pass; graph/runner tests fail only because `backend/manual_review_worker.py` is absent.

- [ ] **Step 3: Implement the bounded graph**

Mirror the established checkpoint boundary and `run_once` control flow without modifying `backend/optimization_worker.py`. Use `workflow_run.id` as LangGraph `thread_id`. Each node fresh-loads or obtains server-derived state from persistence. `load_trusted_input` renews before every real RAG dependency boundary; deterministic validation runs against the persisted manual output; compliance is called only for normal RAG quality.

Call `ProductComplianceAgentClient.request(proposal, deterministic, trusted, call_type=PRIMARY, iteration=0, before_http_attempt=renew, max_attempts=1)`. Only a `DEEPSEEK_SCHEMA_INVALID` primary result may call the same client once with `call_type=SCHEMA_REPAIR`, the same first three arguments, `iteration=0`, the renewal callback, and `max_attempts=1`. Persist all safe call records and one immutable review atomically, then terminalize from that stored review. Propagate `CancelledError`; translate only the fixed database/checkpoint paths through a fresh owner guard.

- [ ] **Step 4: Run GREEN and focused compatibility**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_worker.py tests/test_optimization_worker.py tests/test_compliance_agent.py tests/test_optimization_rag.py -v
D:\E-commerce_operations_env\python.exe -m compileall backend tests
git diff --check
```

Expected: all Worker paths are offline, the manual graph is type-isolated and bounded, and existing optimization/RAG/compliance behavior remains green.

- [ ] **Step 5: Commit the Worker**

```powershell
git add -- backend/manual_review_worker.py tests/test_manual_review_worker.py
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add recoverable manual review worker"
git show --check --oneline HEAD
git status --short
```

Expected cached names are exactly the two paths above.

---

### Task 7: Import-safe Manual Review Worker CLI

**Files:**

- Create: `scripts/run_manual_review_worker.py`
- Modify: `tests/test_manual_review_worker.py`

**Interfaces:**

```python
# scripts/run_manual_review_worker.py
async def main(once: bool) -> None
```

The CLI binds `async_session_factory`, `Settings`, `AsyncPostgresSaver`, `manual_review_worker.run_once`, and `functools.partial(load_optimization_trusted_input, session_factory=async_session_factory, settings=settings)`. It derives the owner as `hostname:pid`, uses the claimed workflow ID as the saver thread ID through `run_once`, supports `--once`, and otherwise waits one second only when no row was claimed.

- [ ] **Step 1: Write failing CLI binding tests**

Assert import has no settings/database/saver/model/network side effect, `--once` invokes exactly one `run_once`, loop mode repeats and only idles on `None`, Windows selects `WindowsSelectorEventLoopPolicy`, and the bound trusted-input loader is the existing production loader. Verify no optimization Agent, new RAG adapter, model path, or provider URL is bound.

- [ ] **Step 2: Run RED**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_worker.py -k "cli or production_binding" -v
```

Expected: selected tests fail because the script is absent.

- [ ] **Step 3: Implement the minimal CLI by following the existing optimization script**

Keep all operational imports inside `main`; use no new command framework, daemon abstraction, configuration, or service entry. Do not add a Compose Worker service.

- [ ] **Step 4: Run GREEN**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_worker.py tests/test_optimization_worker.py -v
D:\E-commerce_operations_env\python.exe -m compileall backend tests scripts
git diff --check
```

Expected: CLI tests pass without connecting to PostgreSQL, Milvus, a model, or the network.

- [ ] **Step 5: Commit the CLI**

```powershell
git add -- scripts/run_manual_review_worker.py tests/test_manual_review_worker.py
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: wire manual review worker CLI"
git show --check --oneline HEAD
git status --short
```

Expected cached names are exactly the two paths above.

---

### Task 8: Submit, reject, and request-changes actions

**Files:**

- Modify: `backend/schemas.py`
- Create: `backend/approvals.py`
- Modify: `backend/routes.py`
- Create: `tests/test_approval_api.py`

**Interfaces:**

```python
# backend/schemas.py
class ProposalActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: str = Field(min_length=1, max_length=36)


class ProposalCommentActionRequest(ProposalActionRequest):
    comment: str


class ApprovalActionView(BaseModel):
    id: str
    proposal_id: str
    proposal_revision_id: str
    actor_id: str
    actor_role: UserRole
    action: ApprovalActionType
    comment: str | None
    created_at: datetime
```

Every action request resource ID is non-empty and at most 36 characters. `comment` is trimmed `1..500` and rejects every Unicode control character. No response exposes idempotency/request hashes.

```python
# backend/approvals.py
@dataclass(frozen=True)
class ApprovalDomainError(Exception):
    code: str
    status_code: int


@dataclass(frozen=True)
class ApprovalActionResult:
    action: ApprovalAction
    created: bool


async def submit_proposal(
    session: AsyncSession,
    *,
    actor_id: str,
    proposal_id: str,
    request: ProposalActionRequest,
    idempotency_key: str | None,
    request_id: str,
) -> ApprovalActionResult

async def reject_proposal(
    session: AsyncSession,
    *,
    actor_id: str,
    proposal_id: str,
    request: ProposalCommentActionRequest,
    idempotency_key: str | None,
    request_id: str,
) -> ApprovalActionResult

async def request_proposal_changes(
    session: AsyncSession,
    *,
    actor_id: str,
    proposal_id: str,
    request: ProposalCommentActionRequest,
    idempotency_key: str | None,
    request_id: str,
) -> ApprovalActionResult
```

- [ ] **Step 1: Write failing action/RBAC/idempotency tests**

Test operator/supervisor/admin submit with exact scope; only supervisor/admin reject/request changes; both approval roles may act on their own submitted revision. Admin still requires exact scope. Test empty/37-character revision IDs and 37-character proposal path IDs as request-boundary failures. A scoped operator's rejected approval attempt appends one `authorization_denied/denied` audit with empty safe details and no action/state mutation; invisible/cross-scope resources remain 404 without exposing identifiers. Disabled actor/store, cross-store, missing scope, stale revision, inconsistent chains, non-pending state, and changed Product/citation facts use stable safe codes.

A first-write submit requires `draft_ready`, current request revision, no active manual run, `normal+passed+error_code NULL` review, current Product base version, and current citations. It writes one append-only submit action/audit, sets `submitted_revision_id`, and changes the original optimization run to `pending_approval/normal` atomically.

Reject/request changes require `pending_approval` and exact submitted revision only for a first write. Reject writes action/audit and terminal `rejected`, keeps `submitted_revision_id` pointing to the rejected revision, and leaves Product plus immutable history unchanged. Request changes writes action/audit, changes the original run to `pending_manual/normal/approval_changes_requested`, and alone clears `submitted_revision_id`; Product remains unchanged. `pending_approval` remains uneditable by the manual-revision route.

For all three endpoints, test missing/invalid key, exact replay, same-key/different request, concurrent unique-key race, conflicting action race, commit failure rollback, and response/log hash absence. Submit exact replay must return the original action while the run is `pending_approval`; reject replay must return it from `rejected` with `submitted_revision_id` preserved; request-changes replay must return it from `pending_manual` after that pointer is cleared. Each replay executes fresh actor/store/scope/ownership checks but not the first-write state/pointer gate and performs zero new action/audit/status writes. Assert rejected/requested comments remain only on the action row and never enter audit details.

- [ ] **Step 2: Run RED**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_approval_api.py tests/test_manual_review_api.py -v
```

Expected: manual creation tests stay green; action tests fail because schemas, services, and routes do not exist.

- [ ] **Step 3: Implement the three locked actions**

Add `POST /proposals/{id}/submit`, `POST /approvals/{id}/reject`, and `POST /approvals/{id}/request-changes`, with every `id` path value constrained to `1..36`. Hash a canonical request containing action, actor, proposal, revision, normalized body, and base version. Apply the global order: key validation; fresh actor, Store, exact scope, proposal and resource ownership; existing action lookup by proposal/actor/action/key; exact request/immutable action comparison and action-specific successful terminal-chain validation; only then, when no action exists, lock/refetch original run, current/submitted revision and review, Product and current facts and apply the first-write state gates. Persist a known-resource role denial in its own safe audit transaction before returning 403; no proposal/action/Product state is changed.

For exact replay, accept the immutable first-success chain in its legal post-state: submit at `pending_approval` or after a later action, reject at `rejected` with the rejected pointer retained, and request changes at `pending_manual` with the pointer cleared. Do not require the pre-write `draft_ready`/`pending_approval` states. After an insert race, rollback and repeat the same replay order in a fresh transaction. A different request hash returns `IDEMPOTENCY_REPLAY_CONFLICT`; a conflicting action winner returns `409/APPROVAL_ACTION_CONFLICT` with no second action/audit/status write.

- [ ] **Step 4: Run GREEN and route regression**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_approval_api.py tests/test_manual_review_api.py tests/test_optimization_api.py -v
D:\E-commerce_operations_env\python.exe -m compileall backend tests
git diff --check
```

Expected: action/RBAC/state/idempotency tests pass and selection/proposal reads remain compatible.

- [ ] **Step 5: Commit the non-publish actions**

```powershell
git add -- backend/schemas.py backend/approvals.py backend/routes.py tests/test_approval_api.py
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add proposal approval actions"
git show --check --oneline HEAD
git status --short
```

Expected cached names are exactly the four paths above.

---

### Task 9: Atomic idempotent local Listing publication

**Files:**

- Modify: `backend/schemas.py`
- Modify: `backend/approvals.py`
- Modify: `backend/routes.py`
- Modify: `tests/test_approval_api.py`

**Interfaces:**

```python
# backend/schemas.py
class PublishRecordView(BaseModel):
    id: str
    proposal_id: str
    proposal_revision_id: str
    product_id: str
    store_id: str
    approved_by: str
    approval_action_id: str
    before_snapshot: dict[str, object]
    after_snapshot: dict[str, object]
    base_product_version: int
    published_product_version: int
    published_at: datetime


# backend/approvals.py
@dataclass(frozen=True)
class ApprovalPublishResult:
    action: ApprovalAction
    publish_record: PublishRecord
    created: bool


async def approve_proposal(
    session: AsyncSession,
    *,
    actor_id: str,
    proposal_id: str,
    request: ProposalActionRequest,
    idempotency_key: str | None,
    request_id: str,
) -> ApprovalPublishResult
```

- [ ] **Step 1: Write failing publish transaction tests**

Test supervisor and admin approval including self-approval; operator 403; all roles require exact live scope. Snapshot Product and every SKU/inventory row before the request. After success assert:

- title, selling points, rendered description, search keywords, and validated attribute completions match the submitted immutable revision;
- description rendering is `heading + "\n" + body` with `"\n\n"` between sections;
- existing attributes are merged only with validated completions;
- `current_version` increases exactly from base to base+1;
- Product code/category/brand/enabled and all price/SKU code/spec/stock/inventory/order/traffic facts are unchanged;
- exactly one approve action, publish record, `proposal_approved` audit, and `simulated_publish_completed` audit exist;
- original run is `completed/normal/simulated_published` with no error.

Test exact same-key replay from the successful `completed/simulated_published` terminal chain with Product already at `base+1`; it must return the original action/publish record after fresh actor/store/scope/resource checks and must not apply the pre-approve `pending_approval` or base-version equality gates. Also test same-key/different request, completed proposal replay under a new key, concurrent approve/approve, approve versus reject/request changes, stale Product version before any publish exists, stale submitted/current pointer, non-passed/degraded review, changed citation/SKU facts, publish unique races, and existing non-exact publish corruption. Inject failures after Product mutation, action flush, publish flush, each audit insert, and final run update; every fault must rollback Product/version/action/publish/audit/status together.

- [ ] **Step 2: Run RED**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_approval_api.py -k "approve or publish or rollback" -v
```

Expected: selected tests fail because approve publication is not implemented; submit/reject/request-changes tests remain green.

- [ ] **Step 3: Implement the one locked transaction**

Add `POST /approvals/{proposal_id}/approve` with `proposal_id: Annotated[str, Path(min_length=1, max_length=36)]`. Validate the key, then fresh-load actor, Store, exact scope, proposal, and resource ownership. Compute hashes and query the existing approve action/publish record for this key before first-write state checks. An exact result must validate the immutable revision/action/publish/snapshot chain, `completed/simulated_published`, and Product at the recorded `base+1`, then return it without mutation. If this key has no action, query the unique publish record for the same proposal/requested revision; an exact completed chain returns that record under a new key without adding an action. Only when neither prior result exists does the service lock in stable order the original run, submitted/current revision, unique review, Product, and current SKUs and require `pending_approval`, exact revision pointers, no active manual run, `normal+passed+no error`, current base version, and fresh citations/SKU facts.

Create the allowlisted before snapshot, render description deterministically, update only the five Listing groups, merge trusted attributes, increment version once, and create the after snapshot. Insert approve action, unique publish record with a server-derived domain/proposal/revision SHA-256 idempotency hash, and the two safe audits; update the original run last; commit once.

Same-key replay and post-`IntegrityError` recovery repeat the global fresh authorization/ownership-first lookup order, never the first-write `pending_approval`/base-version gate. A completed proposal with the same published revision returns the one existing publish record without inserting a new action/audit or incrementing Product. A different request hash returns `IDEMPOTENCY_REPLAY_CONFLICT`; any non-exact immutable publish chain returns `PUBLISH_REPLAY_CONFLICT`; a conflicting action winner returns `APPROVAL_ACTION_CONFLICT`; a stale Product before any prior publish exists returns `PRODUCT_VERSION_CONFLICT`.

- [ ] **Step 4: Run GREEN and immutable-fact regression**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_approval_api.py tests/test_manual_review_api.py tests/test_optimization_api.py -v
D:\E-commerce_operations_env\python.exe -m compileall backend tests
git diff --check
```

Expected: every success/replay/race/fault-injection case passes and price/SKU/stock/inventory invariants are explicit.

- [ ] **Step 5: Commit local publication**

```powershell
git add -- backend/schemas.py backend/approvals.py backend/routes.py tests/test_approval_api.py
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: publish approved listings atomically"
git show --check --oneline HEAD
git status --short
```

Expected cached names are exactly the four paths above.

---

### Task 10: Safe approval, audit, and proposal read views

**Files:**

- Modify: `backend/schemas.py`
- Modify: `backend/approvals.py`
- Modify: `backend/audit_events.py`
- Modify: `backend/proposals.py`
- Modify: `backend/routes.py`
- Modify: `tests/test_approval_api.py`
- Modify: `tests/test_optimization_api.py`

**Interfaces:**

```python
# backend/schemas.py
class ApprovalListItem(BaseModel):
    proposal_id: str
    proposal_revision_id: str
    revision_number: int
    store_id: str
    product_id: str
    submitted_by: str
    status: Literal["pending_approval"]
    submitted_at: datetime

class ApprovalListView(BaseModel):
    items: list[ApprovalListItem]
    page: int
    page_size: int
    total: int

class AuditEventView(BaseModel):
    id: str
    event_type: AuditEventType
    outcome: AuditOutcome
    actor_id: str | None
    actor_role: UserRole | None
    store_id: str
    proposal_id: str | None
    proposal_revision_id: str | None
    workflow_run_id: str | None
    approval_action_id: str | None
    publish_record_id: str | None
    request_id: str | None
    error_code: str | None
    details: dict[str, object]
    created_at: datetime

class AuditEventListView(BaseModel):
    items: list[AuditEventView]
    page: int
    page_size: int
    total: int

class ManualReviewSummary(BaseModel):
    manual_review_run_id: str
    workflow_run_id: str
    proposal_revision_id: str
    status: WorkflowStatus
    quality_status: WorkflowQuality
    current_step: str | None
    error_code: str | None
```

`ProposalRevisionView.iteration` and `ComplianceReviewView.iteration` become `int | None`; revision views add `revision_number`, `origin`, `created_by`, and `parent_revision_id`. `ProposalDetailView` adds only safe active manual-review, submitted revision, latest action, and publish summaries.

```python
# backend/approvals.py
@dataclass(frozen=True)
class ApprovalReadResult:
    proposal_id: str
    proposal_revision_id: str
    revision_number: int
    store_id: str
    product_id: str
    submitted_by: str
    submitted_at: datetime


async def list_pending_approvals(
    session: AsyncSession, *, actor_id: str, page: int, page_size: int
) -> tuple[list[ApprovalReadResult], int]

# backend/audit_events.py
async def list_audit_events(
    session: AsyncSession,
    *,
    actor_id: str,
    page: int,
    page_size: int,
    store_id: str | None,
    proposal_id: str | None,
    action: ApprovalActionType | None,
) -> tuple[list[AuditEvent], int]
```

- [ ] **Step 1: Write failing read/RBAC/security tests**

Test `GET /approvals` for supervisor/admin only, exact scope intersection, pending-only rows, `page>=1`, `page_size=1..100`, stable `created_at,id` ordering, and `total` equal to the scoped unpaginated match count. Test `GET /audit-events` with bounded pagination and optional `store_id`, `proposal_id`, and `action: ApprovalActionType` filters; the action filter joins `AuditEvent.approval_action_id` to `ApprovalAction.id` and filters `ApprovalAction.action`, rather than filtering `AuditEvent.event_type`. Its `total` is also the scoped unpaginated count after all filters. Empty/37-character resource filters fail validation, and no event outside any caller scope may appear. Operator receives 403 for both lists.

Extend `GET /proposals/{id}` tests for agent/manual revision metadata, active manual run safe state, submitted revision, latest action, and publish record. Every read refreshes active actor/store/scope and verifies all cross-table ownership links. Parameterize inconsistent pointers/FKs and stale identity map.

Recursively inspect all three response families and captured logs. Reject idempotency/request/trusted fact/publish hashes, lease owner/expiry, checkpoint data, full input/output storage JSON, Prompt/provider fields, paths, vectors, Authorization/Key names, raw response, and audit-unsafe keys.

- [ ] **Step 2: Run RED**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_approval_api.py tests/test_optimization_api.py -k "list or audit or proposal_read or safe" -v
```

Expected: existing proposal tests pass where unchanged; new endpoints/fields and manual-null iteration assertions fail.

- [ ] **Step 3: Implement scoped bounded reads**

Add `GET /approvals` and `GET /audit-events`; extend only the existing proposal aggregate read. Constrain optional `store_id`/`proposal_id` query resource IDs to `1..36`, parse optional `action` as `ApprovalActionType`, and implement it through the approval-action relationship. Use database joins and exact scopes rather than post-filtering unauthorized rows. Consume both values returned by each service tuple: map rows to `items` and the integer to response `total`. Return stable typed fields, not raw ORM `__dict__`, workflow input/output, revision JSON containers beyond the established safe proposal output, or hashes.

Keep listing logic in `approvals.py`/`audit_events.py`; routes validate query/resource bounds and action enum, call services, map items plus total, and translate `ApprovalDomainError`/`ProposalDomainError`.

- [ ] **Step 4: Run GREEN**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_approval_api.py tests/test_optimization_api.py tests/test_manual_review_api.py -v
D:\E-commerce_operations_env\python.exe -m compileall backend tests
git diff --check
```

Expected: scoped pagination/order/safe-view tests pass without changing selection or prior proposal response semantics.

- [ ] **Step 5: Commit read views**

```powershell
git add -- backend/schemas.py backend/approvals.py backend/audit_events.py backend/proposals.py backend/routes.py tests/test_approval_api.py tests/test_optimization_api.py
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: expose safe approval and audit views"
git show --check --oneline HEAD
git status --short
```

Expected cached names are exactly the seven paths above.

---

### Task 11: Explicit PostgreSQL concurrency and recovery proof

**Files:**

- Create: `tests/test_manual_review_postgres.py`

**Interfaces:**

- Consumes: real configured PostgreSQL, `AsyncPostgresSaver`, manual claim/persistence/Worker/services, and existing explicit integration-test conventions.
- Produces: one module-level opt-in suite marked with `pytest.mark.asyncio(loop_scope="module")` and `pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="explicit PostgreSQL integration opt-in required")`; on Windows it selects `WindowsSelectorEventLoopPolicy` during module import.

- [ ] **Step 1: Write the default-skipped substantive suite**

Record exact IDs for users, `(user_id,store_id)` scopes, stores, products, SKUs, inventory rows, analysis/optimization/manual workflows, candidates, proposals, revisions, reviews, calls, documents/versions/chunks, actions, publishes, audits, and checkpoint thread IDs. Before every claim, query for eligible manual-review rows excluding the recorded workflow IDs; skip rather than claim an external row. Cleanup only recorded checkpoint `thread_id` values and delete ORM rows in reverse FK order; never use a broad prefix delete.

The body must prove real `FOR UPDATE SKIP LOCKED` claim exclusion, attempt cap, expired reclaim, same thread resume after review commit/checkpoint write cancellation, exact immutable review/call/audit replay, non-exact race behavior, checkpoint get/put failure with owner replacement, old-owner zero writes at context/review/final/fail boundaries, and only manual workflow claimability. For an owned expired attempt-three manual workflow, assert the single locked exhaustion transaction sets both manual and original optimization workflows to failed/degraded with shared `LEASE_ATTEMPTS_EXHAUSTED`, clears the exact active pointer and lease, and inserts one `manual_review_failed` audit; inject a database failure and assert the complete chain remains unchanged, while owned analysis/optimization control rows are untouched.

Add real transaction races for one active manual run per proposal, exact/same-key-different-body manual revision creation, submit, concurrent approve/approve, approve versus reject/request changes, Product version conflict, and fault-injected approve rollback. Explicitly replay manual creation after pointer advancement/another active run, submit from `pending_approval`, reject from `rejected` with submitted pointer retained, request changes from `pending_manual` with it cleared, and approve from `completed` with Product at `base+1`; each must fresh-reguard actor/store/scope/ownership, return its original immutable result, and write nothing. Assert one version increment/publish and invariant price/SKU/stock/inventory facts.

For the migration proof, use a transaction-scoped unique PostgreSQL schema and the migration's narrow backfill/guard helpers: seed the relevant `0004` row shape, assert number/origin/creator/parent backfill, assert malformed history aborts, and assert downgrade refusal once each stage-five fact class is present. Drop only that recorded schema in `finally`; do not alter the project schema's migration version during pytest.

- [ ] **Step 2: Confirm ordinary collection skips without side effects**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_postgres.py -v
```

Expected: every PostgreSQL integration test is collected and skipped; no database connection occurs.

- [ ] **Step 3: Run the explicitly authorized PostgreSQL proof**

In a fresh PowerShell process, apply the unified block, set only `$env:RUN_POSTGRES_INTEGRATION="1"`, and run:

```powershell
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_postgres.py -v
```

Expected: all tests pass, cleanup assertions report zero rows for every recorded ID, and unrelated rows/checkpoints are unchanged. Remove `RUN_POSTGRES_INTEGRATION` in `finally`. Any connection, ownership, cleanup, or test failure stops the task; do not widen cleanup or enable another opt-in.

- [ ] **Step 4: Run focused offline regression after the integration process**

```powershell
# In a new process, reapply the unified ordinary offline environment.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_models.py tests/test_manual_review_api.py tests/test_manual_review_worker.py tests/test_approval_api.py tests/test_manual_review_postgres.py -v
git diff --check
```

Expected: ordinary suites pass and the PostgreSQL module skips by default.

- [ ] **Step 5: Commit the integration proof**

```powershell
git add -- tests/test_manual_review_postgres.py
git diff --cached --name-only
git diff --cached --check
git commit -m "test: prove manual review PostgreSQL recovery"
git show --check --oneline HEAD
git status --short
```

Expected cached names contain only `tests/test_manual_review_postgres.py`.

---

### Task 12: Offline vertical acceptance and final gates

**Files:**

- Create: `tests/test_manual_review_flow.py`

**Interfaces:**

- Consumes: public FastAPI endpoints, `manual_review_worker.run_once`, SQLite session/client fixtures, in-memory checkpoint saver, fake trusted loader, and `httpx.MockTransport`.
- Produces: three vertical offline acceptance tests with no direct service mutation outside fixture seeding.

- [ ] **Step 1: Confirm the vertical suite is absent**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_flow.py -v
```

Expected: pytest exits non-zero only because `tests/test_manual_review_flow.py` does not exist. No collection from another module, environment, or dependency failure is acceptable.

- [ ] **Step 2: Write the three vertical flows**

1. Start from `pending_manual`, POST a manual revision, run the independent Worker through deterministic+semantic pass, submit, self-approve as supervisor and separately as admin, and assert one publish record, exact allowed Listing mapping, version +1, unchanged price/SKU/stock/inventory, and safe ordered audit history.
2. Start from `draft_ready`, submit, request changes, assert no Product change, create a second manual revision with a new workflow/thread ID, pass review, resubmit, approve, and assert the first workflow/history remains immutable.
3. Submit then reject, assert terminal `rejected`, no Product/version change, no publish record, immutable suggestions retained, and one safe rejection action/audit.

Each new stage-five POST write endpoint also exercises exact idempotent replay and same-key/different-request conflict, including the approved post-success states rather than pre-write state gates. Each flow proves the manual Worker makes zero optimization Agent calls, compliance PRIMARY once, SCHEMA_REPAIR only in the dedicated schema-invalid subcase, and no response/checkpoint/audit leakage.

- [ ] **Step 3: Run the integrated public flow GREEN**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_flow.py -v
```

Expected: all three public flows pass. A failure stops Task 12 for review; do not change production files under this task's one-file whitelist or weaken a vertical assertion.

- [ ] **Step 4: Run the complete final verification matrix**

```powershell
# Apply the unified ordinary offline environment first.
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_models.py tests/test_manual_review_api.py tests/test_manual_review_worker.py tests/test_approval_api.py tests/test_manual_review_flow.py -v
D:\E-commerce_operations_env\python.exe -m pytest -v --junitxml=D:\E-commerce_operations_runtime\tmp\manual-review-final-offline.xml
D:\E-commerce_operations_env\python.exe -m compileall backend tests scripts alembic
D:\E-commerce_operations_env\python.exe -m alembic current
D:\E-commerce_operations_env\python.exe -m alembic check
docker compose config --quiet
git diff --check
git status --short
git diff --cached --name-only
```

Expected: focused and full ordinary suites pass with PostgreSQL/RAG/DeepSeek opt-ins absent; the JUnit suite has zero failures/errors; compileall exits zero; Alembic reports `0005 (head)` and no drift; Compose and diff checks exit zero; the cached list is empty before staging.

Do not run a real DeepSeek smoke. The existing compliance Prompt/client did not change; MockTransport and Worker orchestration tests are the complete model-boundary acceptance for this phase.

- [ ] **Step 5: Commit the vertical acceptance**

```powershell
git add -- tests/test_manual_review_flow.py
git diff --cached --name-only
git diff --cached --check
git commit -m "test: verify manual review publish flow"
git show --check --stat --oneline HEAD
git status --short
```

Expected cached names contain only `tests/test_manual_review_flow.py`.

---

## Final Acceptance Checklist

- [ ] Existing analysis, knowledge, selection, optimization Agent/Worker, compliance client, RAG, and proposal reads remain compatible.
- [ ] Migration preserves every automatic revision/review/call, assigns exact revision metadata, and refuses business-fact loss on downgrade.
- [ ] Manual edit accepts only five editable groups, inherits and rechecks all readonly suggestions/citations, and creates one active manual workflow atomically.
- [ ] The independent Worker claims only `manual_review`, uses six nodes and one thread per workflow, calls no optimization Agent, and safely resumes after lease/checkpoint cancellation.
- [ ] Deterministic and semantic tracks both pass before `draft_ready`; valid non-pass/degrade stays `pending_manual`; fatal facts fail atomically.
- [ ] Operator can edit/submit but cannot approve; supervisor/admin can edit, submit, and self-approve/reject/request changes with exact store scope.
- [ ] Every new POST write endpoint in this phase has exact replay and same-key/different-request protection; no key/hash appears in a response, log, checkpoint, or audit detail.
- [ ] Approval publishes once in one transaction, Product version increments once, and price/SKU/stock/inventory remain unchanged.
- [ ] Reject is terminal without Product mutation; request changes returns to a fresh independent manual-review flow.
- [ ] Approval, publish, and audit rows are append-only; audit details use the exact safe allowlist.
- [ ] Offline full suite, compileall, Alembic current/check, Compose, diff/Git gates, and the explicitly authorized PostgreSQL proof pass with exact cleanup.
- [ ] Delivery claims only local PostgreSQL simulated publication; frontend, real platform publication, price/SKU application, price roles, and new real-provider coverage remain unimplemented.
