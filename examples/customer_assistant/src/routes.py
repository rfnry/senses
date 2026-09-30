from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from rfnry_senses import Identity

import server

PAGE = Path(__file__).resolve().parent.parent / "static" / "index.html"
ORIGIN = "http://localhost:8000"
DANA = Identity(role="user", id="dana", name="Dana")

router = APIRouter()


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
    response.set_cookie("customer", "dana", httponly=True, samesite="strict")
    return response


@router.websocket("/senses")
async def senses(ws: WebSocket) -> None:
    if ws.headers.get("origin") != ORIGIN or ws.cookies.get("customer") != "dana":
        await ws.close(code=1008)
        return
    await ws.accept()
    await server.converse(Socket(ws), identity=DANA, scope="acme-home")
