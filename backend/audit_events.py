import json
import unicodedata
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from backend.common import (
    AuditEventType,
    AuditOutcome,
    ComplianceRiskLevel,
    ProposalRevisionOrigin,
    UserRole,
    WorkflowQuality,
    WorkflowStatus,
    WorkflowType,
)
from backend.models import AuditEvent


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
    }
)

_EDITABLE_FIELDS = frozenset(
    {"title", "selling_points", "description", "keywords", "attribute_completions"}
)


def _safe_details(details: dict[str, object]) -> None:
    if not set(details) <= AUDIT_DETAIL_KEYS:
        raise ValueError("unsafe audit details")
    enum_values = {
        "from_status": {item.value for item in WorkflowStatus},
        "to_status": {item.value for item in WorkflowStatus},
        "origin": {item.value for item in ProposalRevisionOrigin},
        "workflow_type": {item.value for item in WorkflowType},
        "quality_status": {item.value for item in WorkflowQuality},
        "risk_level": {item.value for item in ComplianceRiskLevel},
    }
    for key, allowed in enum_values.items():
        if key in details and details[key] not in allowed:
            raise ValueError("unsafe audit details")
    for key in ("revision_number", "published_from_version", "published_to_version"):
        if key in details and (
            not isinstance(details[key], int)
            or isinstance(details[key], bool)
            or details[key] < 1
        ):
            raise ValueError("unsafe audit details")
    if "review_passed" in details and not isinstance(details["review_passed"], bool):
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
            or any(not isinstance(field, str) or field not in _EDITABLE_FIELDS for field in fields)
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


def add_audit_event(
    session: AsyncSession,
    *,
    event_type: AuditEventType,
    outcome: AuditOutcome,
    store_id: str,
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
) -> AuditEvent:
    safe_details = {} if details is None else details
    _safe_details(safe_details)
    event = AuditEvent(
        id=str(uuid4()),
        event_type=event_type,
        outcome=outcome,
        actor_id=actor_id,
        actor_role=actor_role,
        store_id=store_id,
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
