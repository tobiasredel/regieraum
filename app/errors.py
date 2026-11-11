"""Einheitliche Fehlerantworten: {"error": {"code", "message"}}."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    """Fehler mit Statuscode, Code und Meldung für Menschen.

    `message` ist der deutsche Text als fester Schlüssel für das Wörterbuch; Variables steht in Platzhaltern
    (`ApiError(404, "not_found", "Folge {id} nicht gefunden", id=7)`). `str(exc)` und `exc.message` liefern den fertigen
    deutschen Text, `template` und `params` gehen an `i18n.tr`, wenn die Antwort in der Sprache der Oberfläche entsteht."""

    def __init__(self, status: int, code: str, message: str, **params):
        from .i18n import tr

        self.template = message
        self.params = params
        self.message = tr("de", message, **params) if params else message
        super().__init__(self.message)
        self.status = status
        self.code = code

    def localized(self, lang: str) -> str:
        from .i18n import tr

        return tr(lang, self.template, **self.params)


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


_HTTP_MESSAGES = {
    404: ("not_found", "Nicht gefunden"),
    405: ("method_not_allowed", "Methode nicht erlaubt"),
    401: ("unauthorized", "Nicht angemeldet"),
    403: ("forbidden", "Dafür fehlt die Berechtigung"),
}


def install(app: FastAPI) -> None:
    def lang(request: Request) -> str:
        from .i18n import ui_lang

        return ui_lang(getattr(request.app.state, "db", None))

    @app.exception_handler(ApiError)
    async def _api(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(error_body(exc.code, exc.localized(lang(request))), status_code=exc.status)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        from .i18n import tr

        code, msg = _HTTP_MESSAGES.get(exc.status_code, ("error", str(exc.detail)))
        return JSONResponse(error_body(code, tr(lang(request), msg)), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _val(request: Request, exc: RequestValidationError) -> JSONResponse:
        from .i18n import tr

        request_word = tr(lang(request), "Anfrage")
        fields = ", ".join(".".join(str(x) for x in e["loc"][1:]) or request_word for e in exc.errors())
        return JSONResponse(error_body("invalid", tr(lang(request), "Ungültige Angaben: {fields}", fields=fields)),
                            status_code=422)
