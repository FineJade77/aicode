from __future__ import annotations

import hmac
import os
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, Response

UNAUTHENTICATED_PATHS = {"/v1/daemon/status"}
TOKEN_ENV = "AICODE_RUNTIME_TOKEN"
ANONYMOUS_ENV = "AICODE_ALLOW_ANONYMOUS"
_BEARER_PREFIX = "Bearer "
_TRUTHY = {"1", "true", "yes", "on"}

NO_TOKEN_DETAIL = (
    "runtime token is not configured; start the Runtime with `aicode runtime start` "
    f"(which generates one), set {TOKEN_ENV}, or set {ANONYMOUS_ENV}=1 to accept unauthenticated "
    "local requests"
)


def runtime_token() -> str:
    """Read the token per request rather than at import time.

    Import-time capture made the module order-dependent and impossible to
    reconfigure without reimporting, which conflicts with building the
    application runtime inside the ASGI lifespan.
    """
    return os.getenv(TOKEN_ENV, "")


def anonymous_allowed() -> bool:
    return os.getenv(ANONYMOUS_ENV, "").strip().casefold() in _TRUTHY


def is_authorized(header_value: str) -> bool:
    token = runtime_token()
    if not token:
        # Fail closed. Any local process can otherwise forge approvals — accepting
        # an edit or a risky shell command on the user's behalf — so an
        # unconfigured token must not mean "no authentication required".
        # Unauthenticated access stays possible, but only as an explicit opt-in.
        return anonymous_allowed()
    provided = _extract_bearer_token(header_value)
    return hmac.compare_digest(provided, token)


async def auth_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    if request.url.path in UNAUTHENTICATED_PATHS:
        return await call_next(request)
    if not is_authorized(request.headers.get("authorization", "")):
        detail = "unauthorized" if runtime_token() else NO_TOKEN_DETAIL
        return JSONResponse(status_code=401, content={"detail": detail})
    return await call_next(request)


def _extract_bearer_token(header_value: str) -> str:
    if not header_value.startswith(_BEARER_PREFIX):
        return ""
    return header_value[len(_BEARER_PREFIX) :]
