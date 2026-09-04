# Phase 9 Knowledge Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the sole Phase 9 data foundation and an authorized knowledge-management console without changing knowledge retrieval semantics.

**Architecture:** This plan owns revision `0006_phase9_management_console` and the shared evaluation/audit persistence foundations. Existing knowledge content, runs, Worker, and search implementation stay authoritative. Routes add only a safe version-history projection and store-context authorization; `KnowledgePage.vue` uses the existing `api.ts` request boundary.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, PostgreSQL, pytest, Vue 3, TypeScript, Element Plus, Vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-04-phase-9-management-console-design.md`

## Global Constraints

- The current 电商项目开发窗口 executes this plan sequentially with `superpowers:executing-plans`; do not dispatch subagents.
- Create exactly one Phase 9 migration, `0006_phase9_management_console.py` after `0005`; Plans 2–4 consume it and create no migration.
- Add no dependency, microservice, standalone service, queue, cache, model/provider call, network operation, store partition of knowledge, or knowledge download/delete/re-enable endpoint.
- Before every ordinary offline gate, execute `Remove-Item Env:RUN_POSTGRES_INTEGRATION -ErrorAction SilentlyContinue; Remove-Item Env:RUN_KNOWLEDGE_INTEGRATION -ErrorAction SilentlyContinue; Remove-Item Env:RUN_DEEPSEEK_SMOKE -ErrorAction SilentlyContinue; Remove-Item Env:RUN_TASK10_DEEPSEEK_SMOKE -ErrorAction SilentlyContinue; Remove-Item Env:RUN_PHASE9_EVALUATION_WRITE -ErrorAction SilentlyContinue; Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue`. PostgreSQL proof alone sets `RUN_POSTGRES_INTEGRATION=1`; immediately after every PostgreSQL command execute `Remove-Item Env:RUN_POSTGRES_INTEGRATION -ErrorAction SilentlyContinue`. It remains offline.
- Every server request reloads active user, enabled store, current scope, and resource ownership. Admin is global without a `UserStoreScope` row.
- New API, CLI, audit, and page views never expose secret, hash, prompt, provider data, exception, document body, filename, storage path, vector, workflow input/output, checkpoint, or lease.
- Do not read, modify, or stage `docs/business-and-technical-guide.md` or `docs/interview-q-and-a.md`.
- Do not modify or clean C-drive personal files outside the current Codex worktree; put cache and temporary files on D drive first.
- Phase 10 retains real-model evaluation, fault injection, real-service browser E2E, reports, and final document work.

---

## Locked File Structure and Responsibilities

| Path | Responsibility |
|---|---|
| `backend/common.py` | Closed `EvaluationAgentType`, `EvaluationRunStatus`, and Phase 9 audit event values. |
| `backend/models.py` | `EvaluationCase`, `EvaluationRun`, `EvaluationResult`; resource-compatible `AuditEvent`. |
| `alembic/versions/0006_phase9_management_console.py` | New tables, constraints, indexes, audit compatibility, guarded downgrade. |
| `backend/audit_events.py` | Shared Phase 9 safe detail validation and resource-aware append helper. |
| `backend/knowledge_runs.py` | Knowledge success facts and audit writes in the existing database transaction. |
| `backend/schemas.py`, `backend/routes.py` | Safe history DTO/route and required `store_id` search request. |
| `frontend/src/api.ts`, `frontend/src/types.ts`, `frontend/src/router.ts`, `frontend/src/capabilities.ts`, `frontend/src/components/AppShell.vue`, `frontend/src/pages/KnowledgePage.vue` | Typed knowledge console and desktop/tablet capability. |
| `tests/test_phase9_models.py`, `tests/test_phase9_postgres.py`, `tests/test_knowledge_history.py`, `tests/test_knowledge_search_scope.py` | Schema, PostgreSQL, history, and authorization tests. |

### Task 1: Implement the sole `0006` persistence foundation

**Files:**
- Modify: `backend/common.py`, `backend/models.py`, `backend/audit_events.py`
- Create: `alembic/versions/0006_phase9_management_console.py`, `tests/test_phase9_models.py`, `tests/test_phase9_postgres.py`

**Interfaces:**
- Consumes: `AuditEvent`, `Store`, `User`, `UserStoreScope`, and Alembic revision `0005`.
- Produces: `EvaluationAgentType`, `EvaluationRunStatus`, `EvaluationCase`, `EvaluationRun`, `EvaluationResult`, and `add_audit_event(session, *, event_type, outcome, store_id: str | None, resource_type: str | None, resource_id: str | None, details: dict[str, object] | None) -> AuditEvent`.

- [ ] **Step 1: Write the failing model and PostgreSQL migration tests**

Extend `tests/test_phase9_models.py` with direct ORM construction and no test helper:

~~~python
def test_evaluation_result_declares_exact_identity() -> None:
    assert 'uq_evaluation_results_run_case_agent' in {
        constraint.name for constraint in EvaluationResult.__table__.constraints
    }

def test_audit_resource_pair_rejects_one_side() -> None:
    assert any(
        constraint.name == 'ck_audit_events_resource_pair'
        for constraint in AuditEvent.__table__.constraints
    )
~~~

In `tests/test_phase9_postgres.py`, use the existing `backend.database.async_session_factory` directly in the test:

~~~python
async with async_session_factory() as session:
    session.add(AuditEvent(
        id='audit-resource-pair', event_type=AuditEventType.ADMIN_USER_UPDATED,
        outcome=AuditOutcome.SUCCESS, store_id=None, actor_id=None, actor_role=None,
        resource_type='user', resource_id=None, details={},
    ))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()
~~~

Test `0005 -> 0006` upgrade; the three tables; both foreign keys; all checks; unique `(evaluation_run_id, evaluation_case_id, agent_type)`; required indexes; old audit rows readable; and downgrade success with zero Phase 9 facts versus `RuntimeError('cannot downgrade phase-nine management console with facts')` after inserting one evaluation or new audit event.

- [ ] **Step 2: Run RED**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_models.py -v`

Expected: FAIL because the new enums/models/checks are absent.

Run: `$env:RUN_POSTGRES_INTEGRATION='1'; D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_postgres.py -v`

Expected: FAIL because revision `0006` and its upgrade/downgrade contract are absent; clear `RUN_POSTGRES_INTEGRATION` after the command.

- [ ] **Step 3: Implement the lossless schema and safe audit extension**

~~~python
class EvaluationResult(Base):
    __tablename__ = 'evaluation_results'
    __table_args__ = (
        UniqueConstraint('evaluation_run_id', 'evaluation_case_id', 'agent_type',
                         name='uq_evaluation_results_run_case_agent'),
        CheckConstraint('latency_ms >= 0', name='ck_evaluation_results_latency_ms'),
    )
~~~

Create immutable `evaluation_cases`, `evaluation_runs`, and `evaluation_results` with every spec field, closed checks, non-null JSON, matching-type service invariant, and indexes `(agent_type, created_at, id)`, `(store_id, created_at, id)`, `(status, created_at, id)`. Widen `AuditEvent.store_id` to nullable; add paired `resource_type`/`resource_id`; add Phase 9 event values and event/actor/outcome indexes. Validate only approved Phase 9 audit keys and canonical UTF-8 JSON no larger than 4096 bytes before `session.add()`.

In `downgrade()`, query all three evaluation tables and the seven Phase 9 audit values before dropping any fact-bearing schema; refuse if any exists. Do not update existing workflow, proposal, approval, AgentCall, or knowledge row.

- [ ] **Step 4: Run GREEN and compatibility gates**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_models.py -v`

Expected: PASS.

Run: `$env:RUN_POSTGRES_INTEGRATION='1'; D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_postgres.py -v`

Expected: PASS against local PostgreSQL with upgrade/downgrade, constraints, indexes, and old-row compatibility; clear the opt-in.

- [ ] **Step 5: Commit the foundation**

~~~bash
git add backend/common.py backend/models.py backend/audit_events.py alembic/versions/0006_phase9_management_console.py tests/test_phase9_models.py tests/test_phase9_postgres.py
git diff --cached --check
git commit -m "feat: add phase nine management schema"
~~~

### Task 2: Persist safe knowledge events and serve version history

**Files:**
- Modify: `backend/knowledge_runs.py`, `backend/routes.py`, `backend/schemas.py`, `tests/test_knowledge_api.py`
- Create: `tests/test_knowledge_history.py`

**Interfaces:**
- Consumes: `add_audit_event`, `AuditEventType.KNOWLEDGE_DOCUMENT_CREATED`, `AuditEventType.KNOWLEDGE_VERSION_CREATED`, and `AuditEventType.KNOWLEDGE_DOCUMENT_DISABLED`.
- Produces: `KnowledgeVersionHistoryItem`, `KnowledgeVersionHistoryView`, and `GET /knowledge/documents/{document_id}/versions?page&page_size`.

- [ ] **Step 1: Write failing history and same-transaction tests**

~~~python
async def test_admin_history_hides_source_storage(client, admin_headers) -> None:
    response = await client.get('/knowledge/documents/document-1/versions', headers=admin_headers)
    assert response.status_code == 200
    assert set(response.json()['items'][0]).isdisjoint(
        {'original_filename', 'storage_path', 'canonical_text', 'chunks'}
    )

async def test_version_create_commits_one_database_audit(session) -> None:
    rows = list(await session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.KNOWLEDGE_VERSION_CREATED)
    ))
    assert len(rows) == 1
~~~

Use the existing knowledge API fixture setup to cover admin success, operator/supervisor 403, unknown document 404, 422 page bounds, safe status/error fields, and an injected database flush error that leaves no success audit or partial version.

- [ ] **Step 2: Run RED**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_knowledge_api.py tests/test_knowledge_history.py -v`

Expected: FAIL because the history projection and database audit events are absent.

- [ ] **Step 3: Add the safe route and transactional audit writes**

~~~python
class KnowledgeVersionHistoryItem(BaseModel):
    id: str
    version_number: int
    status: KnowledgeVersionStatus
    parser_version: str | None
    chunker_version: str | None
    embedding_version: str | None
    error_code: str | None
    created_at: datetime
~~~

Before each existing successful document-create, version-create, or disable commit, append exactly one matching safe `AuditEvent` in that same session transaction. The admin-only history query orders by `version_number DESC, id DESC` and returns document ID/name/category/enabled plus only the DTO fields. Retain upload idempotency, cleanup, and Worker behavior; add no content or file action.

- [ ] **Step 4: Run GREEN and knowledge regressions**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_knowledge_api.py tests/test_knowledge_history.py tests/test_knowledge_runs.py tests/test_knowledge_worker.py -v`

Expected: PASS without a model, Milvus, provider, or network call.

- [ ] **Step 5: Commit safe history**

~~~bash
git add backend/knowledge_runs.py backend/routes.py backend/schemas.py tests/test_knowledge_api.py tests/test_knowledge_history.py
git diff --cached --check
git commit -m "feat: expose safe knowledge version history"
~~~

### Task 3: Require a real-time store context before search

**Files:**
- Modify: `backend/schemas.py`, `backend/routes.py`, `tests/test_knowledge_api.py`, `frontend/src/api.ts`, `frontend/src/types.ts`
- Create: `tests/test_knowledge_search_scope.py`

**Interfaces:**
- Consumes: `require_store_access(store_id, user, session)` and `search_active_knowledge`.
- Produces: `KnowledgeSearchRequest.store_id: str` bounded to 1..36 and `searchKnowledge(query: KnowledgeSearchQuery, signal?: AbortSignal): Promise<KnowledgeSearchResult>`.

- [ ] **Step 1: Write failing authorization-first tests**

~~~python
async def test_store_rejection_precedes_dependency_load(client, scoped_operator, loader) -> None:
    response = await client.post('/knowledge/search', json={'store_id': 'other-store', 'query': '规则'})
    assert response.status_code == 403
    assert loader.calls == 0
~~~

Cover absent/malformed store ID, active scoped operator/supervisor/admin, disabled/unknown store 404, missing scope 403, and unchanged safe zero_hit/low_confidence/dependency status envelopes.

- [ ] **Step 2: Run RED**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_knowledge_search_scope.py tests/test_knowledge_api.py -v`

Expected: FAIL because the request does not require `store_id` and the loader can start first.

- [ ] **Step 3: Add the minimal authorization-first order**

~~~python
await require_store_access(request.store_id, user, session)
outcome = await search_active_knowledge(
    session, query=request.query, categories=request.categories, top_k=request.top_k,
    retrieval_path='hybrid_rerank', load_dependencies=load_dependencies,
)
~~~

Use store ID only for current authorization and safe audit context. Keep documents global and retain the existing scoring, calibration, RAG quality, and loader interfaces.

- [ ] **Step 4: Run GREEN and search regression**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_knowledge_search_scope.py tests/test_knowledge_search.py tests/test_knowledge_api.py -v`

Expected: PASS offline.

- [ ] **Step 5: Commit search context**

~~~bash
git add backend/schemas.py backend/routes.py tests/test_knowledge_api.py tests/test_knowledge_search_scope.py frontend/src/api.ts frontend/src/types.ts
git diff --cached --check
git commit -m "feat: authorize knowledge search store context"
~~~

### Task 4: Build the knowledge console with `canUseKnowledge`

**Files:**
- Modify: `frontend/src/api.ts`, `frontend/src/types.ts`, `frontend/src/router.ts`, `frontend/src/capabilities.ts`, `frontend/src/capabilities.test.ts`, `frontend/src/components/AppShell.vue`
- Create: `frontend/src/pages/KnowledgePage.vue`, `frontend/src/pages/KnowledgePage.test.ts`, `frontend/tests/phase9-management.spec.ts`

**Interfaces:**
- Consumes: existing knowledge APIs, safe history API, `ApiError`, and `CurrentUser.role`.
- Produces: `canUseKnowledge(role: UserRole, isMobile: boolean): boolean`, route `/app/knowledge`, and no-request mobile guard.

- [ ] **Step 1: Write failing capability/page/browser tests**

~~~ts
it('blocks management loading on a phone', async () => {
  session.user = { id: 'operator-1', username: 'operator', role: 'operator' }
  setMobile(true)
  await router.push({ name: 'knowledge' })
  expect(fetch).not.toHaveBeenCalled()
})
~~~

Cover all three desktop/tablet roles search; admin-only document/history/upload/disable controls; loading/empty/filtered-empty/401/403/404/409/422/error states; text-bound result snippets; no download/delete/re-enable; and a fixture browser mobile deep link with zero calls.

- [ ] **Step 2: Run frontend RED**

Run: `npm --prefix frontend test -- KnowledgePage capabilities`

Expected: FAIL because `canUseKnowledge`, route, API types, and page do not exist.

- [ ] **Step 3: Implement one Element Plus page**

~~~ts
export function canUseKnowledge(role: UserRole, isMobile: boolean): boolean {
  return !isMobile && (role === 'operator' || role === 'supervisor' || role === 'admin')
}
~~~

Use this function in navigation, route guard, and mobile zero-request test. Add typed `apiRequest` calls, cancellation with `AbortController`, safe status tags, and admin-only document drawer/actions. Render the device restriction before `onMounted` calls load. Use Vue interpolation only.

- [ ] **Step 4: Run frontend GREEN and local PostgreSQL knowledge gate**

Run: `npm --prefix frontend test -- KnowledgePage capabilities && npm --prefix frontend run typecheck && npm --prefix frontend run build && npm --prefix frontend run test:e2e -- phase9-management`

Expected: PASS with fixture API traffic.

Run: `$env:RUN_POSTGRES_INTEGRATION='1'; D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_postgres.py tests/test_knowledge_api.py -v`

Expected: PASS with safe history/search/audit checks; clear the opt-in.

- [ ] **Step 5: Commit the knowledge vertical slice**

~~~bash
git add frontend/src/api.ts frontend/src/types.ts frontend/src/router.ts frontend/src/capabilities.ts frontend/src/capabilities.test.ts frontend/src/components/AppShell.vue frontend/src/pages/KnowledgePage.vue frontend/src/pages/KnowledgePage.test.ts frontend/tests/phase9-management.spec.ts
git diff --cached --check
git commit -m "feat: add knowledge management console"
~~~
