import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.common import (
    ApprovalActionType,
    AuditEventType,
    AuditOutcome,
    ComplianceRiskLevel,
    EvaluationAgentType,
    EvaluationRunStatus,
    KnowledgeVersionStatus,
    PlatformDeliveryStatus,
    ProposalRevisionOrigin,
    UserRole,
    UserStatus,
    WorkflowQuality,
    WorkflowStatus,
    WorkflowType,
)
from backend.models import ApprovalAction, AuditEvent, Store, User, UserStoreScope


AUDIT_DETAIL_KEYS = frozenset(
    {
        "from_status",
        "to_status",
        "revision_number",
        "origin",
        "workflow_type",
        "quality_status",
        "current_step",
        "changed_fields",
        "review_passed",
        "risk_level",
        "published_from_version",
        "published_to_version",
        "from_role",
        "to_role",
        "from_user_status",
        "to_user_status",
        "scope_count",
        "store_enabled",
        "document_status",
        "evaluation_agent_type",
        "evaluation_status",
        "case_count",
        "platform_delivery_status",
        "attempt_count",
        "provider",
    }
)

_EDITABLE_FIELDS = frozenset(
    {"title", "selling_points", "description", "keywords", "attribute_completions"}
)
_AUDIT_CHANGED_FIELDS = _EDITABLE_FIELDS | frozenset({"role", "status", "store_scopes", "enabled"})
_RESOURCE_TYPES = frozenset(
    {"user", "store", "knowledge_document", "knowledge_version", "evaluation_run", "platform_delivery"}
)


@dataclass
class AuditEventDomainError(Exception):
    code: str
    status_code: int


@dataclass(frozen=True)
class AuditEventFilters:
    page: int = 1
    page_size: int = 20
    store_id: str | None = None
    proposal_id: str | None = None
    action: ApprovalActionType | None = None
    workflow_run_id: str | None = None
    event_type: AuditEventType | None = None
    actor_id: str | None = None
    outcome: AuditOutcome | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None

    def __post_init__(self):
        valid = type(self.page) is int and self.page >= 1 and type(self.page_size) is int and 1 <= self.page_size <= 100
        for name in ('store_id','proposal_id','workflow_run_id','actor_id'):
            value = getattr(self,name)
            valid = valid and (value is None or isinstance(value,str) and 1 <= len(value) <= 36)
        for name,kind in (('action',ApprovalActionType),('event_type',AuditEventType),('outcome',AuditOutcome)):
            value = getattr(self,name)
            valid = valid and (value is None or isinstance(value,str) and value in {item.value for item in kind})
        for value in (self.created_from,self.created_to):
            valid = valid and (value is None or isinstance(value,datetime) and value.utcoffset() == timedelta(0))
        if (self.created_from is None) != (self.created_to is None): valid = False
        if valid and self.created_from is not None:
            valid = timedelta(0) <= self.created_to - self.created_from <= timedelta(days=31)
        if not valid: raise AuditEventDomainError('AUDIT_FILTER_INVALID',422)


def _safe_details(details: dict[str, object]) -> None:
    if not isinstance(details, dict) or not set(details) <= AUDIT_DETAIL_KEYS:
        raise ValueError("unsafe audit details")
    enum_values = {
        "from_status": {item.value for item in WorkflowStatus},
        "to_status": {item.value for item in WorkflowStatus},
        "origin": {item.value for item in ProposalRevisionOrigin},
        "workflow_type": {item.value for item in WorkflowType},
        "quality_status": {item.value for item in WorkflowQuality},
        "risk_level": {item.value for item in ComplianceRiskLevel},
        "from_role": {item.value for item in UserRole},
        "to_role": {item.value for item in UserRole},
        "from_user_status": {item.value for item in UserStatus},
        "to_user_status": {item.value for item in UserStatus},
        "document_status": {item.value for item in KnowledgeVersionStatus},
        "evaluation_agent_type": {item.value for item in EvaluationAgentType},
        "evaluation_status": {item.value for item in EvaluationRunStatus},
        "platform_delivery_status": {item.value for item in PlatformDeliveryStatus},
        "provider": {"contract_simulator"},
    }
    for key, allowed in enum_values.items():
        if key in details and (not isinstance(details[key], str) or details[key] not in allowed):
            raise ValueError("unsafe audit details")
    for key in (
        "revision_number",
        "published_from_version",
        "published_to_version",
        "scope_count",
        "case_count",
        "attempt_count",
    ):
        if key in details and (
            not isinstance(details[key], int)
            or isinstance(details[key], bool)
            or details[key] < (0 if key in {"scope_count", "case_count", "attempt_count"} else 1)
        ):
            raise ValueError("unsafe audit details")
    if "review_passed" in details and not isinstance(details["review_passed"], bool):
        raise ValueError("unsafe audit details")
    if "store_enabled" in details and not isinstance(details["store_enabled"], bool):
        raise ValueError("unsafe audit details")
    if "current_step" in details and (
        not isinstance(details["current_step"], str)
        or not 1 <= len(details["current_step"]) <= 64
        or any(unicodedata.category(char) == "Cc" for char in details["current_step"])
    ):
        raise ValueError("unsafe audit details")
    if "changed_fields" in details:
        fields = details["changed_fields"]
        if (
            not isinstance(fields, list)
            or any(not isinstance(field, str) or field not in _AUDIT_CHANGED_FIELDS for field in fields)
            or len(fields) != len(set(fields))
        ):
            raise ValueError("unsafe audit details")
    try:
        encoded = json.dumps(
            details,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("unsafe audit details") from None
    if len(encoded) > 4096:
        raise ValueError("unsafe audit details")


def _safe_resource(resource_type: str | None, resource_id: str | None) -> None:
    if (resource_type is None) != (resource_id is None):
        raise ValueError("unsafe audit resource")
    if resource_type is not None and (
        resource_type not in _RESOURCE_TYPES
        or not isinstance(resource_id, str)
        or not 1 <= len(resource_id) <= 36
    ):
        raise ValueError("unsafe audit resource")


def add_audit_event(
    session: AsyncSession,
    *,
    event_type: AuditEventType,
    outcome: AuditOutcome,
    store_id: str | None,
    actor_id: str | None = None,
    actor_role: UserRole | None = None,
    proposal_id: str | None = None,
    proposal_revision_id: str | None = None,
    workflow_run_id: str | None = None,
    approval_action_id: str | None = None,
    publish_record_id: str | None = None,
    request_id: str | None = None,
    error_code: str | None = None,
    details: dict[str, object] | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
) -> AuditEvent:
    safe_details = {} if details is None else details
    _safe_details(safe_details)
    _safe_resource(resource_type, resource_id)
    event = AuditEvent(
        id=str(uuid4()),
        event_type=event_type,
        outcome=outcome,
        actor_id=actor_id,
        actor_role=actor_role,
        store_id=store_id,
        resource_type=resource_type,
        resource_id=resource_id,
        proposal_id=proposal_id,
        proposal_revision_id=proposal_revision_id,
        workflow_run_id=workflow_run_id,
        approval_action_id=approval_action_id,
        publish_record_id=publish_record_id,
        request_id=request_id,
        error_code=error_code,
        details=dict(safe_details),
    )
    session.add(event)
    return event


async def list_audit_events(
    session: AsyncSession,
    *,
    actor_id: str,
    filters: AuditEventFilters,
) -> tuple[list[AuditEvent], int]:
    from backend.auth import store_visibility_predicate
    try:
        actor = await session.scalar(
            select(User)
            .where(User.id == actor_id)
            .execution_options(populate_existing=True)
        )
        if (
            actor is None
            or actor.status is not UserStatus.ACTIVE
            or actor.role not in {UserRole.SUPERVISOR, UserRole.ADMIN}
        ):
            raise AuditEventDomainError("PROPOSAL_ACTION_FORBIDDEN", 403)

        statement = select(AuditEvent)
        statement = statement.where(store_visibility_predicate(actor,AuditEvent.store_id))
        for name in ('store_id','proposal_id','workflow_run_id','event_type','actor_id','outcome'):
            value = getattr(filters,name)
            if value is not None: statement = statement.where(getattr(AuditEvent,name) == value)
        if filters.created_from is not None:
            statement = statement.where(AuditEvent.created_at >= filters.created_from,AuditEvent.created_at <= filters.created_to)
        if filters.action is not None:
            statement = statement.join(
                ApprovalAction,
                and_(
                    ApprovalAction.id == AuditEvent.approval_action_id,
                    ApprovalAction.store_id == AuditEvent.store_id,
                ),
            ).where(ApprovalAction.action == filters.action)

        total = int(
            await session.scalar(select(func.count()).select_from(statement.subquery())) or 0
        )
        events = list(
            await session.scalars(
                statement.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
                .offset((filters.page - 1) * filters.page_size)
                .limit(filters.page_size)
            )
        )
        for event in events:
            _safe_details(event.details)
            _safe_resource(event.resource_type,event.resource_id)
        return events, total
    except AuditEventDomainError:
        await session.rollback()
        raise
    except (SQLAlchemyError, ValueError, TypeError):
        await session.rollback()
        raise AuditEventDomainError("PROPOSAL_DATA_INCONSISTENT", 503) from None
