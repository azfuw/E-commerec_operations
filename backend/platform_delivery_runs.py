from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import and_, func, literal, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from backend.audit_events import add_audit_event
from backend.common import AuditEventType, AuditOutcome, PlatformDeliveryStatus
from backend.models import PlatformDelivery, PublishRecord


_ERROR_CODES = frozenset(
    {
        "PLATFORM_AUTH_FAILED",
        "PLATFORM_FORBIDDEN",
        "PLATFORM_IDEMPOTENCY_CONFLICT",
        "PLATFORM_RATE_LIMITED",
        "PLATFORM_TIMEOUT",
        "PLATFORM_CONNECTION_FAILED",
        "PLATFORM_SERVER_ERROR",
        "PLATFORM_REQUEST_REJECTED",
        "PLATFORM_REQUEST_INVALID",
        "PLATFORM_RESPONSE_INVALID",
        "PLATFORM_SIGNATURE_INVALID",
        "PLATFORM_DELIVERY_FAILED",
    }
)


def _after_database_time(
    session: AsyncSession, seconds: int
) -> ColumnElement[datetime]:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        return func.now() + text("make_interval(secs => :seconds)").bindparams(
            seconds=seconds
        )
    return func.datetime(func.now(), literal(f"+{seconds} seconds"))


def _validate_owner(lease_owner: str) -> None:
    if not isinstance(lease_owner, str) or not 1 <= len(lease_owner) <= 128:
        raise ValueError("invalid lease owner")


def _validate_error(error_code: str) -> None:
    if error_code not in _ERROR_CODES:
        raise ValueError("invalid platform delivery error code")


def _add_terminal_audit(
    session: AsyncSession,
    delivery: PlatformDelivery,
    *,
    succeeded: bool,
) -> None:
    add_audit_event(
        session,
        event_type=(
            AuditEventType.PLATFORM_DELIVERY_COMPLETED
            if succeeded
            else AuditEventType.PLATFORM_DELIVERY_FAILED
        ),
        outcome=AuditOutcome.SUCCESS if succeeded else AuditOutcome.FAILED,
        store_id=delivery.store_id,
        publish_record_id=delivery.publish_record_id,
        error_code=delivery.error_code,
        resource_type="platform_delivery",
        resource_id=delivery.id,
        details={
            "provider": delivery.provider,
            "platform_delivery_status": delivery.status.value,
            "attempt_count": delivery.attempt_count,
        },
    )


def _mark_failed(
    session: AsyncSession, delivery: PlatformDelivery, error_code: str
) -> None:
    delivery.status = PlatformDeliveryStatus.FAILED
    delivery.next_attempt_at = None
    delivery.lease_owner = None
    delivery.lease_expires_at = None
    delivery.external_operation_id = None
    delivery.error_code = error_code
    delivery.completed_at = func.now()
    _add_terminal_audit(session, delivery, succeeded=False)


async def _owned_delivery(
    session: AsyncSession, delivery_id: str, lease_owner: str
) -> PlatformDelivery | None:
    return await session.scalar(
        select(PlatformDelivery)
        .where(
            PlatformDelivery.id == delivery_id,
            PlatformDelivery.status == PlatformDeliveryStatus.PROCESSING,
            PlatformDelivery.lease_owner == lease_owner,
            PlatformDelivery.lease_expires_at > func.now(),
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )


async def claim_next_platform_delivery(
    session: AsyncSession, *, lease_owner: str, lease_seconds: int
) -> PlatformDelivery | None:
    _validate_owner(lease_owner)
    if type(lease_seconds) is not int or lease_seconds <= 0:
        raise ValueError("invalid lease seconds")
    try:
        exhausted = list(
            await session.scalars(
                select(PlatformDelivery)
                .where(
                    PlatformDelivery.status == PlatformDeliveryStatus.PROCESSING,
                    PlatformDelivery.lease_expires_at <= func.now(),
                    PlatformDelivery.attempt_count >= 3,
                )
                .order_by(PlatformDelivery.created_at, PlatformDelivery.id)
                .with_for_update(skip_locked=True)
            )
        )
        for delivery in exhausted:
            _mark_failed(session, delivery, "PLATFORM_DELIVERY_FAILED")

        eligible = or_(
            and_(
                PlatformDelivery.status == PlatformDeliveryStatus.PENDING,
                or_(
                    PlatformDelivery.next_attempt_at.is_(None),
                    PlatformDelivery.next_attempt_at <= func.now(),
                ),
            ),
            and_(
                PlatformDelivery.status == PlatformDeliveryStatus.PROCESSING,
                PlatformDelivery.lease_expires_at <= func.now(),
            ),
        )
        older_record = aliased(PublishRecord)
        older_delivery = aliased(PlatformDelivery)
        predecessor = (
            select(1)
            .select_from(older_delivery)
            .join(older_record, older_record.id == older_delivery.publish_record_id)
            .where(
                older_record.store_id == PublishRecord.store_id,
                older_record.product_id == PublishRecord.product_id,
                older_record.published_product_version < PublishRecord.published_product_version,
                older_delivery.status.in_((PlatformDeliveryStatus.PENDING, PlatformDeliveryStatus.PROCESSING)),
            )
            .exists()
        )
        delivery = await session.scalar(
            select(PlatformDelivery)
            .join(PublishRecord, PublishRecord.id == PlatformDelivery.publish_record_id)
            .where(eligible, PlatformDelivery.attempt_count < 3, ~predecessor)
            .order_by(PlatformDelivery.created_at, PlatformDelivery.id)
            .with_for_update(of=PlatformDelivery, skip_locked=True)
            .limit(1)
        )
        if delivery is None:
            await session.commit()
            return None
        delivery.status = PlatformDeliveryStatus.PROCESSING
        delivery.attempt_count += 1
        delivery.next_attempt_at = None
        delivery.lease_owner = lease_owner
        delivery.lease_expires_at = _after_database_time(session, lease_seconds)
        delivery.error_code = None
        await session.commit()
        return delivery
    except (SQLAlchemyError, ValueError):
        await session.rollback()
        raise

async def complete_platform_delivery(
    session: AsyncSession,
    *,
    delivery_id: str,
    lease_owner: str,
    external_operation_id: str,
) -> bool:
    _validate_owner(lease_owner)
    if not isinstance(external_operation_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", external_operation_id
    ):
        raise ValueError("invalid external operation id")
    try:
        delivery = await _owned_delivery(session, delivery_id, lease_owner)
        if delivery is None:
            await session.rollback()
            return False
        delivery.status = PlatformDeliveryStatus.SUCCEEDED
        delivery.next_attempt_at = None
        delivery.lease_owner = None
        delivery.lease_expires_at = None
        delivery.external_operation_id = external_operation_id
        delivery.error_code = None
        delivery.completed_at = func.now()
        _add_terminal_audit(session, delivery, succeeded=True)
        await session.commit()
        return True
    except (SQLAlchemyError, ValueError):
        await session.rollback()
        raise


async def return_platform_delivery_for_retry(
    session: AsyncSession,
    *,
    delivery_id: str,
    lease_owner: str,
    error_code: str,
    retry_after_seconds: int | None,
) -> bool:
    _validate_owner(lease_owner)
    _validate_error(error_code)
    if retry_after_seconds is not None and (
        type(retry_after_seconds) is not int
        or not 0 <= retry_after_seconds <= 60
    ):
        raise ValueError("invalid retry delay")
    try:
        delivery = await _owned_delivery(session, delivery_id, lease_owner)
        if delivery is None:
            await session.rollback()
            return False
        if delivery.attempt_count >= 3:
            _mark_failed(session, delivery, error_code)
        else:
            delay = (
                retry_after_seconds
                if retry_after_seconds is not None
                else min(60, 2**delivery.attempt_count)
            )
            delivery.status = PlatformDeliveryStatus.PENDING
            delivery.next_attempt_at = _after_database_time(session, delay)
            delivery.lease_owner = None
            delivery.lease_expires_at = None
            delivery.error_code = error_code
        await session.commit()
        return True
    except (SQLAlchemyError, ValueError):
        await session.rollback()
        raise


async def fail_platform_delivery(
    session: AsyncSession,
    *,
    delivery_id: str,
    lease_owner: str,
    error_code: str,
) -> bool:
    _validate_owner(lease_owner)
    _validate_error(error_code)
    try:
        delivery = await _owned_delivery(session, delivery_id, lease_owner)
        if delivery is None:
            await session.rollback()
            return False
        _mark_failed(session, delivery, error_code)
        await session.commit()
        return True
    except (SQLAlchemyError, ValueError):
        await session.rollback()
        raise
