# Phase 9 Knowledge Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Add the sole Phase 9 persistence foundation plus safe browser knowledge management and store-context search.

**Architecture:** This plan owns the only Phase 9 Alembic revision, immutable evaluation/audit persistence foundations consumed later, and the knowledge-management vertical slice. Existing knowledge routes and Worker remain the source of truth; the Vue page reaches them only through the existing api.ts boundary.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, PostgreSQL, pytest, Vue 3, TypeScript, Element Plus, Vitest, Playwright.

**Spec:** docs/superpowers/specs/2026-09-04-phase-9-management-console-design.md

## Global Constraints

- Create exactly one Phase 9 migration, 0006 after 0005; Plans 2–4 reuse it and create no migration.
- Keep FastAPI, PostgreSQL, Vue 3, Element Plus, api.ts, session.ts, and same-origin /app hosting. Add no library, microservice, state store, chart, queue, cache, provider call, model download, or network dependency.
- Ordinary tests clear RUN_POSTGRES_INTEGRATION, RUN_KNOWLEDGE_INTEGRATION, RUN_DEEPSEEK_SMOKE, RUN_TASK10_DEEPSEEK_SMOKE, and DEEPSEEK_API_KEY. Local PostgreSQL verification uses RUN_POSTGRES_INTEGRATION=1 but stays offline.
- Server authorization always reloads active user, enabled store, scope, and resource ownership. Admin is globally visible and never needs a UserStoreScope row.
- Never create a Phase 9 read model containing a secret, hash, prompt, provider payload, exception text, document body, file name/path, vector, workflow input/output, checkpoint, or lease.
- Mobile hides the four management modules; an authenticated management deep link renders the device restriction before a management request. Phase 10 owns real models, fault injection, genuine full-service browser E2E, reports, and document verification.

---

## Locked File Structure and Responsibilities

| Path | Responsibility |
|---|---|
| backend/common.py | Closed evaluation and new audit enums. |
| backend/models.py | EvaluationCase, EvaluationRun, EvaluationResult, and global/resource-safe AuditEvent mapping. |
| alembic/versions/0006_phase9_management_console.py | Lossless schema, constraints/indexes, and fact-protecting downgrade guard. |
| backend/audit_events.py | Phase 9 safe detail validation and resource-aware append helper. |
| backend/knowledge_runs.py | Same-transaction knowledge success audit calls. |
| backend/schemas.py and backend/routes.py | Safe history DTO/route and authorized search request. |
| frontend/src/api.ts, types.ts, router.ts, capabilities.ts, components/AppShell.vue, pages/KnowledgePage.vue | Typed knowledge console and no-request mobile deep-link guard. |
| tests/test_phase9_models.py, tests/test_phase9_postgres.py, tests/test_knowledge_history.py, tests/test_knowledge_search_scope.py | Offline and local PostgreSQL contract coverage. |

### Task 1: Create the single 0006 persistence foundation

**Files:**
- Modify: backend/common.py, backend/models.py, backend/audit_events.py
- Create: alembic/versions/0006_phase9_management_console.py, tests/test_phase9_models.py, tests/test_phase9_postgres.py

**Interfaces:**
- Consumes: AuditEvent, Store, User, UserStoreScope, and Alembic 0005.
- Produces: EvaluationAgentType (analysis|optimization|compliance|knowledge_retrieval), EvaluationRunStatus (completed|failed), EvaluationCase, EvaluationRun, EvaluationResult, and add_audit_event with optional store_id/resource_type/resource_id.

- [ ] **Step 1: Write the failing schema/migration tests**

~~~python
def test_evaluation_result_has_exact_immutable_identity() -> None:
    assert 'uq_evaluation_results_run_case_agent' in {
        item.name for item in EvaluationResult.__table__.constraints
    }

def test_audit_resource_columns_are_paired() -> None:
    with pytest.raises(IntegrityError):
        make_audit_event(resource_type='evaluation_run', resource_id=None)
~~~

Add PostgreSQL cases for upgrade 0005→0006; evaluation FKs/checks/indexes; unique (evaluation_run_id, evaluation_case_id, agent_type); nullable global audit store; paired resource fields; downgrade success when no Phase 9 fact exists and stable RuntimeError when any evaluation row or new audit type exists.

- [ ] **Step 2: Run the focused RED test**

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_models.py -v

Expected: FAIL because the Phase 9 models, audit columns, and 0006 migration do not exist.

- [ ] **Step 3: Implement the smallest lossless foundation**

~~~python
class EvaluationResult(Base):
    __tablename__ = 'evaluation_results'
    __table_args__ = (
        UniqueConstraint('evaluation_run_id', 'evaluation_case_id', 'agent_type',
                         name='uq_evaluation_results_run_case_agent'),
        CheckConstraint('latency_ms >= 0', name='ck_evaluation_results_latency_ms'),
    )
~~~

Implement all spec columns: immutable evaluation_cases, evaluation_runs, and evaluation_results; exact result identity; matching closed enum/check values; non-null JSON; run indexes (agent_type, created_at, id), (store_id, created_at, id), (status, created_at, id). Widen AuditEvent store_id to nullable, add paired resource_type/resource_id (user|store|knowledge_document|knowledge_version|evaluation_run), add the seven Phase 9 event values, and append event/actor/outcome indexes. Expand AUDIT_DETAIL_KEYS only with approved fields and canonical UTF-8 JSON validation under 4096 bytes before session.add().

The downgrade checks tables and Phase 9 audit values before any destructive step and raises cannot downgrade phase-nine management console with facts; it must not rewrite existing workflow, proposal, approval, or call facts.

- [ ] **Step 4: Run GREEN and PostgreSQL gates**

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_models.py -v

Expected: PASS offline.

Run: $env:RUN_POSTGRES_INTEGRATION='1'; D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_postgres.py -v

Expected: PASS against local PostgreSQL, then clear RUN_POSTGRES_INTEGRATION.

- [ ] **Step 5: Commit the foundation**

~~~bash
git add backend/common.py backend/models.py backend/audit_events.py alembic/versions/0006_phase9_management_console.py tests/test_phase9_models.py tests/test_phase9_postgres.py
git diff --cached --check
git commit -m "feat: add phase nine management schema"
~~~

### Task 2: Record safe knowledge facts and expose version history

**Files:**
- Modify: backend/knowledge_runs.py, backend/routes.py, backend/schemas.py, tests/test_knowledge_api.py
- Create: tests/test_knowledge_history.py

**Interfaces:**
- Consumes: add_audit_event and the three knowledge AuditEventType values from Task 1.
- Produces: KnowledgeVersionHistoryItem, KnowledgeVersionHistoryView, and GET /knowledge/documents/{document_id}/versions?page&page_size.

- [ ] **Step 1: Write failing history/audit tests**

~~~python
async def test_admin_version_history_excludes_storage_and_content(client, admin_headers) -> None:
    response = await client.get('/knowledge/documents/document-1/versions', headers=admin_headers)
    assert response.status_code == 200
    assert set(response.json()['items'][0]).isdisjoint({'original_filename', 'storage_path', 'chunks'})

async def test_successful_version_writes_one_safe_audit(session) -> None:
    assert await count_events(session, 'knowledge_version_created') == 1
~~~

Cover admin success, operator/supervisor 403, unknown document 404, 422 page bounds, status/error short codes, one audit in the same successful transaction, and failed persistence with neither changed fact nor success audit.

- [ ] **Step 2: Run the focused RED test**

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_knowledge_api.py tests/test_knowledge_history.py -v

Expected: FAIL because neither safe history DTO/route nor database knowledge audit exists.

- [ ] **Step 3: Implement history and transaction-bound audit**

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

Append exactly one database event before the existing transaction commits a successful document create, version create, or disable. Add the admin-only history query ordered version_number DESC, id DESC, returning document ID/name/category/enabled and only the safe version fields above. Retain existing upload idempotency, cleanup, Worker claims, and disable semantics; add no download, delete, re-enable, content, chunk, or vector route.

- [ ] **Step 4: Run GREEN and knowledge regression**

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_knowledge_api.py tests/test_knowledge_history.py tests/test_knowledge_runs.py tests/test_knowledge_worker.py -v

Expected: PASS without model, Milvus, provider, or network activity.

- [ ] **Step 5: Commit the safe history slice**

~~~bash
git add backend/knowledge_runs.py backend/routes.py backend/schemas.py tests/test_knowledge_api.py tests/test_knowledge_history.py
git diff --cached --check
git commit -m "feat: expose safe knowledge version history"
~~~

### Task 3: Require store context before search dependencies

**Files:**
- Modify: backend/schemas.py, backend/routes.py, tests/test_knowledge_api.py, frontend/src/types.ts, frontend/src/api.ts
- Create: tests/test_knowledge_search_scope.py

**Interfaces:**
- Consumes: require_store_access(store_id, user, session) and search_active_knowledge.
- Produces: KnowledgeSearchRequest.store_id: str (1..36) and searchKnowledge(query: KnowledgeSearchQuery, signal?: AbortSignal): Promise<KnowledgeSearchResult>.

- [ ] **Step 1: Write failing authorization-first tests**

~~~python
async def test_search_authorizes_store_before_loader(client, scoped_operator, loader) -> None:
    response = await client.post('/knowledge/search', json={'store_id': 'other-store', 'query': '规则'})
    assert response.status_code == 403
    assert loader.calls == 0
~~~

Cover missing/malformed store_id, active scoped operator/supervisor/admin success, unknown/disabled store 404, missing scope 403, and unchanged safe zero_hit/low_confidence/dependency status rendering.

- [ ] **Step 2: Run the focused RED test**

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_knowledge_search_scope.py tests/test_knowledge_api.py -v

Expected: FAIL because the request has no required store_id and retrieval can begin before scope validation.

- [ ] **Step 3: Add the minimal authorization-first path**

~~~python
await require_store_access(request.store_id, user, session)
outcome = await search_active_knowledge(
    session, query=request.query, categories=request.categories, top_k=request.top_k,
    retrieval_path='hybrid_rerank', load_dependencies=load_dependencies,
)
~~~

Add the bounded required field and call the existing store helper before loading any dependency. The store is authorization/audit context only: documents remain global, and retrieval scoring, calibration, RAG quality, and loader signatures remain unchanged.

- [ ] **Step 4: Run GREEN and search regression**

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_knowledge_search_scope.py tests/test_knowledge_search.py tests/test_knowledge_api.py -v

Expected: PASS entirely offline.

- [ ] **Step 5: Commit the search contract**

~~~bash
git add backend/schemas.py backend/routes.py tests/test_knowledge_api.py tests/test_knowledge_search_scope.py frontend/src/types.ts frontend/src/api.ts
git diff --cached --check
git commit -m "feat: authorize knowledge search store context"
~~~

### Task 4: Build the responsive knowledge console

**Files:**
- Modify: frontend/src/api.ts, frontend/src/types.ts, frontend/src/router.ts, frontend/src/capabilities.ts, frontend/src/capabilities.test.ts, frontend/src/components/AppShell.vue
- Create: frontend/src/pages/KnowledgePage.vue, frontend/src/pages/KnowledgePage.test.ts, frontend/tests/phase9-management.spec.ts

**Interfaces:**
- Consumes: safe knowledge list/history/search APIs, existing upload/disable APIs, CurrentUser.role, and ApiError.
- Produces: route /app/knowledge, canAccessPhase9Management(role, isMobile), and admin-only mutations.

- [ ] **Step 1: Write failing page/navigation/device tests**

~~~ts
it('renders the device restriction before management fetches', async () => {
  setMobile(true)
  await router.push({ name: 'knowledge' })
  expect(fetch).not.toHaveBeenCalled()
  expect(wrapper.text()).toContain('此管理功能仅支持桌面或平板')
})
~~~

Cover search for each allowed role, admin history/upload/disable controls, loading, empty, filtered-empty, 401, 403, 404, 409, 422, unknown error, text-bound citation snippets, and absence of download/delete/re-enable. Extend deterministic Playwright fixtures for desktop/tablet and mobile zero-request behavior.

- [ ] **Step 2: Run focused frontend RED tests**

Run: npm --prefix frontend test -- KnowledgePage capabilities

Expected: FAIL because route, page, capability, and typed calls do not exist.

- [ ] **Step 3: Implement one Element Plus page through api.ts**

~~~ts
export function canAccessPhase9Management(role: UserRole, isMobile: boolean): boolean {
  return !isMobile && ['operator', 'supervisor', 'admin'].includes(role)
}
~~~

Create a page with store-select search, safe status tags, and an admin-only document table/version drawer. Add typed apiRequest calls, AbortController cleanup, role routes, and nav only when allowed and non-mobile. The mobile route branch must render before onMounted creates a request. Use interpolation, never v-html.

- [ ] **Step 4: Run frontend GREEN gates**

Run: npm --prefix frontend test -- KnowledgePage capabilities

Expected: PASS.

Run: npm --prefix frontend run typecheck && npm --prefix frontend run build && npm --prefix frontend run test:e2e -- phase9-management

Expected: PASS using fixture browser responses only.

- [ ] **Step 5: Commit the console**

~~~bash
git add frontend/src/api.ts frontend/src/types.ts frontend/src/router.ts frontend/src/capabilities.ts frontend/src/capabilities.test.ts frontend/src/components/AppShell.vue frontend/src/pages/KnowledgePage.vue frontend/src/pages/KnowledgePage.test.ts frontend/tests/phase9-management.spec.ts
git diff --cached --check
git commit -m "feat: add knowledge management console"
~~~

### Task 5: Run Plan 1 acceptance evidence before Plan 2

**Files:**
- Modify: tests/test_phase9_postgres.py, tests/test_knowledge_api.py, frontend/tests/phase9-management.spec.ts

**Interfaces:**
- Consumes: every Task 1–4 public contract and Alembic 0006.
- Produces: repeatable offline and PostgreSQL proof that later plans consume without another migration.

- [ ] **Step 1: Write the failing cross-layer acceptance test**

~~~python
async def test_0006_keeps_existing_knowledge_readable_and_audit_safe(session) -> None:
    assert await safe_history_for_existing_document(session)
    assert await all_event_details_are_allowlisted(session)
~~~

- [ ] **Step 2: Run the acceptance RED test**

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_postgres.py::test_0006_keeps_existing_knowledge_readable_and_audit_safe -v

Expected: FAIL until all previous tasks honor the transaction and response boundary.

- [ ] **Step 3: Correct only demonstrated Plan 1 integration mismatches**

~~~python
assert set(event.details) <= AUDIT_DETAIL_KEYS
assert 'canonical_text' not in response_text
~~~

Do not add evaluation execution, audit filtering, user administration, or another migration.

- [ ] **Step 4: Run Plan 1 verification**

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_models.py tests/test_knowledge_api.py tests/test_knowledge_history.py tests/test_knowledge_search_scope.py tests/test_knowledge_runs.py tests/test_knowledge_worker.py -v

Expected: PASS offline.

Run: $env:RUN_POSTGRES_INTEGRATION='1'; D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_postgres.py -v

Expected: PASS against local PostgreSQL; then clear the opt-in.

- [ ] **Step 5: Commit acceptance evidence**

~~~bash
git add tests/test_phase9_postgres.py tests/test_knowledge_api.py frontend/tests/phase9-management.spec.ts
git diff --cached --check
git commit -m "test: verify phase nine knowledge foundation"
~~~
