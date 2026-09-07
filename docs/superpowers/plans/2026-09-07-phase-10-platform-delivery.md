# Phase 10 Durable Platform Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a durable, observable, idempotent platform-delivery boundary backed by PostgreSQL and exercised against a local contract simulator.

**Architecture:** The existing approval transaction keeps its local `PublishRecord` semantics and additionally enqueues one `PlatformDelivery`. A leased worker performs the HTTP side effect after commit through one concrete httpx client; the local simulator proves OAuth, retries, idempotency and Webhook behavior without claiming a real commerce-platform integration.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy async, PostgreSQL 16, httpx, Alembic, pytest, Vue 3, TypeScript, Vitest.

**Spec:** `docs/superpowers/specs/2026-09-07-phase-10-production-validation-design.md`

## Global Constraints

- Execute sequentially in the existing `电商项目开发窗口` with `superpowers:executing-plans`; do not dispatch subagents.
- Start from `master` containing spec commit `6de0397`; create an isolated `codex/phase10-platform-delivery` worktree before implementation.
- Create exactly one migration, `0007_phase10_production_validation.py`, after `0006`; later Phase 10 plans create no migration.
- Add no runtime or frontend dependency, microservice, message queue, cache, provider factory or browser-triggered fault control.
- Keep existing local approval completion and `simulated_published` semantics compatible; platform delivery is a separate eventual side effect.
- Platform writes contain only title, selling points, description, keywords and attribute completions. Price, SKU, inventory and order data remain read-only.
- Do not contact a real commerce platform or DeepSeek in this plan. All HTTP uses `httpx.ASGITransport` or a loopback simulator.
- Secrets are accepted only from environment-backed `SecretStr` settings and never appear in Git, exceptions, API responses, audit details or test output.
- Do not read, modify or stage `docs/business-and-technical-guide.md`, `docs/interview-q-and-a.md`, or the user-owned untracked `docs/architecture/` directory.
- Do not modify or clean personal C-drive files outside the isolated worktree. Put temporary state and caches on `D:`.
- Before every ordinary gate clear `RUN_POSTGRES_INTEGRATION`, `RUN_KNOWLEDGE_INTEGRATION`, `RUN_DEEPSEEK_SMOKE`, `RUN_TASK10_DEEPSEEK_SMOKE`, `RUN_PHASE9_EVALUATION_WRITE`, `RUN_PHASE10_DEEPSEEK`, and `DEEPSEEK_API_KEY`.
- Only the PostgreSQL step sets `RUN_POSTGRES_INTEGRATION=1`, and it clears the variable immediately afterward.

---

### Task 1: Phase 10 persistence and audit schema

**Files:**
- Create: `alembic/versions/0007_phase10_production_validation.py`
- Create: `tests/test_phase10_models.py`
- Create: `tests/test_phase10_postgres.py`
- Modify: `backend/common.py`
- Modify: `backend/models.py`
- Modify: `backend/audit_events.py`
- Modify: `backend/schemas.py`

**Interfaces:**
- Produces: `PlatformDeliveryStatus(PENDING, PROCESSING, SUCCEEDED, FAILED)`.
- Produces: `PlatformDelivery` and `PlatformWebhookReceipt` SQLAlchemy models.
- Produces audit event types `PLATFORM_DELIVERY_ENQUEUED`, `PLATFORM_DELIVERY_COMPLETED`, `PLATFORM_DELIVERY_FAILED`, and `PLATFORM_WEBHOOK_RECEIVED`.
- Produces audit resource type `platform_delivery` and detail keys `platform_delivery_status`, `attempt_count`, and `provider`.
- Produces: `PlatformDeliveryView` and nullable `PublishRecordView.platform_delivery`.

- [ ] **Step 1: Write failing model-shape tests**

```python
# tests/test_phase10_models.py
from backend.common import AuditEventType, PlatformDeliveryStatus
from backend.models import PlatformDelivery, PlatformWebhookReceipt


def _constraint_names(model) -> set[str]:
    return {constraint.name for constraint in model.__table__.constraints if constraint.name}


def test_platform_delivery_model_closes_state_and_identity() -> None:
    assert set(PlatformDeliveryStatus) == {
        PlatformDeliveryStatus.PENDING,
        PlatformDeliveryStatus.PROCESSING,
        PlatformDeliveryStatus.SUCCEEDED,
        PlatformDeliveryStatus.FAILED,
    }
    assert {
        "uq_platform_deliveries_publish_record_id",
        "ck_platform_deliveries_status",
        "ck_platform_deliveries_attempt_count",
        "ck_platform_deliveries_lease",
        "ck_platform_deliveries_terminal",
    } <= _constraint_names(PlatformDelivery)
    assert {index.name for index in PlatformDelivery.__table__.indexes} >= {
        "ix_platform_deliveries_claim",
        "ix_platform_deliveries_store_created",
    }


def test_webhook_receipt_and_audit_contract_are_closed() -> None:
    assert "uq_platform_webhook_receipts_event_id" in _constraint_names(
        PlatformWebhookReceipt
    )
    assert {
        AuditEventType.PLATFORM_DELIVERY_ENQUEUED,
        AuditEventType.PLATFORM_DELIVERY_COMPLETED,
        AuditEventType.PLATFORM_DELIVERY_FAILED,
        AuditEventType.PLATFORM_WEBHOOK_RECEIVED,
    } <= set(AuditEventType)
```

- [ ] **Step 2: Run the model test and verify RED**

Run:

```powershell
D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase10_models.py -q
```

Expected: collection fails because the Phase 10 enum and models do not exist.

- [ ] **Step 3: Add the closed enum, models and safe response types**

Add to `backend/common.py`:

```python
class PlatformDeliveryStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
```

Add the four audit enum members listed in Interfaces. In `backend/models.py`, implement these exact responsibilities:

```python
class PlatformDelivery(Base):
    __tablename__ = "platform_deliveries"
    # One row per PublishRecord; provider is contract_simulator in Phase 10.
    # attempt_count is 0..3. Only processing rows have both lease fields.
    # succeeded requires external_operation_id and completed_at.
    # failed requires completed_at and a safe error_code.


class PlatformWebhookReceipt(Base):
    __tablename__ = "platform_webhook_receipts"
    # event_id is globally unique; event_type is exactly publish.confirmed.
    # payload_digest is a 64-character SHA-256 hex string; no raw body is stored.
```

Use named SQL constraints from Step 1. Use `DateTime(timezone=True)`, `String(36)` IDs, `provider String(32)`, `external_operation_id String(128)`, `error_code String(64)`, and `payload_digest String(64)`. Add a delivery foreign key to `PublishRecord` and a receipt foreign key to `PlatformDelivery` without ORM relationships, matching the current model style.

Add to `backend/schemas.py`:

```python
class PlatformDeliveryView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    status: PlatformDeliveryStatus
    attempt_count: int
    external_operation_id: str | None
    error_code: str | None
    completed_at: datetime | None


class PublishRecordView(BaseModel):
    # existing fields stay unchanged
    platform_delivery: PlatformDeliveryView | None = None
```

- [ ] **Step 4: Extend the audit allowlists without accepting arbitrary metadata**

Update `AUDIT_DETAIL_KEYS`, `_RESOURCE_TYPES`, `_safe_details()` and `AuditEventView.resource_type` so only these additions are accepted:

```python
"platform_delivery_status": {item.value for item in PlatformDeliveryStatus}
"provider": {"contract_simulator"}
"attempt_count": non_negative_int
"platform_delivery": valid_resource_type
```

Do not allow external operation IDs, event IDs, response fragments, URLs, Token metadata or retry headers in audit details.

- [ ] **Step 5: Write the migration and PostgreSQL RED tests**

`0007_phase10_production_validation.py` must:

1. replace the audit event and resource check constraints with the Phase 10 closed values;
2. create `platform_deliveries` before `platform_webhook_receipts`;
3. create the two indexes named in Step 1;
4. guard downgrade when either new table contains a row or a Phase 10 audit event exists;
5. drop child receipts before deliveries on a safe downgrade;
6. restore the exact `0006` audit constraints.

Add real PostgreSQL assertions:

```python
@pytest.mark.postgres_integration
async def test_phase10_migration_enforces_delivery_and_webhook_constraints():
    # Upgrade 0006 -> 0007 in a UUID-named temporary schema.
    # Assert both tables, FKs, unique constraints and indexes exist.
    # INSERT invalid status, attempt_count=4, partial lease, succeeded without
    # external_operation_id, duplicate publish_record_id and duplicate event_id;
    # every INSERT must raise IntegrityError inside its own nested transaction.
    # Assert downgrade succeeds while empty and refuses while any Phase 10 fact exists.
```

- [ ] **Step 6: Run model and PostgreSQL GREEN**

Run:

```powershell
D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase10_models.py -q
$env:RUN_POSTGRES_INTEGRATION='1'
try {
  D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase10_postgres.py -q --tb=short
} finally {
  Remove-Item Env:RUN_POSTGRES_INTEGRATION -ErrorAction SilentlyContinue
}
```

Expected: both commands pass; the PostgreSQL test cleans only its UUID-named schema.

- [ ] **Step 7: Commit Task 1**

```powershell
git add backend/common.py backend/models.py backend/audit_events.py backend/schemas.py alembic/versions/0007_phase10_production_validation.py tests/test_phase10_models.py tests/test_phase10_postgres.py
git diff --cached --check
git commit -m "feat: add durable platform delivery schema"
```

---

### Task 2: Atomic approval enqueue and safe reads

**Files:**
- Modify: `backend/approvals.py`
- Modify: `backend/proposals.py`
- Modify: `backend/routes.py`
- Modify: `backend/schemas.py`
- Modify: `tests/test_approval_api.py`
- Modify: `tests/test_auth_and_scope.py`
- Modify: `tests/test_manual_review_models.py`
- Modify: `tests/test_phase9_models.py`

**Interfaces:**
- Changes: `ApprovalPublishResult.platform_delivery: PlatformDelivery | None`.
- Changes: `ProposalReadResult.platform_delivery: PlatformDelivery | None`.
- Produces: `_publish_record_view(record, delivery) -> PublishRecordView` in `backend/routes.py` for the approve and proposal-detail routes.
- Preserves: old `PublishRecord` rows return `platform_delivery=null` and are not backfilled.

- [ ] **Step 1: Add failing atomicity and replay tests**

Extend the existing approval fixture instead of creating a second approval setup:

```python
async def test_approve_enqueues_exactly_one_platform_delivery_and_replays_it(
    client, session, manual_route_data
):
    # Reuse the existing helper that prepares a publishable submitted revision.
    first = await client.post(
        "/approvals/proposal-1/approve",
        json={"revision_id": "revision-1"},
        headers=_headers(manual_route_data["supervisor"], "phase10-approve"),
    )
    replay = await client.post(
        "/approvals/proposal-1/approve",
        json={"revision_id": "revision-1"},
        headers=_headers(manual_route_data["supervisor"], "phase10-approve"),
    )
    assert first.status_code == 201 and replay.status_code == 200
    assert first.json()["platform_delivery"]["status"] == "pending"
    assert replay.json() == first.json()
    assert await session.scalar(select(func.count(PlatformDelivery.id))) == 1
```

Add a failure-injection parameter at each flush boundary after the delivery and enqueue audit are added; assert zero `ApprovalAction`, `PublishRecord`, `PlatformDelivery` and Phase 10 audit rows survive rollback.

Add a historical compatibility test that inserts an old publish record without a delivery and asserts proposal detail returns `"platform_delivery": null`.

- [ ] **Step 2: Run focused approval tests and verify RED**

Run:

```powershell
D:\E-commerce_operations_env\python.exe -m pytest tests/test_approval_api.py -k "platform_delivery or publish" -q --tb=short
```

Expected: new tests fail because approval does not create or return a delivery.

- [ ] **Step 3: Enqueue inside `_first_publish()`**

Extend `ApprovalPublishResult` with a nullable delivery. After the `PublishRecord` is flushed, create exactly one pending row:

```python
delivery = PlatformDelivery(
    id=str(uuid4()),
    publish_record_id=record.id,
    store_id=context.store.id,
    provider="contract_simulator",
    status=PlatformDeliveryStatus.PENDING,
    attempt_count=0,
)
session.add(delivery)
await session.flush()
add_audit_event(
    session,
    event_type=AuditEventType.PLATFORM_DELIVERY_ENQUEUED,
    outcome=AuditOutcome.SUCCESS,
    store_id=context.store.id,
    actor_id=context.actor.id,
    actor_role=context.actor.role,
    proposal_id=context.proposal.id,
    proposal_revision_id=context.revision.id,
    workflow_run_id=context.run.id,
    approval_action_id=action.id,
    publish_record_id=record.id,
    request_id=request_id,
    resource_type="platform_delivery",
    resource_id=delivery.id,
    details={
        "provider": "contract_simulator",
        "platform_delivery_status": "pending",
        "attempt_count": 0,
    },
)
```

Do not call an HTTP client from `approvals.py`. Replay loads the unique delivery if present, verifies its store and publish IDs, and returns it; historical rows may legitimately have none.

- [ ] **Step 4: Load and render the delivery through existing read paths**

`get_proposal_for_actor()` loads at most one delivery by `publish_record_id` and validates its `store_id`. Add one route helper used by both existing response sites:

```python
def _publish_record_view(
    record: PublishRecord, delivery: PlatformDelivery | None
) -> PublishRecordView:
    return PublishRecordView(
        **PublishRecordView.model_validate(record).model_dump(),
        platform_delivery=(
            None if delivery is None else PlatformDeliveryView.model_validate(delivery)
        ),
    )
```

Adjust the base validation call so it does not supply `platform_delivery` twice. No new HTTP endpoint is added.

- [ ] **Step 5: Update exact legacy assertions**

Update only exact enum/resource/index sets that intentionally changed in `tests/test_manual_review_models.py` and `tests/test_phase9_models.py`. Do not weaken equality assertions into broad subset checks unless the underlying contract is explicitly extensible.

- [ ] **Step 6: Run approval and authorization GREEN**

Run:

```powershell
D:\E-commerce_operations_env\python.exe -m pytest tests/test_approval_api.py tests/test_auth_and_scope.py tests/test_manual_review_models.py tests/test_phase9_models.py -q --tb=short
```

Expected: all pass; operator approval remains forbidden, supervisor/admin rules are unchanged, and replay creates no duplicate delivery.

- [ ] **Step 7: Commit Task 2**

```powershell
git add backend/approvals.py backend/proposals.py backend/routes.py backend/schemas.py tests/test_approval_api.py tests/test_auth_and_scope.py tests/test_manual_review_models.py tests/test_phase9_models.py
git diff --cached --check
git commit -m "feat: enqueue platform delivery after approval"
```

---

### Task 3: Contract client and local simulator

**Files:**
- Create: `backend/platform_client.py`
- Create: `tests/support/__init__.py`
- Create: `tests/support/platform_simulator.py`
- Create: `tests/test_platform_client.py`
- Create: `tests/test_platform_simulator.py`
- Modify: `backend/config.py`
- Modify: `.env.example`

**Interfaces:**
- Produces: `PlatformClientError(code: str, retryable: bool, retry_after_seconds: int | None)`.
- Produces: `PlatformPublishResult(external_operation_id: str)`.
- Produces: `CommercePlatformClient.publish_listing(*, store_id: str, payload: dict[str, object], idempotency_key: str) -> PlatformPublishResult`.
- Produces: `create_platform_simulator(*, fault: str | None = None) -> FastAPI` for tests and loopback E2E only.

- [ ] **Step 1: Write failing client contract tests**

```python
async def test_client_refreshes_once_and_reuses_the_idempotency_key():
    app = create_platform_simulator(fault="expire_first_token")
    client = CommercePlatformClient(settings(), transport=httpx.ASGITransport(app=app))
    result = await client.publish_listing(
        store_id="store-1",
        payload={"title": "已审批标题"},
        idempotency_key="a" * 64,
    )
    assert result.external_operation_id.startswith("operation-")
    assert app.state.token_requests == 2
    assert app.state.write_idempotency_keys == ["a" * 64, "a" * 64]


async def test_same_key_same_body_is_one_remote_side_effect_and_changed_body_conflicts():
    # Two identical calls return the same operation ID and one stored mutation.
    # A third call with the same key and another title raises PLATFORM_IDEMPOTENCY_CONFLICT.
```

Parameterize `401`, `429`, `500`, malformed JSON, timeout and connection failure. Assert only closed `PLATFORM_*` codes and retryability leave the client.

- [ ] **Step 2: Run client tests and verify RED**

```powershell
D:\E-commerce_operations_env\python.exe -m pytest tests/test_platform_client.py tests/test_platform_simulator.py -q
```

Expected: collection fails because the client and simulator do not exist.

- [ ] **Step 3: Add optional secure settings**

Add optional defaults so ordinary application import remains valid:

```python
platform_base_url: str | None = None
platform_client_id: SecretStr | None = None
platform_client_secret: SecretStr | None = None
platform_webhook_secret: SecretStr | None = None
platform_timeout_seconds: float = 10.0
platform_delivery_lease_seconds: int = 60
```

Add blank examples to `.env.example`. Never put test values or working credentials there.

- [ ] **Step 4: Implement the concrete client**

Use one `httpx.AsyncClient`, `base_url.rstrip('/')`, a finite timeout, and no logging of bodies. The algorithm is:

```python
token = await self._token()
response = await self._post_listing(token, store_id, payload, idempotency_key)
if response.status_code == 401:
    token = await self._token(force=True)
    response = await self._post_listing(token, store_id, payload, idempotency_key)
return self._validated_result(response)
```

Token acquisition and refresh may happen once per publish call. Validate `store_id`, the exact five-field payload, 64-character hex idempotency key, `external_operation_id` length, and response shape before returning. Parse `Retry-After` only as an integer in `0..60`; otherwise use `None`. Map transport, timeout, 429, 5xx, authentication, forbidden, conflict and schema errors to closed codes.

- [ ] **Step 5: Implement the deterministic simulator**

The test app exposes:

```text
POST /oauth/token
PUT  /v1/stores/{store_id}/listings/{product_id}
GET  /v1/stores/{store_id}/products?page&page_size
GET  /v1/stores/{store_id}/orders?page&page_size
GET  /v1/stores/{store_id}/inventory?page&page_size
GET  /health/live
```

The app stores only deterministic in-memory digests and operation IDs. It validates bearer tokens, allowed fields, page bounds and idempotency. Fault selection is constructor state or a startup environment value used only by the test support module; the product client must not send a fault header.

- [ ] **Step 6: Run client and simulator GREEN plus secret scan**

```powershell
D:\E-commerce_operations_env\python.exe -m pytest tests/test_platform_client.py tests/test_platform_simulator.py -q --tb=short
rg -n "platform_client_secret|Authorization|raw_response" backend/platform_client.py tests/test_platform_client.py tests/test_platform_simulator.py
```

Expected: tests pass. Matches are limited to safe field names/assertions; no literal credential or response content is emitted.

- [ ] **Step 7: Commit Task 3**

```powershell
git add .env.example backend/config.py backend/platform_client.py tests/support/__init__.py tests/support/platform_simulator.py tests/test_platform_client.py tests/test_platform_simulator.py
git diff --cached --check
git commit -m "feat: add commerce platform contract client"
```

---

### Task 4: Leased platform delivery worker

**Files:**
- Create: `backend/platform_delivery_runs.py`
- Create: `backend/platform_delivery_worker.py`
- Create: `scripts/run_platform_delivery_worker.py`
- Create: `tests/test_platform_delivery_runs.py`
- Create: `tests/test_platform_delivery_worker.py`
- Modify: `tests/test_phase10_postgres.py`

**Interfaces:**
- Produces: `claim_next_platform_delivery(session, *, lease_owner: str, lease_seconds: int) -> PlatformDelivery | None`.
- Produces: `complete_platform_delivery(session, *, delivery_id: str, lease_owner: str, external_operation_id: str) -> bool`.
- Produces: `return_platform_delivery_for_retry(session, *, delivery_id: str, lease_owner: str, error_code: str, retry_after_seconds: int | None) -> bool`.
- Produces: `fail_platform_delivery(session, *, delivery_id: str, lease_owner: str, error_code: str) -> bool`.
- Produces: `platform_delivery_worker.run_once(session_factory, *, settings, lease_owner, client) -> str | None`.

- [ ] **Step 1: Write failing lifecycle tests**

Cover these exact transitions:

```python
async def test_worker_success_claims_outside_approval_and_completes_once(
    platform_delivery_factory, delivery_settings, success_client
):
    delivery_id = await seed_pending_delivery(platform_delivery_factory)
    processed = await run_once(
        platform_delivery_factory,
        settings=delivery_settings,
        lease_owner="worker-1",
        client=success_client,
    )
    assert processed == delivery_id
    row = await load_delivery(platform_delivery_factory, delivery_id)
    assert row.status is PlatformDeliveryStatus.SUCCEEDED
    assert row.attempt_count == 1
    assert row.external_operation_id == "operation-1"


async def test_transient_failures_retry_three_total_attempts_then_fail(
    platform_delivery_factory, delivery_settings, transient_client
):
    delivery_id = await seed_pending_delivery(platform_delivery_factory)
    for expected_attempt in (1, 2, 3):
        await make_delivery_due(platform_delivery_factory, delivery_id)
        assert await run_once(
            platform_delivery_factory,
            settings=delivery_settings,
            lease_owner=f"worker-{expected_attempt}",
            client=transient_client,
        ) == delivery_id
    row = await load_delivery(platform_delivery_factory, delivery_id)
    assert (row.status, row.attempt_count, row.error_code) == (
        PlatformDeliveryStatus.FAILED,
        3,
        "PLATFORM_SERVER_ERROR",
    )


async def test_stale_owner_cannot_complete_or_fail_after_lease_reclaim(
    platform_delivery_factory
):
    delivery_id = await seed_expired_processing_delivery(
        platform_delivery_factory, lease_owner="worker-1"
    )
    async with platform_delivery_factory() as session:
        claimed = await claim_next_platform_delivery(
            session, lease_owner="worker-2", lease_seconds=60
        )
        assert claimed is not None and claimed.id == delivery_id
    async with platform_delivery_factory() as session:
        assert not await complete_platform_delivery(
            session,
            delivery_id=delivery_id,
            lease_owner="worker-1",
            external_operation_id="operation-stale",
        )
        assert not await fail_platform_delivery(
            session,
            delivery_id=delivery_id,
            lease_owner="worker-1",
            error_code="PLATFORM_SERVER_ERROR",
        )
```

Add a replay test where the simulator accepts the first request and the test raises a transport disconnect before local completion. The next worker call must receive the same operation ID and the simulator must contain one mutation.

- [ ] **Step 2: Run worker tests and verify RED**

```powershell
D:\E-commerce_operations_env\python.exe -m pytest tests/test_platform_delivery_runs.py tests/test_platform_delivery_worker.py -q
```

Expected: collection fails because the run and worker modules do not exist.

- [ ] **Step 3: Implement claim and owner-guarded transitions**

Follow the existing knowledge/analysis lease pattern:

```python
select(PlatformDelivery).where(
    PlatformDelivery.status == PlatformDeliveryStatus.PENDING,
    or_(PlatformDelivery.next_attempt_at.is_(None), PlatformDelivery.next_attempt_at <= func.now()),
).order_by(PlatformDelivery.created_at, PlatformDelivery.id).with_for_update(skip_locked=True)
```

Expired `processing` rows are eligible for reclaim. Claim increments `attempt_count`, sets both lease fields and commits. Every terminal/retry update filters by `id`, `status=processing`, `lease_owner` and an unexpired lease using database time. Retry clears the lease and uses a deterministic capped delay; terminal writes set `completed_at` and clear the lease.

Successful and final failed transitions append one audit event with the delivery resource. Transient attempts do not append audit rows.

- [ ] **Step 4: Implement `run_once()` and the CLI**

`run_once()` must:

1. claim and commit;
2. load the linked immutable publish record;
3. construct only the five allowed listing fields;
4. derive the outbound idempotency key from `PublishRecord.publish_idempotency_hash`;
5. call the client outside a database transaction;
6. complete, retry or fail through a fresh session;
7. convert unexpected exceptions to `PLATFORM_DELIVERY_FAILED` without leaking their text.

`scripts/run_platform_delivery_worker.py` mirrors existing worker CLIs, supports `--once`, derives `lease_owner` from hostname/PID, and refuses to start unless `RUN_PHASE10_PLATFORM_DELIVERY=1` and all four platform settings are present. It never prints setting values.

- [ ] **Step 5: Add PostgreSQL concurrency proof**

Use two independent connections/tasks against UUID-scoped facts. Assert `SKIP LOCKED` gives one delivery to one worker, expired leases are reclaimable, and stale-owner completion changes zero rows. Do not use SQLite as evidence for concurrency.

- [ ] **Step 6: Run worker GREEN**

```powershell
D:\E-commerce_operations_env\python.exe -m pytest tests/test_platform_delivery_runs.py tests/test_platform_delivery_worker.py -q --tb=short
$env:RUN_POSTGRES_INTEGRATION='1'
try {
  D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase10_postgres.py -q --tb=short
} finally {
  Remove-Item Env:RUN_POSTGRES_INTEGRATION -ErrorAction SilentlyContinue
}
```

Expected: lifecycle and real PostgreSQL concurrency tests pass.

- [ ] **Step 7: Commit Task 4**

```powershell
git add backend/platform_delivery_runs.py backend/platform_delivery_worker.py scripts/run_platform_delivery_worker.py tests/test_platform_delivery_runs.py tests/test_platform_delivery_worker.py tests/test_phase10_postgres.py
git diff --cached --check
git commit -m "feat: deliver approved listings with leased worker"
```

---

### Task 5: Signed idempotent Webhook receipt

**Files:**
- Create: `backend/platform_webhooks.py`
- Create: `tests/test_platform_webhooks.py`
- Modify: `backend/routes.py`
- Modify: `backend/schemas.py`
- Modify: `tests/test_phase10_postgres.py`

**Interfaces:**
- Produces: `PlatformWebhookPayload(event_type: Literal['publish.confirmed'], delivery_id: str, external_operation_id: str)` with `extra='forbid'`.
- Produces: `receive_platform_webhook(session, *, body: bytes, event_id: str, timestamp: str, signature: str, secret: str) -> bool`, where the boolean is `created`.
- Produces: `POST /integrations/platform/webhooks`, returning 201 for a new receipt and 200 for an exact duplicate.

- [ ] **Step 1: Write failing signature and replay tests**

```python
def sign(secret: bytes, timestamp: str, event_id: str, body: bytes) -> str:
    signed = timestamp.encode() + b"." + event_id.encode() + b"." + body
    return hmac.new(secret, signed, hashlib.sha256).hexdigest()


async def test_valid_webhook_is_recorded_once_and_duplicate_is_idempotent(
    webhook_client, webhook_session
):
    first = await post_signed(event_id="event-1")
    duplicate = await post_signed(event_id="event-1")
    assert first.status_code == 201 and duplicate.status_code == 200
    assert await count_rows(webhook_session, PlatformWebhookReceipt) == 1
    assert await count_audits(
        webhook_session, AuditEventType.PLATFORM_WEBHOOK_RECEIVED
    ) == 1



async def test_signature_authenticates_event_id(webhook_client, webhook_session):
    timestamp = str(int(datetime.now(UTC).timestamp()))
    body = valid_webhook_body()
    signature = sign(TEST_WEBHOOK_SECRET, timestamp, "event-1", body)
    first = await post_signed(
        event_id="event-1", timestamp=timestamp, body=body, signature=signature
    )
    changed_event = await post_signed(
        event_id="event-2", timestamp=timestamp, body=body, signature=signature
    )
    assert first.status_code == 201 and changed_event.status_code == 401
    assert await count_rows(webhook_session, PlatformWebhookReceipt) == 1


@pytest.mark.parametrize("fault", ["bad_signature", "stale_timestamp", "changed_duplicate"])
async def test_invalid_webhook_writes_nothing(
    fault, webhook_client, webhook_session
):
    response = await post_fault(webhook_client, fault=fault, event_id="event-invalid")
    assert response.status_code in {401, 409, 422}
    assert await count_rows(webhook_session, PlatformWebhookReceipt) == 0
    assert await count_audits(
        webhook_session, AuditEventType.PLATFORM_WEBHOOK_RECEIVED
    ) == 0
```

The changed-duplicate case uses the same event ID with a different body and must return `PLATFORM_WEBHOOK_REPLAY_CONFLICT` with no second receipt or audit.

- [ ] **Step 2: Run Webhook tests and verify RED**

```powershell
D:\E-commerce_operations_env\python.exe -m pytest tests/test_platform_webhooks.py -q
```

Expected: tests fail because the endpoint and receipt service do not exist.

- [ ] **Step 3: Implement strict verification and receipt persistence**

Verify before JSON parsing:

```python
expected = hmac.new(
    secret.encode("utf-8"),
    timestamp.encode("ascii") + b"." + event_id.encode("ascii") + b"." + body,
    hashlib.sha256,
).hexdigest()
if not hmac.compare_digest(expected, signature):
    raise PlatformWebhookError("PLATFORM_WEBHOOK_SIGNATURE_INVALID", 401)
```

Require an integer Unix timestamp within plus/minus five minutes of UTC database/application time, event ID length `1..128` using the safe ASCII segment grammar `[A-Za-z0-9_-]+`, lowercase 64-character hex signature, content type JSON, exact schema and matching delivery/external operation ID. The signed representation is the exact bytes `timestamp + b"." + event_id + b"." + raw_body`; changing only the event ID invalidates the signature and writes nothing. Persist only the body SHA-256 digest. Resolve unique-event races by rollback and exact digest/identity comparison; an exact replay returns `created=False`, while a changed replay returns 409.

The accepted audit uses `resource_type='platform_delivery'`, the delivery ID and safe closed details. Bad signatures and unknown operations do not create audits because no trusted store context exists.

- [ ] **Step 4: Add the route without generic exception leakage**

Read `await request.body()` with a small explicit maximum before passing bytes to the service. Map only `PlatformWebhookError.code/status_code`; map unexpected database failure to `PLATFORM_WEBHOOK_UNAVAILABLE` with 503. Do not echo request headers, event IDs or bodies.

- [ ] **Step 5: Run Webhook and PostgreSQL GREEN**

```powershell
D:\E-commerce_operations_env\python.exe -m pytest tests/test_platform_webhooks.py -q --tb=short
$env:RUN_POSTGRES_INTEGRATION='1'
try {
  D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase10_postgres.py -q --tb=short
} finally {
  Remove-Item Env:RUN_POSTGRES_INTEGRATION -ErrorAction SilentlyContinue
}
```

Expected: unit/API tests and real unique-race proof pass.

- [ ] **Step 6: Commit Task 5**

```powershell
git add backend/platform_webhooks.py backend/routes.py backend/schemas.py tests/test_platform_webhooks.py tests/test_phase10_postgres.py
git diff --cached --check
git commit -m "feat: verify platform delivery webhooks"
```

---

### Task 6: Platform delivery status in the proposal UI

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/pages/ProposalPage.vue`
- Modify: `frontend/src/pages/ProposalPage.test.ts`
- Modify: `frontend/tests/apiFixture.ts`
- Modify: `frontend/tests/core-flow.spec.ts`

**Interfaces:**
- Produces: TypeScript `PlatformDelivery` with the five safe response fields.
- Changes: `PublishRecord.platform_delivery: PlatformDelivery | null`.
- Produces no new frontend API call or write action.

- [ ] **Step 1: Write failing UI tests**

Add fixture variants for `pending`, `processing`, `succeeded`, `failed` and historical `null`:

```typescript
expect(wrapper.get('[data-test="platform-delivery"]').text()).toContain('平台投递成功')
expect(wrapper.get('[data-test="platform-delivery"]').text()).toContain('operation-1')
expect(wrapper.get('[data-test="platform-delivery-error"]').text()).toContain('平台暂时不可用')
expect(wrapper.find('[data-test="platform-retry"]').exists()).toBe(false)
```

Assert Token, URL, headers and raw errors never render. Preserve the current mobile supervisor approval test.

- [ ] **Step 2: Run the component test and verify RED**

```powershell
npm --prefix frontend test -- --run src/pages/ProposalPage.test.ts
```

Expected: new status selectors do not exist.

- [ ] **Step 3: Add the safe nested type and status presentation**

Use a closed label map:

```typescript
const platformStatusText: Record<PlatformDelivery['status'], string> = {
  pending: '等待平台投递',
  processing: '正在投递平台',
  succeeded: '平台投递成功',
  failed: '平台投递失败',
}
```

Render the summary inside the existing publish section. Show attempt count, optional external operation ID, safe error copy and completion time. Keep the local snapshot heading and explicitly state that the target is the contract simulator. Do not add retry/configuration buttons.

- [ ] **Step 4: Update fixture browser expectations**

The existing fixture core flow returns a succeeded delivery nested in the publish record and asserts the new text. It remains named a fixture browser flow, not a real-service E2E.

- [ ] **Step 5: Run frontend GREEN**

```powershell
npm --prefix frontend test -- --run
npm --prefix frontend run typecheck
npm --prefix frontend run build
npm --prefix frontend run test:e2e
```

Expected: all existing and new tests pass; the known bundle-size warning is non-blocking unless size materially regresses.

- [ ] **Step 6: Commit Task 6**

```powershell
git add frontend/src/types.ts frontend/src/pages/ProposalPage.vue frontend/src/pages/ProposalPage.test.ts frontend/tests/apiFixture.ts frontend/tests/core-flow.spec.ts
git diff --cached --check
git commit -m "feat: show platform delivery evidence"
```

---

### Task 7: Plan 1 integration gate

**Files:**
- Modify only if a failing gate exposes a defect in Plan 1 files.

**Interfaces:**
- Verifies all Plan 1 interfaces together without DeepSeek or real external network.

- [ ] **Step 1: Clear every external/integration opt-in**

```powershell
Remove-Item Env:RUN_POSTGRES_INTEGRATION -ErrorAction SilentlyContinue
Remove-Item Env:RUN_KNOWLEDGE_INTEGRATION -ErrorAction SilentlyContinue
Remove-Item Env:RUN_DEEPSEEK_SMOKE -ErrorAction SilentlyContinue
Remove-Item Env:RUN_TASK10_DEEPSEEK_SMOKE -ErrorAction SilentlyContinue
Remove-Item Env:RUN_PHASE9_EVALUATION_WRITE -ErrorAction SilentlyContinue
Remove-Item Env:RUN_PHASE10_DEEPSEEK -ErrorAction SilentlyContinue
Remove-Item Env:RUN_PHASE10_PLATFORM_DELIVERY -ErrorAction SilentlyContinue
Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
```

- [ ] **Step 2: Run all ordinary backend tests**

```powershell
D:\E-commerce_operations_env\python.exe -m pytest -q --tb=short
```

Expected: all ordinary tests pass and every real integration test is skipped by its explicit marker/guard.

- [ ] **Step 3: Run the complete PostgreSQL gate**

```powershell
$env:RUN_POSTGRES_INTEGRATION='1'
try {
  D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase10_postgres.py tests/test_phase9_postgres.py tests/test_analysis_postgres.py tests/test_optimization_postgres.py tests/test_manual_review_postgres.py -q --tb=short
} finally {
  Remove-Item Env:RUN_POSTGRES_INTEGRATION -ErrorAction SilentlyContinue
}
```

Expected: all migration, transaction, lease, concurrency and compatibility proofs pass.

- [ ] **Step 4: Run build and repository checks**

```powershell
$env:JWT_SECRET_KEY='phase10-local-check-only-at-least-32-characters'
try {
  D:\E-commerce_operations_env\python.exe -m compileall -q backend scripts alembic
  D:\E-commerce_operations_env\python.exe -m alembic current
  D:\E-commerce_operations_env\python.exe -m alembic check
} finally {
  Remove-Item Env:JWT_SECRET_KEY -ErrorAction SilentlyContinue
}
npm --prefix frontend run typecheck
npm --prefix frontend run build
docker compose config --quiet
git diff --check
git status --short
```

Expected: Alembic reports `0007 (head)` and no new upgrade operations; the isolated worktree is clean except for intentional uncommitted verification fixes, which must be committed before review.

- [ ] **Step 5: Commit any gate-only correction and stop for independent review**

If no correction was needed, create no empty commit. Report commit IDs, exact test counts, skipped markers, PostgreSQL evidence and safe known warnings. Do not start Plan 2 before the review window approves Plan 1.
