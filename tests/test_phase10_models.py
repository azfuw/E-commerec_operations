from backend.common import AuditEventType, PlatformDeliveryStatus
from backend.models import PlatformDelivery, PlatformWebhookReceipt


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
