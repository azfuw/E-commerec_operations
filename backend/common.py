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
    COMPLETED = "completed"
    DRAFT_READY = "draft_ready"
    PENDING_MANUAL = "pending_manual"
    FAILED = "failed"


class WorkflowType(StrEnum):
    ANALYSIS = "analysis"
    OPTIMIZATION = "optimization"


class WorkflowQuality(StrEnum):
    NORMAL = "normal"
    PARTIAL = "partial"
    DEGRADED = "degraded"


class KnowledgeVersionStatus(StrEnum):
    ACCEPTED = "accepted"
    PROCESSING = "processing"
    ACTIVE = "active"
    FAILED = "failed"
    DISABLED = "disabled"


class AgentCallType(StrEnum):
    PRIMARY = "primary"
    SCHEMA_REPAIR = "schema_repair"


class ComplianceRiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def utc_now() -> datetime:
    return datetime.now(UTC)
