from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from itertools import count
from time import monotonic
from typing import Any, Literal

from pydantic import TypeAdapter

from rfnry_senses.contracts import (
    AudioFrame,
    LiveAudio,
    LiveConnection,
    LiveDone,
    LiveFailed,
    LiveHeard,
    LiveModel,
    LiveSaid,
    LiveStarted,
    LiveToolCall,
    SpeechModel,
    Transcription,
    TranscriptionModel,
)
from rfnry_senses.errors import (
    ConfigurationError,
    InputError,
    ModelError,
    SensesException,
    TransportError,
)
from rfnry_senses.events import Closed, Event, Heard, Informed, Said, Silent, Speaking, ToolCalled
from rfnry_senses.models import Content, DecisionModel, Identity, Image, Part, ToolSpec
from rfnry_senses.tools import ToolsLike
from rfnry_senses.transport import Played, Transport
from rfnry_senses.voice import Ledger
from rfnry_senses.window import FrameWindow

log = logging.getLogger("rfnry_senses")

PLAYED_GRACE = 0.25
PLAYBACK_SLACK = 1.0
_PARTS: TypeAdapter[list[Part]] = TypeAdapter(list[Part])


def as_content(result: object) -> Content:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return json.dumps(result)
    if isinstance(result, list):
        return _PARTS.validate_python(
            [p.model_dump() if hasattr(p, "model_dump") else p for p in result]
        )
    raise ModelError(
        f"session: a tool returned {type(result).__name__}; a tool returns text, a dict or parts"
    )


@dataclass(eq=False)
class Utterance:
    triggered_by: Identity | None
    text: str = ""
    verbatim: bool = False
    done: asyncio.Future[Said] | None = None
    number: int = 0
    spoke: bool = False
    finished: bool = False
    ledger: Ledger = field(default_factory=Ledger)


class Subscription:
    def __init__(self, subscribers: list[asyncio.Queue[Event | None]], closed: bool) -> None:
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        if closed:
            self._queue.put_nowait(None)
        else:
            subscribers.append(self._queue)

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Event:
        event = await self._queue.get()
        if event is None:
            self._queue.put_nowait(None)
            raise StopAsyncIteration
        return event


class Session:
    def __init__(
        self,
        transport: Transport,
        *,
        live: LiveModel | None = None,
        tools: ToolsLike | None = None,
        instructions: str | None = None,
        transcription: TranscriptionModel | None = None,
        speech: SpeechModel | None = None,
        turns: DecisionModel | None = None,
        video: bool = False,
        window: float = 10.0,
        window_bytes: int = 8 * 1024 * 1024,
        turn_threshold: float = 0.5,
        turn_timeout: float = 3.0,
        tool_timeout: float | None = 30.0,
    ) -> None:
        if live is not None and (
            transcription is not None or speech is not None or turns is not None
        ):
            raise ConfigurationError(
                "session: a live model hears and speaks itself; give it no transcription, speech or turn models"
            )
        if live is None and (tools is not None or instructions is not None):
            raise ConfigurationError("session: tools and instructions are for a live model")
        if turns is not None and transcription is None:
            raise ConfigurationError("session: turns need a transcription model")
        for name, value in (
            ("window", window),
            ("window_bytes", window_bytes),
            ("turn_timeout", turn_timeout),
        ):
            if value <= 0:
                raise ConfigurationError(f"session: {name} must be positive")
        if tool_timeout is not None and tool_timeout <= 0:
            raise ConfigurationError("session: tool_timeout must be positive or None")
        if not 0 < turn_threshold <= 1:
            raise ConfigurationError("session: turn_threshold must be in (0, 1]")
        self.scope = transport.scope
        self.person = transport.identity
        self._transport = transport
        self._live = live
        self._tools = tools
        self._instructions = instructions
        self._transcription = transcription
        self._speech = speech
        self._turns = turns
        self._window = FrameWindow(window, window_bytes) if video else None
        self._turn_threshold = turn_threshold
        self._turn_timeout = turn_timeout
        self._tool_timeout = tool_timeout
        self._connection: LiveConnection | None = None
        self._stream: Transcription | None = None
        self._subscribers: list[asyncio.Queue[Event | None]] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._opened = False
        self._closed = False
        self._muted = False
        self._numbers = count(1)
        self._utterance: Utterance | None = None
        self._playing: asyncio.Task[None] | None = None
        self._waiting: deque[Utterance] = deque()
        self._queue: asyncio.Queue[Utterance] = asyncio.Queue()
        self._causes: deque[Utterance] = deque()
        self._responding = False
        self._played: dict[int, asyncio.Future[float]] = {}
        self._lock = asyncio.Lock()
        self._frame: Image | None = None
        self._frame_ready = asyncio.Event()
        self._turn: list[str] = []
        self._hold: asyncio.Task[None] | None = None
        self._last_heard = ""

    @property
    def _voice(self) -> bool:
        return self._live is not None or self._speech is not None

    async def __aenter__(self) -> Session:
        if self._live is not None:
            specs = (
                [ToolSpec.model_validate(d) for d in self._tools.definitions()]
                if self._tools is not None
                else []
            )
            self._connection = await self._live.open(tools=specs, instructions=self._instructions)
            self._spawn(self._live_events())
        self._spawn(self._inbound())
        self._opened = True
        await self._transport.send_control({"type": "ready"})
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def events(self) -> Subscription:
        return Subscription(self._subscribers, self._closed)

    async def say(self, text: str, *, triggered_by: Identity) -> Said:
        self._ready(text)
        done: asyncio.Future[Said] = asyncio.get_running_loop().create_future()
        u = Utterance(triggered_by, text, verbatim=True, done=done)
        if self._connection is not None:
            connection = self._connection
            return await self._ask(u, lambda: connection.say(text))
        raise ConfigurationError("session: say needs a live model or a speech model")

    async def prompt(self, text: str, *, triggered_by: Identity) -> Said:
        self._ready(text)
        connection = self._need_live("prompt")
        done: asyncio.Future[Said] = asyncio.get_running_loop().create_future()
        u = Utterance(triggered_by, done=done)
        return await self._ask(u, lambda: connection.send_text(text, respond=True))

    async def inform(self, text: str, *, triggered_by: Identity) -> None:
        self._ready(text)
        connection = self._need_live("inform")
        async with self._lock:
            await connection.send_text(text, respond=False)
        await self._emit(Informed(scope=self.scope, text=text, triggered_by=triggered_by))

    async def close(self) -> None:
        await self._close("closed", None, None)

    def _check_open(self) -> None:
        if self._closed:
            raise TransportError("session: the session is closed")
        if not self._opened:
            raise TransportError("session: open the session with async with before using it")

    def _ready(self, text: str) -> None:
        self._check_open()
        if not text.strip():
            raise InputError("session: text is empty")

    def _need_live(self, verb: str) -> LiveConnection:
        if self._connection is None:
            raise ConfigurationError(f"session: {verb} needs a live model")
        return self._connection

    def _spawn(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(self._guard(work))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _: work.close())
        return task

    async def _guard(self, work: Coroutine[Any, Any, None]) -> None:
        try:
            await work
        except asyncio.CancelledError:
            raise
        except SensesException as exc:
            await self._close("failed", str(exc), exc)
        except Exception as exc:
            log.exception("session: a background task failed")
            await self._close("failed", f"session: {exc}", ModelError(f"session: {exc}"))

    async def _emit(self, event: Event) -> None:
        for queue in self._subscribers:
            queue.put_nowait(event)
        if not isinstance(event, Informed | ToolCalled):
            with contextlib.suppress(Exception):
                await self._transport.send_control(
                    {"type": "event", "event": event.model_dump(mode="json")}
                )

    async def _inbound(self) -> None:
        async for item in self._transport.inbound():
            if isinstance(item, Played):
                future = self._played.get(item.utterance)
                if future is not None and not future.done():
                    future.set_result(float(item.ms))
            elif isinstance(item, AudioFrame):
                await self._hear(item)
            else:
                await self._see(item)
        await self._close("hung_up", None, None)

    async def _hear(self, frame: AudioFrame) -> None:
        if self._connection is not None:
            await self._connection.send_audio(frame)

    async def _see(self, image: Image) -> None:
        return None

    async def _live_events(self) -> None:
        assert self._connection is not None
        async for event in self._connection.events():
            if isinstance(event, LiveAudio):
                u = self._utterance
                if u is not None and self._responding:
                    await self._send(u, event.frame)
            elif isinstance(event, LiveSaid):
                u = self._utterance
                if u is not None and self._responding:
                    u.ledger.mark(event.text, ahead=True)
                    if not u.verbatim:
                        u.text += event.text
            elif isinstance(event, LiveHeard):
                if event.text.strip():
                    await self._emit(
                        Heard(
                            scope=self.scope,
                            text=event.text,
                            final=event.final,
                            speaker=self.person,
                        )
                    )
            elif isinstance(event, LiveStarted):
                await self._response_started()
            elif isinstance(event, LiveDone):
                await self._response_done()
            elif isinstance(event, LiveToolCall):
                self._spawn(self._call(event))
            elif isinstance(event, LiveFailed):
                raise ModelError(f"session: the live model failed: {event.message}")
        raise ModelError("session: the live connection ended")

    async def _response_started(self) -> None:
        if self._responding:
            raise ModelError("session: a response started before the last one was done")
        self._responding = True
        previous = self._utterance
        if previous is not None:
            self._utterance = None
            await self._finish(previous, previous.ledger.heard(None), interrupted=False)
        self._start(self._causes.popleft() if self._causes else Utterance(None))

    async def _response_done(self) -> None:
        if not self._responding:
            raise ModelError("session: a response ended before it started")
        self._responding = False
        if self._utterance is not None:
            self._spawn(self._conclude(self._utterance))

    def _start(self, u: Utterance) -> None:
        u.number = next(self._numbers)
        self._utterance = u
        self._played[u.number] = asyncio.get_running_loop().create_future()

    async def _send(self, u: Utterance, frame: AudioFrame) -> None:
        if not u.spoke:
            u.spoke = True
            await self._transport.send_control({"type": "speaking", "utterance": u.number})
            await self._emit(Speaking(scope=self.scope))
        if self._utterance is not u:
            return
        u.ledger.sent(frame, monotonic())
        await self._transport.send_audio(frame, u.number)

    async def _conclude(self, u: Utterance) -> None:
        played = 0.0
        if u.spoke:
            await self._transport.send_control({"type": "silent", "utterance": u.number})
            played = await self._await_played(
                u, u.ledger.remaining(monotonic()) + PLAYBACK_SLACK, u.ledger.sent_ms
            )
        if self._utterance is u:
            self._utterance = None
            await self._finish(u, u.ledger.heard(played) if u.spoke else "", interrupted=False)

    async def _await_played(self, u: Utterance, timeout: float, fallback: float | None) -> float:
        future = self._played.get(u.number)
        if future is not None:
            with contextlib.suppress(TimeoutError):
                return await asyncio.wait_for(asyncio.shield(future), timeout)
        return fallback if fallback is not None else u.ledger.estimate(monotonic())

    async def _finish(self, u: Utterance, heard: str, interrupted: bool) -> None:
        if u.finished:
            return
        u.finished = True
        self._played.pop(u.number, None)
        said = Said(
            scope=self.scope,
            text=u.text,
            heard=heard,
            interrupted=interrupted,
            triggered_by=u.triggered_by,
        )
        if u.spoke:
            await self._emit(Silent(scope=self.scope))
        if heard:
            self._last_heard = heard
        if u.done is not None and not u.done.done():
            u.done.set_result(said)
        if u.done is not None or u.spoke or u.text:
            await self._emit(said)

    async def _ask(self, u: Utterance, send: Callable[[], Awaitable[None]]) -> Said:
        assert u.done is not None
        async with self._lock:
            self._causes.append(u)
            try:
                await send()
            except BaseException:
                self._causes.remove(u)
                raise
        return await u.done

    async def _call(self, call: LiveToolCall) -> None:
        assert self._connection is not None
        output: Content
        try:
            if self._tools is None:
                raise InputError(f"tools: unknown tool {call.name!r}")
            result = await asyncio.wait_for(
                self._tools.dispatch(call.name, call.args), self._tool_timeout
            )
            output, is_error = as_content(result), False
        except TimeoutError:
            output, is_error = f"timed out after {self._tool_timeout:g}s", True
        except Exception as exc:
            output, is_error = f"{type(exc).__name__}: {exc}", True
        await self._connection.submit_result(call.call_id, output, is_error)
        await self._emit(
            ToolCalled(
                scope=self.scope, name=call.name, input=call.args, output=output, is_error=is_error
            )
        )

    async def _close(
        self,
        reason: Literal["closed", "hung_up", "failed"],
        message: str | None,
        error: SensesException | None,
    ) -> None:
        if self._closed:
            return
        self._closed = True
        current = asyncio.current_task()
        tasks = [t for t in (*self._tasks, self._playing) if t is not None and t is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        failure = error or TransportError("session: the session closed before the reply was spoken")
        for u in (self._utterance, *self._waiting, *self._causes):
            if u is not None and u.done is not None and not u.done.done():
                u.done.set_exception(failure)
        for closing in (self._connection, self._stream):
            if closing is not None:
                with contextlib.suppress(Exception):
                    await closing.close()
        await self._emit(Closed(scope=self.scope, reason=reason, message=message))
        for queue in self._subscribers:
            queue.put_nowait(None)
        with contextlib.suppress(Exception):
            await self._transport.close()
