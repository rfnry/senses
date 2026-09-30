from pathlib import Path

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from rfnry_senses import Identity

import server

PAGE = Path(__file__).resolve().parent.parent / "static" / "index.html"
ORIGIN = "http://localhost:8000"
AVERY = Identity(role="user", id="avery", name="Avery")

router = APIRouter()


class Ask(BaseModel):
    question: str


class Socket:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.send_text = ws.send_text
        self.send_bytes = ws.send_bytes
        self.close = ws.close

    async def receive(self) -> str | bytes:
        message = await self.ws.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        return message["text"] if message.get("text") is not None else message["bytes"]


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
    await server.share(Socket(ws), identity=AVERY, scope="firm-acme")


@router.post("/ask")
async def ask(body: Ask) -> dict:
    if not server.screens:
        raise HTTPException(409, "no screen is shared")
    return await server.ask(body.question)
