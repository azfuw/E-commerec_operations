import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from time import perf_counter
from typing import Generic, TypeVar

import httpx

from backend.config import Settings

T = TypeVar("T")
JsonParser = Callable[[str], T]
BeforeHttpAttempt = Callable[[int], Awaitable[bool]]


@dataclass(frozen=True)
class DeepSeekAttemptRecord:
    attempt: int
    model: str
    status: str
    input_hash: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    duration_ms: int
    estimated_cost: Decimal | None
    error_code: str | None


@dataclass(frozen=True)
class DeepSeekJsonResult(Generic[T]):
    response: T | None
    records: list[DeepSeekAttemptRecord]
    error_code: str | None


def _nonnegative_token(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        token = int(value)
    except (TypeError, ValueError):
        return 0
    return token if token >= 0 else 0


class DeepSeekJsonRuntime:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def request(
        self,
        *,
        system_prompt: str,
        user_payload: Mapping[str, object],
        parse_response: JsonParser[T],
        before_http_attempt: BeforeHttpAttempt | None = None,
        max_attempts: int = 3,
    ) -> DeepSeekJsonResult[T]:
        if not 1 <= max_attempts <= 3:
            raise ValueError("max_attempts must be between 1 and 3")
        api_key = self.settings.deepseek_api_key
        if api_key is None or not api_key.get_secret_value().strip():
            return DeepSeekJsonResult(response=None, records=[], error_code="DEEPSEEK_KEY_MISSING")

        canonical_payload = json.dumps(
            user_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        input_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        payload = {
            "model": self.settings.deepseek_model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": canonical_payload},
            ],
        }
        records: list[DeepSeekAttemptRecord] = []
        timeout = httpx.Timeout(self.settings.deepseek_timeout_seconds)
        headers = {"Authorization": f"Bearer {api_key.get_secret_value()}"}
        async with httpx.AsyncClient(
            base_url=self.settings.deepseek_base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=self.transport,
        ) as http_client:
            for attempt in range(1, max_attempts + 1):
                if before_http_attempt is not None and not await before_http_attempt(attempt):
                    return DeepSeekJsonResult(response=None, records=records, error_code="LEASE_LOST")
                started = perf_counter()
                response: httpx.Response | None = None
                try:
                    response = await http_client.post("/chat/completions", json=payload)
                except httpx.TimeoutException:
                    error_code = "DEEPSEEK_TIMEOUT"
                except httpx.TransportError:
                    error_code = "DEEPSEEK_TRANSPORT"
                else:
                    if response.status_code == 429:
                        error_code = "DEEPSEEK_RATE_LIMIT"
                    elif 500 <= response.status_code <= 599:
                        error_code = "DEEPSEEK_SERVER_ERROR"
                    elif response.status_code == 401:
                        error_code = "DEEPSEEK_UNAUTHORIZED"
                    elif response.status_code == 403:
                        error_code = "DEEPSEEK_FORBIDDEN"
                    elif response.is_success:
                        try:
                            content = response.json()["choices"][0]["message"]["content"]
                            if not isinstance(content, str):
                                raise ValueError
                            parsed = parse_response(content)
                        except (KeyError, IndexError, TypeError, ValueError):
                            error_code = "DEEPSEEK_SCHEMA_INVALID"
                        else:
                            records.append(self._record(attempt, input_hash, "succeeded", None, response, started))
                            return DeepSeekJsonResult(response=parsed, records=records, error_code=None)
                    else:
                        error_code = "DEEPSEEK_HTTP_ERROR"

                records.append(self._record(attempt, input_hash, "failed", error_code, response, started))
                if error_code not in {
                    "DEEPSEEK_TIMEOUT",
                    "DEEPSEEK_TRANSPORT",
                    "DEEPSEEK_RATE_LIMIT",
                    "DEEPSEEK_SERVER_ERROR",
                } or attempt == max_attempts:
                    return DeepSeekJsonResult(response=None, records=records, error_code=error_code)

        raise AssertionError("request loop must return")

    def _record(
        self,
        attempt: int,
        input_hash: str,
        status: str,
        error_code: str | None,
        response: httpx.Response | None,
        started: float,
    ) -> DeepSeekAttemptRecord:
        try:
            body = response.json() if response is not None else {}
        except ValueError:
            body = {}
        usage = body.get("usage") if isinstance(body, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        total_tokens = _nonnegative_token(usage.get("total_tokens"))
        price = self.settings.deepseek_price_per_million_tokens
        estimated_cost = (
            None
            if price is None
            else (Decimal(total_tokens) * max(price, Decimal("0")) / Decimal(1_000_000)).quantize(
                Decimal("0.000001")
            )
        )
        return DeepSeekAttemptRecord(
            attempt=attempt,
            model=self.settings.deepseek_model,
            status=status,
            input_hash=input_hash,
            prompt_tokens=_nonnegative_token(usage.get("prompt_tokens")),
            completion_tokens=_nonnegative_token(usage.get("completion_tokens")),
            total_tokens=total_tokens,
            duration_ms=int((perf_counter() - started) * 1000),
            estimated_cost=estimated_cost,
            error_code=error_code,
        )
