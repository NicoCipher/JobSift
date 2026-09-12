"""A code has one HTTP status and one recovery class; no exception text escapes."""

from enum import StrEnum

from job_scout.service.schemas import Wire


class ErrorCode(StrEnum):
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INVALID_CURSOR = "INVALID_CURSOR"
    CONFLICT = "CONFLICT"
    SNAPSHOT_EXPIRED = "SNAPSHOT_EXPIRED"
    REPRESENTATION_UNAVAILABLE = "REPRESENTATION_UNAVAILABLE"
    EVIDENCE_SCOPE_UNAVAILABLE = "EVIDENCE_SCOPE_UNAVAILABLE"
    EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    CAPABILITY_NOT_IMPLEMENTED = "CAPABILITY_NOT_IMPLEMENTED"


STATUS = dict(zip(ErrorCode, [401, 403, 404, 422, 400, 409, 409, 409, 409, 503, 429, 500, 501]))
RECOVERY = {
    "SNAPSHOT_EXPIRED": "refresh_snapshot",
    "EVIDENCE_SCOPE_UNAVAILABLE": "adjust_request",
    "EVIDENCE_UNAVAILABLE": "retry_later",
    "RATE_LIMITED": "retry_later",
    "REPRESENTATION_UNAVAILABLE": "choose_postings",
    "INVALID_CURSOR": "restart_traversal",
}


class PublicError(Wire):
    code: ErrorCode
    message: str
    request_id: str
    retryable: bool
    details: dict


class ErrorEnvelope(Wire):
    error: PublicError


class ServiceError(Exception):
    def __init__(self, code: str):
        self.code = ErrorCode(code)
        super().__init__(code)


def public_error(code, request_id):
    code = ErrorCode(code)
    return ErrorEnvelope(
        error=PublicError(
            code=code,
            message=code.value.replace("_", " ").capitalize() + ".",
            request_id=request_id,
            retryable=code in {"EVIDENCE_UNAVAILABLE", "RATE_LIMITED"},
            details={"recovery": RECOVERY[code]}
            if code in RECOVERY
            else {"fields": [{"path": "query", "code": "invalid", "message": "Invalid request."}]}
            if code == "VALIDATION_ERROR"
            else {},
        )
    )
