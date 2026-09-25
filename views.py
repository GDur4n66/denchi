from http import HTTPStatus

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from lnbits.core.models import User
from lnbits.decorators import check_user_exists
from lnbits.helpers import template_renderer

from .receipt import receipt_config, valid_terminal_token

denchi_ext_generic = APIRouter()


@denchi_ext_generic.get("/", response_class=HTMLResponse)
async def index(request: Request, user: User = Depends(check_user_exists)):
    return template_renderer(["denchi/templates"]).TemplateResponse(
        request, "denchi/index.html", {"user": user.json()}
    )


@denchi_ext_generic.get("/lookup", response_class=HTMLResponse)
async def public_lookup(request: Request):
    return template_renderer(["denchi/templates"]).TemplateResponse(
        request, "denchi/lookup.html", {"public": True, "receipt_terminal": False}
    )


@denchi_ext_generic.get("/lookup/print/{token}", response_class=HTMLResponse)
async def printer_terminal_lookup(request: Request, token: str):
    if not valid_terminal_token(token):
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="Page not found.")
    response = template_renderer(["denchi/templates"]).TemplateResponse(
        request,
        "denchi/lookup.html",
        {
            "public": True,
            "receipt_terminal": True,
            "receipt_token": token,
            "receipt_auto_print": receipt_config().auto_print,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
