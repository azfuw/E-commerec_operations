from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.main import create_app


@pytest_asyncio.fixture
async def hosted_client(tmp_path) -> AsyncIterator[AsyncClient]:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("frontend-index", encoding="utf-8")
    (tmp_path / "assets" / "app.js").write_text("asset", encoding="utf-8")
    app = create_app(frontend_dist=tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_frontend_is_same_origin_without_capturing_backend_routes(
    hosted_client,
) -> None:
    assert (await hosted_client.get("/app")).text == "frontend-index"
    assert (await hosted_client.get("/app/proposals/example")).text == "frontend-index"
    assert (await hosted_client.get("/app/assets/app.js")).text == "asset"
    assert (await hosted_client.get("/app/assets/missing.js")).status_code == 404
    assert (await hosted_client.get("/health/live")).json() == {"status": "ok"}
    assert (await hosted_client.get("/stores")).status_code == 401
    assert (await hosted_client.get("/not-an-app-route")).status_code == 404
