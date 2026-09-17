# Logistics correctness review — 2026-09-17

Independent read-only review of the logistics domain, API, persistence, seed, migration 0008, worker, launcher, and relevant frontend/API integration. Only this report was authored by the reviewer.

## Result

No important unresolved backend security or correctness finding remains in the reviewed revision. Three concrete defects were reproduced during review and corrected by their implementation owners. The frontend corrections were checked in source and its focused helper tests pass; complete browser acceptance remains part of the integration owner's validation.

## Reproduced findings and disposition

### [P2, fixed] Patrol persisted a task from an obsolete shipment snapshot

Location: `backend/logistics.py`, `patrol` (current lines 431–460).

Reproduction used two independent `AsyncSession` objects. Session A loaded an overdue `pending_dispatch` shipment with `shipment_rows`; session B called `record_event(..., action="dispatch")` and committed; session A then passed its previously loaded rows to `patrol`. The patrol created one `dispatch_overdue` task whose evidence still said `pending_dispatch`, although the committed shipment was `in_transit`. Its `observed_at` was later than the recorded dispatch. Subsequent patrols do not automatically resolve that false open task.

The correction refreshes the candidate facts while holding the appropriate write protection: SQLite reserves its writer lock before refreshing, and PostgreSQL locks shipment rows in stable ID order. Return facts are refreshed and locked before deriving their evidence. Return and exception mutations acquire the shipment lock before child rows, keeping lock order consistent. The new separate-session regression and existing concurrent deduplication test both passed independently, followed by the complete logistics suite.

### [P1, fixed] The UI could not receive or close a return with tracking information

Location: `frontend/src/pages/LogisticsPage.vue`, `openReturnAction` / `saveReturnAction` (current lines 374–405); `backend/logistics.py`, `transition_return`.

The UI prefilled a return's existing carrier and tracking number and sent them for every transition. A normal `in_transit → received` or `received → closed` action therefore received HTTP 422 because the backend permits those fields only when registering `in_transit`. This was reproduced against the actual backend using a valid approved/mailed return and the payload emitted by the UI.

The UI now includes carrier and tracking fields only when the destination status is `in_transit`, preserving the backend validation. The corrected request construction was inspected; browser acceptance should exercise both receiving and closing.

### [P2, fixed] A newly created return could not immediately be approved with the default time

Location: `frontend/src/pages/LogisticsPage.vue`, return action time initialization and submission (current lines 374–405); `frontend/src/logistics.ts`, Beijing input conversion.

`localNow()` truncated seconds, while the backend records `requested_at` with fractional seconds. Creating a return and approving it within the same minute therefore submitted a timestamp earlier than the application and received HTTP 422. A backend reproduction confirmed the rejection.

The corrected inputs retain seconds and explicitly represent Beijing time. An untouched action time is submitted using the current complete timestamp, avoiding rounding it backward. The focused test verifies Beijing input conversion; the unchanged backend chronology checks still reject genuinely earlier events.

## Other verified boundaries

- A populated three-store demo was exercised through the real logistics router as the single-store warehouse user. Shipment, return, and exception lists contained only that store; foreign shipment details and mutations, return creation/transition, and exception resolution returned 404. Explicit foreign-store list, assignee, Agent run, and brief requests returned 403. A foreign tracking-number question returned no citations, and patrol scanned only the 12 visible shipments.
- Terminal timestamp constraints now explicitly require non-null delivery, receipt, and closure dates. The original SQL `CHECK` NULL issue was independently corrected by the backend author during review. Its focused regression and append-only event update/delete checks pass.
- Shipment and return inputs normalize aware datetimes to UTC; actual event chronology and terminal transitions are checked. Forecasts remain separate from actual dispatch, delivery, and return receipt. Commerce order facts are not rewritten by these operations.
- Patrol deduplication uses a database unique key and conflict-safe insert; the event is appended only for the winning insertion. Shipment/return/task mutations retain version conflict handling. Worker failures close their session and retry with a fresh session.
- Seed records use deterministic IDs and do not refresh existing logistics state or rewrite commerce orders. The launcher uses a dedicated persistent SQLite database, retains its signing key, and binds to loopback.
- Migration 0008 was applied to an isolated in-memory SQLite database. Its columns, nullability, and named `CHECK` expressions matched current logistics metadata; empty downgrade succeeded. The migration creates append-only triggers and refuses downgrade when facts exist.

## Fresh verification

```powershell
& 'D:\E-commerce_operations_env\python.exe' -m pytest tests/test_logistics.py -q -p no:cacheprovider
# 12 passed in 2.90s

npm.cmd --prefix frontend test -- src/logistics.test.ts
# 1 test file, 3 tests passed
```

Additional isolated probes passed for populated cross-store API isolation and migration/schema parity. The previously reported 1087-test baseline was not rerun. This reviewer did not run live PostgreSQL lock contention or the complete browser flow; those results must come from their respective integration checks.
