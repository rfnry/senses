from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from rfnry_senses import Identity, serve

import harness

ORIGIN = "http://localhost:8000"
ALEX = Identity(role="user", id="alex", name="Alex")


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
app.mount("/client", StaticFiles(directory="../../packages/typescript/dist", check_dir=False))


@app.get("/")
async def page() -> HTMLResponse:
    response = HTMLResponse(Path("static/index.html").read_text())
    response.set_cookie("technician", "alex", httponly=True, samesite="strict")
    return response


@app.websocket("/senses")
async def senses(ws: WebSocket, plant: str) -> None:
    signed_in = ws.headers.get("origin") == ORIGIN and ws.cookies.get("technician") == "alex"
    if not signed_in or plant not in harness.PLANTS:
        await ws.close(code=1008)
        return
    await ws.accept()
    link = await serve(Socket(ws), identity=ALEX, scope=plant)
    if link is not None:
        await harness.converse(link)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
