# Phase 9 Audit Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe indexed audit filters and an authorized responsive audit-log console while preserving append-only history.

**Architecture:** Plan 1 supplies the resource-compatible `AuditEvent` schema and `AUDIT_DETAIL_KEYS`. This plan puts all filter validation and scoped/global query logic in `backend/audit_events.py`, adds one read-only FastAPI route, then renders the safe view in Vue. No audit event is changed or queried by free text/details content.

**Tech Stack:** FastAPI, SQLAlchemy async, pytest, PostgreSQL, Vue 3, TypeScript, Element Plus, Vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-04-phase-9-management-console-design.md`

## Global Constraints

- The current 电商项目开发窗口 executes this plan sequentially with `superpowers:executing-plans`; do not dispatch subagents.
- Reuse Plan 1 `0006`, `AuditEvent`, event types, resource fields, and `AUDIT_DETAIL_KEYS`; create no migration, table, or audit writer.
- Audit facts are append-only. This plan has no update/delete/replay/repair event action.
- Legal filters are `page`, `page_size`, `store_id`, `proposal_id`, `action`, `workflow_run_id`, `event_type`, `actor_id`, `outcome`, `created_from`, `created_to`. IDs are 1..36, page>=1, page_size 1..100, UTC range is at most 31 days and start<=end.
- Order is `created_at DESC, id DESC`. Do not add full-text search, arbitrary sort, details JSON filter, error text search, raw payload, or chart library.
- `supervisor` sees current scoped records, including disabled-store history, and no global records. `admin` sees all global/store history without scope. `operator` gets 403.
- Response/UI exclude secrets, hashes, prompts/raw output, exception, document data, workflow internals, lease/checkpoint, or publish snapshot. Phase 10 retains true browser E2E and reports.
- Before every ordinary offline gate, execute `Remove-Item Env:RUN_POSTGRES_INTEGRATION -ErrorAction SilentlyContinue; Remove-Item Env:RUN_KNOWLEDGE_INTEGRATION -ErrorAction SilentlyContinue; Remove-Item Env:RUN_DEEPSEEK_SMOKE -ErrorAction SilentlyContinue; Remove-Item Env:RUN_TASK10_DEEPSEEK_SMOKE -ErrorAction SilentlyContinue; Remove-Item Env:RUN_PHASE9_EVALUATION_WRITE -ErrorAction SilentlyContinue; Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue`. PostgreSQL proof alone sets `RUN_POSTGRES_INTEGRATION=1`; immediately after every PostgreSQL command execute `Remove-Item Env:RUN_POSTGRES_INTEGRATION -ErrorAction SilentlyContinue`.
- Do not read, modify, or stage `docs/business-and-technical-guide.md` or `docs/interview-q-and-a.md`.
- Do not modify or clean C-drive personal files outside the current Codex worktree; put cache and temporary files on D drive first.

---

## Locked File Structure and Responsibilities

| Path | Responsibility |
|---|---|
| `backend/audit_events.py` | `AuditEventFilters` and the sole filtered query. |
| `backend/schemas.py`, `backend/routes.py` | Bounded query/response DTOs and `GET /audit-events`. |
| `tests/test_audit_events.py`, `tests/test_audit_api.py`, `tests/test_phase9_postgres.py` | Service, RBAC, safe shape, paging, and local PostgreSQL tests. |
| `frontend/src/api.ts`, `frontend/src/types.ts`, `frontend/src/router.ts`, `frontend/src/capabilities.ts`, `frontend/src/components/AppShell.vue`, `frontend/src/pages/AuditEventsPage.vue` | Typed audit list and desktop/tablet route. |
| `frontend/src/pages/AuditEventsPage.test.ts`, `frontend/tests/phase9-management.spec.ts` | Component/device and fixture-browser evidence. |

### Task 1: Implement safe audit filtering and PostgreSQL scope proof

**Files:**
- Modify: `backend/audit_events.py`, `backend/schemas.py`, `tests/test_phase9_postgres.py`
- Create: `tests/test_audit_events.py`

**Interfaces:**
- Consumes: `AuditEvent`, `ApprovalAction`, `Store`, `User`, `UserStoreScope`, `AuditEventType`, `AuditOutcome`, and `AUDIT_DETAIL_KEYS`.
- Produces: `AuditEventFilters` and `list_audit_events(session: AsyncSession, *, actor_id: str, filters: AuditEventFilters) -> tuple[list[AuditEvent], int]`.

- [ ] **Step 1: Write failing offline and real-PostgreSQL tests**

~~~python
def test_filter_rejects_range_longer_than_31_days() -> None:
    with pytest.raises(AuditEventDomainError, match='AUDIT_FILTER_INVALID'):
        AuditEventFilters(
            created_from=datetime(2026, 1, 1, tzinfo=UTC),
            created_to=datetime(2026, 2, 2, tzinfo=UTC),
        )
~~~

In `tests/test_phase9_postgres.py`, open separate existing session-factory sessions and seed direct rows:

~~~python
async with async_session_factory() as writer, async_session_factory() as reader:
    writer.add(AuditEvent(
        id='global-admin-event', event_type=AuditEventType.ADMIN_USER_UPDATED,
        outcome=AuditOutcome.SUCCESS, store_id=None, actor_id='admin-1',
        actor_role=UserRole.ADMIN, resource_type='user', resource_id='user-2', details={},
    ))
    await writer.commit()
    events, _ = await list_audit_events(reader, actor_id='admin-1', filters=AuditEventFilters())
    assert 'global-admin-event' in {event.id for event in events}
~~~

Add direct offline cases for supervisor scoped/disabled-store history, supervisor global exclusion, admin global visibility, operator 403, every legal filter, stable ties, unsafe persisted details -> closed 503, and no mutation. Add PostgreSQL checks for the same visibility and descending paging.

- [ ] **Step 2: Run RED**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_audit_events.py -v`

Expected: FAIL because current query only accepts three filters, sorts ascending, and forces an admin scope join.

Run: `$env:RUN_POSTGRES_INTEGRATION='1'; D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_postgres.py -v`

Expected: FAIL until global/admin and scoped/supervisor paths are distinct; clear the opt-in.

- [ ] **Step 3: Implement the bounded query in the existing module**

~~~python
statement = select(AuditEvent)
if actor.role is UserRole.SUPERVISOR:
    statement = statement.join(
        UserStoreScope,
        and_(UserStoreScope.user_id == actor.id, UserStoreScope.store_id == AuditEvent.store_id),
    ).where(AuditEvent.store_id.is_not(None))
elif actor.role is not UserRole.ADMIN:
    raise AuditEventDomainError('AUDIT_FORBIDDEN', 403)
~~~

Validate every filter before SQL, apply indexed equality/timestamp predicates, count the exact filtered subquery, order `AuditEvent.created_at.desc(), AuditEvent.id.desc()`, and call `_safe_details` on every selected row. Do not create a generic query builder or mutate facts.

- [ ] **Step 4: Run GREEN and PostgreSQL audit gates**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_audit_events.py tests/test_approval_api.py -v`

Expected: PASS offline.

Run: `$env:RUN_POSTGRES_INTEGRATION='1'; D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_postgres.py -v`

Expected: PASS with append-only history, global admin visibility, scoped supervisor visibility, safe details, and stable paging; clear the opt-in.

- [ ] **Step 5: Commit audit filtering**

~~~bash
git add backend/audit_events.py backend/schemas.py tests/test_audit_events.py tests/test_phase9_postgres.py
git diff --cached --check
git commit -m "feat: add safe audit event filters"
~~~

### Task 2: Expose expanded `GET /audit-events`

**Files:**
- Modify: `backend/routes.py`, `backend/schemas.py`
- Create: `tests/test_audit_api.py`

**Interfaces:**
- Consumes: `AuditEventFilters` and `list_audit_events`.
- Produces: `GET /audit-events` with all legal filters and `AuditEventListView(items: list[AuditEventView], page: int, page_size: int, total: int)`.

- [ ] **Step 1: Write failing route and safe-response tests**

~~~python
async def test_route_returns_descending_safe_page(client, admin_headers) -> None:
    response = await client.get(
        '/audit-events?event_type=admin_user_updated&page=1&page_size=1',
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()['total'] == 2
    assert 'request_hash' not in response.text
~~~

Cover 401, operator 403, supervisor global/cross-store absence, admin global/disabled-store presence, all validation 422, stable filter-error code, and no exception detail response.

- [ ] **Step 2: Run route RED**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_audit_api.py -v`

Expected: FAIL because route inputs and response DTO cannot represent the expanded contract.

- [ ] **Step 3: Bind every typed filter and convert domain errors**

~~~python
@router.get('/audit-events', response_model=AuditEventListView)
async def list_audit_events_route(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    store_id: Annotated[str | None, Query(min_length=1, max_length=36)] = None,
    proposal_id: Annotated[str | None, Query(min_length=1, max_length=36)] = None,
    action: ApprovalActionType | None = None,
    workflow_run_id: Annotated[str | None, Query(min_length=1, max_length=36)] = None,
    event_type: AuditEventType | None = None,
    actor_id: Annotated[str | None, Query(min_length=1, max_length=36)] = None,
    outcome: AuditOutcome | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AuditEventListView:
    filters = AuditEventFilters(
        page=page, page_size=page_size, store_id=store_id, proposal_id=proposal_id,
        action=action, workflow_run_id=workflow_run_id, event_type=event_type,
        actor_id=actor_id, outcome=outcome, created_from=created_from, created_to=created_to,
    )
    events, total = await list_audit_events(session, actor_id=user.id, filters=filters)
    return AuditEventListView(
        items=[AuditEventView.model_validate(event) for event in events],
        page=page, page_size=page_size, total=total,
    )
~~~

Wrap the service call with the existing `AuditEventDomainError` to safe `HTTPException` conversion. Do not expose a raw ORM model.

- [ ] **Step 4: Run route GREEN**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_audit_api.py tests/test_approval_api.py tests/test_optimization_api.py -v`

Expected: PASS offline.

- [ ] **Step 5: Commit the route**

~~~bash
git add backend/routes.py backend/schemas.py tests/test_audit_api.py
git diff --cached --check
git commit -m "feat: expose filtered audit events"
~~~

### Task 3: Build Audit Events with `canViewAuditEvents`

**Files:**
- Modify: `frontend/src/api.ts`, `frontend/src/types.ts`, `frontend/src/router.ts`, `frontend/src/capabilities.ts`, `frontend/src/components/AppShell.vue`
- Create: `frontend/src/pages/AuditEventsPage.vue`, `frontend/src/pages/AuditEventsPage.test.ts`
- Modify: `frontend/tests/phase9-management.spec.ts`

**Interfaces:**
- Consumes: `GET /audit-events` and `ApiError`.
- Produces: `canViewAuditEvents(role: UserRole, isMobile: boolean): boolean`, route `/app/audit-events`, and `listAuditEvents(query: AuditEventQuery, signal?: AbortSignal): Promise<AuditEventList>`.

- [ ] **Step 1: Write failing capability/page/browser tests**

~~~ts
it('does not request audit data on a phone', async () => {
  session.user = { id: 'supervisor-1', username: 'supervisor', role: 'supervisor' }
  setMobile(true)
  await router.push({ name: 'audit-events' })
  expect(fetch).not.toHaveBeenCalled()
})
~~~

Cover supervisor/admin navigation, operator forbidden view, all list states, filter/paging retention after error, field-safe drawer, text-only detail rendering, and deterministic browser desktop/tablet/mobile paths.

- [ ] **Step 2: Run frontend RED**

Run: `npm --prefix frontend test -- AuditEventsPage capabilities`

Expected: FAIL because capability, route, typed request, and page are absent.

- [ ] **Step 3: Implement the safe table and drawer**

~~~ts
export function canViewAuditEvents(role: UserRole, isMobile: boolean): boolean {
  return !isMobile && (role === 'supervisor' || role === 'admin')
}
~~~

Use this exact function for nav, route, and mobile zero-request behavior. Add `apiRequest`/`AbortController` request handling, only server-approved filter fields, Element Plus table/pagination/drawer, text interpolation, visible labels, keyboard controls, and aria-live status. The route renders the mobile restriction before load.

- [ ] **Step 4: Run frontend GREEN**

Run: `npm --prefix frontend test -- AuditEventsPage capabilities && npm --prefix frontend run typecheck && npm --prefix frontend run build && npm --prefix frontend run test:e2e -- phase9-management`

Expected: PASS with fixture browser responses.

- [ ] **Step 5: Commit audit UI**

~~~bash
git add frontend/src/api.ts frontend/src/types.ts frontend/src/router.ts frontend/src/capabilities.ts frontend/src/components/AppShell.vue frontend/src/pages/AuditEventsPage.vue frontend/src/pages/AuditEventsPage.test.ts frontend/tests/phase9-management.spec.ts
git diff --cached --check
git commit -m "feat: add audit log console"
~~~
