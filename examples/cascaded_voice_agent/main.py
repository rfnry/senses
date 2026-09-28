import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect, WebSocketException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from rfnry_engine import Engine, TextDelta
from rfnry_senses import (
    AudioFrame,
    Closed,
    Heard,
    Identity,
    Loopback,
    Session,
    Transport,
    TransportError,
    TurnEnded,
    serve,
)

from providers import AnthropicProvider, GoogleSpeech, OpenAIProvider

ORIGIN = "http://localhost:8000"
CLIENT = Path(__file__).parents[2] / "packages/typescript/dist"
engine = Engine("agents", generation=AnthropicProvider())
voice = GoogleSpeech()


async def reply(text: str, scope: str, history: list[dict[str, str]]) -> AsyncIterator[str]:
    chunks: asyncio.Queue[str | None] = asyncio.Queue()

    async def forward(event: object) -> None:
        if isinstance(event, TextDelta):
            chunks.put_nowait(event.text)

    async def run() -> None:
        try:
            await engine.run(
                "concierge", scope=scope, input=text, history=history, on_event=forward
            )
        finally:
            chunks.put_nowait(None)

    running = asyncio.create_task(run())
    try:
        while (chunk := await chunks.get()) is not None:
            yield chunk
        await running
    finally:
        running.cancel()


async def converse(link: Transport, replies: int | None = None) -> None:
    history: list[dict[str, str]] = []
    async with Session(link, transcription=OpenAIProvider(), speech=voice) as session:
        async for event in session.events():
            match event:
                case Heard(final=True, text=text, speaker=speaker):
                    print(f"heard  {speaker.name}: {text}")
                case TurnEnded(text=text):
                    try:
                        said = await session.say(
                            reply(text, session.scope, history), triggered_by=event.speaker
                        )
                    except TransportError:
                        return
                    print(f"said   {said.heard!r} interrupted={said.interrupted}")
                    history += [
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": said.heard},
                    ]
                    replies = None if replies is None else replies - 1
                    if replies == 0:
                        return
                case Closed(reason=reason, message=message):
                    print(f"closed {reason}: {message}")


async def guest_says(link: Loopback, text: str) -> None:
    async for frame in voice.speak(text):
        await link.speak(frame)
        await asyncio.sleep(frame.duration_ms / 1000)
    silence = AudioFrame(data=bytes(4800), sample_rate=24000)
    for _ in range(80):
        await link.speak(silence)
        await asyncio.sleep(0.1)


async def headless() -> None:
    link = Loopback(Identity(role="user", id="ana", name="Ana"), scope="acme")
    talking = asyncio.create_task(converse(link, replies=1))
    await guest_says(link, "Hi! Do you have a table for two at eight tonight?")
    await asyncio.wait_for(talking, 60)
    print(
        f"the guest's line received {sum(f.duration_ms for f in link.audio) / 1000:.1f} s of voice"
    )


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


def signed_in(ws: WebSocket) -> Identity:
    if ws.headers.get("origin") != ORIGIN or ws.cookies.get("guest") != "ana":
        raise WebSocketException(code=1008)
    return Identity(role="user", id="ana", name="Ana")


app = FastAPI()
app.mount("/client", StaticFiles(directory=CLIENT, check_dir=False), name="client")


@app.get("/")
async def page() -> HTMLResponse:
    response = HTMLResponse((Path(__file__).parent / "static/index.html").read_text())
    response.set_cookie("guest", "ana", httponly=True, samesite="strict")
    return response


@app.websocket("/senses")
async def senses(ws: WebSocket, guest: Identity = Depends(signed_in)) -> None:
    await ws.accept()
    link = await serve(Socket(ws), identity=guest, scope="acme")
    if link is not None:
        await converse(link)


if __name__ == "__main__":
    if sys.argv[1:] == ["browser"]:
        print(f"open {ORIGIN}")
        uvicorn.run(app, host="localhost", port=8000)
    else:
        asyncio.run(headless())
