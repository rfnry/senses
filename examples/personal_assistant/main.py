from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from rfnry_senses import Identity, serve

import harness

ORIGIN = "http://localhost:8000"
JORDAN = Identity(role="user", id="jordan", name="Jordan")


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
    response.set_cookie("person", "jordan", httponly=True, samesite="strict")
    return response


@app.websocket("/senses")
async def senses(ws: WebSocket) -> None:
    if ws.headers.get("origin") != ORIGIN or ws.cookies.get("person") != "jordan":
        await ws.close(code=1008)
        return
    await ws.accept()
    link = await serve(Socket(ws), identity=JORDAN, scope="jordan")
    if link is not None:
        await harness.converse(link)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
