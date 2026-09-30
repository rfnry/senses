from pathlib import Path

from fastapi import APIRouter, WebSocket
from fastapi.responses import HTMLResponse
from rfnry_senses import Identity, serve

import harness
from utils import Socket

PAGE = Path(__file__).resolve().parent.parent / "static" / "index.html"
ORIGIN = "http://localhost:8000"
DANA = Identity(role="user", id="dana", name="Dana")

router = APIRouter()


@router.get("/")
async def page() -> HTMLResponse:
    response = HTMLResponse(PAGE.read_text())
    response.set_cookie("customer", "dana", httponly=True, samesite="strict")
    return response


@router.websocket("/senses")
async def senses(ws: WebSocket) -> None:
    if ws.headers.get("origin") != ORIGIN or ws.cookies.get("customer") != "dana":
        await ws.close(code=1008)
        return
    await ws.accept()
    link = await serve(Socket(ws), identity=DANA, scope="acme-home")
    if link is not None:
        await harness.converse(link)
