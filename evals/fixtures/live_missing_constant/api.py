"""A tiny API surface."""

from codes import HTTP_CREATED, HTTP_OK


def ok(body: str) -> tuple[int, str]:
    return HTTP_OK, body


def created(body: str) -> tuple[int, str]:
    return HTTP_CREATED, body
