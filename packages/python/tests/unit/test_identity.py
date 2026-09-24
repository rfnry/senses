import asyncio
import json
from typing import Any, cast

import pytest

from rfnry_senses.errors import InputError
from rfnry_senses.events import Informed, Looked, Muted
from rfnry_senses.senses_tools import SensesTools
from rfnry_senses.session import Session
from rfnry_senses.transport import Loopback
from rfnry_senses.websocket import serve

from .fakes import ANA, HOST, JPEG, Lookalike, ScriptedLive, lookalike, next_of, response, until
from .test_websocket import Socket


async def test_serve_and_the_loopback_read_the_person_by_their_fields() -> None:
    link = await serve(
        Socket(json.dumps({"type": "hello", "protocol": 1})), identity=lookalike(ANA), scope="acme"
    )
    assert link is not None and link.identity == ANA
    loopback = Loopback(lookalike(ANA), "acme")
    assert loopback.identity == ANA
    async with Session(loopback) as s:
        assert s.person == ANA


async def test_every_verb_reads_its_cause_by_its_fields() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    host = lookalike(HOST)
    async with Session(link, live=live, video=True) as s:
        events = s.events()
        await link.show(JPEG)
        await asyncio.sleep(0.01)
        await s.look(triggered_by=host)
        assert (await next_of(events, Looked)).triggered_by == HOST
        saying = asyncio.create_task(s.say("Hello.", triggered_by=host))
        await until(lambda: len(live.connection.calls) == 1)
        live.connection.emit(*response("Hello."))
        assert (await saying).triggered_by == HOST
        prompting = asyncio.create_task(s.prompt("Greet Ana.", triggered_by=host))
        await until(lambda: len(live.connection.calls) == 2)
        live.connection.emit(*response("Hi Ana."))
        assert (await prompting).triggered_by == HOST
        await s.inform("Ana is vegetarian.", triggered_by=host)
        assert (await next_of(events, Informed)).triggered_by == HOST
        assert await s.interrupt(triggered_by=host) is None
        await s.mute(triggered_by=host)
        await s.unmute(triggered_by=host)
        muted = [await next_of(events, Muted), await next_of(events, Muted)]
        assert [m.triggered_by for m in muted] == [HOST, HOST]
        tools = SensesTools(s, triggered_by=host)
        assert tools.triggered_by == HOST
        informing = await tools.dispatch("inform", {"text": "The terrace is open."})
        assert informing == "informed"
        assert (await next_of(events, Informed)).triggered_by == HOST


async def test_a_value_that_is_not_an_identity_is_refused_at_the_boundary() -> None:
    robot: Any = Lookalike(cast(Any, "robot"), "r2")
    with pytest.raises(InputError, match="^models: not an identity"):
        Loopback(robot, "acme")
    with pytest.raises(InputError, match="^models: not an identity"):
        await serve(Socket(), identity=robot, scope="acme")
    async with Session(Loopback(ANA, "acme"), live=ScriptedLive()) as s:
        with pytest.raises(InputError, match="^models: not an identity"):
            await s.inform("hi", triggered_by=robot)
        with pytest.raises(InputError, match="^models: not an identity"):
            SensesTools(s, triggered_by=robot)
