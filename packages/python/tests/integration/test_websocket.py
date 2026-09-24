import asyncio
import json
from typing import Any

import pytest
from fastapi import Depends, FastAPI, HTTPException, WebSocket
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection
from starlette.testclient import WebSocketDenialResponse
from websockets.asyncio.client import connect
from websockets.asyncio.server import ServerConnection, serve as listen

from rfnry_senses.contracts import AudioFrame
from rfnry_senses.events import TurnEnded
from rfnry_senses.models import Identity
from rfnry_senses.session import Session
from rfnry_senses.websocket import serve
from rfnry_senses.wire import decode, encode_audio

from ..unit.fakes import ANA, HOST, ScriptedTranscription, Tone, pcm, until
from .socket_adapter import Socket

pytestmark = pytest.mark.integration

HELLO = json.dumps({"type": "hello", "protocol": 1})


class Adapted:
    def __init__(self, ws: ServerConnection) -> None:
        self.ws = ws

    async def receive(self) -> str | bytes:
        return await self.ws.recv()

    async def send_text(self, data: str) -> None:
        await self.ws.send(data)

    async def send_bytes(self, data: bytes) -> None:
        await self.ws.send(data)

    async def close(self) -> None:
        await self.ws.close()


async def test_a_cascaded_session_over_a_real_socket() -> None:
    stt = ScriptedTranscription()
    opened: asyncio.Queue[Session] = asyncio.Queue()
    ended = asyncio.Event()

    async def handler(ws: ServerConnection) -> None:
        link = await serve(Adapted(ws), identity=ANA, scope="acme")
        assert link is not None
        async with Session(link, transcription=stt, speech=Tone()) as session:
            await opened.put(session)
            await ended.wait()

    async with listen(handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        async with connect(
            f"ws://127.0.0.1:{port}", additional_headers={"authorization": "Bearer t"}
        ) as client:
            await client.send(HELLO)
            assert json.loads(await client.recv()) == {"type": "ready"}
            session = await opened.get()
            await client.send(encode_audio(pcm(20), 0))
            await until(lambda: len(stt.fed) == 1)
            stt.hear("a table for two", end=False)
            heard = json.loads(await client.recv())
            assert heard["event"]["kind"] == "heard" and heard["event"]["scope"] == "acme"
            saying = asyncio.create_task(session.say("For two.", triggered_by=HOST))
            frames: list[Any] = []
            while True:
                message = await client.recv()
                if isinstance(message, bytes):
                    frames.append(decode(message))
                    continue
                control = json.loads(message)
                if control["type"] == "silent":
                    await client.send(
                        json.dumps({"type": "played", "utterance": control["utterance"], "ms": 200})
                    )
                    break
            said = await saying
            assert frames and said.heard == "For two."
            await client.close()
            await until(lambda: session._closed, timeout=2)
            ended.set()


class User:
    def __init__(self, identity: Identity, org: str) -> None:
        self.identity, self.org = identity, org


def signed_in(connection: HTTPConnection) -> User:
    if connection.headers.get("authorization") != "Bearer t":
        raise HTTPException(status_code=403)
    return User(ANA, "acme")


class Run:
    reply = "For two."


class HearsWhenFed(ScriptedTranscription):
    async def feed(self, frame: AudioFrame) -> None:
        await super().feed(frame)
        self.hear("A table for two.")


class Engine:
    async def run(self, agent: str, *, scope: str, input: str) -> Run:
        return Run()


def test_the_getting_started_endpoint_through_fastapi() -> None:
    stt, tts, engine = HearsWhenFed(), Tone(), Engine()
    app = FastAPI()

    @app.websocket("/senses")
    async def senses(ws: WebSocket, user: User = Depends(signed_in)) -> None:
        await ws.accept()
        link = await serve(Socket(ws), identity=user.identity, scope=user.org)
        if link is None:
            return
        async with Session(link, transcription=stt, speech=tts) as session:
            async for event in session.events():
                if isinstance(event, TurnEnded):
                    run = await engine.run("concierge", scope=session.scope, input=event.text)
                    await session.say(run.reply, triggered_by=event.speaker)

    client = TestClient(app)
    with pytest.raises(WebSocketDenialResponse) as refused, client.websocket_connect("/senses"):
        pass
    assert refused.value.status_code == 403
    with client.websocket_connect("/senses", headers={"authorization": "Bearer t"}) as ws:
        ws.send_text(HELLO)
        assert ws.receive_json() == {"type": "ready"}
        ws.send_bytes(encode_audio(pcm(20), 0))
        audio = 0
        while True:
            message = ws.receive()
            if message.get("bytes") is not None:
                audio += 1
                continue
            control = json.loads(message["text"])
            if control["type"] == "silent":
                ws.send_text(
                    json.dumps({"type": "played", "utterance": control["utterance"], "ms": 10_000})
                )
            if control["type"] == "event" and control["event"]["kind"] == "said":
                said = control["event"]
                break
    assert (
        audio == 2
        and said["heard"] == "For two."
        and said["triggered_by"] == ANA.model_dump(mode="json")
    )
