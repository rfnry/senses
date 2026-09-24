import asyncio
import json
from typing import Any

import pytest

from rfnry_senses.contracts import AudioFrame
from rfnry_senses.errors import InputError
from rfnry_senses.events import Heard
from rfnry_senses.session import Session
from rfnry_senses.websocket import Connection, serve
from rfnry_senses.wire import decode, encode_audio

from .fakes import ANA, HOST, ScriptedTranscription, Tone, next_of, pcm, until

HELLO = json.dumps({"type": "hello", "protocol": 1})


class Socket:
    def __init__(self, *messages: str | bytes | None) -> None:
        self.inbox: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        for message in messages:
            self.inbox.put_nowait(message)
        self.sent: list[str | bytes] = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.closed = False

    async def receive(self) -> str | bytes:
        item = await self.inbox.get()
        if item is None:
            raise ConnectionError("closed")
        return item

    async def send_text(self, data: str) -> None:
        await self.gate.wait()
        self.sent.append(data)

    async def send_bytes(self, data: bytes) -> None:
        await self.gate.wait()
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True
        self.inbox.put_nowait(None)

    def texts(self) -> list[dict[str, Any]]:
        return [json.loads(m) for m in self.sent if isinstance(m, str)]


async def test_serve_reads_the_hello_and_carries_the_person_and_their_scope() -> None:
    socket = Socket(HELLO)
    assert isinstance(socket, Connection)
    link = await serve(socket, identity=ANA, scope="acme")
    assert link is not None and link.identity == ANA and link.scope == "acme" and socket.sent == []


@pytest.mark.parametrize(
    ("first", "message"),
    [
        (
            json.dumps({"type": "hello", "protocol": 2}),
            "transport: protocol 2 is not supported; this server speaks 1",
        ),
        (b"\x01", "wire: the first frame must be a hello"),
    ],
)
async def test_serve_refuses_and_says_why(first: str | bytes, message: str) -> None:
    socket = Socket(first)
    assert await serve(socket, identity=ANA, scope="acme") is None
    assert socket.texts() == [{"type": "error", "message": message}] and socket.closed


async def test_a_client_gone_before_its_hello_is_no_transport() -> None:
    socket = Socket(None)
    assert await serve(socket, identity=ANA, scope="acme") is None


async def test_an_empty_scope_is_the_consumers_bug() -> None:
    with pytest.raises(InputError, match="^transport: scope is empty$"):
        await serve(Socket(HELLO), identity=ANA, scope="")


async def test_a_session_runs_over_the_socket(caplog: pytest.LogCaptureFixture) -> None:
    socket, stt, tts = Socket(HELLO), ScriptedTranscription(), Tone()
    link = await serve(socket, identity=ANA, scope="acme")
    assert link is not None
    async with Session(link, transcription=stt, speech=tts) as s:
        events = s.events()
        await socket.inbox.put(encode_audio(pcm(20), 0))
        await socket.inbox.put("not json")
        await until(lambda: len(stt.fed) == 1)
        stt.hear("hello", end=False)
        await next_of(events, Heard)
        saying = asyncio.create_task(s.say("Hi there.", triggered_by=HOST))
        await until(lambda: {"type": "silent", "utterance": 1} in socket.texts())
        await socket.inbox.put(json.dumps({"type": "played", "utterance": 1, "ms": 200}))
        said = await saying
    kinds = [m["type"] for m in socket.texts()]
    audio = [decode(m) for m in socket.sent if isinstance(m, bytes)]
    assert kinds[0] == "ready" and kinds.index("speaking") < kinds.index("silent")
    assert (
        all(isinstance(a, AudioFrame) for a in audio)
        and len(audio) == 2
        and said.heard == "Hi there."
    )
    assert socket.texts()[-1]["event"]["kind"] == "closed" and socket.closed
    assert "transport: dropped a frame" in caplog.text


async def test_clearing_drops_queued_audio_and_keeps_controls() -> None:
    socket = Socket(HELLO)
    link = await serve(socket, identity=ANA, scope="acme")
    assert link is not None
    socket.gate.clear()
    for _ in range(3):
        await link.send_audio(pcm(20), 1)
    await link.send_control({"type": "stop", "utterance": 1})
    await asyncio.sleep(0)
    link.clear_audio()
    socket.gate.set()
    await until(lambda: len(socket.sent) == 2)
    assert isinstance(socket.sent[0], bytes) and json.loads(socket.sent[1]) == {
        "type": "stop",
        "utterance": 1,
    }
