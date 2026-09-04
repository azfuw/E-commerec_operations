# Phase 9 System Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let enabled admins safely manage existing users, scopes, and store enabled state while making admin visibility globally consistent.

**Architecture:** Plan 1 supplies the database/audit foundation and Plan 3 supplies audit reads. A small `backend/admin_management.py` service owns locked write transactions. A shared predicate in `backend/auth.py` corrects every existing read path that incorrectly requires an admin `UserStoreScope`; the Vue page is a desktop/tablet two-tab management surface.

**Tech Stack:** FastAPI, SQLAlchemy async, PostgreSQL, pytest, Vue 3, TypeScript, Element Plus, Vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-04-phase-9-management-console-design.md`

## Global Constraints

- Reuse migration `0006`; do not add migration, dependency, microservice, queue, cache, provider, model, or network call.
- All `/admin` routes require a freshly active admin. Under a stable lock order reload actor, target, and enabled admins; before and after mutation enforce an enabled admin remains.
- Never self-disable or self-demote. Concurrent writes must return `ADMIN_GUARD_VIOLATION` or `ADMIN_CONFLICT` rather than leave zero enabled admins or partial writes.
- `PUT /admin/users/{user_id}/store-scopes` atomically replaces exactly the requested enabled, unique store set. `PATCH /admin/stores/{store_id}` accepts only `{"enabled": boolean}`.
- Admin sees all stores/global facts without `UserStoreScope`; supervisor/operator keep exact scope rules. Preserve existing supervisor/admin self-approval.
- Preserve disabled user/store history; block future authentication, action, new scope assignment, and new store use. Do not create user/store, rename, delete, SSO, invitation, bulk import, price/SKU flow, or browser evaluation write.
- Successful mutations append exactly one safe audit event in their write transaction. Exclude secrets, hashes, password data, JWT, login state, prompt/raw I/O, workflow internals, or exceptions.
- Mobile hides system management and renders a restriction before an admin fetch. Phase 10 owns real browser E2E, reports, and final document verification.

---

## Locked File Structure and Responsibilities

| Path | Responsibility |
|---|---|
| `backend/auth.py` | Reusable store visibility predicate for every read path. |
| `backend/workbench.py`, `backend/proposals.py`, `backend/approvals.py`, `backend/audit_events.py`, `backend/agent_evaluations.py` | Apply global admin visibility without bypassing resource checks. |
| `backend/admin_management.py` | Locked user/status/scope/store transactions and closed domain errors. |
| `backend/schemas.py`, `backend/routes.py` | Exact admin DTOs and five GET/PATCH/PUT paths. |
| `tests/test_auth_and_scope.py`, `tests/test_admin_management.py`, `tests/test_admin_api.py`, `tests/test_phase9_postgres.py` | RBAC, atomicity, concurrency, PostgreSQL, and migration evidence. |
| `frontend/src/api.ts`, `types.ts`, `router.ts`, `capabilities.ts`, `components/AppShell.vue`, `pages/SystemManagementPage.vue` | Typed desktop/tablet admin console. |
| `frontend/src/pages/SystemManagementPage.test.ts`, `frontend/tests/phase9-management.spec.ts` | Component and browser-fixture proof. |

### Task 1: Unify global admin visibility for current read models

**Files:**
- Modify: `backend/auth.py`, `backend/workbench.py`, `backend/proposals.py`, `backend/approvals.py`, `backend/audit_events.py`, `backend/agent_evaluations.py`
- Create: `tests/test_auth_and_scope.py`

**Interfaces:**
- Consumes: `UserRole`, `UserStatus`, `Store`, and `UserStoreScope`.
- Produces: `store_visibility_predicate(actor: User, store_id_column: ColumnElement[str]) -> ColumnElement[bool]`.

- [ ] **Step 1: Write failing cross-path scope tests**

~~~python
@pytest.mark.parametrize('reader', [
    read_workbench, read_proposal, read_approvals, read_audit,
    read_evaluation_runs, read_agent_calls,
])
async def test_admin_without_scope_reads_visible_facts(reader, session) -> None:
    assert await reader(session, actor_id='admin-without-scope')

async def test_supervisor_without_scope_cannot_read_other_store(session) -> None:
    assert not await read_workbench(session, actor_id='supervisor-without-scope')
~~~

Include disabled-store history where the endpoint permits history, operator restrictions, and unchanged supervisor/admin self-approval.

- [ ] **Step 2: Run focused RED**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_auth_and_scope.py tests/test_workbench_api.py tests/test_approval_api.py -v`

Expected: FAIL because several paths unconditionally join `UserStoreScope` for admin.

- [ ] **Step 3: Implement and apply one predicate**

~~~python
def store_visibility_predicate(actor: User, store_id_column: ColumnElement[str]) -> ColumnElement[bool]:
    if actor.role is UserRole.ADMIN:
        return true()
    return store_id_column.in_(
        select(UserStoreScope.store_id).where(UserStoreScope.user_id == actor.id)
    )
~~~

Apply it only after the existing fresh active-user/role checks, preserving each resource-chain and enabled/history condition. Do not use it in a write to bypass lock checks.

- [ ] **Step 4: Run GREEN and regressions**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_auth_and_scope.py tests/test_workbench_api.py tests/test_approval_api.py tests/test_optimization_api.py tests/test_audit_events.py tests/test_agent_evaluation_api.py tests/test_agent_calls_api.py -v`

Expected: PASS offline.

- [ ] **Step 5: Commit visibility consistency**

~~~bash
git add backend/auth.py backend/workbench.py backend/proposals.py backend/approvals.py backend/audit_events.py backend/agent_evaluations.py tests/test_auth_and_scope.py
git diff --cached --check
git commit -m "fix: make admin store visibility global"
~~~

### Task 2: Add locked admin-management operations and API

**Files:**
- Create: `backend/admin_management.py`, `tests/test_admin_management.py`, `tests/test_admin_api.py`
- Modify: `backend/schemas.py`, `backend/routes.py`

**Interfaces:**
- Consumes: `add_audit_event`, `User`, `Store`, `UserStoreScope`, `UserRole`, `UserStatus`.
- Produces: `list_admin_users`, `update_admin_user`, `replace_user_store_scopes`, `list_admin_stores`, `update_admin_store`; `AdminUserPatch`, `AdminStoreScopeReplace`, `AdminStorePatch`; the five `/admin` endpoints in the spec.

- [ ] **Step 1: Write failing transaction/RBAC tests**

~~~python
async def test_concurrent_last_admin_demotions_leave_one_enabled_admin(two_sessions) -> None:
    results = await asyncio.gather(
        update_admin_user(two_sessions.first, 'admin-a', 'admin-b', AdminUserPatch(role='supervisor')),
        update_admin_user(two_sessions.second, 'admin-b', 'admin-a', AdminUserPatch(role='supervisor')),
        return_exceptions=True,
    )
    assert await enabled_admin_count(two_sessions.first) >= 1
    assert any(getattr(item, 'code', None) in {'ADMIN_GUARD_VIOLATION', 'ADMIN_CONFLICT'}
               for item in results)
~~~

Cover 401/non-admin 403, self guard, target 404, exact role/status input, no-op target state, atomic complete scope replacement, duplicate/unknown/disabled store rejection, enabled-only store patch, one audit per success, history preservation, and zero writes on flush/commit error.

- [ ] **Step 2: Run focused RED**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_admin_management.py tests/test_admin_api.py -v`

Expected: FAIL because DTOs, service, and admin routes do not exist.

- [ ] **Step 3: Implement stable lock-order transactions**

~~~python
locked_admins = list(await session.scalars(
    select(User).where(User.role == UserRole.ADMIN, User.status == UserStatus.ACTIVE)
    .order_by(User.id).with_for_update()
))
if actor.id == target.id and patch.status is UserStatus.DISABLED:
    raise AdminDomainError('ADMIN_GUARD_VIOLATION', 409)
~~~

For a user patch lock actor, target, then active admins; check pre/post enabled-admin count; write one `admin_user_updated` audit before the sole commit. For scope replacement lock actor, target, old scope rows, and requested stores in ID order; reject duplicate/unknown/disabled IDs; delete old scope and insert the exact set with one `admin_user_scopes_replaced` event. For store patch lock actor/store, accept only the exact enabled field, update it, and write `admin_store_updated`. Roll back all writes for integrity/database errors and return a closed code.

- [ ] **Step 4: Run GREEN/regressions**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_admin_management.py tests/test_admin_api.py tests/test_auth_and_scope.py tests/test_approval_api.py tests/test_optimization_api.py -v`

Expected: PASS offline.

- [ ] **Step 5: Commit admin operations**

~~~bash
git add backend/admin_management.py backend/schemas.py backend/routes.py tests/test_admin_management.py tests/test_admin_api.py
git diff --cached --check
git commit -m "feat: add safe system management api"
~~~

### Task 3: Build desktop/tablet System Management

**Files:**
- Modify: `frontend/src/api.ts`, `frontend/src/types.ts`, `frontend/src/router.ts`, `frontend/src/capabilities.ts`, `frontend/src/components/AppShell.vue`
- Create: `frontend/src/pages/SystemManagementPage.vue`, `frontend/src/pages/SystemManagementPage.test.ts`, `frontend/tests/phase9-management.spec.ts`

**Interfaces:**
- Consumes: all five admin paths and `ApiError`.
- Produces: `/app/admin`, `listAdminUsers`, `updateAdminUser`, `replaceAdminUserScopes`, `listAdminStores`, `updateAdminStore`, and `canManageSystem(role, isMobile)`.

- [ ] **Step 1: Write failing page and device tests**

~~~ts
it('requires confirmation before store disable', async () => {
  await wrapper.get('[data-test="disable-store-store-1"]').trigger('click')
  expect(wrapper.text()).toContain('确认停用')
  expect(fetch).not.toHaveBeenCalled()
})
~~~

Cover admin nav/data, supervisor/operator forbidden state, loading/empty/filtered-empty/401/403/404/409/422/unknown states, user role/status, complete scope selection, enabled-only store change, absence of username/name/code edit, no self-disable control, successful server refetch, and mobile deep link with no fetch.

- [ ] **Step 2: Run frontend RED**

Run: `npm --prefix frontend test -- SystemManagementPage capabilities`

Expected: FAIL because typed requests, route, capability, and two-tab page are absent.

- [ ] **Step 3: Implement the two-tab page**

~~~ts
export function canManageSystem(role: UserRole, isMobile: boolean): boolean {
  return role === 'admin' && !isMobile
}
~~~

Use Element Plus tables/forms/dialog confirmation and existing apiRequest/ApiError. Submit only complete target values, disable duplicate submit while pending, refetch server facts after success, use interpolation and aria-live status, and render mobile restriction before load().

- [ ] **Step 4: Run frontend GREEN**

Run: `npm --prefix frontend test -- SystemManagementPage capabilities`

Expected: PASS.

Run: `npm --prefix frontend run typecheck && npm --prefix frontend run build && npm --prefix frontend run test:e2e -- phase9-management`

Expected: PASS with fixture traffic only.

- [ ] **Step 5: Commit the console**

~~~bash
git add frontend/src/api.ts frontend/src/types.ts frontend/src/router.ts frontend/src/capabilities.ts frontend/src/components/AppShell.vue frontend/src/pages/SystemManagementPage.vue frontend/src/pages/SystemManagementPage.test.ts frontend/tests/phase9-management.spec.ts
git diff --cached --check
git commit -m "feat: add system management console"
~~~

### Task 4: Run Phase 9 complete acceptance and final gates

**Files:**
- Create: `tests/test_phase9_acceptance.py`
- Modify: `tests/test_phase9_postgres.py`, `frontend/tests/phase9-management.spec.ts`

**Interfaces:**
- Consumes: every Plan 1–4 public model, route, CLI, audit, and UI contract.
- Produces: only offline, local-PostgreSQL, fixture-browser, and Git evidence.

- [ ] **Step 1: Write failing combined acceptance tests**

~~~python
async def test_postgres_concurrent_admin_scope_evaluation_and_audit_contract(two_connections) -> None:
    await assert_last_admin_guard(two_connections)
    await assert_scope_replacement_is_atomic(two_connections)
    await assert_evaluation_is_complete_or_failed_zero_result(two_connections)
    await assert_audit_is_safe_and_append_only(two_connections)
~~~

Also prove offline that manual-review Worker never invokes optimization Agent, RAG zero_hit/low_confidence makes no DeepSeek request, schema repair remains bounded, and existing price/SKU/inventory/approval behavior remains unchanged. Add fixture browser paths for all four management modules on desktop/tablet and restrictions on mobile.

- [ ] **Step 2: Run acceptance RED**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_acceptance.py -v`

Expected: FAIL until Plans 1–3 and Tasks 1–3 satisfy their combined contracts.

- [ ] **Step 3: Repair only an observed cross-plan mismatch**

~~~python
assert response.status_code in {200, 403, 404, 409, 422}
assert 'Authorization' not in response.text
~~~

Do not add a real-model smoke, platform call, price/SKU change, frontend evaluation write, migration, external dependency, report, or document edit.

- [ ] **Step 4: Run final verification**

Run: `D:\E-commerce_operations_env\python.exe -m pytest -v`

Expected: PASS with ordinary opt-ins/keys cleared.

Run: `npm --prefix frontend test && npm --prefix frontend run typecheck && npm --prefix frontend run build && npm --prefix frontend run test:e2e`

Expected: PASS using deterministic fixtures.

Run: `$env:RUN_POSTGRES_INTEGRATION='1'; D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_postgres.py tests/test_analysis_postgres.py tests/test_optimization_postgres.py tests/test_manual_review_postgres.py -v`

Expected: PASS against local PostgreSQL, including Alembic upgrade/downgrade/current/check, constraints/indexes, concurrent last-admin protection, scope replacement, management audit, complete successful evaluation, and failed zero-result atomicity; then clear the opt-in.

Run: `D:\E-commerce_operations_env\python.exe -m compileall backend tests scripts && D:\E-commerce_operations_env\python.exe -m alembic current && D:\E-commerce_operations_env\python.exe -m alembic check && docker compose config --quiet && git diff --check`

Expected: every command exits 0.

- [ ] **Step 5: Commit final acceptance evidence**

~~~bash
git add tests/test_phase9_acceptance.py tests/test_phase9_postgres.py frontend/tests/phase9-management.spec.ts
git diff --cached --check
git commit -m "test: verify phase nine management console"
~~~
