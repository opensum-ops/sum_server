"""Domain error hierarchy + FastAPI exception handlers.

All service-level errors subclass :class:`AppError`. The handler maps them to a
stable JSON envelope so callers can rely on a consistent error shape.
"""
from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger(__name__)

class AppError(Exception):
    code: str = "app.error"
    http_status: int = 500
    message: str = "internal error"

    def __init__ (
            self,
            message: str | None = None,
            *, 
            details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or self.message)
        if message:
            self.message = message
        self.details = details or {}

class NotFoundError(AppError):
    code = "not_found"
    http_status = 404
    message = "resource not found"

class ConflictError(AppError):
    code = "conflict"
    http_status = 409
    message = "conflict"

class ForbiddenError(AppError):
    code = "forbidden"
    http_status = 403
    message = "forbidden"

class AuthError(AppError):
    code = "unauthorized"
    http_status = 401
    message = "unauthorized"

class InvalidInputError(AppError):
    code = "invalid"
    http_status = 422
    message = "invalid input"

class SignatureError(AppError):
    code = "bad_signature"
    http_status = 400
    message = "invalid signature"

class EnrollmentError(AppError):
    code = "enrollment_failed"
    http_status = 409
    message = "enrollment token invalid or already used"

class PreconditionFailedError(AppError):
    code = "precondition_failed"
    http_status = 412
    message = "precondition failed"

def _envelope(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}

def install_error_handlers(app:FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_req: Request, exc:AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=_envelope(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_req: Request, exc:RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_envelope("invalid", "invalid request", {"errors": exc.errors()}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _starlette_http(_req: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope("http_error", str(exc.detail), {}),
        )
    
    @app.exception_handler(Exception)
    async def _unhandled(req: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_exception", path=req.url.path, error=str(exc))
        return JSONResponse(
            status_code=500,
            content=_envelope("internal_error", "internal error", {}),
        )