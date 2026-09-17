from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, status
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from backend.routes import router
from backend.logistics import router as logistics_router
from backend.schemas import KnowledgeEnvelope, KnowledgeError

DEFAULT_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


def _knowledge_error_response(
    *, status_code: int, category: str, code: str, message: str, request_id: str, headers=None
) -> JSONResponse:
    envelope = KnowledgeEnvelope(
        request_id=request_id,
        status="error",
        data=None,
        quality=None,
        error=KnowledgeError(category=category, code=code, message=message),
    )
    return JSONResponse(
        status_code=status_code, content=envelope.model_dump(mode="json"), headers=headers
    )


def _exception_request_id(exception: StarletteHTTPException) -> str:
    detail = exception.detail
    value = detail.get("request_id") if isinstance(detail, dict) else None
    if isinstance(value, str) and value:
        try:
            return str(UUID(value))
        except ValueError:
            pass
    return str(uuid4())


def _knowledge_http_mapping(exception: StarletteHTTPException) -> tuple[str, str, str]:
    detail = exception.detail
    detail_code = detail.get("code") if isinstance(detail, dict) else None
    safe_code = detail_code if isinstance(detail_code, str) and detail_code.startswith("KNOWLEDGE_") else None
    if exception.status_code == status.HTTP_401_UNAUTHORIZED:
        return "authorization_error", "KNOWLEDGE_AUTHENTICATION_REQUIRED", "Authentication required"
    if exception.status_code == status.HTTP_403_FORBIDDEN:
        return "authorization_error", "KNOWLEDGE_AUTHORIZATION_REQUIRED", "Authorization required"
    if exception.status_code == status.HTTP_404_NOT_FOUND:
        return "not_found", safe_code or "KNOWLEDGE_NOT_FOUND", "Knowledge resource not found"
    if exception.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
        if safe_code == "KNOWLEDGE_DEPENDENCY_TIMEOUT":
            return "timeout", safe_code, "Knowledge dependency timed out"
        return "dependency_error", safe_code or "KNOWLEDGE_DEPENDENCY_UNAVAILABLE", "Knowledge dependency unavailable"
    return "validation_error", safe_code or "KNOWLEDGE_REQUEST_INVALID", "Knowledge request is invalid"


def _mount_frontend(app: FastAPI, frontend_dist: Path) -> None:
    index = frontend_dist / "index.html"
    if not frontend_dist.is_dir() or not index.is_file():
        return
    assets = frontend_dist / "assets"
    if assets.is_dir():
        app.mount("/app/assets", StaticFiles(directory=assets), name="frontend-assets")

    @app.get("/app", include_in_schema=False)
    @app.get("/app/{path:path}", include_in_schema=False)
    async def frontend() -> FileResponse:
        return FileResponse(index)


def create_app(frontend_dist: Path | None = None) -> FastAPI:
    app = FastAPI(title="智营台 API", version="0.1.0")
    app.include_router(router)
    app.include_router(logistics_router)

    @app.exception_handler(StarletteHTTPException)
    async def knowledge_http_exception(request: Request, exception: StarletteHTTPException):
        if not request.url.path.startswith("/knowledge/"):
            return await http_exception_handler(request, exception)
        category, code, message = _knowledge_http_mapping(exception)
        return _knowledge_error_response(
            status_code=exception.status_code,
            category=category,
            code=code,
            message=message,
            request_id=_exception_request_id(exception),
            headers=exception.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def knowledge_validation_exception(request: Request, exception: RequestValidationError):
        if request.url.path.startswith(('/admin/','/agent-evaluations/')) or request.url.path in {'/agent-calls','/audit-events'}:
            return JSONResponse(status_code=422,content={'detail':{'code': 'AUDIT_FILTER_INVALID' if request.url.path == '/audit-events' else 'MANAGEMENT_REQUEST_INVALID','request_id':str(uuid4())}})
        if not request.url.path.startswith("/knowledge/"):
            return await request_validation_exception_handler(request, exception)
        return _knowledge_error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            category="validation_error",
            code="KNOWLEDGE_REQUEST_INVALID",
            message="Knowledge request is invalid",
            request_id=str(uuid4()),
        )

    @app.get("/health/live", tags=["health"])
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    _mount_frontend(app, frontend_dist or DEFAULT_FRONTEND_DIST)
    return app


app = create_app()
