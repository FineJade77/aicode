from __future__ import annotations

import hmac
import os
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, Response

RUNTIME_TOKEN = os.getenv("AICODE_RUNTIME_TOKEN", "")
UNAUTHENTICATED_PATHS = {"/v1/daemon/status"}
_BEARER_PREFIX = "Bearer "


def _extract_bearer_token(header_value: str) -> str:
    if not header_value.startswith(_BEARER_PREFIX):
        return ""
    return header_value[len(_BEARER_PREFIX) :]


def is_authorized(header_value: str) -> bool:
    if not RUNTIME_TOKEN:
        return True
    provided = _extract_bearer_token(header_value)
    return hmac.compare_digest(provided, RUNTIME_TOKEN)


async def auth_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    if request.url.path in UNAUTHENTICATED_PATHS:
        return await call_next(request)
    if not is_authorized(request.headers.get("authorization", "")):
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await call_next(request)
