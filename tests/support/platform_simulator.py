from __future__ import annotations

import asyncio
import hashlib
import json
import os
from typing import Annotated

from fastapi import FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import ValidationError

from backend.platform_client import _ListingPatch


def create_platform_simulator(*, fault: str | None = None) -> FastAPI:
    app = FastAPI()
    app.state.fault = fault or os.getenv("PLATFORM_SIMULATOR_FAULT")
    app.state.token_requests = 0
    app.state.write_idempotency_keys = []
    app.state.mutation_count = 0
    app.state.operations = {}
    app.state.disconnect_used = False

    @app.get("/health/live")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/oauth/token")
    async def token(
        client_id: Annotated[str, Form()], client_secret: Annotated[str, Form()]
    ) -> dict[str, str]:
        if not client_id or not client_secret:
            raise HTTPException(401)
        app.state.token_requests += 1
        return {
            "access_token": f"token-{app.state.token_requests}",
            "token_type": "bearer",
        }

    def authorize(authorization: str | None) -> None:
        if not authorization or not authorization.startswith("Bearer token-"):
            raise HTTPException(401)

    @app.put("/v1/stores/{store_id}/listings/{product_id}")
    async def publish(
        store_id: str,
        product_id: str,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        app.state.write_idempotency_keys.append(idempotency_key)
        authorize(authorization)
        if app.state.fault == "expire_first_token" and authorization == "Bearer token-1":
            raise HTTPException(401)
        if app.state.fault in {"unauthorized", "401"}:
            raise HTTPException(401)
        if app.state.fault == "forbidden":
            raise HTTPException(403)
        if app.state.fault in {"rate_limited", "429"}:
            raise HTTPException(429, headers={"Retry-After": "7"})
        if app.state.fault in {"server_error", "500"}:
            raise HTTPException(500)
        if app.state.fault == "timeout":
            await asyncio.sleep(1)
        if idempotency_key is None or len(idempotency_key) != 64 or any(
            c not in "0123456789abcdef" for c in idempotency_key
        ):
            raise HTTPException(422)
        try:
            payload = _ListingPatch.model_validate(await request.json()).model_dump(exclude_none=True)
        except (ValueError, ValidationError):
            raise HTTPException(422) from None
        digest = hashlib.sha256(
            json.dumps(
                [store_id, product_id, payload], ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        prior = app.state.operations.get(idempotency_key)
        if prior is not None and prior[0] != digest:
            raise HTTPException(409)
        if prior is None:
            operation_id = f"operation-{digest[:24]}"
            app.state.operations[idempotency_key] = (digest, operation_id)
            app.state.mutation_count += 1
        else:
            operation_id = prior[1]
        if app.state.fault == "malformed_json":
            return PlainTextResponse("not-json")
        if app.state.fault == "accepted_then_disconnect" and not app.state.disconnect_used:
            app.state.disconnect_used = True

            async def broken_body():
                yield b'{"external_operation_id":"'
                raise RuntimeError("simulated disconnect")

            return StreamingResponse(broken_body(), media_type="application/json")
        return {"external_operation_id": operation_id}

    def page(kind: str, store_id: str, number: int, size: int) -> dict[str, object]:
        return {
            "items": [
                {"id": f"{kind}-{index}", "store_id": store_id}
                for index in range((number - 1) * size + 1, min(number * size, 5) + 1)
            ],
            "page": number,
            "page_size": size,
            "total": 5,
        }

    async def read_page(
        kind: str, store_id: str, page_number: int, page_size: int, authorization: str | None
    ) -> dict[str, object]:
        authorize(authorization)
        return page(kind, store_id, page_number, page_size)

    @app.get("/v1/stores/{store_id}/products")
    async def products(
        store_id: str,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        return await read_page("product", store_id, page, page_size, authorization)

    @app.get("/v1/stores/{store_id}/orders")
    async def orders(
        store_id: str,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        return await read_page("order", store_id, page, page_size, authorization)

    @app.get("/v1/stores/{store_id}/inventory")
    async def inventory(
        store_id: str,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        return await read_page("inventory", store_id, page, page_size, authorization)

    return app


app = create_platform_simulator()
