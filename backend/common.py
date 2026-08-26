from datetime import UTC, datetime
from enum import StrEnum


class UserRole(StrEnum):
    OPERATOR = "operator"
    SUPERVISOR = "supervisor"
    ADMIN = "admin"


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class OrderStatus(StrEnum):
    PAID = "paid"
    SHIPPED = "shipped"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class RefundStatus(StrEnum):
    NONE = "none"
    REQUESTED = "requested"
    REFUNDED = "refunded"
    RETURNED = "returned"


class WorkflowStatus(StrEnum):
    ACCEPTED = "accepted"
    PROCESSING = "processing"
    AWAITING_SELECTION = "awaiting_selection"
    FAILED = "failed"


class WorkflowQuality(StrEnum):
    NORMAL = "normal"
    PARTIAL = "partial"
    DEGRADED = "degraded"


class AgentCallType(StrEnum):
    PRIMARY = "primary"
    SCHEMA_REPAIR = "schema_repair"


def utc_now() -> datetime:
    return datetime.now(UTC)
