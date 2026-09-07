import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

from backend.common import AuditEventType, PlatformDeliveryStatus
from backend.models import Base, PlatformDelivery, PlatformWebhookReceipt


def _constraint_names(model) -> set[str]:
    return {
        constraint.name
        for constraint in model.__table__.constraints
        if constraint.name
    }


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
        "ck_platform_deliveries_provider",
        "ck_platform_deliveries_attempt_count",
        "ck_platform_deliveries_lease",
        "ck_platform_deliveries_terminal",
    } <= _constraint_names(PlatformDelivery)
    assert {index.name for index in PlatformDelivery.__table__.indexes} >= {
        "ix_platform_deliveries_claim",
        "ix_platform_deliveries_store_created",
    }


def test_webhook_receipt_and_audit_contract_are_closed() -> None:
    assert {
        "uq_platform_webhook_receipts_event_id",
        "ck_platform_webhook_receipts_event_type",
        "ck_platform_webhook_receipts_payload_digest",
    } <= _constraint_names(PlatformWebhookReceipt)
    assert {
        AuditEventType.PLATFORM_DELIVERY_ENQUEUED,
        AuditEventType.PLATFORM_DELIVERY_COMPLETED,
        AuditEventType.PLATFORM_DELIVERY_FAILED,
        AuditEventType.PLATFORM_WEBHOOK_RECEIVED,
    } <= set(AuditEventType)


def test_webhook_digest_constraint_accepts_only_lowercase_hex_on_sqlite() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            PlatformWebhookReceipt.__table__.insert().values(
                id="receipt-valid",
                platform_delivery_id="delivery-1",
                event_id="event-valid",
                event_type="publish.confirmed",
                payload_digest="a" * 64,
            )
        )

    for index, digest in enumerate(("a" * 63, "A" * 64, "g" * 64)):
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    PlatformWebhookReceipt.__table__.insert().values(
                        id=f"receipt-invalid-{index}",
                        platform_delivery_id="delivery-1",
                        event_id=f"event-invalid-{index}",
                        event_type="publish.confirmed",
                        payload_digest=digest,
                    )
                )

    engine.dispose()
