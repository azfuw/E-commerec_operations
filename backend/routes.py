from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.analysis_runs import (
    create_analysis_run,
    get_workflow_run,
    list_analysis_candidates,
)
from backend.auth import (
    create_access_token,
    get_current_user,
    require_store_access,
    verify_password,
)
from backend.common import UserRole, UserStatus, WorkflowStatus
from backend.config import Settings, get_settings
from backend.database import get_session
from backend.models import Product, Store, User, UserStoreScope
from backend.schemas import (
    AccessToken,
    AnalysisCandidateView,
    AnalysisRunAccepted,
    AnalysisRunRequest,
    LoginRequest,
    ProductSummary,
    StoreSummary,
    WorkflowRunView,
)

router = APIRouter()


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
        workflow_type="analysis",
        store_id=run.store_id,
        start_date=run.start_date,
        end_date=run.end_date,
        status=run.status,
        quality_status=run.quality_status,
        current_step=run.current_step,
        attempt_count=run.attempt_count,
        candidates_ready=run.status is WorkflowStatus.AWAITING_SELECTION,
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
