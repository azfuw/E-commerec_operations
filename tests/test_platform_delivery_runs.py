from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.common import AuditEventType, PlatformDeliveryStatus, UserRole
from backend.database import Base
from backend.models import AuditEvent, PlatformDelivery, PublishRecord, Store, User, UserStoreScope
from backend.platform_delivery_runs import (
    claim_next_platform_delivery,
    complete_platform_delivery,
    fail_platform_delivery,
    return_platform_delivery_for_retry,
)


@pytest_asyncio.fixture
async def platform_delivery_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _snapshot() -> dict[str, object]:
    return {
        "title": "已审批标题",
        "selling_points": ["耐用", "易清洁"],
        "description": "已审批描述",
        "search_keywords": ["家居", "棉质"],
        "attributes": {"材质": "棉", "可回收": True},
        "current_version": 8,
    }


async def seed_pending_delivery(
    factory, *, suffix: str | None = None, product_id: str | None = None,
    store_id: str | None = None, version: int = 8, publish_hash: str = "a" * 64,
) -> str:
    token = suffix or uuid4().hex[:8]
    record_id = f"publish-{token}"
    delivery_id = f"delivery-{token}"
    async with factory() as session:
        visible_store_id = store_id or f"store-{token}"
        if await session.get(Store, visible_store_id) is None:
            session.add(Store(id=visible_store_id, code=visible_store_id, name="Delivery store"))
        session.add(User(id=f"user-{token}", username=f"user-{token}", password_hash="unused", role=UserRole.SUPERVISOR))
        await session.flush()
        session.add(UserStoreScope(user_id=f"user-{token}", store_id=visible_store_id))
        session.add(
            PublishRecord(
                id=record_id,
                proposal_id=f"proposal-{token}",
                proposal_revision_id=f"revision-{token}",
                product_id=product_id or f"product-{token}",
                store_id=store_id or f"store-{token}",
                approved_by=f"user-{token}",
                approval_action_id=f"action-{token}",
                publish_idempotency_hash=publish_hash,
                before_snapshot={**_snapshot(), "current_version": version - 1},
                after_snapshot={**_snapshot(), "current_version": version},
                base_product_version=version - 1,
                published_product_version=version,
            )
        )
        await session.flush()
        session.add(
            PlatformDelivery(
                id=delivery_id,
                publish_record_id=record_id,
                store_id=store_id or f"store-{token}",
                provider="contract_simulator",
                status=PlatformDeliveryStatus.PENDING,
                attempt_count=0,
            )
        )
        await session.commit()
    return delivery_id


async def seed_expired_processing_delivery(
    factory, *, lease_owner: str, attempt_count: int = 1
) -> str:
    delivery_id = await seed_pending_delivery(factory)
    async with factory() as session:
        row = await session.get(PlatformDelivery, delivery_id)
        assert row is not None
        row.status = PlatformDeliveryStatus.PROCESSING
        row.attempt_count = attempt_count
        row.lease_owner = lease_owner
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    return delivery_id


async def load_delivery(factory, delivery_id: str) -> PlatformDelivery:
    async with factory() as session:
        row = await session.get(PlatformDelivery, delivery_id)
        assert row is not None
        return row


async def make_delivery_due(factory, delivery_id: str) -> None:
    async with factory() as session:
        row = await session.get(PlatformDelivery, delivery_id)
        assert row is not None
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()


async def test_claim_increments_attempt_and_commits_processing_lease(
    platform_delivery_factory,
) -> None:
    delivery_id = await seed_pending_delivery(platform_delivery_factory)

    async with platform_delivery_factory() as session:
        claimed = await claim_next_platform_delivery(
            session, lease_owner="worker-1", lease_seconds=60
        )

    row = await load_delivery(platform_delivery_factory, delivery_id)
    assert claimed is not None and claimed.id == delivery_id
    assert (
        row.status,
        row.attempt_count,
        row.lease_owner,
        row.error_code,
    ) == (PlatformDeliveryStatus.PROCESSING, 1, "worker-1", None)
    assert row.lease_expires_at is not None


async def test_retry_clears_lease_and_uses_capped_retry_after(
    platform_delivery_factory,
) -> None:
    delivery_id = await seed_pending_delivery(platform_delivery_factory)
    async with platform_delivery_factory() as session:
        await claim_next_platform_delivery(
            session, lease_owner="worker-1", lease_seconds=60
        )
    before = datetime.now(UTC)

    async with platform_delivery_factory() as session:
        assert await return_platform_delivery_for_retry(
            session,
            delivery_id=delivery_id,
            lease_owner="worker-1",
            error_code="PLATFORM_RATE_LIMITED",
            retry_after_seconds=60,
        )

    row = await load_delivery(platform_delivery_factory, delivery_id)
    retry_at = row.next_attempt_at
    assert retry_at is not None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    assert before + timedelta(seconds=59) <= retry_at <= datetime.now(UTC) + timedelta(
        seconds=61
    )
    assert (
        row.status,
        row.attempt_count,
        row.lease_owner,
        row.lease_expires_at,
        row.error_code,
    ) == (
        PlatformDeliveryStatus.PENDING,
        1,
        None,
        None,
        "PLATFORM_RATE_LIMITED",
    )
    async with platform_delivery_factory() as session:
        assert await session.scalar(select(func.count(AuditEvent.id))) == 0


@pytest.mark.parametrize("delayed", [False, True])
@pytest.mark.parametrize("terminal", ["complete", "fail"])
async def test_successive_versions_wait_for_predecessor_terminal_state(
    platform_delivery_factory, delayed, terminal,
) -> None:
    factory = platform_delivery_factory
    older = await seed_pending_delivery(factory, product_id="same-product", store_id="same-store")
    async with factory() as session:
        first = await claim_next_platform_delivery(session, lease_owner="older", lease_seconds=60)
        assert first is not None and first.id == older
    newer = await seed_pending_delivery(factory, product_id="same-product", store_id="same-store", version=9, publish_hash="b" * 64)
    # Creation order cannot replace the immutable published version ordering.
    async with factory() as session:
        row = await session.get(PlatformDelivery, newer)
        row.created_at = datetime.now(UTC) - timedelta(days=1)
        await session.commit()
    if delayed:
        async with factory() as session:
            assert await return_platform_delivery_for_retry(
                session, delivery_id=older, lease_owner="older",
                error_code="PLATFORM_CONNECTION_FAILED", retry_after_seconds=60,
            )
    async with factory() as session:
        assert await claim_next_platform_delivery(session, lease_owner="newer", lease_seconds=60) is None

    unrelated = await seed_pending_delivery(factory, store_id="same-store", publish_hash="c" * 64)
    async with factory() as session:
        other = await claim_next_platform_delivery(session, lease_owner="other", lease_seconds=60)
        assert other is not None and other.id == unrelated
    if delayed:
        await make_delivery_due(factory, older)
        async with factory() as session:
            retried = await claim_next_platform_delivery(session, lease_owner="older", lease_seconds=60)
            assert retried is not None and retried.id == older
    async with factory() as session:
        if terminal == "complete":
            assert await complete_platform_delivery(
                session, delivery_id=older, lease_owner="older", external_operation_id="older-operation",
            )
        else:
            assert await fail_platform_delivery(
                session, delivery_id=older, lease_owner="older", error_code="PLATFORM_FORBIDDEN",
            )
    async with factory() as session:
        released = await claim_next_platform_delivery(session, lease_owner="newer", lease_seconds=60)
        assert released is not None and released.id == newer


@pytest.mark.parametrize("transition", ["complete", "fail"])
async def test_terminal_transition_writes_one_safe_audit(
    platform_delivery_factory, transition: str
) -> None:
    delivery_id = await seed_pending_delivery(platform_delivery_factory)
    async with platform_delivery_factory() as session:
        await claim_next_platform_delivery(
            session, lease_owner="worker-1", lease_seconds=60
        )
    async with platform_delivery_factory() as session:
        changed = (
            await complete_platform_delivery(
                session,
                delivery_id=delivery_id,
                lease_owner="worker-1",
                external_operation_id="operation-1",
            )
            if transition == "complete"
            else await fail_platform_delivery(
                session,
                delivery_id=delivery_id,
                lease_owner="worker-1",
                error_code="PLATFORM_FORBIDDEN",
            )
        )
    assert changed

    row = await load_delivery(platform_delivery_factory, delivery_id)
    async with platform_delivery_factory() as session:
        audits = list(await session.scalars(select(AuditEvent)))
    expected = (
        (
            PlatformDeliveryStatus.SUCCEEDED,
            "operation-1",
            None,
            AuditEventType.PLATFORM_DELIVERY_COMPLETED,
            "success",
        )
        if transition == "complete"
        else (
            PlatformDeliveryStatus.FAILED,
            None,
            "PLATFORM_FORBIDDEN",
            AuditEventType.PLATFORM_DELIVERY_FAILED,
            "failed",
        )
    )
    assert (
        row.status,
        row.external_operation_id,
        row.error_code,
        audits[0].event_type,
        audits[0].outcome.value,
    ) == expected
    assert row.completed_at is not None
    assert row.lease_owner is row.lease_expires_at is None
    assert len(audits) == 1
    assert (
        audits[0].resource_type,
        audits[0].resource_id,
        audits[0].publish_record_id,
        audits[0].details,
    ) == (
        "platform_delivery",
        delivery_id,
        row.publish_record_id,
        {
            "provider": "contract_simulator",
            "platform_delivery_status": row.status.value,
            "attempt_count": 1,
        },
    )


async def test_stale_owner_cannot_complete_retry_or_fail_after_lease_reclaim(
    platform_delivery_factory,
) -> None:
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
    async with platform_delivery_factory() as session:
        assert not await return_platform_delivery_for_retry(
            session,
            delivery_id=delivery_id,
            lease_owner="worker-1",
            error_code="PLATFORM_SERVER_ERROR",
            retry_after_seconds=None,
        )
    async with platform_delivery_factory() as session:
        assert not await fail_platform_delivery(
            session,
            delivery_id=delivery_id,
            lease_owner="worker-1",
            error_code="PLATFORM_SERVER_ERROR",
        )
    row = await load_delivery(platform_delivery_factory, delivery_id)
    assert (row.status, row.attempt_count, row.lease_owner) == (
        PlatformDeliveryStatus.PROCESSING,
        2,
        "worker-2",
    )


@pytest.mark.parametrize("transition", ["complete", "retry", "fail"])
async def test_expired_owner_cannot_write_any_transition(
    platform_delivery_factory, transition: str
) -> None:
    delivery_id = await seed_expired_processing_delivery(
        platform_delivery_factory, lease_owner="worker-1"
    )
    async with platform_delivery_factory() as session:
        changed = (
            await complete_platform_delivery(
                session,
                delivery_id=delivery_id,
                lease_owner="worker-1",
                external_operation_id="operation-expired",
            )
            if transition == "complete"
            else await return_platform_delivery_for_retry(
                session,
                delivery_id=delivery_id,
                lease_owner="worker-1",
                error_code="PLATFORM_SERVER_ERROR",
                retry_after_seconds=None,
            )
            if transition == "retry"
            else await fail_platform_delivery(
                session,
                delivery_id=delivery_id,
                lease_owner="worker-1",
                error_code="PLATFORM_SERVER_ERROR",
            )
        )
    assert not changed
    row = await load_delivery(platform_delivery_factory, delivery_id)
    assert (row.status, row.attempt_count, row.lease_owner) == (
        PlatformDeliveryStatus.PROCESSING,
        1,
        "worker-1",
    )


async def test_third_expired_attempt_is_failed_without_incrementing_to_four(
    platform_delivery_factory,
) -> None:
    delivery_id = await seed_expired_processing_delivery(
        platform_delivery_factory, lease_owner="worker-3", attempt_count=3
    )

    async with platform_delivery_factory() as session:
        assert (
            await claim_next_platform_delivery(
                session, lease_owner="worker-4", lease_seconds=60
            )
            is None
        )

    row = await load_delivery(platform_delivery_factory, delivery_id)
    assert (
        row.status,
        row.attempt_count,
        row.error_code,
        row.lease_owner,
        row.lease_expires_at,
    ) == (
        PlatformDeliveryStatus.FAILED,
        3,
        "PLATFORM_DELIVERY_FAILED",
        None,
        None,
    )
    async with platform_delivery_factory() as session:
        audits = list(await session.scalars(select(AuditEvent)))
    assert len(audits) == 1
    assert audits[0].event_type is AuditEventType.PLATFORM_DELIVERY_FAILED
