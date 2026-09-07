import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from pydantic import SecretStr

from backend.config import Settings
from backend.platform_client import CommercePlatformClient, PlatformClientError
from tests.support.platform_simulator import create_platform_simulator


async def _token(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/oauth/token",
        data={
            "client_id": "client-id",
            "client_secret": "client-secret",
            "grant_type": "client_credentials",
        },
    )
    return response.json()["access_token"]


async def test_simulator_validates_idempotency_fields_and_pagination() -> None:
    app = create_platform_simulator()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://platform.test"
    ) as client:
        token = await _token(client)
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "f" * 64}
        published = await client.put(
            "/v1/stores/store-1/listings/product-1",
            headers=headers,
            json={"title": "已审批标题", "keywords": ["家居"]},
        )
        repeated = await client.put(
            "/v1/stores/store-1/listings/product-1",
            headers=headers,
            json={"title": "已审批标题", "keywords": ["家居"]},
        )
        conflict = await client.put(
            "/v1/stores/store-1/listings/product-1",
            headers=headers,
            json={"title": "不同标题"},
        )
        invalid = await client.put(
            "/v1/stores/store-1/listings/product-1",
            headers={**headers, "Idempotency-Key": "1" * 64},
            json={"inventory": 2},
        )
        page = await client.get(
            "/v1/stores/store-1/products?page=2&page_size=2",
            headers={"Authorization": f"Bearer {token}"},
        )
        bad_page = await client.get(
            "/v1/stores/store-1/orders?page=0&page_size=101",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert published.json() == repeated.json()
    assert conflict.status_code == 409
    assert invalid.status_code == 422
    assert page.json() == {
        "items": [
            {"id": "product-3", "store_id": "store-1"},
            {"id": "product-4", "store_id": "store-1"},
        ],
        "page": 2,
        "page_size": 2,
        "total": 5,
    }
    assert bad_page.status_code == 422
    assert app.state.mutation_count == 1


@pytest.mark.parametrize(("fault", "status"), [("401", 401), ("429", 429), ("500", 500)])
async def test_simulator_supports_numeric_fault_names(fault: str, status: int) -> None:
    app = create_platform_simulator(fault=fault)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://platform.test"
    ) as client:
        token = await _token(client)
        response = await client.put(
            "/v1/stores/store-1/listings/product-1",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "7" * 64},
            json={"title": "已审批标题"},
        )
    assert response.status_code == status


async def test_simulator_independently_rejects_bad_oauth_and_unissued_tokens() -> None:
    app = create_platform_simulator()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://platform.test"
    ) as client:
        wrong_secret = await client.post(
            "/oauth/token",
            data={
                "client_id": "client-id",
                "client_secret": "wrong",
                "grant_type": "client_credentials",
            },
        )
        wrong_grant = await client.post(
            "/oauth/token",
            data={
                "client_id": "client-id",
                "client_secret": "client-secret",
                "grant_type": "password",
            },
        )
        unissued = await client.put(
            "/v1/stores/store-1/listings/product-1",
            headers={"Authorization": "Bearer token-999", "Idempotency-Key": "0" * 64},
            json={"title": "已审批标题"},
        )
        token = await _token(client)
        null_field = await client.put(
            "/v1/stores/store-1/listings/product-1",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "0" * 64},
            json={"title": None},
        )
    assert wrong_secret.status_code == wrong_grant.status_code == unissued.status_code == 401
    assert null_field.status_code == 422


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_accepted_then_disconnect_replays_over_real_loopback() -> None:
    port = _free_port()
    environment = {**os.environ, "PLATFORM_SIMULATOR_FAULT": "accepted_then_disconnect"}
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tests.support.platform_simulator:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "critical",
        ],
        cwd=os.getcwd(),
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                if httpx.get(f"{base_url}/health/live", timeout=0.1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        else:
            pytest.fail("simulator did not start")

        configured = Settings(
            jwt_secret_key=SecretStr("unit-test-jwt"),
            platform_base_url=base_url,
            platform_client_id=SecretStr("client-id"),
            platform_client_secret=SecretStr("client-secret"),
            platform_timeout_seconds=1,
        )
        client = CommercePlatformClient(configured)
        kwargs = {
            "store_id": "store-1",
            "product_id": "product-1",
            "payload": {"title": "已审批标题"},
            "idempotency_key": "9" * 64,
        }
        with pytest.raises(PlatformClientError) as raised:
            await client.publish_listing(**kwargs)
        assert raised.value.code == "PLATFORM_CONNECTION_FAILED"
        replay = await client.publish_listing(**kwargs)
        assert replay.external_operation_id.startswith("operation-")
        await client.aclose()
    finally:
        process.terminate()
        process.wait(timeout=5)
