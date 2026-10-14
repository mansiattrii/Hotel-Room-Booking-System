from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    """An error that maps directly to an HTTP response with the standard error body."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_body(code: str, message: str, details: object | None = None) -> dict[str, object]:
    """Build the error envelope shared by every non-2xx response."""
    error: dict[str, object] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"error": error}


_HTTP_CODES = {
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_409_CONFLICT: "conflict",
}


async def _app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=error_body(exc.code, exc.message))


async def _http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _HTTP_CODES.get(exc.status_code, "http_error")
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(code, str(exc.detail)),
        headers=exc.headers,
    )


async def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=error_body(
            "validation_error", "Request validation failed", jsonable_encoder(exc.errors())
        ),
    )


def register_error_handlers(app: FastAPI) -> None:
    """Make every error response use the {"error": {"code", "message"}} shape."""
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
