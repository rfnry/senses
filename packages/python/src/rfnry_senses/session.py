from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
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
    LiveInterrupted,
    LiveModel,
    LiveSaid,
    LiveStarted,
    LiveToolCall,
    SpeechModel,
    Transcript,
    Transcription,
    TranscriptionModel,
    UtteranceEnd,
)
from rfnry_senses.errors import (
    ConfigurationError,
    InputError,
    ModelError,
    NotFoundError,
    SensesException,
    TransportError,
)
from rfnry_senses.events import (
    Closed,
    Event,
    Heard,
    Informed,
    Interrupted,
    Looked,
    Muted,
    Said,
    Silent,
    Speaking,
    ToolCalled,
    TurnEnded,
)
from rfnry_senses.models import (
    Check,
    CheckAnswer,
    Content,
    DecisionModel,
    Identity,
    Image,
    Part,
    Text,
    ToolSpec,
    as_decided,
)
from rfnry_senses.tools import ToolsLike
from rfnry_senses.transport import Played, Transport
from rfnry_senses.voice import Ledger, sentences_from
from rfnry_senses.window import FrameWindow

log = logging.getLogger("rfnry_senses")

PLAYED_GRACE = 0.25
PLAYBACK_SLACK = 1.0
_PARTS: TypeAdapter[list[Part]] = TypeAdapter(list[Part])
DONE = Check(
    instructions="Has the person finished their turn, so that it is now the other side's turn to speak?",
    yes="They finished what they meant to say and expect an answer or a reaction",
    no="They paused mid-thought and are about to go on",
)


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
    source: str | AsyncIterator[str] = ""


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
            if self._window is not None:
                self._spawn(self._forward_frames())
        if self._transcription is not None:
            self._stream = self._transcription.stream()
            self._spawn(self._transcripts())
        if self._speech is not None:
            self._spawn(self._speak())
        self._spawn(self._inbound())
        self._opened = True
        await self._transport.send_control({"type": "ready"})
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def events(self) -> Subscription:
        return Subscription(self._subscribers, self._closed)

    async def say(self, text: str | AsyncIterator[str], *, triggered_by: Identity) -> Said:
        if isinstance(text, str):
            self._ready(text)
        else:
            self._check_open()
        done: asyncio.Future[Said] = asyncio.get_running_loop().create_future()
        u = Utterance(
            triggered_by,
            text if isinstance(text, str) else "",
            verbatim=True,
            done=done,
            source=text,
        )
        if self._muted:
            await self._finish(u, "", interrupted=True)
            return await done
        if self._connection is not None:
            connection = self._connection
            if not isinstance(text, str):
                joined = "".join([chunk async for chunk in text])
                self._check_open()
                if not joined.strip():
                    await self._finish(u, "", interrupted=False)
                    return await done
                u.text = joined
            return await self._ask(u, lambda: connection.say(u.text))
        if self._speech is not None:
            self._waiting.append(u)
            self._queue.put_nowait(u)
            return await done
        raise ConfigurationError("session: say needs a live model or a speech model")

    async def prompt(self, text: str, *, triggered_by: Identity) -> Said:
        self._ready(text)
        connection = self._need_live("prompt")
        done: asyncio.Future[Said] = asyncio.get_running_loop().create_future()
        u = Utterance(triggered_by, done=done)
        if self._muted:
            await self._finish(u, "", interrupted=True)
            return await done
        return await self._ask(u, lambda: connection.send_text(text, respond=True))

    async def inform(self, text: str, *, triggered_by: Identity) -> None:
        self._ready(text)
        connection = self._need_live("inform")
        async with self._lock:
            await connection.send_text(text, respond=False)
        await self._emit(Informed(scope=self.scope, text=text, triggered_by=triggered_by))

    async def close(self) -> None:
        await self._close("closed", None, None)

    async def interrupt(self, *, triggered_by: Identity) -> Interrupted | None:
        self._check_open()
        if not self._voice:
            raise ConfigurationError("session: interrupt needs a live model or a speech model")
        return await self._cut(triggered_by)

    async def mute(self, *, triggered_by: Identity) -> None:
        self._check_open()
        if self._muted:
            return
        self._muted = True
        await self._emit(Muted(scope=self.scope, muted=True, triggered_by=triggered_by))
        await self._drop_waiting()
        await self._cut(triggered_by)

    async def unmute(self, *, triggered_by: Identity) -> None:
        self._check_open()
        if not self._muted:
            return
        self._muted = False
        await self._emit(Muted(scope=self.scope, muted=False, triggered_by=triggered_by))

    async def _cut(self, cause: Identity) -> Interrupted | None:
        u = self._utterance
        if u is None:
            return None
        self._utterance = None
        if self._playing is not None:
            self._playing.cancel()
        self._transport.clear_audio()
        played = 0.0
        if u.spoke:
            await self._transport.send_control({"type": "stop", "utterance": u.number})
            played = await self._await_played(u, PLAYED_GRACE, None)
        heard = u.ledger.heard(played) if u.spoke else ""
        if self._connection is not None:
            await self._interrupt_model(round(played))
        cut = Interrupted(scope=self.scope, heard=heard, triggered_by=cause)
        await self._emit(cut)
        await self._finish(u, heard, interrupted=True)
        return cut

    async def _interrupt_model(self, played_ms: int) -> None:
        assert self._connection is not None
        try:
            await self._connection.interrupt(played_ms)
        except Exception as exc:
            log.warning("session: the live model could not cut its context: %s", exc)

    async def _drop_waiting(self) -> None:
        while self._waiting:
            await self._finish(self._waiting.popleft(), "", interrupted=True)

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
        elif self._stream is not None:
            await self._stream.feed(frame)

    async def look(self, frames: int = 1, *, triggered_by: Identity) -> list[Part]:
        self._check_open()
        if self._window is None:
            raise ConfigurationError("session: look needs video=True")
        if frames < 1:
            raise InputError("session: frames must be at least 1")
        seen = self._window.newest(frames)
        if not seen:
            raise NotFoundError(f"session: no frame in the last {self._window.seconds:g} s")
        await self._emit(Looked(scope=self.scope, frames=len(seen), triggered_by=triggered_by))
        parts: list[Part] = []
        for at, image in seen:
            parts += [image, Text(text=f"frame at {at:%H:%M:%S}.{at.microsecond // 1000:03d}")]
        return parts

    async def _see(self, image: Image) -> None:
        if self._window is None:
            return
        self._window.append(image)
        if self._connection is not None:
            self._frame = image
            self._frame_ready.set()

    async def _forward_frames(self) -> None:
        assert self._connection is not None
        while True:
            await self._frame_ready.wait()
            self._frame_ready.clear()
            frame, self._frame = self._frame, None
            if frame is not None:
                await self._connection.send_image(frame)

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
            elif isinstance(event, LiveInterrupted):
                await self._cut(self.person)
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
        u = self._causes.popleft() if self._causes else Utterance(None)
        self._start(u)
        if self._muted:
            self._utterance = None
            await self._interrupt_model(0)
            await self._finish(u, "", interrupted=True)

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

    async def _transcripts(self) -> None:
        assert self._stream is not None
        finals: list[str] = []
        partial = ""
        async for event in self._stream.events():
            if isinstance(event, Transcript):
                text = event.text.strip()
                if not text:
                    continue
                if self._hold is not None:
                    self._hold.cancel()
                    self._hold = None
                if self._utterance is not None or self._waiting:
                    await self._cut(self.person)
                    await self._drop_waiting()
                await self._emit(
                    Heard(scope=self.scope, text=event.text, final=event.final, speaker=self.person)
                )
                if event.final:
                    finals.append(text)
                    partial = ""
                else:
                    partial = text
            elif isinstance(event, UtteranceEnd):
                words = " ".join([*finals, partial] if partial else finals)
                finals, partial = [], ""
                if words:
                    await self._utterance_ended(words)
        log.warning("session: the transcription stream ended; the session no longer hears")

    async def _utterance_ended(self, words: str) -> None:
        self._turn.append(words)
        text = " ".join(self._turn)
        probability = await self._done(text) if self._turns is not None else None
        if probability is None or probability >= self._turn_threshold:
            await self._end_turn(text, probability)
        else:
            self._hold = self._spawn(self._hold_turn(text, probability))

    async def _hold_turn(self, text: str, probability: float) -> None:
        await asyncio.sleep(self._turn_timeout)
        self._hold = None
        await self._end_turn(text, probability)

    async def _end_turn(self, text: str, probability: float | None) -> None:
        self._turn = []
        await self._emit(
            TurnEnded(scope=self.scope, text=text, speaker=self.person, probability=probability)
        )

    async def _done(self, text: str) -> float | None:
        assert self._turns is not None
        try:
            decided = as_decided(
                await self._turns.decide({"said": self._last_heard, "heard": text}, {"done": DONE})
            )
        except Exception as exc:
            log.warning("session: the turn check failed; the turn ends: %s", exc)
            return None
        answer = decided.answers.get("done")
        if not isinstance(answer, CheckAnswer):
            log.warning(
                "session: the decision model did not answer 'done' with a check; the turn ends"
            )
            return None
        return answer.probability

    async def _speak(self) -> None:
        while True:
            u = await self._queue.get()
            if u.finished:
                continue
            playing = self._playing = asyncio.create_task(self._play(u))
            await asyncio.wait({playing})
            self._playing = None
            if not playing.cancelled() and (error := playing.exception()) is not None:
                raise error

    async def _play(self, u: Utterance) -> None:
        assert self._speech is not None
        if u.finished:
            return
        with contextlib.suppress(ValueError):
            self._waiting.remove(u)
        if self._muted:
            await self._finish(u, "", interrupted=True)
            return
        self._start(u)
        streamed = not isinstance(u.source, str)
        pieces = sentences_from(u.source)
        try:
            async for sentence in pieces:
                if streamed:
                    u.text += sentence
                try:
                    async for frame in self._speech.speak(sentence):
                        if self._utterance is not u:
                            return
                        await self._send(u, frame)
                except Exception as exc:
                    log.warning("session: the speech model failed: %s", exc)
                    self._abandon(u, ModelError(f"session: the speech model failed: {exc}"))
                    return
                if self._utterance is not u:
                    return
                u.ledger.mark(sentence)
        except Exception as exc:
            self._abandon(u, exc)
            return
        finally:
            await pieces.aclose()
            close = getattr(u.source, "aclose", None)
            if close is not None:
                await close()
        await self._conclude(u)

    def _abandon(self, u: Utterance, error: Exception) -> None:
        if self._utterance is u:
            self._utterance = None
            self._transport.clear_audio()
        u.finished = True
        self._played.pop(u.number, None)
        if u.done is not None and not u.done.done():
            u.done.set_exception(error)

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
