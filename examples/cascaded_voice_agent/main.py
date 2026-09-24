import asyncio
import sys
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect, WebSocketException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from rfnry_engine import Engine
from rfnry_senses import (
    AudioFrame,
    Closed,
    Heard,
    Identity,
    Loopback,
    Session,
    Transport,
    TurnEnded,
    serve,
)

from providers import AnthropicProvider, GoogleSpeech, OpenAIProvider, TypeSafeProvider

ORIGIN = "http://localhost:8000"
CLIENT = Path(__file__).parents[2] / "packages/typescript/dist"
engine = Engine("agents", generation=AnthropicProvider(), decision=TypeSafeProvider())
voice = GoogleSpeech()


async def converse(link: Transport, replies: int | None = None) -> None:
    async with Session(
        link, transcription=OpenAIProvider(), speech=voice, turns=TypeSafeProvider()
    ) as session:
        async for event in session.events():
            match event:
                case Heard(final=True, text=text, speaker=speaker):
                    print(f"heard  {speaker.name}: {text}")
                case TurnEnded(text=text, probability=probability):
                    print(f"turn   done={probability}: {text}")
                    run = await engine.run("concierge", scope=session.scope, input=text)
                    said = await session.say(run.reply, triggered_by=event.speaker)
                    print(f"said   {said.heard!r} interrupted={said.interrupted}")
                    replies = None if replies is None else replies - 1
                    if replies == 0:
                        return
                case Closed(reason=reason):
                    print(f"closed {reason}")


async def guest_says(link: Loopback, text: str) -> None:
    async for frame in voice.speak(text):
        await link.speak(frame)
        await asyncio.sleep(frame.duration_ms / 1000)
    silence = AudioFrame(data=bytes(4800), sample_rate=24000)
    for _ in range(20):
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
