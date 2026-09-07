from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from backend.config import Settings


class PlatformClientError(Exception):
    def __init__(
        self, code: str, retryable: bool, retry_after_seconds: int | None = None
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class PlatformPublishResult:
    external_operation_id: str


class _ListingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    title: str | None = Field(default=None, min_length=1, max_length=60)
    selling_points: list[Annotated[str, Field(min_length=1, max_length=80)]] | None = Field(
        default=None, min_length=1, max_length=5
    )
    description: str | None = Field(default=None, min_length=1, max_length=10428)
    keywords: list[Annotated[str, Field(min_length=1, max_length=32)]] | None = Field(
        default=None, min_length=1, max_length=20
    )
    attribute_completions: dict[str, str | int | float | bool | None] | None = Field(
        default=None, max_length=50
    )

    @model_validator(mode="after")
    def validate_patch(self) -> "_ListingPatch":
        if not self.model_fields_set:
            raise ValueError("empty patch")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("null patch field")
        if self.attribute_completions is not None:
            for key, value in self.attribute_completions.items():
                if not 1 <= len(key) <= 64 or isinstance(value, str) and len(value) > 512:
                    raise ValueError("invalid attribute completion")
        return self


class _TokenResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    access_token: str = Field(min_length=1, max_length=2048)
    token_type: str


class _PublishResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_operation_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )


class CommercePlatformClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=(settings.platform_base_url or "http://platform.invalid").rstrip("/"),
            timeout=httpx.Timeout(settings.platform_timeout_seconds),
            transport=transport,
        )
        self._access_token: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _token(self, *, force: bool = False) -> str:
        if self._access_token is not None and not force:
            return self._access_token
        client_id = self._settings.platform_client_id
        client_secret = self._settings.platform_client_secret
        if not self._settings.platform_base_url or client_id is None or client_secret is None:
            raise PlatformClientError("PLATFORM_REQUEST_INVALID", False)
        response = await self._request(
            "POST",
            "/oauth/token",
            data={
                "client_id": client_id.get_secret_value(),
                "client_secret": client_secret.get_secret_value(),
                "grant_type": "client_credentials",
            },
        )
        if response.status_code != 200:
            if response.status_code == 429:
                raise self._status_error(response)
            if 500 <= response.status_code <= 599:
                raise self._status_error(response)
            raise PlatformClientError("PLATFORM_AUTH_FAILED", False)
        invalid = False
        try:
            token = _TokenResponse.model_validate(response.json())
            if token.token_type.lower() != "bearer":
                raise ValueError
        except (ValueError, ValidationError):
            invalid = True
        if invalid:
            raise PlatformClientError("PLATFORM_RESPONSE_INVALID", False)
        self._access_token = token.access_token
        return token.access_token

    async def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        error: PlatformClientError | None = None
        try:
            return await self._client.request(method, url, **kwargs)
        except httpx.TimeoutException:
            error = PlatformClientError("PLATFORM_TIMEOUT", True)
        except httpx.TransportError:
            error = PlatformClientError("PLATFORM_CONNECTION_FAILED", True)
        except Exception:
            error = PlatformClientError("PLATFORM_CONNECTION_FAILED", True)
        raise error

    async def _post_listing(
        self,
        token: str,
        store_id: str,
        product_id: str,
        payload: dict[str, object],
        idempotency_key: str,
    ) -> httpx.Response:
        return await self._request(
            "PUT",
            f"/v1/stores/{store_id}/listings/{product_id}",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": idempotency_key},
            json=payload,
        )

    async def publish_listing(
        self,
        *,
        store_id: str,
        product_id: str,
        payload: dict[str, object],
        idempotency_key: str,
    ) -> PlatformPublishResult:
        invalid = False
        try:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,35}", store_id) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_-]{0,35}", product_id
            ):
                raise ValueError
            if len(idempotency_key) != 64 or any(c not in "0123456789abcdef" for c in idempotency_key):
                raise ValueError
            validated = _ListingPatch.model_validate(payload).model_dump(exclude_none=True)
        except (TypeError, ValueError, ValidationError):
            invalid = True
        if invalid:
            raise PlatformClientError("PLATFORM_REQUEST_INVALID", False)

        token = await self._token()
        response = await self._post_listing(
            token, store_id, product_id, validated, idempotency_key
        )
        if response.status_code == 401:
            token = await self._token(force=True)
            response = await self._post_listing(
                token, store_id, product_id, validated, idempotency_key
            )
        return self._validated_result(response)

    @staticmethod
    def _validated_result(response: httpx.Response) -> PlatformPublishResult:
        status = response.status_code
        if status == 401:
            raise PlatformClientError("PLATFORM_AUTH_FAILED", False)
        if status == 403:
            raise PlatformClientError("PLATFORM_FORBIDDEN", False)
        if status == 409:
            raise PlatformClientError("PLATFORM_IDEMPOTENCY_CONFLICT", False)
        if status == 429:
            raise CommercePlatformClient._status_error(response)
        if 500 <= status <= 599:
            raise PlatformClientError("PLATFORM_SERVER_ERROR", True)
        if not 200 <= status <= 299:
            raise PlatformClientError("PLATFORM_REQUEST_REJECTED", False)
        invalid = False
        try:
            parsed = _PublishResponse.model_validate(response.json())
        except (ValueError, ValidationError):
            invalid = True
        if invalid:
            raise PlatformClientError("PLATFORM_RESPONSE_INVALID", False)
        return PlatformPublishResult(parsed.external_operation_id)

    @staticmethod
    def _status_error(response: httpx.Response) -> PlatformClientError:
        if response.status_code == 429:
            value = response.headers.get("Retry-After", "")
            seconds = int(value) if len(value) <= 2 and value.isascii() and value.isdecimal() else -1
            return PlatformClientError(
                "PLATFORM_RATE_LIMITED", True, seconds if 0 <= seconds <= 60 else None
            )
        return PlatformClientError("PLATFORM_SERVER_ERROR", True)
