import hashlib
import hmac
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.audit_events import add_audit_event
from backend.common import AuditEventType, AuditOutcome, PlatformDeliveryStatus
from backend.models import PlatformDelivery, PlatformWebhookReceipt
from backend.schemas import PlatformWebhookPayload


MAX_WEBHOOK_BODY_BYTES = 4096


class PlatformWebhookError(Exception):
    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _fail(code: str, status_code: int) -> PlatformWebhookError:
    return PlatformWebhookError(code, status_code)


def _validated_envelope(
    *, body: bytes, event_id: str, timestamp: str, signature: str, secret: str
) -> tuple[PlatformWebhookPayload, str]:
    if not isinstance(body, bytes) or len(body) > MAX_WEBHOOK_BODY_BYTES:
        raise _fail("PLATFORM_WEBHOOK_TOO_LARGE", 413)
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 128:
        raise _fail("PLATFORM_WEBHOOK_EVENT_INVALID", 422)
    if (
        not isinstance(timestamp, str)
        or not 1 <= len(timestamp) <= 10
        or not timestamp.isascii()
        or not timestamp.isdecimal()
    ):
        raise _fail("PLATFORM_WEBHOOK_TIMESTAMP_INVALID", 401)
    if (
        not isinstance(signature, str)
        or len(signature) != 64
        or any(char not in "0123456789abcdef" for char in signature)
    ):
        raise _fail("PLATFORM_WEBHOOK_SIGNATURE_INVALID", 401)
    if not isinstance(secret, str) or not secret:
        raise _fail("PLATFORM_WEBHOOK_UNAVAILABLE", 503)

    expected = hmac.new(
        secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise _fail("PLATFORM_WEBHOOK_SIGNATURE_INVALID", 401)
    if abs(int(timestamp) - int(datetime.now(UTC).timestamp())) > 300:
        raise _fail("PLATFORM_WEBHOOK_TIMESTAMP_INVALID", 401)

    invalid = False
    try:
        payload = PlatformWebhookPayload.model_validate_json(body)
    except (ValueError, ValidationError):
        invalid = True
    if invalid:
        raise _fail("PLATFORM_WEBHOOK_PAYLOAD_INVALID", 422)
    return payload, hashlib.sha256(body).hexdigest()


def _is_exact_replay(
    receipt: PlatformWebhookReceipt,
    *,
    payload: PlatformWebhookPayload,
    digest: str,
) -> bool:
    return (
        receipt.payload_digest == digest
        and receipt.platform_delivery_id == payload.delivery_id
        and receipt.event_type == payload.event_type
    )


async def _delivery(
    session: AsyncSession, payload: PlatformWebhookPayload
) -> PlatformDelivery | None:
    return await session.scalar(
        select(PlatformDelivery).where(
            PlatformDelivery.id == payload.delivery_id,
            PlatformDelivery.status == PlatformDeliveryStatus.SUCCEEDED,
            PlatformDelivery.external_operation_id == payload.external_operation_id,
        )
    )


async def receive_platform_webhook(
    session: AsyncSession,
    *,
    body: bytes,
    event_id: str,
    timestamp: str,
    signature: str,
    secret: str,
) -> bool:
    payload, digest = _validated_envelope(
        body=body,
        event_id=event_id,
        timestamp=timestamp,
        signature=signature,
        secret=secret,
    )
    existing = await session.scalar(
        select(PlatformWebhookReceipt).where(PlatformWebhookReceipt.event_id == event_id)
    )
    if existing is not None:
        if not _is_exact_replay(existing, payload=payload, digest=digest):
            raise _fail("PLATFORM_WEBHOOK_REPLAY_CONFLICT", 409)
        if await _delivery(session, payload) is None:
            raise _fail("PLATFORM_WEBHOOK_OPERATION_INVALID", 422)
        return False

    delivery = await _delivery(session, payload)
    if delivery is None:
        raise _fail("PLATFORM_WEBHOOK_OPERATION_INVALID", 422)

    session.add(
        PlatformWebhookReceipt(
            id=str(uuid4()),
            platform_delivery_id=delivery.id,
            event_id=event_id,
            event_type=payload.event_type,
            payload_digest=digest,
        )
    )
    add_audit_event(
        session,
        event_type=AuditEventType.PLATFORM_WEBHOOK_RECEIVED,
        outcome=AuditOutcome.SUCCESS,
        store_id=delivery.store_id,
        resource_type="platform_delivery",
        resource_id=delivery.id,
        details={
            "provider": delivery.provider,
            "platform_delivery_status": delivery.status.value,
            "attempt_count": delivery.attempt_count,
        },
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(PlatformWebhookReceipt).where(
                PlatformWebhookReceipt.event_id == event_id
            )
        )
        if existing is None:
            raise
        race_conflict = not _is_exact_replay(
            existing, payload=payload, digest=digest
        )
    else:
        return True
    if race_conflict:
        raise _fail("PLATFORM_WEBHOOK_REPLAY_CONFLICT", 409)
    return False
