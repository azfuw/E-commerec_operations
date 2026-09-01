import logging
from time import perf_counter
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, Form, Header, HTTPException, Path, Query, Response, UploadFile, status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.analysis_runs import (
    create_analysis_run,
    get_workflow_run,
    list_analysis_candidates,
)
from backend.approvals import (
    ApprovalDomainError,
    approve_proposal,
    list_pending_approvals,
    reject_proposal,
    request_proposal_changes,
    submit_proposal,
)
from backend.audit_events import AuditEventDomainError, list_audit_events
from backend.auth import (
    create_access_token,
    get_current_user,
    require_roles,
    require_store_access,
    verify_password,
)
from backend.common import (
    ApprovalActionType,
    KnowledgeVersionStatus,
    UserRole,
    UserStatus,
    WorkflowStatus,
    WorkflowType,
)
from backend.config import Settings, get_settings
from backend.database import get_session
from backend.knowledge_content import KnowledgeContentError, read_and_validate_upload, store_validated_upload
from backend.knowledge_index import KnowledgeDependencyError
from backend.knowledge_runs import (
    create_document_version,
    disable_knowledge_document,
    find_document_by_idempotency_key,
    find_document_version_by_idempotency_key,
    find_document_version_by_sha,
)
from backend.knowledge_search import (
    KnowledgeSearchLoader,
    get_knowledge_search_loader,
    search_active_knowledge,
)
from backend.manual_reviews import (
    ManualReviewDomainError,
    create_manual_revision,
)
from backend.models import (
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    Product,
    Store,
    User,
    UserStoreScope,
)
from backend.proposals import (
    ProposalDomainError,
    get_proposal_for_actor,
    select_product_for_optimization,
)
from backend.schemas import (
    AccessToken,
    AnalysisCandidateView,
    AnalysisRunAccepted,
    AnalysisRunRequest,
    ApprovalActionView,
    ApprovalListItem,
    ApprovalListView,
    AuditEventListView,
    AuditEventView,
    ComplianceReviewView,
    KnowledgeCitation,
    KnowledgeEnvelope,
    KnowledgeSearchRequest,
    LoginRequest,
    ManualReviewSummary,
    ManualRevisionAccepted,
    ManualRevisionRequest,
    OptimizationWorkflowSummary,
    ProductSelectionRequest,
    ProductSelectionView,
    ProductSummary,
    ProposalActionRequest,
    ProposalCommentActionRequest,
    ProposalDetailView,
    ProposalRevisionView,
    ProposalView,
    PublishRecordView,
    StoreSummary,
    WorkflowRunView,
)

router = APIRouter()
_logger = logging.getLogger("backend.knowledge")


def _request_id() -> str:
    return str(uuid4())


def _envelope(
    *,
    request_id: str,
    status_value: Literal["accepted", "success"],
    data: dict[str, object] | None = None,
    quality: dict[str, str] | None = None,
) -> KnowledgeEnvelope:
    return KnowledgeEnvelope(
        request_id=request_id,
        status=status_value,
        data=data,
        quality=quality,
        error=None,
    )


def _proposal_comment_request(
    payload: Annotated[dict[str, object], Body()],
) -> ProposalCommentActionRequest:
    try:
        return ProposalCommentActionRequest.model_validate(payload)
    except ValidationError as error:
        if any(item["loc"] == ("comment",) for item in error.errors()):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "APPROVAL_COMMENT_INVALID"},
            ) from None
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=error.errors(include_url=False),
        ) from None


def _audit(
    *,
    started: float,
    request_id: str,
    actor_id: str,
    action: str,
    outcome: str,
    document_id: str | None = None,
    version_id: str | None = None,
    error_code: str | None = None,
) -> None:
    _logger.info(
        "knowledge_api request_id=%s actor_id=%s document_id=%s version_id=%s "
        "action=%s status=%s error_code=%s elapsed_ms=%d",
        request_id,
        actor_id,
        document_id or "-",
        version_id or "-",
        action,
        outcome,
        error_code or "-",
        int((perf_counter() - started) * 1000),
    )


def _knowledge_http_error(status_code: int, code: str, *, request_id: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "request_id": request_id})


def _version_data(document: KnowledgeDocument, version: KnowledgeDocumentVersion) -> dict[str, object]:
    return {
        "document_id": document.id,
        "version_id": version.id,
        "status": version.status.value,
    }


def _remove_stored_upload(path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


@router.post("/auth/login", response_model=AccessToken)
async def login(
    request: LoginRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AccessToken:
    user = await session.scalar(select(User).where(User.username == request.username))
    if (
        user is None
        or not verify_password(request.password, user.password_hash)
        or user.status is not UserStatus.ACTIVE
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return AccessToken(access_token=create_access_token(user, settings))


@router.get("/stores", response_model=list[StoreSummary])
async def list_stores(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[StoreSummary]:
    statement = select(Store).where(Store.enabled.is_(True))
    if user.role is not UserRole.ADMIN:
        statement = statement.join(UserStoreScope).where(UserStoreScope.user_id == user.id)
    stores = (await session.scalars(statement.order_by(Store.code))).all()
    return [StoreSummary(id=store.id, code=store.code, name=store.name) for store in stores]


@router.get("/stores/{store_id}/products", response_model=list[ProductSummary])
async def list_products(
    store_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ProductSummary]:
    store = await require_store_access(store_id, user, session)
    products = (
        await session.scalars(
            select(Product)
            .where(Product.store_id == store.id, Product.enabled.is_(True))
            .order_by(Product.code)
        )
    ).all()
    return [
        ProductSummary(
            id=product.id,
            code=product.code,
            title=product.title,
            category=product.category,
            current_version=product.current_version,
        )
        for product in products
    ]


@router.post(
    "/analysis-runs",
    response_model=AnalysisRunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_analysis_run_route(
    request: AnalysisRunRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AnalysisRunAccepted:
    await require_store_access(request.store_id, user, session)
    run = await create_analysis_run(
        session,
        store_id=request.store_id,
        created_by=user.id,
        start_date=request.start_date,
        end_date=request.end_date,
    )
    await session.commit()
    return AnalysisRunAccepted(workflow_run_id=run.id, status="accepted")


@router.get("/workflow-runs/{workflow_run_id}", response_model=WorkflowRunView)
async def read_workflow_run(
    workflow_run_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WorkflowRunView:
    run = await get_workflow_run(session, workflow_run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow run not found")
    await require_store_access(run.store_id, user, session)
    return WorkflowRunView(
        id=run.id,
        workflow_type=run.workflow_type,
        store_id=run.store_id,
        start_date=run.start_date,
        end_date=run.end_date,
        status=run.status,
        quality_status=run.quality_status,
        current_step=run.current_step,
        attempt_count=run.attempt_count,
        candidates_ready=(
            run.workflow_type is WorkflowType.ANALYSIS
            and run.status is WorkflowStatus.AWAITING_SELECTION
        ),
        error_code=run.error_code,
    )


@router.get(
    "/analysis-runs/{workflow_run_id}/candidates",
    response_model=list[AnalysisCandidateView],
)
async def read_analysis_candidates(
    workflow_run_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AnalysisCandidateView]:
    run = await get_workflow_run(session, workflow_run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow run not found")
    await require_store_access(run.store_id, user, session)
    if run.status is not WorkflowStatus.AWAITING_SELECTION:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "ANALYSIS_NOT_READY"},
        )
    candidates = await list_analysis_candidates(session, run.id)
    return [AnalysisCandidateView.model_validate(candidate) for candidate in candidates]


@router.post(
    "/analysis-runs/{analysis_run_id}/select-product",
    response_model=ProductSelectionView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def select_product_route(
    response: Response,
    analysis_run_id: str,
    request: ProductSelectionRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ProductSelectionView:
    try:
        result = await select_product_for_optimization(
            session,
            user.id,
            analysis_run_id,
            request.candidate_id,
            idempotency_key,
        )
    except ProposalDomainError as error:
        raise HTTPException(status_code=error.status_code, detail={"code": error.code}) from None
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return ProductSelectionView(
        proposal_id=result.proposal.id,
        optimization_workflow_run_id=result.optimization_run.id,
        status="accepted",
    )


@router.get("/approvals", response_model=ApprovalListView)
async def list_approvals_route(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ApprovalListView:
    try:
        rows, total = await list_pending_approvals(
            session,
            actor_id=user.id,
            page=page,
            page_size=page_size,
        )
    except ApprovalDomainError as error:
        raise HTTPException(status_code=error.status_code, detail={"code": error.code}) from None
    return ApprovalListView(
        items=[
            ApprovalListItem(
                proposal_id=row.proposal_id,
                proposal_revision_id=row.proposal_revision_id,
                revision_number=row.revision_number,
                store_id=row.store_id,
                product_id=row.product_id,
                submitted_by=row.submitted_by,
                status="pending_approval",
                submitted_at=row.submitted_at,
            )
            for row in rows
        ],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/audit-events", response_model=AuditEventListView)
async def list_audit_events_route(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    store_id: Annotated[str | None, Query(min_length=1, max_length=36)] = None,
    proposal_id: Annotated[str | None, Query(min_length=1, max_length=36)] = None,
    action: ApprovalActionType | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AuditEventListView:
    try:
        events, total = await list_audit_events(
            session,
            actor_id=user.id,
            page=page,
            page_size=page_size,
            store_id=store_id,
            proposal_id=proposal_id,
            action=action,
        )
    except AuditEventDomainError as error:
        raise HTTPException(status_code=error.status_code, detail={"code": error.code}) from None
    return AuditEventListView(
        items=[AuditEventView.model_validate(event) for event in events],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/proposals/{proposal_id}", response_model=ProposalDetailView)
async def read_proposal_route(
    proposal_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ProposalDetailView:
    try:
        result = await get_proposal_for_actor(session, user.id, proposal_id)
    except ProposalDomainError as error:
        raise HTTPException(status_code=error.status_code, detail={"code": error.code}) from None
    revision = result.current_revision
    review = result.current_review
    manual = result.active_manual_review_run
    manual_workflow = result.active_manual_workflow
    return ProposalDetailView(
        proposal=ProposalView(
            id=result.proposal.id,
            analysis_run_id=result.proposal.analysis_run_id,
            analysis_candidate_id=result.proposal.analysis_candidate_id,
            optimization_run_id=result.proposal.optimization_run_id,
            store_id=result.proposal.store_id,
            product_id=result.proposal.product_id,
            base_product_version=result.proposal.base_product_version,
            current_revision_id=result.proposal.current_revision_id,
            created_at=result.proposal.created_at,
            updated_at=result.proposal.updated_at,
        ),
        optimization_run=OptimizationWorkflowSummary(
            id=result.optimization_run.id,
            workflow_type=result.optimization_run.workflow_type,
            status=result.optimization_run.status,
            quality_status=result.optimization_run.quality_status,
            error_code=result.optimization_run.error_code,
        ),
        current_revision=None if revision is None else ProposalRevisionView.model_validate(revision),
        current_review=None if review is None else ComplianceReviewView.model_validate(review),
        active_manual_review=None
        if manual is None or manual_workflow is None
        else ManualReviewSummary(
            manual_review_run_id=manual.id,
            workflow_run_id=manual_workflow.id,
            proposal_revision_id=manual.proposal_revision_id,
            status=manual_workflow.status,
            quality_status=manual_workflow.quality_status,
            current_step=manual_workflow.current_step,
            error_code=manual_workflow.error_code,
        ),
        submitted_revision=None
        if result.submitted_revision is None
        else ProposalRevisionView.model_validate(result.submitted_revision),
        latest_action=None
        if result.latest_action is None
        else ApprovalActionView.model_validate(result.latest_action),
        publish_record=None
        if result.publish_record is None
        else PublishRecordView.model_validate(result.publish_record),
    )


@router.post(
    "/proposals/{id}/manual-revision",
    response_model=ManualRevisionAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_manual_revision_route(
    response: Response,
    id: Annotated[str, Path(min_length=1, max_length=36)],
    request: ManualRevisionRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ManualRevisionAccepted:
    try:
        result = await create_manual_revision(
            session,
            actor_id=user.id,
            proposal_id=id,
            request=request,
            idempotency_key=idempotency_key,
            request_id=_request_id(),
        )
    except ManualReviewDomainError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code},
        ) from None
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return ManualRevisionAccepted(
        revision_id=result.revision_id,
        manual_review_workflow_run_id=result.workflow_run_id,
        status="accepted",
    )


@router.post(
    "/proposals/{id}/submit",
    response_model=ApprovalActionView,
    status_code=status.HTTP_201_CREATED,
)
async def submit_proposal_route(
    response: Response,
    id: Annotated[str, Path(min_length=1, max_length=36)],
    request: ProposalActionRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ApprovalActionView:
    try:
        result = await submit_proposal(
            session,
            actor_id=user.id,
            proposal_id=id,
            request=request,
            idempotency_key=idempotency_key,
            request_id=_request_id(),
        )
    except ApprovalDomainError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code},
        ) from None
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return ApprovalActionView.model_validate(result.action)


@router.post(
    "/approvals/{id}/reject",
    response_model=ApprovalActionView,
    status_code=status.HTTP_201_CREATED,
)
async def reject_proposal_route(
    response: Response,
    id: Annotated[str, Path(min_length=1, max_length=36)],
    request: ProposalCommentActionRequest = Depends(_proposal_comment_request),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ApprovalActionView:
    try:
        result = await reject_proposal(
            session,
            actor_id=user.id,
            proposal_id=id,
            request=request,
            idempotency_key=idempotency_key,
            request_id=_request_id(),
        )
    except ApprovalDomainError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code},
        ) from None
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return ApprovalActionView.model_validate(result.action)


@router.post(
    "/approvals/{id}/request-changes",
    response_model=ApprovalActionView,
    status_code=status.HTTP_201_CREATED,
)
async def request_proposal_changes_route(
    response: Response,
    id: Annotated[str, Path(min_length=1, max_length=36)],
    request: ProposalCommentActionRequest = Depends(_proposal_comment_request),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ApprovalActionView:
    try:
        result = await request_proposal_changes(
            session,
            actor_id=user.id,
            proposal_id=id,
            request=request,
            idempotency_key=idempotency_key,
            request_id=_request_id(),
        )
    except ApprovalDomainError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code},
        ) from None
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return ApprovalActionView.model_validate(result.action)


@router.post(
    "/approvals/{proposal_id}/approve",
    response_model=PublishRecordView,
    status_code=status.HTTP_201_CREATED,
)
async def approve_proposal_route(
    response: Response,
    proposal_id: Annotated[str, Path(min_length=1, max_length=36)],
    request: ProposalActionRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> PublishRecordView:
    try:
        result = await approve_proposal(
            session,
            actor_id=user.id,
            proposal_id=proposal_id,
            request=request,
            idempotency_key=idempotency_key,
            request_id=_request_id(),
        )
    except ApprovalDomainError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code},
        ) from None
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return PublishRecordView.model_validate(result.publish_record)


@router.post(
    "/knowledge/documents",
    response_model=KnowledgeEnvelope,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_knowledge_document(
    response: Response,
    name: Annotated[str, Form(min_length=1, max_length=128)],
    category: Annotated[str, Form(min_length=1, max_length=64)],
    file: UploadFile,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
    user: User = Depends(require_roles(UserRole.ADMIN)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> KnowledgeEnvelope:
    started = perf_counter()
    request_id = _request_id()
    actor_id = user.id
    try:
        upload = await read_and_validate_upload(file)
    except KnowledgeContentError as error:
        _audit(
            started=started,
            request_id=request_id,
            actor_id=actor_id,
            action="create",
            outcome="error",
            error_code=error.code,
        )
        raise _knowledge_http_error(
            status.HTTP_400_BAD_REQUEST, error.code, request_id=request_id
        ) from None

    if idempotency_key:
        existing_document = await find_document_by_idempotency_key(
            session, created_by=actor_id, idempotency_key=idempotency_key
        )
        if existing_document is not None:
            existing_version = await find_document_version_by_sha(
                session, document_id=existing_document.id, sha256=upload.sha256
            )
            if existing_version is None:
                _audit(
                    started=started,
                    request_id=request_id,
                    actor_id=actor_id,
                    action="create",
                    outcome="error",
                    document_id=existing_document.id,
                    error_code="KNOWLEDGE_IDEMPOTENCY_CONFLICT",
                )
                raise _knowledge_http_error(
                    status.HTTP_409_CONFLICT,
                    "KNOWLEDGE_IDEMPOTENCY_CONFLICT",
                    request_id=request_id,
                )
            response.status_code = status.HTTP_200_OK
            _audit(
                started=started,
                request_id=request_id,
                actor_id=actor_id,
                action="create",
                outcome="success",
                document_id=existing_document.id,
                version_id=existing_version.id,
            )
            return _envelope(
                request_id=request_id,
                status_value="success",
                data=_version_data(existing_document, existing_version),
            )

    document_id, version_id = str(uuid4()), str(uuid4())
    try:
        stored = store_validated_upload(
            upload,
            upload_dir=settings.knowledge_upload_dir,
            document_id=document_id,
            version_id=version_id,
        )
    except KnowledgeContentError as error:
        _audit(
            started=started,
            request_id=request_id,
            actor_id=actor_id,
            action="create",
            outcome="error",
            document_id=document_id,
            version_id=version_id,
            error_code=error.code,
        )
        raise _knowledge_http_error(
            status.HTTP_400_BAD_REQUEST, error.code, request_id=request_id
        ) from None
    try:
        document, version, created = await create_document_version(
            session,
            document_id=document_id,
            version_id=version_id,
        created_by=actor_id,
            name=name,
            category=category,
            sha256=stored.sha256,
            original_filename=str(stored.original_filename),
            mime_type=stored.mime_type,
            storage_path=str(stored.storage_path),
            idempotency_key=idempotency_key,
        )
        if not created:
            _remove_stored_upload(stored.storage_path)
            response.status_code = status.HTTP_200_OK
            result_status = "success"
        else:
            await session.commit()
            result_status = "accepted"
    except ValueError as error:
        await session.rollback()
        _remove_stored_upload(stored.storage_path)
        code = str(error)
        _audit(
            started=started,
            request_id=request_id,
            actor_id=actor_id,
            action="create",
            outcome="error",
            error_code=code,
        )
        raise _knowledge_http_error(status.HTTP_409_CONFLICT, code, request_id=request_id) from None
    except Exception:
        await session.rollback()
        _remove_stored_upload(stored.storage_path)
        _audit(
            started=started,
            request_id=request_id,
            actor_id=actor_id,
            action="create",
            outcome="error",
            error_code="KNOWLEDGE_WRITE_FAILED",
        )
        raise _knowledge_http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "KNOWLEDGE_WRITE_FAILED",
            request_id=request_id,
        ) from None

    _audit(
        started=started,
        request_id=request_id,
        actor_id=actor_id,
        action="create",
        outcome=result_status,
        document_id=document.id,
        version_id=version.id,
    )
    return _envelope(
        request_id=request_id,
        status_value=result_status,
        data=_version_data(document, version),
    )


@router.get("/knowledge/documents", response_model=KnowledgeEnvelope)
async def list_knowledge_documents(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    category: str | None = Query(default=None, min_length=1, max_length=64),
    enabled: bool | None = None,
    version_status: KnowledgeVersionStatus | None = None,
    user: User = Depends(require_roles(UserRole.ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeEnvelope:
    started = perf_counter()
    request_id = _request_id()
    actor_id = user.id
    statement = select(KnowledgeDocument)
    if category is not None:
        statement = statement.where(KnowledgeDocument.category == category)
    if enabled is not None:
        statement = statement.where(KnowledgeDocument.enabled.is_(enabled))
    if version_status is not None:
        statement = statement.where(
            KnowledgeDocument.id.in_(
                select(KnowledgeDocumentVersion.document_id).where(
                    KnowledgeDocumentVersion.status == version_status
                )
            )
        )
    total = await session.scalar(select(func.count()).select_from(statement.subquery()))
    documents = list(
        (
            await session.scalars(
                statement.order_by(KnowledgeDocument.id).offset((page - 1) * page_size).limit(page_size)
            )
        ).all()
    )
    current_ids = [document.current_version_id for document in documents if document.current_version_id]
    current_versions = {
        version.id: version
        for version in (
            await session.scalars(
                select(KnowledgeDocumentVersion).where(KnowledgeDocumentVersion.id.in_(current_ids))
            )
        ).all()
    }
    items = [
        {
            "document_id": document.id,
            "name": document.name,
            "category": document.category,
            "enabled": document.enabled,
            "current_version_id": document.current_version_id,
            "current_version_status": current_versions[document.current_version_id].status.value
            if document.current_version_id in current_versions
            else None,
        }
        for document in documents
    ]
    _audit(
        started=started,
        request_id=request_id,
        actor_id=user.id,
        action="list",
        outcome="success",
    )
    return _envelope(
        request_id=request_id,
        status_value="success",
        data={"page": page, "page_size": page_size, "total": total or 0, "items": items},
    )


@router.post(
    "/knowledge/documents/{document_id}/versions",
    response_model=KnowledgeEnvelope,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_knowledge_document_version(
    response: Response,
    document_id: str,
    file: UploadFile,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
    user: User = Depends(require_roles(UserRole.ADMIN)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> KnowledgeEnvelope:
    started = perf_counter()
    request_id = _request_id()
    actor_id = user.id
    try:
        upload = await read_and_validate_upload(file)
    except KnowledgeContentError as error:
        _audit(
            started=started,
            request_id=request_id,
            actor_id=actor_id,
            action="version",
            outcome="error",
            document_id=document_id,
            error_code=error.code,
        )
        raise _knowledge_http_error(
            status.HTTP_400_BAD_REQUEST, error.code, request_id=request_id
        ) from None

    document = await session.get(KnowledgeDocument, document_id)
    if document is None:
        raise _knowledge_http_error(
            status.HTTP_404_NOT_FOUND, "KNOWLEDGE_DOCUMENT_NOT_FOUND", request_id=request_id
        )
    if not document.enabled:
        raise _knowledge_http_error(
            status.HTTP_409_CONFLICT, "KNOWLEDGE_DOCUMENT_DISABLED", request_id=request_id
        )
    if idempotency_key:
        existing_version = await find_document_version_by_idempotency_key(
            session, document_id=document_id, idempotency_key=idempotency_key
        )
        if existing_version is not None:
            if existing_version.sha256 != upload.sha256:
                raise _knowledge_http_error(
                    status.HTTP_409_CONFLICT,
                    "KNOWLEDGE_IDEMPOTENCY_CONFLICT",
                    request_id=request_id,
                )
            response.status_code = status.HTTP_200_OK
            _audit(
                started=started,
                request_id=request_id,
                actor_id=actor_id,
                action="version",
                outcome="success",
                document_id=document.id,
                version_id=existing_version.id,
            )
            return _envelope(
                request_id=request_id,
                status_value="success",
                data=_version_data(document, existing_version),
            )
    existing_version = await find_document_version_by_sha(
        session, document_id=document_id, sha256=upload.sha256
    )
    if existing_version is not None:
        response.status_code = status.HTTP_200_OK
        _audit(
            started=started,
            request_id=request_id,
            actor_id=actor_id,
            action="version",
            outcome="success",
            document_id=document.id,
            version_id=existing_version.id,
        )
        return _envelope(
            request_id=request_id,
            status_value="success",
            data=_version_data(document, existing_version),
        )

    version_id = str(uuid4())
    try:
        stored = store_validated_upload(
            upload,
            upload_dir=settings.knowledge_upload_dir,
            document_id=document_id,
            version_id=version_id,
        )
    except KnowledgeContentError as error:
        _audit(
            started=started,
            request_id=request_id,
            actor_id=actor_id,
            action="version",
            outcome="error",
            document_id=document_id,
            version_id=version_id,
            error_code=error.code,
        )
        raise _knowledge_http_error(
            status.HTTP_400_BAD_REQUEST, error.code, request_id=request_id
        ) from None
    try:
        _, version, created = await create_document_version(
            session,
            document_id=document_id,
            version_id=version_id,
            created_by=actor_id,
            name=None,
            category=None,
            sha256=stored.sha256,
            original_filename=str(stored.original_filename),
            mime_type=stored.mime_type,
            storage_path=str(stored.storage_path),
            idempotency_key=idempotency_key,
        )
        if not created:
            _remove_stored_upload(stored.storage_path)
            response.status_code = status.HTTP_200_OK
            result_status = "success"
        else:
            await session.commit()
            result_status = "accepted"
    except ValueError as error:
        await session.rollback()
        _remove_stored_upload(stored.storage_path)
        raise _knowledge_http_error(
            status.HTTP_409_CONFLICT, str(error), request_id=request_id
        ) from None
    except Exception:
        await session.rollback()
        _remove_stored_upload(stored.storage_path)
        raise _knowledge_http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "KNOWLEDGE_WRITE_FAILED",
            request_id=request_id,
        ) from None
    _audit(
        started=started,
        request_id=request_id,
        actor_id=actor_id,
        action="version",
        outcome=result_status,
        document_id=document.id,
        version_id=version.id,
    )
    return _envelope(
        request_id=request_id,
        status_value=result_status,
        data=_version_data(document, version),
    )


@router.post("/knowledge/documents/{document_id}/disable", response_model=KnowledgeEnvelope)
async def disable_knowledge_document_route(
    document_id: str,
    user: User = Depends(require_roles(UserRole.ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeEnvelope:
    started = perf_counter()
    request_id = _request_id()
    if not await disable_knowledge_document(session, document_id=document_id):
        raise _knowledge_http_error(
            status.HTTP_404_NOT_FOUND, "KNOWLEDGE_DOCUMENT_NOT_FOUND", request_id=request_id
        )
    _audit(
        started=started,
        request_id=request_id,
        actor_id=user.id,
        action="disable",
        outcome="success",
        document_id=document_id,
    )
    return _envelope(
        request_id=request_id,
        status_value="success",
        data={"document_id": document_id, "enabled": False},
    )


@router.post("/knowledge/search", response_model=KnowledgeEnvelope)
async def search_knowledge_route(
    request: KnowledgeSearchRequest,
    user: User = Depends(
        require_roles(UserRole.OPERATOR, UserRole.SUPERVISOR, UserRole.ADMIN)
    ),
    session: AsyncSession = Depends(get_session),
    load_dependencies: KnowledgeSearchLoader = Depends(get_knowledge_search_loader),
) -> KnowledgeEnvelope:
    started = perf_counter()
    request_id = _request_id()
    try:
        outcome = await search_active_knowledge(
            session,
            query=request.query,
            categories=request.categories,
            top_k=request.top_k,
            retrieval_path="hybrid_rerank",
            load_dependencies=load_dependencies,
        )
    except KnowledgeDependencyError as error:
        _audit(
            started=started,
            request_id=request_id,
            actor_id=user.id,
            action="search",
            outcome="error",
            error_code=error.code,
        )
        raise _knowledge_http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE, error.code, request_id=request_id
        ) from None
    citations = [KnowledgeCitation(**hit.__dict__).model_dump(mode="json") for hit in outcome.hits]
    _audit(
        started=started,
        request_id=request_id,
        actor_id=user.id,
        action="search",
        outcome="success",
    )
    return _envelope(
        request_id=request_id,
        status_value="success",
        data={"citations": citations},
        quality={"status": outcome.quality_status},
    )
