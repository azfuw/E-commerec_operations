# Phase 9 System Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely manage existing users, scopes, and store enabled state while making global admin visibility consistent and completing Phase 9 final verification.

**Architecture:** Plan 1 supplies persistence/audit facts and Plan 3 audit reads. `backend/admin_management.py` owns locked admin mutations; a shared visibility predicate corrects existing read models. The System Management Vue page is desktop/tablet-only and has User/Store tabs. It adds no creation, rename, SSO, import, price/SKU flow, or evaluation browser write.

**Tech Stack:** FastAPI, SQLAlchemy async, PostgreSQL, pytest, Vue 3, TypeScript, Element Plus, Vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-04-phase-9-management-console-design.md`

## Global Constraints

- The current 电商项目开发窗口 executes this plan sequentially with `superpowers:executing-plans`; do not dispatch subagents.
- Reuse Plan 1 `0006` and Plan 3 audit behavior. Create no migration, dependency, microservice, queue, cache, provider, model, or network operation.
- Every `/admin` path requires a freshly loaded active admin. Never self-disable/self-demote, and never permit zero active admins. Violations return only `ADMIN_GUARD_VIOLATION` or `ADMIN_CONFLICT`.
- `PUT /admin/users/{user_id}/store-scopes` is a complete atomic replacement: unique, enabled, known stores only. `PATCH /admin/stores/{store_id}` accepts exactly `{"enabled": boolean}`; id/name/code are read-only.
- Disabled users/stores preserve history and block future authentication/action/new scope/new-store use. Existing supervisor/admin self-approval remains unchanged.
- Admin sees all global/store facts without `UserStoreScope`; supervisor/operator keep exact scope limits. Backend authorization remains authoritative.
- Each accepted mutation appends one safe audit in its transaction. No response exposes password hashes, JWT, secrets, hashes, prompt/raw I/O, workflow internals, or exceptions.
- `canUseKnowledge`, `canViewAgentObservability`, `canViewAuditEvents`, and `canManageSystem` are the only Phase 9 navigation/route/device functions; each has signature `(role: UserRole, isMobile: boolean) => boolean`.
- Phase 10 retains real model runs, fault injection, real service browser E2E, reports, and document verification.
- Before every ordinary offline gate, execute `Remove-Item Env:RUN_POSTGRES_INTEGRATION -ErrorAction SilentlyContinue; Remove-Item Env:RUN_KNOWLEDGE_INTEGRATION -ErrorAction SilentlyContinue; Remove-Item Env:RUN_DEEPSEEK_SMOKE -ErrorAction SilentlyContinue; Remove-Item Env:RUN_TASK10_DEEPSEEK_SMOKE -ErrorAction SilentlyContinue; Remove-Item Env:RUN_PHASE9_EVALUATION_WRITE -ErrorAction SilentlyContinue; Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue`. PostgreSQL proof alone sets `RUN_POSTGRES_INTEGRATION=1`; immediately after every PostgreSQL command execute `Remove-Item Env:RUN_POSTGRES_INTEGRATION -ErrorAction SilentlyContinue`.
- Do not read, modify, or stage `docs/business-and-technical-guide.md` or `docs/interview-q-and-a.md`.
- Do not modify or clean C-drive personal files outside the current Codex worktree; put cache and temporary files on D drive first.

---

## Locked File Structure and Responsibilities

| Path | Responsibility |
|---|---|
| `backend/auth.py` | `store_visibility_predicate` used by existing read paths. |
| `backend/workbench.py`, `backend/proposals.py`, `backend/approvals.py`, `backend/audit_events.py`, `backend/agent_evaluations.py` | Use that predicate after their existing resource checks. |
| `backend/admin_management.py` | `lock_admin_user_set`, admin reads and locked write transactions. |
| `backend/schemas.py`, `backend/routes.py` | Admin request/response DTOs and five admin routes. |
| `tests/test_auth_and_scope.py`, `tests/test_admin_management.py`, `tests/test_admin_api.py`, `tests/test_phase9_postgres.py` | Visibility, lock order, atomicity, RBAC, and PostgreSQL concurrency evidence. |
| `frontend/src/api.ts`, `frontend/src/types.ts`, `frontend/src/router.ts`, `frontend/src/capabilities.ts`, `frontend/src/components/AppShell.vue`, `frontend/src/pages/SystemManagementPage.vue` | Typed admin console and exact capability use. |
| `frontend/src/pages/SystemManagementPage.test.ts`, `frontend/tests/phase9-management.spec.ts` | Component and deterministic browser fixtures. |

### Task 1: Make existing read visibility correct for global admins

**Files:**
- Modify: `backend/auth.py`, `backend/workbench.py`, `backend/proposals.py`, `backend/approvals.py`, `backend/audit_events.py`, `backend/agent_evaluations.py`, `tests/test_auth_and_scope.py`

**Interfaces:**
- Consumes: `UserRole`, `UserStatus`, `Store`, `UserStoreScope`, and existing read statements.
- Produces: `store_visibility_predicate(actor: User, store_id_column: ColumnElement[str]) -> ColumnElement[bool]`.

- [ ] **Step 1: Write failing current-path visibility tests**

~~~python
async def test_admin_without_scope_sees_every_workbench_store(session: AsyncSession) -> None:
    items, total = await list_workbench_tasks(
        session, actor_id='admin-without-scope', page=1, page_size=20,
        store_id=None, kind=None, status=None,
    )
    assert total == 2
    assert {item.store_id for item in items} == {'flagship', 'other-store'}

async def test_supervisor_without_scope_has_no_pending_approvals(session: AsyncSession) -> None:
    items, total = await list_pending_approvals(
        session, actor_id='supervisor-without-scope', page=1, page_size=20,
    )
    assert items == []
    assert total == 0
~~~

Seed the two enabled stores and pending rows directly in each test. Add separate direct tests with each real signature for `list_audit_events(session, actor_id='admin-without-scope', page=1, page_size=20, store_id=None, proposal_id=None, action=None)`, proposal reads, evaluation-run reads, and AgentCall reads; unpack every `(items, total)` result and assert their role-specific total and store IDs. Include disabled-store history only where the relevant endpoint permits history, operator restriction, and unchanged self-approval.

- [ ] **Step 2: Run RED**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_auth_and_scope.py tests/test_workbench_api.py tests/test_approval_api.py -v`

Expected: FAIL because current read paths have unconditional `UserStoreScope` joins for admin.

- [ ] **Step 3: Implement one predicate and apply it after authorization**

~~~python
def store_visibility_predicate(actor: User, store_id_column: ColumnElement[str]) -> ColumnElement[bool]:
    if actor.role is UserRole.ADMIN:
        return true()
    return store_id_column.in_(
        select(UserStoreScope.store_id).where(UserStoreScope.user_id == actor.id)
    )
~~~

Replace each unconditional scope join in the named modules with this predicate after fresh active-user/role validation. Keep each endpoint’s resource-chain checks and enabled/history policy. Do not use this predicate in a write transaction.

- [ ] **Step 4: Run GREEN**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_auth_and_scope.py tests/test_workbench_api.py tests/test_approval_api.py tests/test_optimization_api.py tests/test_audit_events.py tests/test_agent_evaluation_api.py tests/test_agent_calls_api.py -v`

Expected: PASS offline.

- [ ] **Step 5: Commit visibility correction**

~~~bash
git add backend/auth.py backend/workbench.py backend/proposals.py backend/approvals.py backend/audit_events.py backend/agent_evaluations.py tests/test_auth_and_scope.py
git diff --cached --check
git commit -m "fix: make admin store visibility global"
~~~

### Task 2: Add deadlock-free locked admin operations and routes

**Files:**
- Create: `backend/admin_management.py`, `tests/test_admin_management.py`, `tests/test_admin_api.py`
- Modify: `backend/schemas.py`, `backend/routes.py`, `tests/test_phase9_postgres.py`

**Interfaces:**
- Consumes: `User`, `Store`, `UserStoreScope`, `add_audit_event`, `UserRole`, `UserStatus`.
- Produces: `AdminDomainError(code: str, status_code: int)`, `lock_admin_user_set(session: AsyncSession, actor_id: str, target_id: str) -> tuple[User, User, list[User]]`, `list_admin_users(session: AsyncSession, *, actor_id: str, page: int, page_size: int, role: UserRole | None, status: UserStatus | None, store_id: str | None) -> tuple[list[User], int]`, `update_admin_user`, `replace_user_store_scopes`, `list_admin_stores`, `update_admin_store`, and the five specified `/admin` routes.

- [ ] **Step 1: Write failing atomicity and PostgreSQL concurrency tests**

In `tests/test_admin_management.py`, cover active-admin RBAC, self guard, target 404, exact role/status values, duplicate/unknown/disabled scope stores, exact replacement, enabled-only store patch, one audit per success, and rollback on flush/commit error.

In `tests/test_phase9_postgres.py`, define the two-session fixture with the project factory:

~~~python
@pytest.fixture
async def pg_admin_sessions():
    async with async_session_factory() as first, async_session_factory() as second:
        try:
            yield first, second
        finally:
            await first.rollback()
            await second.rollback()
~~~

Use it directly with a timeout:

~~~python
async def test_cross_admin_updates_have_no_deadlock(pg_admin_sessions) -> None:
    first, second = pg_admin_sessions
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                update_admin_user(first, actor_id='admin-a', user_id='admin-b',
                                  patch=AdminUserPatch(role=UserRole.SUPERVISOR)),
                update_admin_user(second, actor_id='admin-b', user_id='admin-a',
                                  patch=AdminUserPatch(role=UserRole.SUPERVISOR)),
                return_exceptions=True,
            ),
            timeout=5,
        )
    except asyncio.TimeoutError:
        pytest.fail('admin lock order deadlocked')

    assert any(
        isinstance(item, AdminDomainError)
        and item.code in {'ADMIN_GUARD_VIOLATION', 'ADMIN_CONFLICT'}
        for item in results
    )
    async with async_session_factory() as verification:
        active_count = await verification.scalar(
            select(func.count(User.id)).where(
                User.role == UserRole.ADMIN,
                User.status == UserStatus.ACTIVE,
            )
        )
    assert active_count >= 1
~~~

Also use the two sessions to confirm scope replacement is all-or-nothing and that losing concurrent action returns a closed conflict. SQLite tests verify service logic only; they make no row-lock claim.

- [ ] **Step 2: Run RED**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_admin_management.py tests/test_admin_api.py -v`

Expected: FAIL because admin DTOs, service, routes, and locked transaction entry are absent.

Run: `$env:RUN_POSTGRES_INTEGRATION='1'; D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_postgres.py -v`

Expected: FAIL because no deadlock-free admin transaction exists; clear the opt-in.

- [ ] **Step 3: Implement one stable lock entry and all mutations through it**

~~~python
async def lock_admin_user_set(
    session: AsyncSession, actor_id: str, target_id: str
) -> tuple[User, User, list[User]]:
    rows = list(await session.scalars(
        select(User)
        .where(or_(
            User.id.in_((actor_id, target_id)),
            and_(User.role == UserRole.ADMIN, User.status == UserStatus.ACTIVE),
        ))
        .order_by(User.id)
        .with_for_update()
    ))
    actor = next((row for row in rows if row.id == actor_id), None)
    target = next((row for row in rows if row.id == target_id), None)
    admins = [row for row in rows if row.role is UserRole.ADMIN and row.status is UserStatus.ACTIVE]
    if actor is None or actor.role is not UserRole.ADMIN or actor.status is not UserStatus.ACTIVE:
        raise AdminDomainError('ADMIN_GUARD_VIOLATION', 409)
    if target is None:
        raise AdminDomainError('ADMIN_USER_NOT_FOUND', 404)
    return actor, target, admins
~~~

Never lock actor or target before this statement. `update_admin_user` uses it, checks self-disable/self-demotion and enabled-admin count before and after mutation, appends `admin_user_updated`, then commits once. `replace_user_store_scopes` calls the same helper first, then locks current scopes and requested stores in `Store.id` order, validates the exact enabled unique set, deletes/reinserts the full set, appends `admin_user_scopes_replaced`, and commits once. `update_admin_store` calls `lock_admin_user_set(session, actor_id, actor_id)` first, then locks the target store; it accepts only `enabled`, appends `admin_store_updated`, and commits once. Every `IntegrityError`/`SQLAlchemyError` rolls back and returns a closed code.

- [ ] **Step 4: Run GREEN and PostgreSQL locks**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_admin_management.py tests/test_admin_api.py tests/test_auth_and_scope.py -v`

Expected: PASS offline.

Run: `$env:RUN_POSTGRES_INTEGRATION='1'; D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_postgres.py -v`

Expected: PASS with actual two-session timeout gate, no deadlock, one active admin minimum, atomic scope replacement, and audit facts; clear the opt-in.

- [ ] **Step 5: Commit admin service and routes**

~~~bash
git add backend/admin_management.py backend/schemas.py backend/routes.py tests/test_admin_management.py tests/test_admin_api.py tests/test_phase9_postgres.py
git diff --cached --check
git commit -m "feat: add safe system management api"
~~~

### Task 3: Build System Management with `canManageSystem`

**Files:**
- Modify: `frontend/src/api.ts`, `frontend/src/types.ts`, `frontend/src/router.ts`, `frontend/src/capabilities.ts`, `frontend/src/components/AppShell.vue`
- Create: `frontend/src/pages/SystemManagementPage.vue`, `frontend/src/pages/SystemManagementPage.test.ts`, `frontend/tests/phase9-management.spec.ts`

**Interfaces:**
- Consumes: five admin routes and `ApiError`.
- Produces: `canManageSystem(role: UserRole, isMobile: boolean): boolean`, route `/app/admin`, `listAdminUsers`, `updateAdminUser`, `replaceAdminUserScopes`, `listAdminStores`, `updateAdminStore`.

- [ ] **Step 1: Write failing UI/device tests**

~~~ts
it('renders phone restriction before an admin request', async () => {
  session.user = { id: 'admin-1', username: 'admin', role: 'admin' }
  setMobile(true)
  await router.push({ name: 'admin' })
  expect(fetch).not.toHaveBeenCalled()
})
~~~

Cover admin nav/data, supervisor/operator forbidden state, loading/empty/filtered-empty/401/403/404/409/422/error states, role/status edit, exact scope replacement, store enabled action, no id/name/code/username edit, no self-disable, confirmation before disable, and server refetch after success.

- [ ] **Step 2: Run frontend RED**

Run: `npm --prefix frontend test -- SystemManagementPage capabilities`

Expected: FAIL because the route, API types/calls, capability, and two-tab page do not exist.

- [ ] **Step 3: Implement the bounded two-tab page**

~~~ts
export function canManageSystem(role: UserRole, isMobile: boolean): boolean {
  return !isMobile && role === 'admin'
}
~~~

Use this function in nav, route, and mobile zero-request tests. Add typed `apiRequest` calls, Element Plus tables/forms/confirmation, disabled submit while pending, refetch after success, interpolation, aria-live status, and no create/rename/import UI.

- [ ] **Step 4: Run frontend GREEN**

Run: `npm --prefix frontend test -- SystemManagementPage capabilities && npm --prefix frontend run typecheck && npm --prefix frontend run build && npm --prefix frontend run test:e2e -- phase9-management`

Expected: PASS with fixture traffic only.

- [ ] **Step 5: Commit system UI**

~~~bash
git add frontend/src/api.ts frontend/src/types.ts frontend/src/router.ts frontend/src/capabilities.ts frontend/src/components/AppShell.vue frontend/src/pages/SystemManagementPage.vue frontend/src/pages/SystemManagementPage.test.ts frontend/tests/phase9-management.spec.ts
git diff --cached --check
git commit -m "feat: add system management console"
~~~

### Task 4: Run final Phase 9 verification and handoff

**Files:**
- Modify: none

**Interfaces:**
- Consumes: completed Plans 1–4 public contracts.
- Produces: command evidence only; no code, test, migration, dependency, or configuration edit.

- [ ] **Step 1: Freeze the verified tree**

Run: `git status --short && git diff --cached --name-only`

Expected: no working or staged changes before final gates.

- [ ] **Step 2: Run the ordinary offline regression**

Run: `Remove-Item Env:RUN_POSTGRES_INTEGRATION -ErrorAction SilentlyContinue; Remove-Item Env:RUN_KNOWLEDGE_INTEGRATION -ErrorAction SilentlyContinue; Remove-Item Env:RUN_DEEPSEEK_SMOKE -ErrorAction SilentlyContinue; Remove-Item Env:RUN_TASK10_DEEPSEEK_SMOKE -ErrorAction SilentlyContinue; Remove-Item Env:RUN_PHASE9_EVALUATION_WRITE -ErrorAction SilentlyContinue; Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue; D:\E-commerce_operations_env\python.exe -m pytest -v`

Expected: PASS with `RUN_POSTGRES_INTEGRATION`, `RUN_KNOWLEDGE_INTEGRATION`, `RUN_DEEPSEEK_SMOKE`, `RUN_TASK10_DEEPSEEK_SMOKE`, and `DEEPSEEK_API_KEY` cleared.

- [ ] **Step 3: Run frontend fixture verification**

Run: `npm --prefix frontend test && npm --prefix frontend run typecheck && npm --prefix frontend run build && npm --prefix frontend run test:e2e`

Expected: PASS with only deterministic fixture browser requests.

- [ ] **Step 4: Run PostgreSQL and static gates**

Run: `$env:RUN_POSTGRES_INTEGRATION='1'; D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_postgres.py tests/test_analysis_postgres.py tests/test_optimization_postgres.py tests/test_manual_review_postgres.py -v`

Expected: PASS for upgrade/downgrade/current/check, constraints/indexes, deadlock-free last-admin guard, atomic scopes, management audit, evaluation complete/failed-zero facts, and existing workflow compatibility; clear the opt-in.

Run: `D:\E-commerce_operations_env\python.exe -m compileall backend tests scripts && D:\E-commerce_operations_env\python.exe -m alembic current && D:\E-commerce_operations_env\python.exe -m alembic check && docker compose config --quiet && git diff --check`

Expected: every command exits 0.

- [ ] **Step 5: Deliver evidence or stop on first failure**

Run: `git status --short && git diff --cached --name-only && git log -1 --oneline`

Expected: clean working/staged state and the latest Phase 9 implementation commit. If any prior command fails, stop, report its command/output safely, and request a separately reviewed root-cause task; do not modify code during this verification task.
