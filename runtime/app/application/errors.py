from __future__ import annotations


class ApplicationError(Exception):
    status_code = 500


class InvalidRequest(ApplicationError):
    status_code = 400


class NotFound(ApplicationError):
    status_code = 404


class Conflict(ApplicationError):
    status_code = 409


class ProviderUnavailable(InvalidRequest):
    pass
