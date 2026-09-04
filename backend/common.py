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
    PENDING_APPROVAL = "pending_approval"
    REJECTED = "rejected"
    FAILED = "failed"


class WorkflowType(StrEnum):
    ANALYSIS = "analysis"
    OPTIMIZATION = "optimization"
    MANUAL_REVIEW = "manual_review"


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


class ProposalRevisionOrigin(StrEnum):
    AGENT = "agent"
    MANUAL = "manual"


class ApprovalActionType(StrEnum):
    SUBMIT = "submit"
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"


class EvaluationAgentType(StrEnum):
    ANALYSIS = "analysis"
    OPTIMIZATION = "optimization"
    COMPLIANCE = "compliance"
    KNOWLEDGE_RETRIEVAL = "knowledge_retrieval"


class EvaluationRunStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class AuditEventType(StrEnum):
    MANUAL_REVISION_CREATED = "manual_revision_created"
    MANUAL_REVIEW_CLAIMED = "manual_review_claimed"
    MANUAL_REVIEW_COMPLETED = "manual_review_completed"
    MANUAL_REVIEW_FAILED = "manual_review_failed"
    PROPOSAL_SUBMITTED = "proposal_submitted"
    PROPOSAL_APPROVED = "proposal_approved"
    PROPOSAL_REJECTED = "proposal_rejected"
    PROPOSAL_CHANGES_REQUESTED = "proposal_changes_requested"
    SIMULATED_PUBLISH_COMPLETED = "simulated_publish_completed"
    AUTHORIZATION_DENIED = "authorization_denied"
    KNOWLEDGE_DOCUMENT_CREATED = "knowledge_document_created"
    KNOWLEDGE_VERSION_CREATED = "knowledge_version_created"
    KNOWLEDGE_DOCUMENT_DISABLED = "knowledge_document_disabled"
    EVALUATION_RUN_PERSISTED = "evaluation_run_persisted"
    ADMIN_USER_UPDATED = "admin_user_updated"
    ADMIN_USER_SCOPES_REPLACED = "admin_user_scopes_replaced"
    ADMIN_STORE_UPDATED = "admin_store_updated"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    DENIED = "denied"


def utc_now() -> datetime:
    return datetime.now(UTC)
