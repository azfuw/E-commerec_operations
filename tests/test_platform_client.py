import httpx
import pytest
from pydantic import SecretStr

from backend.config import Settings
from backend.platform_client import CommercePlatformClient, PlatformClientError
from tests.support.platform_simulator import create_platform_simulator


def settings() -> Settings:
    return Settings(
        jwt_secret_key=SecretStr("unit-test-jwt"),
        platform_base_url="http://platform.test",
        platform_client_id=SecretStr("client-id"),
        platform_client_secret=SecretStr("client-secret"),
        platform_timeout_seconds=0.05,
    )


async def test_client_refreshes_once_and_reuses_the_idempotency_key() -> None:
    app = create_platform_simulator(fault="expire_first_token")
    client = CommercePlatformClient(settings(), transport=httpx.ASGITransport(app=app))

    result = await client.publish_listing(
        store_id="store-1",
        product_id="product-1",
        payload={"title": "已审批标题"},
        idempotency_key="a" * 64,
    )

    assert result.external_operation_id.startswith("operation-")
    assert app.state.token_requests == 2
    assert app.state.write_idempotency_keys == ["a" * 64, "a" * 64]
    await client.aclose()


async def test_same_key_same_body_is_one_remote_side_effect_and_changed_body_conflicts() -> None:
    app = create_platform_simulator()
    client = CommercePlatformClient(settings(), transport=httpx.ASGITransport(app=app))
    kwargs = {
        "store_id": "store-1",
        "product_id": "product-1",
        "payload": {"title": "已审批标题"},
        "idempotency_key": "b" * 64,
    }

    first = await client.publish_listing(**kwargs)
    second = await client.publish_listing(**kwargs)
    assert first == second
    assert app.state.mutation_count == 1

    with pytest.raises(PlatformClientError) as raised:
        await client.publish_listing(**{**kwargs, "payload": {"title": "另一个标题"}})
    assert (raised.value.code, raised.value.retryable) == (
        "PLATFORM_IDEMPOTENCY_CONFLICT",
        False,
    )
    await client.aclose()


@pytest.mark.parametrize(
    ("fault", "code", "retryable", "retry_after"),
    [
        ("unauthorized", "PLATFORM_AUTH_FAILED", False, None),
        ("forbidden", "PLATFORM_FORBIDDEN", False, None),
        ("rate_limited", "PLATFORM_RATE_LIMITED", True, 7),
        ("server_error", "PLATFORM_SERVER_ERROR", True, None),
        ("malformed_json", "PLATFORM_RESPONSE_INVALID", False, None),
    ],
)
async def test_client_maps_http_failures_to_closed_errors(
    fault: str, code: str, retryable: bool, retry_after: int | None
) -> None:
    app = create_platform_simulator(fault=fault)
    client = CommercePlatformClient(settings(), transport=httpx.ASGITransport(app=app))

    with pytest.raises(PlatformClientError) as raised:
        await client.publish_listing(
            store_id="store-1",
            product_id="product-1",
            payload={"title": "已审批标题"},
            idempotency_key="c" * 64,
        )
    assert (raised.value.code, raised.value.retryable, raised.value.retry_after_seconds) == (
        code,
        retryable,
        retry_after,
    )
    assert str(raised.value) == code
    await client.aclose()


@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (httpx.ReadTimeout("private timeout detail"), "PLATFORM_TIMEOUT"),
        (httpx.ConnectError("private connection detail"), "PLATFORM_CONNECTION_FAILED"),
    ],
)
async def test_client_closes_transport_failures(exception: Exception, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "token-1", "token_type": "bearer"})
        raise exception

    client = CommercePlatformClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(PlatformClientError) as raised:
        await client.publish_listing(
            store_id="store-1",
            product_id="product-1",
            payload={"title": "已审批标题"},
            idempotency_key="d" * 64,
        )
    assert (raised.value.code, raised.value.retryable, str(raised.value)) == (code, True, code)
    await client.aclose()


async def test_client_rejects_unsafe_operation_id_as_invalid_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "token-1", "token_type": "bearer"})
        return httpx.Response(200, json={"external_operation_id": "raw response content"})

    client = CommercePlatformClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(PlatformClientError) as raised:
        await client.publish_listing(
            store_id="store-1",
            product_id="product-1",
            payload={
                "description": "已审批描述",
                "attribute_completions": {"材质": "棉", "可回收": True},
            },
            idempotency_key="8" * 64,
        )
    assert (raised.value.code, str(raised.value)) == (
        "PLATFORM_RESPONSE_INVALID",
        "PLATFORM_RESPONSE_INVALID",
    )
    await client.aclose()


@pytest.mark.parametrize(
    "changes",
    [
        {"store_id": ""},
        {"product_id": ""},
        {"idempotency_key": "g" * 64},
        {"payload": {}},
        {"payload": {"price": 1}},
        {"payload": {"title": ""}},
        {"payload": {"selling_points": ["x"] * 6}},
    ],
)
async def test_client_rejects_invalid_requests_before_network(changes: dict[str, object]) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    client = CommercePlatformClient(settings(), transport=httpx.MockTransport(handler))
    kwargs: dict[str, object] = {
        "store_id": "store-1",
        "product_id": "product-1",
        "payload": {"title": "已审批标题"},
        "idempotency_key": "e" * 64,
    }
    with pytest.raises(PlatformClientError) as raised:
        await client.publish_listing(**{**kwargs, **changes})
    assert (raised.value.code, raised.value.retryable, requests) == (
        "PLATFORM_REQUEST_INVALID",
        False,
        0,
    )
