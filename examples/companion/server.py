import asyncio
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from rfnry_senses import Identity, serve

from harness import companion, narrate

ORIGIN = "http://localhost:8000"
CLIENT = Path(__file__).parents[2] / "packages/typescript/dist"
ANA = Identity(role="user", id="ana", name="Ana")


class Socket:
    def __init__(self, ws: WebSocket) -> None:
        self.ws, self.send_text, self.send_bytes, self.close = (
            ws,
            ws.send_text,
            ws.send_bytes,
            ws.close,
        )

    async def receive(self) -> str | bytes:
        message = await self.ws.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        return message["text"] if message.get("text") is not None else message["bytes"]


app = FastAPI()
app.mount("/client", StaticFiles(directory=CLIENT, check_dir=False), name="client")


@app.get("/")
async def page() -> HTMLResponse:
    response = HTMLResponse((Path(__file__).parent / "static/index.html").read_text())
    response.set_cookie("person", "ana", httponly=True, samesite="strict")
    return response


@app.websocket("/senses")
async def senses(ws: WebSocket) -> None:
    if ws.headers.get("origin") != ORIGIN or ws.cookies.get("person") != "ana":
        await ws.close(code=1008)
        return
    await ws.accept()
    link = await serve(Socket(ws), identity=ANA, scope="home")
    if link is not None:
        async with await companion(link) as session:
            await narrate(session, asyncio.Event())
