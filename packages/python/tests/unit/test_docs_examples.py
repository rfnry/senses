import ast
import asyncio
import contextlib
import inspect
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

import rfnry_senses
from rfnry_senses import Identity, Loopback, Session, Tool, Tools

from .fakes import ANA, JPEG, ScriptedConnection, ScriptedLive, pcm, response

ROOT = Path(__file__).parents[4]
DOCS = ROOT / "docs" / "senses.md"
ADAPTER = ROOT / "packages/python/tests/integration/socket_adapter.py"
BLOCKS: list[tuple[str, str]] = re.findall(
    r"```(\w*)\n(.*?)```", DOCS.read_text(encoding="utf-8"), re.S
)
PYTHON = [body for lang, body in BLOCKS if lang == "python"]


def first_line(block: str) -> str:
    return block.splitlines()[0]


def named(start: str) -> str:
    return next(block for block in PYTHON if first_line(block) == start)


ILLUSTRATIVE = {
    '@app.websocket("/senses")': "needs a web framework; the integration tier runs it through FastAPI",
    "class Socket:": "pinned against tests/integration/socket_adapter.py",
    '@app.post("/senses/offer")': "needs a web framework and a peer; the webrtc tier runs accept_offer",
}

SIGNATURES = {
    "class LiveModel(Protocol):",
    "class Tools:",
}

SHOWN_A_FRAME = {"async with Session(link, video=True) as session:"}


class TextDelta(BaseModel):
    text: str


class Answering(ScriptedConnection):
    async def say(self, text: str) -> None:
        await super().say(text)
        self.emit(*response(*re.findall(r"\S+\s*", text)))

    async def send_text(self, text: str, *, respond: bool) -> None:
        await super().send_text(text, respond=respond)
        if respond:
            self.emit(*response(*re.findall(r"\S+\s*", text)))


class AnsweringLive(ScriptedLive):
    def __init__(self) -> None:
        super().__init__()
        self.connection = Answering()


class Run:
    reply = "For two, at eight."


class Engine:
    async def run(
        self,
        agent: str,
        *,
        scope: str,
        input: Any,
        tools: Any = None,
        on_event: Callable[[Any], Awaitable[None]] | None = None,
    ) -> Run:
        if on_event is not None:
            for word in re.findall(r"\S+\s*", Run.reply):
                await on_event(TextDelta(text=word))
        return Run()

    def tool(
        self, agent: str, *, scope: str, name: str, description: str, tools: Any = None
    ) -> Tools:
        async def body(args: dict[str, Any]) -> str:
            return Run.reply

        return Tools([Tool(name, description, body=body)])


class Link(Loopback):
    async def send_control(self, control: dict[str, Any]) -> None:
        await super().send_control(control)
        await asyncio.sleep(0)


def thread_tools(client: object, *, thread_id: str, triggered_by: Identity) -> Tools:
    return Tools()


async def now(args: dict[str, Any]) -> str:
    return "12:00"


def runnable() -> list[Any]:
    return [
        pytest.param(block, id=first_line(block)[:60])
        for block in PYTHON
        if first_line(block) not in ILLUSTRATIVE and first_line(block) not in SIGNATURES
    ]


def test_every_named_block_is_in_the_docs() -> None:
    lines = [first_line(block) for block in PYTHON]
    assert len(lines) == len(set(lines))
    assert set(ILLUSTRATIVE) | SIGNATURES | SHOWN_A_FRAME <= set(lines)


def test_the_socket_adapter_is_the_documented_one() -> None:
    adapter = ADAPTER.read_text(encoding="utf-8")
    assert adapter[adapter.index("class Socket:") :] == named("class Socket:")


def parameters(params: str) -> list[str]:
    while re.search(r"\[[^\[\]]*\]", params):
        params = re.sub(r"\[[^\[\]]*\]", "", params)
    names = [p.split(":")[0].split("=")[0].strip() for p in params.split(",") if p.strip()]
    return [name for name in names if name != "*"]


@pytest.mark.parametrize("start", sorted(SIGNATURES))
def test_signature_blocks_match_the_code(start: str) -> None:
    owner: Any = None
    checked = 0
    for line in named(start).splitlines():
        if found := re.match(r"class (\w+)", line):
            owner = getattr(rfnry_senses, found.group(1))
        elif found := re.search(r"def (\w+)\(([^)]*)\)", line):
            real = inspect.signature(getattr(owner, found.group(1)))
            expected = [
                ("*" if p.kind is p.VAR_POSITIONAL else "") + p.name
                for p in real.parameters.values()
            ]
            assert parameters(found.group(2)) == expected, f"{owner.__name__}.{found.group(1)}"
            checked += 1
    assert checked


async def context() -> tuple[dict[str, Any], list[Loopback]]:
    namespace: dict[str, Any] = {name: getattr(rfnry_senses, name) for name in rfnry_senses.__all__}
    exec(named("class Script:"), namespace)
    host = Identity(role="assistant", id="host", name="Host")
    tools = Tools([Tool("now", "The current time", body=now)])
    link = Link(ANA, "acme")
    session_link = Link(ANA, "acme")
    await session_link.show(JPEG)

    async def post(who: Identity, text: str) -> None:
        return None

    namespace.update(
        asyncio=asyncio,
        frame=pcm(20),
        ana=ANA,
        host=host,
        assistant=Identity(role="assistant", id="core", name="Core"),
        link=link,
        live=AnsweringLive(),
        stt=namespace["Script"]("A table for two."),
        tts=namespace["Tone"](),
        tools=tools,
        now=now,
        engine=Engine(),
        TextDelta=TextDelta,
        ThreadTools=thread_tools,
        client=object(),
        thread="t1",
        post=post,
    )
    return namespace, [link, session_link]


async def hang_up(links: list[Loopback]) -> None:
    await asyncio.sleep(0.2)
    for link in links:
        await link.hang_up()


@pytest.mark.parametrize("block", runnable())
async def test_docs_example_runs(block: str) -> None:
    namespace, links = await context()
    if first_line(block) in SHOWN_A_FRAME:
        await links[0].show(JPEG)
    live = AnsweringLive()
    session = Session(links[1], live=live, video=True)
    async with session:
        namespace["session"] = session
        ending = asyncio.create_task(hang_up(links))
        try:
            async with asyncio.timeout(5):
                result = eval(
                    compile(block, "senses.md", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT),
                    namespace,
                )
                if inspect.iscoroutine(result):
                    await result
        finally:
            ending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ending


async def test_a_mirror_posts_what_was_heard() -> None:
    namespace, links = await context()
    exec(named("async def mirror(session, post):"), namespace)
    session = Session(links[1], live=AnsweringLive())
    posted: list[tuple[Any, str]] = []

    async def post(who: Any, text: str) -> None:
        posted.append((who, text))

    async with session:
        mirroring = asyncio.create_task(namespace["mirror"](session, post))
        await asyncio.sleep(0)
        await session.say("For two.", triggered_by=namespace["host"])
        await links[1].hang_up()
        await asyncio.wait_for(mirroring, 1)
    assert posted == [(namespace["assistant"], "For two.")]
