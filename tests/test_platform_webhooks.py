import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from backend.common import AuditEventType, AuditOutcome, PlatformDeliveryStatus
from backend.config import Settings, get_settings
from backend.database import Base
from backend.database import get_session
from backend.main import create_app
from backend.models import AuditEvent, PlatformDelivery, PlatformWebhookReceipt
from backend.platform_webhooks import PlatformWebhookError, receive_platform_webhook


SECRET = "webhook-test-secret"


def _body(*, operation_id: str = "operation-1") -> bytes:
    return json.dumps(
        {
            "event_type": "publish.confirmed",
            "delivery_id": "delivery-1",
            "external_operation_id": operation_id,
        },
        separators=(",", ":"),
    ).encode()


def _timestamp() -> str:
    return str(int(datetime.now(UTC).timestamp()))


def _signature(timestamp: str, body: bytes) -> str:
    return hmac.new(
        SECRET.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()


async def _receive(
    session: AsyncSession,
    *,
    body: bytes | None = None,
    event_id: str = "event-1",
    timestamp: str | None = None,
    signature: str | None = None,
) -> bool:
    raw = _body() if body is None else body
    sent_at = _timestamp() if timestamp is None else timestamp
    return await receive_platform_webhook(
        session,
        body=raw,
        event_id=event_id,
        timestamp=sent_at,
        signature=_signature(sent_at, raw) if signature is None else signature,
        secret=SECRET,
    )


@pytest_asyncio.fixture
async def webhook_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(
            PlatformDelivery(
                id="delivery-1",
                publish_record_id="publish-1",
                store_id="store-1",
                status=PlatformDeliveryStatus.SUCCEEDED,
                attempt_count=1,
                external_operation_id="operation-1",
                completed_at=datetime.now(UTC),
            )
        )
        await session.commit()
        yield session
        await session.rollback()
    await engine.dispose()


async def _count(session: AsyncSession, model: type) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


@pytest_asyncio.fixture
async def webhook_client(webhook_session: AsyncSession):
    app = create_app()

    async def override_session():
        yield webhook_session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(
        jwt_secret_key=SecretStr("unit-test-jwt"),
        platform_webhook_secret=SecretStr(SECRET),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def _post_webhook(
    client: AsyncClient,
    *,
    body: bytes | None = None,
    event_id: str = "event-1",
    content_type: str = "application/json; charset=utf-8",
    include_signature: bool = True,
):
    raw = _body() if body is None else body
    timestamp = _timestamp()
    headers = {
        "X-Event-ID": event_id,
        "X-Webhook-Timestamp": timestamp,
        "Content-Type": content_type,
    }
    if include_signature:
        headers["X-Webhook-Signature"] = _signature(timestamp, raw)
    return await client.post(
        "/integrations/platform/webhooks", content=raw, headers=headers
    )


async def test_valid_webhook_is_recorded_once_and_duplicate_is_idempotent(
    webhook_session: AsyncSession,
) -> None:
    assert await _receive(webhook_session) is True
    assert await _receive(webhook_session) is False
    assert await _count(webhook_session, PlatformWebhookReceipt) == 1
    assert await _count(webhook_session, AuditEvent) == 1

    receipt = await webhook_session.scalar(select(PlatformWebhookReceipt))
    audit = await webhook_session.scalar(select(AuditEvent))
    assert receipt is not None and audit is not None
    assert receipt.platform_delivery_id == "delivery-1"
    assert receipt.payload_digest == hashlib.sha256(_body()).hexdigest()
    assert audit.event_type is AuditEventType.PLATFORM_WEBHOOK_RECEIVED
    assert audit.outcome is AuditOutcome.SUCCESS
    assert (audit.store_id, audit.resource_type, audit.resource_id) == (
        "store-1",
        "platform_delivery",
        "delivery-1",
    )
    assert audit.details == {
        "provider": "contract_simulator",
        "platform_delivery_status": PlatformDeliveryStatus.SUCCEEDED.value,
        "attempt_count": 1,
    }


async def test_webhook_route_returns_created_then_idempotent_status(
    webhook_client: AsyncClient, webhook_session: AsyncSession
) -> None:
    first = await _post_webhook(webhook_client)
    duplicate = await _post_webhook(webhook_client)
    assert (first.status_code, duplicate.status_code) == (201, 200)
    assert await _count(webhook_session, PlatformWebhookReceipt) == 1
    assert await _count(webhook_session, AuditEvent) == 1


async def test_webhook_route_closes_conflict_and_untrusted_request_errors(
    webhook_client: AsyncClient, webhook_session: AsyncSession
) -> None:
    assert (await _post_webhook(webhook_client)).status_code == 201
    changed = await _post_webhook(
        webhook_client, body=_body(operation_id="operation-changed")
    )
    wrong_content = await _post_webhook(
        webhook_client,
        event_id="private-event-content",
        content_type="text/plain",
    )
    missing_signature = await _post_webhook(
        webhook_client,
        body=b'private-body-{not-json}',
        event_id="private-event-missing",
        include_signature=False,
    )
    oversized = await _post_webhook(
        webhook_client, body=b"x" * 4097, event_id="private-event-large"
    )

    assert (changed.status_code, changed.json()["detail"]["code"]) == (
        409,
        "PLATFORM_WEBHOOK_REPLAY_CONFLICT",
    )
    assert (wrong_content.status_code, wrong_content.json()["detail"]["code"]) == (
        415,
        "PLATFORM_WEBHOOK_CONTENT_TYPE_INVALID",
    )
    assert (
        missing_signature.status_code,
        missing_signature.json()["detail"]["code"],
    ) == (401, "PLATFORM_WEBHOOK_SIGNATURE_INVALID")
    assert (oversized.status_code, oversized.json()["detail"]["code"]) == (
        413,
        "PLATFORM_WEBHOOK_TOO_LARGE",
    )
    responses = "".join(
        response.text
        for response in (changed, wrong_content, missing_signature, oversized)
    )
    for private in ("private-event", "private-body", SECRET, "not-json"):
        assert private not in responses
    assert await _count(webhook_session, PlatformWebhookReceipt) == 1
    assert await _count(webhook_session, AuditEvent) == 1


async def test_bad_signature_is_rejected_before_json_parsing_and_writes_nothing(
    webhook_session: AsyncSession,
) -> None:
    with pytest.raises(PlatformWebhookError) as raised:
        await _receive(webhook_session, body=b"not-json", signature="0" * 64)
    assert (raised.value.code, raised.value.status_code) == (
        "PLATFORM_WEBHOOK_SIGNATURE_INVALID",
        401,
    )
    assert await _count(webhook_session, PlatformWebhookReceipt) == 0
    assert await _count(webhook_session, AuditEvent) == 0


async def test_stale_timestamp_and_unknown_operation_write_nothing(
    webhook_session: AsyncSession,
) -> None:
    stale = "1"
    with pytest.raises(PlatformWebhookError) as raised:
        await _receive(webhook_session, timestamp=stale)
    assert (raised.value.code, raised.value.status_code) == (
        "PLATFORM_WEBHOOK_TIMESTAMP_INVALID",
        401,
    )

    unknown = _body(operation_id="operation-missing")
    with pytest.raises(PlatformWebhookError) as raised:
        await _receive(webhook_session, body=unknown, event_id="event-unknown")
    assert (raised.value.code, raised.value.status_code) == (
        "PLATFORM_WEBHOOK_OPERATION_INVALID",
        422,
    )
    assert await _count(webhook_session, PlatformWebhookReceipt) == 0
    assert await _count(webhook_session, AuditEvent) == 0


async def test_changed_duplicate_conflicts_without_a_second_write(
    webhook_session: AsyncSession,
) -> None:
    assert await _receive(webhook_session) is True
    changed = _body(operation_id="operation-changed")
    with pytest.raises(PlatformWebhookError) as raised:
        await _receive(webhook_session, body=changed)
    assert (raised.value.code, raised.value.status_code) == (
        "PLATFORM_WEBHOOK_REPLAY_CONFLICT",
        409,
    )
    assert await _count(webhook_session, PlatformWebhookReceipt) == 1
    assert await _count(webhook_session, AuditEvent) == 1


@pytest.mark.parametrize(
    ("changes", "code", "status_code"),
    [
        ({"event_id": ""}, "PLATFORM_WEBHOOK_EVENT_INVALID", 422),
        ({"event_id": "e" * 129}, "PLATFORM_WEBHOOK_EVENT_INVALID", 422),
        ({"timestamp": "9" * 64}, "PLATFORM_WEBHOOK_TIMESTAMP_INVALID", 401),
        ({"signature": "A" * 64}, "PLATFORM_WEBHOOK_SIGNATURE_INVALID", 401),
        ({"body": b"x" * 4097}, "PLATFORM_WEBHOOK_TOO_LARGE", 413),
    ],
)
async def test_untrusted_inputs_are_bounded_before_persistence(
    webhook_session: AsyncSession,
    changes: dict[str, object],
    code: str,
    status_code: int,
) -> None:
    with pytest.raises(PlatformWebhookError) as raised:
        await _receive(webhook_session, **changes)
    assert (raised.value.code, raised.value.status_code, str(raised.value)) == (
        code,
        status_code,
        code,
    )
    assert raised.value.__cause__ is None
    assert await _count(webhook_session, PlatformWebhookReceipt) == 0
    assert await _count(webhook_session, AuditEvent) == 0
