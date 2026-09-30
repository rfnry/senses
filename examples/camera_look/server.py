from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from rfnry_senses import Identity, Session, Transport, serve

from harness import ask

ORIGIN = "http://localhost:8000"
CLIENT = Path(__file__).parents[2] / "packages/typescript/dist"
ANA = Identity(role="user", id="ana", name="Ana")
cameras: dict[str, Session] = {}


async def watch(link: Transport) -> None:
    async with Session(link, video=True) as session:
        cameras[link.identity.id] = session
        async for _ in session.events():
            pass
        cameras.pop(link.identity.id, None)


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


def signed_in(request: Request) -> Identity:
    if request.cookies.get("guest") != "ana":
        raise HTTPException(status_code=403)
    return ANA


class Question(BaseModel):
    text: str


app = FastAPI()
app.mount("/client", StaticFiles(directory=CLIENT, check_dir=False), name="client")


@app.get("/")
async def page() -> HTMLResponse:
    response = HTMLResponse((Path(__file__).parent / "static/index.html").read_text())
    response.set_cookie("guest", "ana", httponly=True, samesite="strict")
    return response


@app.websocket("/senses")
async def senses(ws: WebSocket) -> None:
    if ws.headers.get("origin") != ORIGIN or ws.cookies.get("guest") != "ana":
        await ws.close(code=1008)
        return
    await ws.accept()
    link = await serve(Socket(ws), identity=ANA, scope="acme")
    if link is not None:
        await watch(link)


@app.post("/ask")
async def answer(question: Question, guest: Identity = Depends(signed_in)) -> dict[str, str]:
    session = cameras.get(guest.id)
    if session is None:
        raise HTTPException(status_code=409, detail="start the camera first")
    return {"answer": await ask(session, question.text)}
