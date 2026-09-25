import asyncio
import sys
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from rfnry_engine import Engine
from rfnry_senses import (
    AudioFrame,
    Heard,
    Identity,
    Informed,
    Looked,
    Loopback,
    Said,
    SensesTools,
    Session,
    ToolCalled,
    Tools,
    Transport,
    serve,
)

from desk import desk
from providers import AnthropicProvider, GoogleLive, GoogleSpeech

ORIGIN = "http://localhost:8000"
CLIENT = Path(__file__).parents[2] / "packages/typescript/dist"
ANA = Identity(role="user", id="ana", name="Ana")
MIND = Identity(role="assistant", id="mind", name="Mind")
INSTRUCTIONS = (
    "You talk with a person who hears you, and you hear and see them through their microphone "
    "and camera. Keep every answer to one or two short sentences. A second mind works behind "
    "you: for anything about what the camera shows, or that needs thought, call the consult tool "
    "with the question and answer from what it says."
)

engine = Engine("agents", generation=AnthropicProvider())


class Room:
    def __init__(self) -> None:
        self.tools = Tools()

    def definitions(self) -> list[dict[str, Any]]:
        return self.tools.definitions()

    async def dispatch(self, name: str, args: dict[str, Any]) -> Any:
        return await self.tools.dispatch(name, args)


def companion(link: Transport) -> Session:
    room = Room()
    consult = engine.tool(
        "mind",
        scope=link.scope,
        name="consult",
        description="Ask the mind behind you. It can look through the camera.",
        tools=room,
    )
    session = Session(link, live=GoogleLive(), video=True, tools=consult, instructions=INSTRUCTIONS)
    room.tools = SensesTools(session, triggered_by=MIND)
    return session


async def narrate(session: Session, answered: asyncio.Event) -> None:
    consulted = False
    async for event in session.events():
        match event:
            case Heard(final=True, text=text):
                print(f"heard    {text}")
            case Looked(frames=frames, triggered_by=cause):
                print(f"looked   {frames} frame(s), by {cause.name}")
            case ToolCalled(name=name, input=args, output=output):
                print(f"{name}  {args.get('input')!r} -> {output!r}")
                consulted = True
            case Informed(text=text, triggered_by=cause):
                print(f"informed {text!r}, by {cause.name}")
            case Said(text=text, heard=heard, triggered_by=cause):
                print(f"said     {heard or text!r} (caused by {cause.name if cause else 'nobody'})")
                if consulted:
                    answered.set()


voice = GoogleSpeech(voice="Puck")


async def ana_says(link: Loopback, text: str) -> None:
    async for frame in voice.speak(text):
        await link.speak(frame)
        await asyncio.sleep(frame.duration_ms / 1000)
    for _ in range(20):
        await link.speak(AudioFrame(data=bytes(4800), sample_rate=24000))
        await asyncio.sleep(0.1)


async def camera(link: Loopback) -> None:
    frame = desk()
    while True:
        await link.show(frame)
        await asyncio.sleep(1)


async def headless() -> None:
    link = Loopback(ANA, scope="home")
    answered = asyncio.Event()
    async with companion(link) as session:
        narrating = asyncio.create_task(narrate(session, answered))
        filming = asyncio.create_task(camera(link))
        await asyncio.sleep(1)
        await ana_says(link, "What do you see on my desk?")
        await asyncio.wait_for(answered.wait(), 90)
        await asyncio.sleep(0.5)
        narrating.cancel()
        filming.cancel()


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
        async with companion(link) as session:
            await narrate(session, asyncio.Event())


if __name__ == "__main__":
    if sys.argv[1:] == ["browser"]:
        print(f"open {ORIGIN}")
        uvicorn.run(app, host="localhost", port=8000)
    else:
        asyncio.run(headless())
