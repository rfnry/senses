from pathlib import Path

from fastapi import APIRouter, HTTPException, WebSocket
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from rfnry_senses import Identity, serve

import harness
from utils import Socket

PAGE = Path(__file__).resolve().parent.parent / "static" / "index.html"
ORIGIN = "http://localhost:8000"
AVERY = Identity(role="user", id="avery", name="Avery")

router = APIRouter()


class Ask(BaseModel):
    question: str


@router.get("/")
async def page() -> HTMLResponse:
    response = HTMLResponse(PAGE.read_text())
    response.set_cookie("lawyer", "avery", httponly=True, samesite="strict")
    return response


@router.websocket("/senses")
async def senses(ws: WebSocket) -> None:
    if ws.headers.get("origin") != ORIGIN or ws.cookies.get("lawyer") != "avery":
        await ws.close(code=1008)
        return
    await ws.accept()
    link = await serve(Socket(ws), identity=AVERY, scope="firm-acme")
    if link is not None:
        await harness.share(link)


@router.post("/ask")
async def ask(body: Ask) -> dict:
    if not harness.screens:
        raise HTTPException(409, "no screen is shared")
    return await harness.ask(body.question)
