import asyncio
import contextlib
import fractions
import json
import time

import av
import httpx
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError

from providers import GoogleSpeech
from server import ORIGIN, TOKEN

tasks: set[asyncio.Task[None]] = set()


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


async def call(question: str) -> RTCPeerConnection:
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

        task = asyncio.create_task(collect())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    await pc.setLocalDescription(await pc.createOffer())
    async with httpx.AsyncClient() as http:
        response = await http.post(
            f"{ORIGIN}/senses/offer",
            json={"sdp": pc.localDescription.sdp},
            headers={"authorization": f"Bearer {TOKEN}"},
        )
        response.raise_for_status()
    await pc.setRemoteDescription(RTCSessionDescription(sdp=response.json()["sdp"], type="answer"))
    async for frame in GoogleSpeech(voice="Puck").speak(question):
        guest.pcm += frame.data
    return pc
