import json
from decimal import Decimal

import httpx
import pytest

from backend.config import Settings
from backend.deepseek_runtime import DeepSeekJsonRuntime


def _runtime(
    transport: httpx.AsyncBaseTransport,
    *,
    api_key: str | None = "test-only-transport-token",
    price: Decimal | None = None,
) -> DeepSeekJsonRuntime:
    return DeepSeekJsonRuntime(
        Settings(
            _env_file=None,
            jwt_secret_key="test-only-secret-at-least-32-characters",
            deepseek_api_key=api_key,
            deepseek_base_url="https://mock.deepseek.invalid",
            deepseek_price_per_million_tokens=price,
        ),
        transport=transport,
    )


def _completion(value: object, usage: object = None) -> httpx.Response:
    body: dict[str, object] = {"choices": [{"message": {"content": json.dumps(value)}}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


async def test_runtime_retries_only_transient_failures_and_renews_before_each_post() -> None:
    responses = iter([httpx.Response(429), httpx.Response(500), _completion({"ok": True})])
    attempts: list[int] = []
    posts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        assert request.url.path == "/chat/completions"
        assert json.loads(request.content)["response_format"] == {"type": "json_object"}
        return next(responses)

    async def renew(attempt: int) -> bool:
        attempts.append(attempt)
        return True

    result = await _runtime(httpx.MockTransport(handler)).request(
        system_prompt="test prompt",
        user_payload={"facts": {"product_id": "p-1"}},
        parse_response=json.loads,
        before_http_attempt=renew,
    )

    assert result.response == {"ok": True}
    assert result.error_code is None
    assert attempts == [1, 2, 3]
    assert posts == len(result.records) == 3


@pytest.mark.parametrize(
    ("failure", "error_code"),
    [
        ("timeout", "DEEPSEEK_TIMEOUT"),
        ("transport", "DEEPSEEK_TRANSPORT"),
        ("rate_limit", "DEEPSEEK_RATE_LIMIT"),
        ("server", "DEEPSEEK_SERVER_ERROR"),
    ],
)
async def test_runtime_exhausts_only_transient_failures(failure: str, error_code: str) -> None:
    posts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        if failure == "timeout":
            raise httpx.TimeoutException("timeout", request=request)
        if failure == "transport":
            raise httpx.ConnectError("transport", request=request)
        return httpx.Response(429 if failure == "rate_limit" else 500)

    result = await _runtime(httpx.MockTransport(handler)).request(
        system_prompt="test prompt", user_payload={"a": 1}, parse_response=json.loads
    )

    assert result.response is None
    assert result.error_code == error_code
    assert posts == len(result.records) == 3


@pytest.mark.parametrize(
    ("response", "parser", "error_code"),
    [
        (httpx.Response(400), json.loads, "DEEPSEEK_HTTP_ERROR"),
        (httpx.Response(401), json.loads, "DEEPSEEK_UNAUTHORIZED"),
        (httpx.Response(403), json.loads, "DEEPSEEK_FORBIDDEN"),
        (httpx.Response(200, json={}), json.loads, "DEEPSEEK_SCHEMA_INVALID"),
        (_completion({"ok": True}), lambda _content: (_ for _ in ()).throw(ValueError()), "DEEPSEEK_SCHEMA_INVALID"),
    ],
)
async def test_runtime_does_not_retry_http_or_schema_failures(response, parser, error_code: str) -> None:
    posts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return response

    result = await _runtime(httpx.MockTransport(handler)).request(
        system_prompt="test prompt", user_payload={"a": 1}, parse_response=parser
    )

    assert result.response is None
    assert result.error_code == error_code
    assert posts == len(result.records) == 1


async def test_runtime_stops_before_http_for_missing_key_or_lost_lease() -> None:
    posts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return _completion({"ok": True})

    async def lost_lease(_attempt: int) -> bool:
        return False

    lease_lost = await _runtime(httpx.MockTransport(handler)).request(
        system_prompt="test prompt", user_payload={"a": 1}, parse_response=json.loads,
        before_http_attempt=lost_lease,
    )
    missing = await _runtime(httpx.MockTransport(handler), api_key=None).request(
        system_prompt="test prompt", user_payload={"a": 1}, parse_response=json.loads
    )
    blank = await _runtime(httpx.MockTransport(handler), api_key="   ").request(
        system_prompt="test prompt", user_payload={"a": 1}, parse_response=json.loads
    )

    assert (lease_lost.error_code, missing.error_code, blank.error_code) == (
        "LEASE_LOST", "DEEPSEEK_KEY_MISSING", "DEEPSEEK_KEY_MISSING"
    )
    assert lease_lost.records == missing.records == blank.records == []
    assert posts == 0


async def test_runtime_canonical_hash_usage_cost_and_safe_records() -> None:
    usage = {"prompt_tokens": "invalid", "completion_tokens": -1, "total_tokens": True}

    async def handler(_request: httpx.Request) -> httpx.Response:
        return _completion({"ok": True}, usage)

    first = await _runtime(httpx.MockTransport(handler)).request(
        system_prompt="test prompt", user_payload={"facts": {"b": 1, "a": 2}}, parse_response=json.loads
    )
    second = await _runtime(httpx.MockTransport(handler), price=Decimal("2.5")).request(
        system_prompt="test prompt", user_payload={"facts": {"a": 2, "b": 1}}, parse_response=json.loads
    )

    assert first.records[0].input_hash == second.records[0].input_hash
    assert (first.records[0].prompt_tokens, first.records[0].completion_tokens, first.records[0].total_tokens) == (0, 0, 0)
    assert first.records[0].estimated_cost is None
    assert second.records[0].estimated_cost == Decimal("0.000000")
    assert not {"prompt", "payload", "raw_response", "headers", "authorization", "key"} & set(vars(first.records[0]))


async def test_runtime_quantizes_nonnegative_cost_and_rejects_invalid_attempt_limits() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return _completion({"ok": True}, {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8})

    runtime = _runtime(httpx.MockTransport(handler), price=Decimal("2.5"))
    result = await runtime.request(system_prompt="test prompt", user_payload={"a": 1}, parse_response=json.loads)
    assert result.records[0].estimated_cost == Decimal("0.000020")
    one_attempt = await runtime.request(
        system_prompt="test prompt", user_payload={"a": 1}, parse_response=json.loads, max_attempts=1
    )
    assert len(one_attempt.records) == 1
    for max_attempts in (0, 4):
        with pytest.raises(ValueError):
            await runtime.request(
                system_prompt="test prompt", user_payload={"a": 1}, parse_response=json.loads,
                max_attempts=max_attempts,
            )
