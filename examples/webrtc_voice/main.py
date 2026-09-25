import asyncio
import contextlib
import fractions
import json
import sys
import time
from pathlib import Path

import av
import httpx
import uvicorn
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from rfnry_engine import Engine
from rfnry_senses import Heard, Identity, Said, Session, ToolCalled, Transport
from rfnry_senses.webrtc import accept_offer

from providers import AnthropicProvider, GoogleLive, GoogleSpeech

ORIGIN = "http://localhost:8000"
CLIENT = Path(__file__).parents[2] / "packages/typescript/dist"
TOKEN = "ana-demo-token"
ANA = Identity(role="user", id="ana", name="Ana")
INSTRUCTIONS = (
    "You are the voice concierge of the Acme bistro. Keep every answer to one short sentence. "
    "You know nothing about tonight yourself: for any question about tables, hours or the "
    "terrace, call the consult tool first and answer from what it says, in your own words, as "
    "your own knowledge. Never mention the tool or anyone behind you."
)

engine = Engine("agents", generation=AnthropicProvider())
consult = engine.tool(
    "core",
    scope="acme",
    name="consult",
    description="Look up tonight's tables, hours and the terrace.",
)
answered = asyncio.Event()


async def converse(link: Transport) -> None:
    session = Session(link, live=GoogleLive(), instructions=INSTRUCTIONS)
    await session.start(tools=consult)
    async with session:
        async for event in session.events():
            match event:
                case Heard(final=True, text=text):
                    print(f"heard    {text}")
                case ToolCalled(input=args, output=output):
                    print(f"consult  {args.get('input')!r} -> {output!r}")
                case Said(heard=heard, interrupted=interrupted):
                    print(f"said     {heard!r} interrupted={interrupted}")
                    answered.set()


def signed_in(request: Request) -> Identity:
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    if token != TOKEN and request.cookies.get("guest") != "ana":
        raise HTTPException(status_code=401)
    return ANA


app = FastAPI()
app.mount("/client", StaticFiles(directory=CLIENT, check_dir=False), name="client")


@app.get("/")
async def page() -> HTMLResponse:
    response = HTMLResponse((Path(__file__).parent / "static/index.html").read_text())
    response.set_cookie("guest", "ana", httponly=True, samesite="strict")
    return response


@app.post("/senses/offer")
async def offer(body: dict, guest: Identity = Depends(signed_in)) -> dict:
    link = await accept_offer(body["sdp"], identity=guest, scope="acme")
    asyncio.create_task(converse(link))
    return {"sdp": link.answer}


class Guest(MediaStreamTrack):
    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self.pcm = b""
        self.pts = 0

    async def recv(self) -> av.AudioFrame:
        await asyncio.sleep(0.02)
        chunk, self.pcm = self.pcm[:960], self.pcm[960:]
        frame = av.AudioFrame(format="s16", layout="mono", samples=480)
        frame.planes[0].update(chunk.ljust(960, b"\0"))
        frame.sample_rate, frame.pts, frame.time_base = (
            24000,
            self.pts,
            fractions.Fraction(1, 24000),
        )
        self.pts += 480
        return frame


async def headless() -> None:
    server = uvicorn.Server(uvicorn.Config(app, host="localhost", port=8000, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    pc, guest = RTCPeerConnection(), Guest()
    pc.addTrack(guest)
    channel = pc.createDataChannel("senses")
    speaking: dict[int, float] = {}

    @channel.on("open")
    def greet() -> None:
        channel.send(json.dumps({"type": "hello", "protocol": 1}))

    @channel.on("message")
    def played(message: str) -> None:
        control = json.loads(message)
        if control["type"] == "speaking":
            speaking[control["utterance"]] = time.monotonic()
        if control["type"] in ("stop", "silent"):
            started = speaking.pop(control["utterance"], time.monotonic())
            ms = (time.monotonic() - started) * 1000 if control["type"] == "stop" else 3_600_000
            channel.send(
                json.dumps({"type": "played", "utterance": control["utterance"], "ms": round(ms)})
            )

    @pc.on("track")
    def listen(track: MediaStreamTrack) -> None:
        async def collect() -> None:
            with contextlib.suppress(MediaStreamError):
                while True:
                    await track.recv()

        asyncio.create_task(collect())

    await pc.setLocalDescription(await pc.createOffer())
    async with httpx.AsyncClient() as http:
        response = await http.post(
            f"{ORIGIN}/senses/offer",
            json={"sdp": pc.localDescription.sdp},
            headers={"authorization": f"Bearer {TOKEN}"},
        )
        response.raise_for_status()
    await pc.setRemoteDescription(RTCSessionDescription(sdp=response.json()["sdp"], type="answer"))
    async for frame in GoogleSpeech(voice="Puck").speak(
        "Hi! Until when is the terrace open tonight?"
    ):
        guest.pcm += frame.data
    await asyncio.wait_for(answered.wait(), 60)
    await pc.close()
    await asyncio.sleep(0.5)
    server.should_exit = True
    await serving


if __name__ == "__main__":
    if sys.argv[1:] == ["browser"]:
        print(f"open {ORIGIN}")
        uvicorn.run(app, host="localhost", port=8000)
    else:
        asyncio.run(headless())
