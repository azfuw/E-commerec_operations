from __future__ import annotations

from sqlalchemy import select

from backend.config import Settings
from backend.models import PlatformDelivery, PublishRecord
from backend.platform_client import CommercePlatformClient, PlatformClientError
from backend.platform_delivery_runs import (
    claim_next_platform_delivery,
    complete_platform_delivery,
    fail_platform_delivery,
    return_platform_delivery_for_retry,
)


class PlatformDeliveryWorkerError(Exception):
    def __init__(self) -> None:
        super().__init__("PLATFORM_DELIVERY_FAILED")


def _publish_payload(record: PublishRecord) -> dict[str, object]:
    snapshot = record.after_snapshot
    if not isinstance(snapshot, dict):
        raise ValueError("invalid publish snapshot")
    return {
        "title": snapshot["title"],
        "selling_points": snapshot["selling_points"],
        "description": snapshot["description"],
        "keywords": snapshot["search_keywords"],
        "attribute_completions": snapshot["attributes"],
    }


async def _load_publish_request(
    session_factory, delivery: PlatformDelivery
) -> tuple[str, str, dict[str, object], str]:
    async with session_factory() as session:
        record = await session.scalar(
            select(PublishRecord).where(
                PublishRecord.id == delivery.publish_record_id
            )
        )
        if record is None or record.store_id != delivery.store_id:
            raise ValueError("invalid platform delivery linkage")
        request = (
            record.store_id,
            record.product_id,
            _publish_payload(record),
            record.publish_idempotency_hash,
        )
        await session.commit()
        return request


async def _apply_transition(session_factory, transition, **kwargs: object) -> bool:
    failed = False
    changed = False
    try:
        async with session_factory() as session:
            changed = await transition(session, **kwargs)
    except Exception:
        failed = True
    if failed:
        raise PlatformDeliveryWorkerError()
    return changed


async def run_once(
    session_factory,
    *,
    settings: Settings,
    lease_owner: str,
    client: CommercePlatformClient,
) -> str | None:
    claim_failed = False
    delivery = None
    try:
        async with session_factory() as session:
            delivery = await claim_next_platform_delivery(
                session,
                lease_owner=lease_owner,
                lease_seconds=settings.platform_delivery_lease_seconds,
            )
    except Exception:
        claim_failed = True
    if claim_failed:
        raise PlatformDeliveryWorkerError()
    if delivery is None:
        return None
    delivery_id = delivery.id

    client_error: tuple[str, bool, int | None] | None = None
    unexpected = False
    try:
        store_id, product_id, payload, idempotency_key = await _load_publish_request(
            session_factory, delivery
        )
        result = await client.publish_listing(
            store_id=store_id,
            product_id=product_id,
            payload=payload,
            idempotency_key=idempotency_key,
        )
    except PlatformClientError as error:
        client_error = (error.code, error.retryable, error.retry_after_seconds)
    except Exception:
        unexpected = True

    if client_error is not None:
        error_code, retryable, retry_after_seconds = client_error
        if retryable and delivery.attempt_count < 3:
            await _apply_transition(
                session_factory,
                return_platform_delivery_for_retry,
                delivery_id=delivery_id,
                lease_owner=lease_owner,
                error_code=error_code,
                retry_after_seconds=retry_after_seconds,
            )
        else:
            await _apply_transition(
                session_factory,
                fail_platform_delivery,
                delivery_id=delivery_id,
                lease_owner=lease_owner,
                error_code=error_code,
            )
    elif unexpected:
        await _apply_transition(
            session_factory,
            fail_platform_delivery,
            delivery_id=delivery_id,
            lease_owner=lease_owner,
            error_code="PLATFORM_DELIVERY_FAILED",
        )
    else:
        await _apply_transition(
            session_factory,
            complete_platform_delivery,
            delivery_id=delivery_id,
            lease_owner=lease_owner,
            external_operation_id=result.external_operation_id,
        )
    return delivery_id
