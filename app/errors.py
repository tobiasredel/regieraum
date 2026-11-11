"""Einheitliche Fehlerantworten: {"error": {"code", "message"}}."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


_HTTP_MESSAGES = {
    404: ("not_found", "Nicht gefunden"),
    405: ("method_not_allowed", "Methode nicht erlaubt"),
    401: ("unauthorized", "Nicht angemeldet"),
    403: ("forbidden", "Dafür fehlt die Berechtigung"),
}


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, msg = _HTTP_MESSAGES.get(exc.status_code, ("error", str(exc.detail)))
        return JSONResponse(error_body(code, msg), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _val(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = ", ".join(".".join(str(x) for x in e["loc"][1:]) or "Anfrage" for e in exc.errors())
        return JSONResponse(error_body("invalid", f"Ungültige Angaben: {fields}"), status_code=422)
