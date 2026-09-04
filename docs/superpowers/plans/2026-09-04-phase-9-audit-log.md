# Phase 9 Audit Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Extend append-only audit reads with safe indexed filters and an authorized responsive audit-log console.

**Architecture:** Plan 1 supplies resource-compatible AuditEvent schema, event values, and safe details validation. This plan adds one bounded query, its GET route, and a Vue list/detail page. It never changes an audit row, permits no details JSON/text search, and keeps admin global visibility distinct from supervisor store scope.

**Tech Stack:** FastAPI, SQLAlchemy async, pytest, Vue 3, TypeScript, Element Plus, Vitest, Playwright.

**Spec:** docs/superpowers/specs/2026-09-04-phase-9-management-console-design.md

## Global Constraints

- Reuse Plan 1 migration 0006 and AUDIT_DETAIL_KEYS exactly; create no migration, table, writer, or event type.
- Audit is append-only. This work reads events and validates stored safe detail JSON only; no update, delete, replay, or repair route exists.
- Supervisor sees only current UserStoreScope records, including disabled-store history, and no global event. Admin sees every store-scoped and global event without a scope row. Operator gets 403.
- Only page/page_size, store_id, proposal_id, action, workflow_run_id, event_type, actor_id, outcome, created_from, and created_to are legal filters. IDs are 1..36; page>=1; page_size 1..100; UTC time range is at most 31 days and start<=end.
- Sort is created_at DESC then id DESC. Never add full-text search, arbitrary sort, error text, details query, raw payload, or chart dependency.
- Safe responses/UI contain only relationship IDs, enums, allowlisted details, short error code, request ID, and timestamps. Never expose credentials, hashes, prompts/raw output, exceptions, document body/path/vector, workflow state, lease/checkpoint, or publish snapshot.
- Mobile hides audit navigation and renders the device restriction before audit requests. Phase 10 owns true browser E2E and reports.

---

## Locked File Structure and Responsibilities

| Path | Responsibility |
|---|---|
| backend/audit_events.py | Parsed filter type and sole filtered audit query. |
| backend/schemas.py | Query bounds and safe list/detail DTOs. |
| backend/routes.py | GET /audit-events binding and safe domain error conversion. |
| tests/test_audit_events.py and tests/test_audit_api.py | Service, role/scope, filters, paging, safety, and error evidence. |
| frontend/src/api.ts, types.ts, router.ts, capabilities.ts, components/AppShell.vue | Typed audit fetch and desktop/tablet route/nav. |
| frontend/src/pages/AuditEventsPage.vue and frontend/src/pages/AuditEventsPage.test.ts | Field-safe filterable table and details drawer. |
| frontend/tests/phase9-management.spec.ts | Fixture-only navigation and mobile zero-request proof. |

### Task 1: Define and prove the safe audit query

**Files:**
- Modify: backend/audit_events.py, backend/schemas.py
- Create: tests/test_audit_events.py

**Interfaces:**
- Consumes: AuditEvent, ApprovalAction, Store, User, UserStoreScope, AuditEventType, AuditOutcome, and AUDIT_DETAIL_KEYS.
- Produces: AuditEventFilters and list_audit_events(session, actor_id, filters) -> tuple[list[AuditEvent], int].

- [ ] **Step 1: Write failing filter and scope tests**

~~~python
async def test_admin_reads_global_and_disabled_store_history_without_scope(session) -> None:
    items, total = await list_audit_events(session, actor_id='admin-1', filters=AuditEventFilters())
    assert {event.store_id for event in items} == {None, 'disabled-store', 'enabled-store'}
    assert total == 3

def test_rejects_time_range_over_31_days() -> None:
    with pytest.raises(AuditEventDomainError, match='AUDIT_FILTER_INVALID'):
        AuditEventFilters(created_from=utc('2026-01-01'), created_to=utc('2026-02-02'))
~~~

Cover active supervisor scope, global exclusion, disabled-store history with retained scope, operator 403, invalid ID/enum/timestamp, every filter, deterministic ties, and unsafe persisted details mapped to a stable 503 without JSON/error internals.

- [ ] **Step 2: Run focused RED**

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_audit_events.py -v

Expected: FAIL because the current query has three filters, ascending order, and a mandatory scope join.

- [ ] **Step 3: Implement the bounded existing-module query**

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

Validate filter values before SQL. Use indexed equality/timestamp predicates, order by created_at.desc(), id.desc(), and count the same filtered subquery. Recheck each selected details with _safe_details. Do not add a generic query builder, custom sort, or mutation.

- [ ] **Step 4: Run service GREEN/regression**

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_audit_events.py tests/test_approval_api.py -v

Expected: PASS offline; original store/proposal/action semantics stay compatible.

- [ ] **Step 5: Commit the query contract**

~~~bash
git add backend/audit_events.py backend/schemas.py tests/test_audit_events.py
git diff --cached --check
git commit -m "feat: add safe audit event filters"
~~~

### Task 2: Expose the expanded audit GET route

**Files:**
- Modify: backend/routes.py, backend/schemas.py
- Create: tests/test_audit_api.py

**Interfaces:**
- Consumes: AuditEventFilters and list_audit_events from Task 1.
- Produces: GET /audit-events with all approved filters and AuditEventListView containing items, page, page_size, and total.

- [ ] **Step 1: Write failing route/RBAC/response tests**

~~~python
async def test_audit_route_uses_descending_page_and_safe_details(client, admin_headers) -> None:
    response = await client.get('/audit-events?event_type=admin_user_updated&page=1&page_size=1',
                                headers=admin_headers)
    assert response.status_code == 200
    assert response.json()['total'] == 2
    assert 'request_hash' not in response.text
~~~

Cover 401, operator 403, supervisor global/cross-scope absence, admin global/disabled-store presence, all 422 bounds, AUDIT_FILTER_INVALID code, and no database exception leak.

- [ ] **Step 2: Run route RED**

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_audit_api.py -v

Expected: FAIL because the route cannot parse or return the expanded safe contract.

- [ ] **Step 3: Add typed query binding and error conversion**

~~~python
@router.get('/audit-events', response_model=AuditEventListView)
async def list_audit_events_route(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    event_type: AuditEventType | None = None,
    outcome: AuditOutcome | None = None,
    ...
) -> AuditEventListView:
    ...
~~~

Bind every specified filter with exact types/lengths, create one AuditEventFilters instance, pass it to the service, and map AuditEventDomainError to the established safe detail code. Do not return the raw service tuple.

- [ ] **Step 4: Run route GREEN/regression**

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_audit_api.py tests/test_approval_api.py tests/test_optimization_api.py -v

Expected: PASS offline.

- [ ] **Step 5: Commit the route**

~~~bash
git add backend/routes.py backend/schemas.py tests/test_audit_api.py
git diff --cached --check
git commit -m "feat: expose filtered audit events"
~~~

### Task 3: Build the responsive audit-log page

**Files:**
- Modify: frontend/src/api.ts, frontend/src/types.ts, frontend/src/router.ts, frontend/src/capabilities.ts, frontend/src/components/AppShell.vue
- Create: frontend/src/pages/AuditEventsPage.vue, frontend/src/pages/AuditEventsPage.test.ts, frontend/tests/phase9-management.spec.ts

**Interfaces:**
- Consumes: GET /audit-events and ApiError.
- Produces: route /app/audit-events, listAuditEvents(query, signal), AuditEventList type, and a zero-request mobile branch.

- [ ] **Step 1: Write failing UI/device tests**

~~~ts
it('preserves selected filters after an API error', async () => {
  await wrapper.get('[data-test="audit-event-type"]').setValue('proposal_approved')
  await flushPromises()
  expect(wrapper.get('[data-test="audit-event-type"]').element.value).toBe('proposal_approved')
})
~~~

Cover supervisor/admin nav/data, operator forbidden route, loading/empty/filtered-empty/401/403/404/409/422/unknown states, paging, field-safe details, text-only rendering, and mobile deep link without fetch. Extend deterministic browser fixtures.

- [ ] **Step 2: Run frontend RED**

Run: npm --prefix frontend test -- AuditEventsPage capabilities

Expected: FAIL because typed request, route, page, and nav are absent.

- [ ] **Step 3: Implement one Element Plus list/detail page**

~~~ts
export function listAuditEvents(query: AuditEventQuery, signal?: AbortSignal) {
  return apiRequest<AuditEventList>('/audit-events?' + queryString(query), { signal })
}
~~~

Use only approved select/date filters, Element Plus pagination/table/drawer, AbortController, visible labels, keyboard controls, and aria-live status. Drawer displays safe individual fields via interpolation rather than raw JSON/HTML. Hide nav on mobile and return restriction before load().

- [ ] **Step 4: Run frontend GREEN**

Run: npm --prefix frontend test -- AuditEventsPage capabilities

Expected: PASS.

Run: npm --prefix frontend run typecheck && npm --prefix frontend run build && npm --prefix frontend run test:e2e -- phase9-management

Expected: PASS using fixtures only.

- [ ] **Step 5: Commit the audit console**

~~~bash
git add frontend/src/api.ts frontend/src/types.ts frontend/src/router.ts frontend/src/capabilities.ts frontend/src/components/AppShell.vue frontend/src/pages/AuditEventsPage.vue frontend/src/pages/AuditEventsPage.test.ts frontend/tests/phase9-management.spec.ts
git diff --cached --check
git commit -m "feat: add audit log console"
~~~

### Task 4: Prove append-only compatibility and local PostgreSQL behavior

**Files:**
- Modify: tests/test_phase9_postgres.py, tests/test_audit_events.py, tests/test_audit_api.py

**Interfaces:**
- Consumes: Plan 1 schema, Tasks 1–3 contracts, and existing manual-review/approval events.
- Produces: local PostgreSQL proof of append-only compatibility and correct global/scope visibility.

- [ ] **Step 1: Write failing PostgreSQL acceptance test**

~~~python
async def test_postgres_audit_filters_keep_history_append_only_and_scope_correct(two_connections) -> None:
    await write_admin_update_and_global_event(two_connections.first)
    assert await read_supervisor_events(two_connections.second) == {'scoped-store'}
    assert await read_admin_events(two_connections.second) == {'scoped-store', 'global'}
~~~

Test old events, new global resource metadata, disabled-store history, unsafe details closed failure, and concurrent reads without mutation.

- [ ] **Step 2: Run PostgreSQL RED**

Run: $env:RUN_POSTGRES_INTEGRATION='1'; D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_postgres.py::test_postgres_audit_filters_keep_history_append_only_and_scope_correct -v

Expected: FAIL until admin/global/supervisor paths are distinct.

- [ ] **Step 3: Fix only the demonstrated query compatibility defect**

~~~python
assert selected_event.created_at <= previous_created_at
assert set(selected_event.details) <= AUDIT_DETAIL_KEYS
~~~

Keep Plan 1 migration and every event immutable. Do not add text search, event mutation, or external integration.

- [ ] **Step 4: Run Plan 3 verification**

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_audit_events.py tests/test_audit_api.py tests/test_approval_api.py tests/test_optimization_api.py -v

Expected: PASS offline.

Run: $env:RUN_POSTGRES_INTEGRATION='1'; D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_postgres.py -v

Expected: PASS against local PostgreSQL; then clear the opt-in.

- [ ] **Step 5: Commit acceptance evidence**

~~~bash
git add tests/test_phase9_postgres.py tests/test_audit_events.py tests/test_audit_api.py
git diff --cached --check
git commit -m "test: verify phase nine audit log"
~~~
