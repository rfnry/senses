# rfnry-senses — implementation plan

> For agentic workers: execute task by task, tests first. Steps use checkboxes (`- [ ]`). Every
> task ends green on its gate and in its own commit. Read `CLAUDE.md` and `docs/senses.md` before
> the first task; `docs/senses.md` is the contract and wins over this plan where they differ — stop
> and report the difference rather than pick.

**Goal:** a from-scratch `rfnry-senses` (Python) and `@rfnry/senses` (TypeScript) that give a model
eyes, ears and a voice in real time: one session, live or cascaded, with barge-in truncated to what
the person heard, declared attribution, a camera window, a WebSocket and a WebRTC transport, and a
framework-free browser client — replacing `../rfnry-speech` and `../rfnry-vision`.

**Architecture:** one `Session` class over a `Transport` protocol. The session owns a handful of
background tasks (inbound media, the live model's events or the transcription stream, the cascaded
speaker, the frame forwarder), started on `async with` and cancelled on close. Utterances are
numbered; the number fences stale audio on the server and rides in the audio header to fence it in
the client. A `Ledger` per utterance maps words to milliseconds of audio sent, so a `played` report
turns into the words heard. Nothing is stored; everything a session knows is in memory and ends
with it. The family blocks (parts, `Tools`, the decision contract, `Identity`) are copied byte for
byte and pinned by `test_family.py`.

**Tech stack:** Python 3.12, pydantic v2, pytest + pytest-asyncio (auto), mypy strict, ruff, poe,
uv; extra `webrtc` = aiortc (which brings PyAV). TypeScript 5 strict, ESM only, no runtime
dependencies, vitest, Biome (format + lint), `tsc` for the build, npm with a lockfile, Node ≥ 22.
The thread plan did not exist when this was written; the TypeScript toolchain here is the choice
for both clients — the thread plan should adopt it or name its difference.

**Spec:** `docs/plans/2026-09-24-senses-design.md` (as corrected in commit "docs: senses.md — the
contract, from the design", then "docs: senses — streamed say, hello-token auth, video path
explained", then "docs: senses — auth at the consumer's upgrade; setFps; OpenAI transcription in
examples") and `docs/senses.md`. Depends on: the content parts, landed in
rfnry-engine and rfnry-memory; `engine.tool(agent, *, scope, …) -> Tools`
(`../engine/docs/plans/2026-09-24-engine-tool-plan.md`, engine commit 79af329) for the two-minds
examples; rfnry-thread's `Identity` block and `ThreadTools` for `test_family.py` and the
`two_minds_thread` example (both skip until thread lands).

## Decisions made while planning

1. **Module map** (`packages/python/src/rfnry_senses/`): `errors.py`; `models.py` (the copied family
   blocks and `Identity`); `tools.py` (the copied `Tools` block); `contracts.py` (`AudioFrame`, the
   live, transcription and speech contracts and their events); `events.py` (what a session
   reports); `voice.py` (`sentences`, `Ledger`); `window.py` (`FrameWindow`); `transport.py`
   (`Transport`, `Played`, `Loopback`); `session.py` (`Session`); `senses_tools.py`
   (`SensesTools`); `wire.py` (the byte and JSON frames); `websocket.py` (`Connection`, `serve`,
   `WebSocketTransport`); `webrtc.py` (the extra: `accept_offer`, `WebRTCTransport`).
2. **Constants are module-level** in `session.py` — `PLAYED_GRACE = 0.25`, `PLAYBACK_SLACK = 1.0` —
   so tests can shrink them with `monkeypatch`; they are not constructor arguments. Time comes from
   `time.monotonic` imported by name into `session.py` and `window.py`, which tests replace with a
   `Clock`.
3. **One utterance at a time.** `Session._utterance` is the utterance being spoken or concluding.
   A new live response finishes a still-concluding previous one with everything it sent (the
   client will play it); a barge-in cuts the current one only. Rare overlaps are approximated, not
   modelled.
4. **Heard is cut on whole marks.** Cascaded marks are sentences, ended at the audio sent when the
   sentence finished. Live marks are the model's transcript deltas, ended with the next audio frame
   sent after them (the transcript precedes its audio). `heard(played_ms)` keeps marks whose end is
   at most `played_ms + 1`.
5. **Played.** The client answers `stop` and `silent` with `played {utterance, ms}`. After `stop`
   the session waits `PLAYED_GRACE`, then estimates `min(sent_ms, ms since the first frame was
   sent)`. After `silent` it waits the audio still to play plus `PLAYBACK_SLACK`, then assumes all
   of it played.
6. **The transcription stream ending is not fatal** (the voice still works; logged as a warning).
   The live connection ending, `LiveFailed`, a contract breach, or any unexpected exception in a
   background task closes the session with `Closed(reason="failed")` and fails every waiting `say`
   or `prompt` with the error (`ModelError` unless it was another `SensesException`). A speech
   model failing fails only the `say` that needed it.
7. **Subscriptions** register when `events()` is called (a class, not an async generator) and live
   as long as the session; an abandoned iterator keeps its queue until close. Documented cost, not
   a leak across sessions.
8. **The WebRTC extra has no numpy.** PCM goes in and out of PyAV frames through
   `frame.planes[0]`; the old code's `numpy` round trips are dropped. No default STUN server (the
   old default named a vendor); `ice_servers=` is the consumer's.
9. **The TypeScript media layer is injectable** for tests only through module-internal
   constructors (`Playback(context, report)`, `WebSocketLink(socketFactory)`); `connect()` is the
   one public entry and takes no test hooks.
10. **`say` takes `str | AsyncIterator[str]`** (Task 8). Cascaded: `voice.sentences_from` cuts the
    chunks with the same `sentences` splitter — a sentence is complete once text follows it — so
    synthesis starts on the first one; `Said.text` accumulates the sentences taken. Live: the
    chunks are joined and sent once the stream ends, because `LiveConnection.say` takes whole text;
    the cause joins the FIFO then. A cut stops reading the stream and `aclose()`s it. An empty
    stream returns an empty `Said`; a stream that raises fails only its `say`, with its own error.
    `SensesTools`' `say` stays text (a model's tool arguments are a string).
11. **Authentication lives at the consumer** (superseding the hello-token decision), as in
    rfnry-thread. `serve(connection, *, identity, scope)` and `accept_offer(sdp, *, identity, scope)`
    take who the person is and their scope from the consumer's authenticated endpoint; there is no
    `authenticate` callback, no credential in the hello (it carries only `protocol`) and no hello
    deadline. `Connection` still exposes no headers or cookies: the consumer's framework reads them
    on the upgrade, before senses. The WebRTC client sends its `hello` as the first data-channel
    message; a wrong protocol there gets an `error` frame and ends the transport. The TypeScript
    client takes `protocols` (WebSocket subprotocols) and `headers` (the WebRTC offer request);
    cookies need neither.
12. **`setFps(fps)`** on the TypeScript connection: the WebSocket path restarts the camera's
    sampling interval at the new rate; the WebRTC path calls `applyConstraints({ frameRate: fps })`
    on the camera track. A non-positive `fps` throws `senses: fps must be positive`; no camera
    throws `senses: setFps needs a camera`.

## Pre-change checklist

1. **Wanted two model generations ahead?** Yes. Models that hear, see and speak natively still
   need their audio and frames carried in real time, cut off when a person talks, and a record of
   what was heard and who caused what. Nothing here captions, filters or second-guesses perception.
2. **More or less useful as models improve?** More — substrate: transports, media plumbing,
   barge-in truncation, attribution, the model contracts. The cascaded mode's turn check gets
   cheaper and better as decision models improve; it is one optional `Check`.
3. **If less, a structural boundary?** The output gate (`mute`), what the person actually heard,
   camera permission (`video=`, `pauseCamera`), tenant scope, transport limits — all kept as
   boundaries.
4. **Exists because a model is weak at X?** No. There is no voice-activity detector, no vision
   model, no frame sampling policy; the transcription model's own utterance end and an optional
   decision check stand in for the cut `TurnDetector`.
5. **Generation only, decision only, both?** A live model alone; transcription and speech around
   any text mind (with no generation model in the session at all); a decision model only for turn
   completion, optional; a camera-only session with no model. Each verb names what it needs and
   raises `ConfigurationError` without it.

## Global constraints

- No comments, no docstrings, no vendor names in `packages/` or `docs/senses.md` (rules 4, 7).
  Vendor names appear only in `examples/*/providers.py`, their `pyproject.toml` and this plan.
- Every error message starts with its module's file stem — `session:`, `wire:`, `transport:`,
  `contracts:`, `models:`, `tools:`, `senses_tools:` — then lowercase (rule 9); logs are lowercase.
- `scope` comes from the consumer's endpoint through the transport; every event carries it (rule 8).
- The library writes nothing to disk; tests that write golden fixtures do so only under
  `RFNRY_WRITE_FIXTURES=1`.
- `docs/senses.md` changes in the same commit as any change to the public surface (rule 5).
- The family blocks are pasted, never edited in place; a change to one is a change in every
  sibling first.
- Python gate for every task: `cd packages/python && uv run poe format && uv run poe check:fix &&
  uv run poe check && uv run poe typecheck && uv run poe test` (the plan's snippets are not
  import-sorted; `check:fix` sorts them). TypeScript gate: `cd packages/typescript && npm run check
  && npm run typecheck && npm test`.

## Review focus

The spec implies five failure modes the tests must pin (each has a named test below):

1. **Barge-in racing the last synthesized chunk** — a sentence that finishes synthesizing, or a
   live audio frame that arrives, after the cut never reaches the person, and a frame already on
   the wire is dropped by the client (`test_audio_after_the_cut_never_reaches_the_person`,
   `test_a_sentence_finished_after_barge_in_is_never_sent`, TS
   `drops audio of a stopped utterance`).
2. **`played_ms` never arriving** — an interruption waits `PLAYED_GRACE`, then estimates from the
   clock; a natural end waits the remaining audio plus slack, then counts everything sent
   (`test_a_silent_client_is_estimated_from_the_clock`,
   `test_a_silent_client_at_the_natural_end_heard_everything`).
3. **Attribution FIFO under concurrent `say`/`prompt`** — the order causes are queued is the order
   the connection is called, so each response gets the right cause, and an unsolicited response
   gets `None` (`test_concurrent_say_and_prompt_keep_their_causes_in_order`).
4. **A live model emitting tool calls during interruption** — every call is dispatched and answered
   exactly once, the interruption is not delayed by it, and nothing is submitted after close
   (`test_a_tool_call_made_by_an_interrupted_response_is_answered_once`,
   `test_no_result_is_submitted_after_close`).
5. **Frame-window eviction under a stalled client** — a camera that stops sending leaves an empty
   window after `window` seconds, never a stale frame presented as current; the byte cap evicts
   oldest-first and keeps the newest (`test_a_stalled_camera_leaves_an_empty_window`,
   `test_the_byte_cap_keeps_the_newest_frame`).

Also worth a reviewer's eye: decision 3 (overlapping live responses are approximated); the muted
live response being cut with `interrupt(0)` (it tells the model it was not heard, and cancels the
generation); the `Looked`/`Muted`/`Informed` events the docs added; a streamed `say` cut while
senses is waiting on the consumer's stream — the cancellation lands inside the consumer's
generator, and `_play`'s `finally` closes it either way
(`test_a_barge_in_mid_stream_stops_reading_the_stream`).

## Adapted from the old libraries, or written fresh

| piece | source |
| --- | --- |
| sentence cutting | adapted: `../rfnry-speech/packages/server-python/src/rfnry_speech_server/session/cascaded.py` `_cut_sentences` |
| chunk ledger, heard at `played_ms` | adapted: same file, `chunk_ledger`, `_heard_text`; generalised to live transcript marks |
| generation fence, barge-in sequence (clear, wait `played`, truncate, emit) | adapted: same file, `_force_cancel_current_turn`; the number now also rides in the audio header |
| live event mapping, `triggered_by` FIFO | adapted: `rfnry_speech_server/session/live.py` `_consume_provider_events` and `session.py` `_pending_triggers`; empty queue → `None` (fresh) |
| tool calls answered once, cancelled on close | adapted: `../rfnry-vision/.../session/live.py` `pending` set |
| frame window | adapted: `rfnry_vision_server/session/frames.py` `FrameWindow`; `newest(n)` and wall-clock stamps are fresh |
| WS framing | fresh (the old ws-pcm hid a JSON+base64 frame inside the channel and ws-frames had no header); hello handling adapted from `transport/ws_pcm.py` `wrap_ws_for_pcm` and `ws_frames.py` |
| WebSocket outbound queue, `clear` | adapted: `rfnry_speech_server/transport/websocket.py` `_outbound`, `clear_outbound`; controls now share the queue so order holds |
| aiortc outbound voice track: 20 ms rechunk, pacing, prebuffer | adapted: `rfnry_speech_server/transport/webrtc.py` `_PCMOutboundTrack` (numpy removed) |
| aiortc inbound resample, data-channel controls, `accept_offer` | adapted: same file; the data-channel hello and the `answer` attribute are fresh |
| camera track → JPEG | adapted: `rfnry_vision_server/transport/convert.py` `_codec_for`/`_encode` (input is already an `av.VideoFrame`) |
| capture worklet, stream resampler, PCM16 | adapted: `../rfnry-speech/packages/client-react/src/audio/capture-worklet.ts`, `capture.ts`, `resampler.ts` |
| playback with played ms | adapted: `client-react/src/audio/playback.ts` (`flush`, `_inFlightMs`); per-utterance epochs and the fence are fresh |
| WebRTC played ms from receiver stats | adapted: `client-react/src/audio/remotePlayback.ts` |
| browser WebRTC offer flow | adapted: `client-react/src/transport/webrtc.ts`, `../rfnry-vision/packages/client-react/src/transport/webrtc.ts` |
| camera JPEG sampling | adapted: `../rfnry-vision/packages/client-react/src/video/capture.ts`, `encoder.ts` (the encoder's canvas path only) |
| session, `say`/`prompt`/`inform`, `look`, events, `SensesTools`, `serve`, `Loopback`, the cascaded turn check | fresh |

## Task 1 — Scaffold and errors

**Files:** create `.gitignore`, `packages/python/pyproject.toml`, `packages/python/README.md`,
`packages/python/src/rfnry_senses/{__init__.py,errors.py,py.typed}`,
`packages/python/tests/{__init__.py,unit/__init__.py,unit/conftest.py,unit/test_errors.py,integration/__init__.py,e2e/__init__.py}`.

**Interfaces:**

```python
class SensesException(Exception): ...
class ConfigurationError(SensesException): ...
class InputError(SensesException): ...
class NotFoundError(SensesException): ...
class ModelError(SensesException): ...
class TransportError(SensesException): ...
```

- [ ] **Step 1: pyproject.** Copy `../engine/packages/python/pyproject.toml` and change: `name =
  "rfnry-senses"`, `description = "Eyes, ears and a voice for models, in real time"`,
  `dependencies = ["pydantic>=2.13.4"]`, `[project.optional-dependencies] webrtc = ["aiortc"]`
  (add it with `uv add --optional webrtc aiortc` and keep the resolved lower bound); `dev` = the
  engine's dev list minus `asyncpg`, `types-PyYAML`, `types-regex`, plus `aiortc`, `websockets`,
  `fastapi`, `httpx2` (Starlette's test client deprecates `httpx`; changed in Task 13). Ruff's
  bugbear takes `extend-immutable-calls = ["fastapi.Depends"]`, and mypy relaxes `warn_return_any`
  for `tests.integration.socket_adapter` only — the docs' adapter is pinned verbatim and returns
  the ASGI message's value (both added in Task 13). `packages = ["src/rfnry_senses"]`. Markers and `addopts`:

```toml
markers = [
    "integration: real sockets in this process",
    "webrtc: real peer connections (needs the webrtc extra)",
    "live: calls real models (set RFNRY_LIVE_ADAPTERS to a module path)",
]
addopts = "-m 'not integration and not webrtc and not live'"
```

  Poe tasks as the engine's, with `"test:integration" = "pytest -q -m integration"`, `"test:webrtc"
  = "pytest -q -m webrtc"`, `"test:e2e" = "pytest -q -m live"`. `[tool.mypy] files =
  ["src/rfnry_senses", "tests"]`, overrides `ignore_missing_imports` for `aiortc.*` and `av.*`.
  Ruff `line-length = 100` (the decision-model block's formatting depends on it), and
  `[tool.ruff.format] exclude = ["src/rfnry_senses/models.py", "src/rfnry_senses/tools.py"]`: the
  siblings keep the family blocks' long lines unformatted, so formatting the copies would break
  byte-identity (found in Task 2).
- [ ] **Step 2: `.gitignore`** — `__pycache__/`, `.venv/`, `*.egg-info/`, `node_modules/`,
  `packages/typescript/dist/`, `examples/**/uv.lock`, `.env*`.
- [ ] **Step 3: failing test** — `tests/unit/test_errors.py`:

```python
import pytest

from rfnry_senses.errors import (
    ConfigurationError,
    InputError,
    ModelError,
    NotFoundError,
    SensesException,
    TransportError,
)


@pytest.mark.parametrize("error", [ConfigurationError, InputError, ModelError, NotFoundError, TransportError])
def test_every_error_is_a_senses_error(error: type[Exception]) -> None:
    assert issubclass(error, SensesException) and error.__name__.endswith("Error")


def test_the_root_takes_the_exception_suffix() -> None:
    assert SensesException.__name__ == "SensesException" and SensesException.__bases__ == (Exception,)
```

- [ ] **Step 4: implement** `errors.py` exactly as the interface. `__init__.py` holds `__all__:
  list[str] = []` until Task 15. `conftest.py` is empty for now.
- [ ] **Step 5: gate, commit** `feat: rfnry-senses scaffold and errors`.

## Task 2 — The family blocks and `test_family.py`

**Files:** create `src/rfnry_senses/models.py`, `src/rfnry_senses/tools.py`,
`tests/unit/test_family.py`, `tests/unit/test_models.py`.

**Interfaces:** `State`, `Text`, `Image`, `Audio`, `Document`, `Part`, `Content`, `ToolSpec`,
`Usage`, `Choice`, `Score`, `Check`, `Question`, `ChoiceAnswer`, `ScoreAnswer`, `CheckAnswer`,
`Answer`, `Decided`, `as_decided`, `UsageLike`, `ChoiceAnswerLike`, `ScoreAnswerLike`,
`CheckAnswerLike`, `AnswerLike`, `DecidedLike`, `DecisionModel`, `Identity` in `models.py`;
`Result`, `Body`, `Dispatcher`, `ToolsLike`, `Tool`, `Tools` in `tools.py`.

- [ ] **Step 1: failing tests** — `tests/unit/test_family.py`:

```python
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[4]
FAMILY = ROOT.parent
OWN = ROOT / "packages/python/src/rfnry_senses"

BLOCKS = {
    "state": ("models.py", "State = ", "\n", ("engine", "memory")),
    "parts": ("models.py", "class Text(BaseModel):", "Content = str | list[Part]\n", ("engine", "memory", "thread")),
    "tool spec": ("models.py", "class ToolSpec(BaseModel):", "    input_schema: dict[str, Any]\n", ("engine", "memory")),
    "usage": ("models.py", "class Usage(BaseModel):", "            cache_read=self.cache_read + other.cache_read,\n        )\n", ("engine", "memory")),
    "decision": ("models.py", "class Choice(BaseModel):", "    model: str | None = None\n", ("engine", "memory")),
    "as_decided": ("models.py", "def as_decided(", "from exc\n", ("engine", "memory")),
    "usage like": ("models.py", "class UsageLike(Protocol):", "    def cache_read(self) -> int: ...\n", ("engine", "memory")),
    "answers like": ("models.py", "class ChoiceAnswerLike(Protocol):", "    def model(self) -> str | None: ...\n", ("engine", "memory")),
    "decision model": ("models.py", "class DecisionModel(Protocol):", "| DecidedLike: ...\n", ("engine",)),
    "tools": ("tools.py", "Result = ", "if n not in names})\n", ("engine", "memory", "thread")),
    "identity": ("models.py", "class Identity(BaseModel):", "    metadata: dict[str, Any] = {}\n", ("thread",)),
}

SOURCES = {
    "engine": {"tools.py": "engine/packages/python/src/rfnry_engine/tools/tools.py", "models.py": "engine/packages/python/src/rfnry_engine/models.py"},
    "memory": {"tools.py": "memory/packages/python/src/rfnry_memory/tools.py", "models.py": "memory/packages/python/src/rfnry_memory/models.py"},
    "thread": {"tools.py": "thread/packages/python/src/rfnry_thread", "models.py": "thread/packages/python/src/rfnry_thread"},
}


def cut(text: str, start: str, end: str) -> str | None:
    first = text.find(start)
    if first < 0:
        return None
    last = text.find(end, first + len(start))
    return text[first:] if last < 0 else text[first : last + len(end)]


def sibling(name: str, own_file: str, start: str) -> str | None:
    path = FAMILY / SOURCES[name][own_file]
    if path.is_dir():
        return next((f.read_text(encoding="utf-8") for f in sorted(path.rglob("*.py")) if start in f.read_text(encoding="utf-8")), None)
    return path.read_text(encoding="utf-8") if path.exists() else None


PAIRS = [(block, name) for block, spec in BLOCKS.items() for name in spec[3]]


@pytest.mark.parametrize(("block", "name"), PAIRS, ids=[f"{b}-{n}" for b, n in PAIRS])
def test_the_shared_block_is_byte_identical_with_the_sibling(block: str, name: str) -> None:
    own_file, start, end, _ = BLOCKS[block]
    theirs = sibling(name, own_file, start)
    if theirs is None or cut(theirs, start, end) is None:
        pytest.skip(f"no {block} block in a {name} checkout at {FAMILY / name}")
    assert cut((OWN / own_file).read_text(encoding="utf-8"), start, end) == cut(theirs, start, end)
```

  `tests/unit/test_models.py`:

```python
import pytest

from rfnry_senses.errors import InputError, ModelError
from rfnry_senses.models import CheckAnswer, Decided, Identity, Image, Text, as_decided


def test_a_media_part_carries_data_or_url_not_both() -> None:
    with pytest.raises(InputError, match="^models: image carries data or url, not both$"):
        Image(media_type="image/jpeg", data=b"x", url="https://example.com/a.jpg")
    with pytest.raises(InputError, match="^models: image carries data or url, not both$"):
        Image(media_type="image/jpeg")


def test_bytes_travel_as_standard_base64() -> None:
    image = Image(media_type="image/jpeg", data=b"\xff\xd8\xff")
    assert image.model_dump(mode="json")["data"] == "/9j/"
    assert Image.model_validate_json(image.model_dump_json()) == image
    assert Text(text="hi").model_dump() == {"type": "text", "text": "hi"}


def test_a_decision_is_read_by_its_fields() -> None:
    assert isinstance(as_decided({"done": CheckAnswer(probability=0.8)}), Decided)
    with pytest.raises(ModelError, match="^models: the decision model's answers do not fit the contract"):
        as_decided({"done": {"type": "check", "probability": 2.0}})


def test_an_identity_has_a_role() -> None:
    ana = Identity(role="user", id="ana", name="Ana")
    assert ana.metadata == {} and Identity.model_validate(ana.model_dump()) == ana
```

- [ ] **Step 2: implement `models.py`.** Header:

```python
from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_serializer, model_validator

from rfnry_senses.errors import InputError, ModelError
```

  Then paste from `../engine/packages/python/src/rfnry_engine/models.py`, byte for byte, in this
  order, two blank lines between blocks: the `State = …` line; `class Text` through `Content = str
  | list[Part]`; `class ToolSpec`; `class Usage` (through its `__add__`); `class Choice` through
  `class Decided`; `def as_decided`; `class UsageLike`; `class ChoiceAnswerLike` through `class
  DecidedLike`; `@runtime_checkable` + `class DecisionModel` (the engine's three-line
  formatting). Nothing else from the engine file (no `Message`, `Reply`, `settle`, `confidence`,
  generation protocols). Then, last in the file:

```python
class Identity(BaseModel):
    role: Literal["user", "assistant", "system"]
    id: str
    name: str | None = None
    metadata: dict[str, Any] = {}
```

  If rfnry-thread has landed by now, copy its `Identity` instead and keep the test green. The
  identity block ends at its `metadata` line, the marker rfnry-thread's own family test uses (a
  `"\n\n\n"` end would cut to the end of this file, where `Identity` is last, and never match a
  sibling that has code after it).
- [ ] **Step 3: implement `tools.py`.** Copy `../engine/packages/python/src/rfnry_engine/tools/tools.py`
  whole, changing only the two import lines to `rfnry_senses.errors` and `rfnry_senses.models`.
- [ ] **Step 4: gate** (the family tests pass against engine and memory; thread skips until it
  lands). **Commit** `feat: the family blocks — parts, tools, the decision contract, identity`.

## Task 3 — Contracts and events

**Files:** create `src/rfnry_senses/contracts.py`, `src/rfnry_senses/events.py`,
`tests/unit/test_contracts.py`, `tests/unit/test_events.py`.

**Interfaces** — `contracts.py`, complete:

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rfnry_senses.errors import InputError
from rfnry_senses.models import Content, Image, ToolSpec


class AudioFrame(BaseModel):
    model_config = ConfigDict(frozen=True)

    data: bytes
    sample_rate: int
    channels: int = 1

    @model_validator(mode="after")
    def _whole_samples(self) -> Self:
        if self.sample_rate <= 0:
            raise InputError(f"contracts: sample rate {self.sample_rate} is not positive")
        if self.channels not in (1, 2):
            raise InputError(f"contracts: audio has 1 or 2 channels, not {self.channels}")
        if len(self.data) % (2 * self.channels):
            raise InputError(f"contracts: {len(self.data)} bytes do not fill {self.channels}-channel 16-bit samples")
        return self

    @property
    def duration_ms(self) -> float:
        return len(self.data) / (2 * self.channels) * 1000 / self.sample_rate


class LiveAudio(BaseModel):
    frame: AudioFrame


class LiveHeard(BaseModel):
    text: str
    final: bool = False


class LiveSaid(BaseModel):
    text: str


class LiveStarted(BaseModel):
    pass


class LiveDone(BaseModel):
    pass


class LiveToolCall(BaseModel):
    call_id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class LiveInterrupted(BaseModel):
    pass


class LiveFailed(BaseModel):
    message: str


LiveEvent = LiveAudio | LiveHeard | LiveSaid | LiveStarted | LiveDone | LiveToolCall | LiveInterrupted | LiveFailed


class Transcript(BaseModel):
    text: str
    final: bool = False


class UtteranceEnd(BaseModel):
    pass


@runtime_checkable
class LiveConnection(Protocol):
    async def send_audio(self, frame: AudioFrame) -> None: ...
    async def send_image(self, image: Image) -> None: ...
    async def send_text(self, text: str, *, respond: bool) -> None: ...
    async def say(self, text: str) -> None: ...
    async def interrupt(self, played_ms: int) -> None: ...
    async def submit_result(self, call_id: str, result: Content, is_error: bool) -> None: ...
    def events(self) -> AsyncIterator[LiveEvent]: ...
    async def close(self) -> None: ...


@runtime_checkable
class LiveModel(Protocol):
    async def open(self, *, tools: list[ToolSpec], instructions: str | None) -> LiveConnection: ...


@runtime_checkable
class Transcription(Protocol):
    async def feed(self, frame: AudioFrame) -> None: ...
    def events(self) -> AsyncIterator[Transcript | UtteranceEnd]: ...
    async def close(self) -> None: ...


@runtime_checkable
class TranscriptionModel(Protocol):
    def stream(self) -> Transcription: ...


@runtime_checkable
class SpeechModel(Protocol):
    def speak(self, text: str) -> AsyncIterator[AudioFrame]: ...
```

`events.py`, complete:

```python
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from rfnry_senses.models import Content, Identity


class _Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Heard(_Event):
    kind: Literal["heard"] = "heard"
    text: str
    final: bool
    speaker: Identity


class TurnEnded(_Event):
    kind: Literal["turn_ended"] = "turn_ended"
    text: str
    speaker: Identity
    probability: float | None = None


class Said(_Event):
    kind: Literal["said"] = "said"
    text: str
    heard: str
    interrupted: bool
    triggered_by: Identity | None


class Interrupted(_Event):
    kind: Literal["interrupted"] = "interrupted"
    heard: str
    triggered_by: Identity


class Speaking(_Event):
    kind: Literal["speaking"] = "speaking"


class Silent(_Event):
    kind: Literal["silent"] = "silent"


class Looked(_Event):
    kind: Literal["looked"] = "looked"
    frames: int
    triggered_by: Identity


class Muted(_Event):
    kind: Literal["muted"] = "muted"
    muted: bool
    triggered_by: Identity


class Informed(_Event):
    kind: Literal["informed"] = "informed"
    text: str
    triggered_by: Identity


class ToolCalled(_Event):
    kind: Literal["tool_called"] = "tool_called"
    name: str
    input: dict[str, Any]
    output: Content
    is_error: bool


class Closed(_Event):
    kind: Literal["closed"] = "closed"
    reason: Literal["closed", "hung_up", "failed"]
    message: str | None = None


Event = Annotated[
    Heard | TurnEnded | Said | Interrupted | Speaking | Silent | Looked | Muted | Informed | ToolCalled | Closed,
    Field(discriminator="kind"),
]
```

- [ ] **Step 1: failing tests** — `tests/unit/test_contracts.py`:

```python
import pytest

from rfnry_senses.contracts import AudioFrame
from rfnry_senses.errors import InputError


def test_a_frame_knows_its_duration() -> None:
    assert AudioFrame(data=bytes(640), sample_rate=16000).duration_ms == 20.0
    assert AudioFrame(data=bytes(960), sample_rate=24000, channels=2).duration_ms == 10.0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"data": bytes(3), "sample_rate": 16000}, "^contracts: 3 bytes do not fill 1-channel 16-bit samples$"),
        ({"data": bytes(4), "sample_rate": 0}, "^contracts: sample rate 0 is not positive$"),
        ({"data": bytes(6), "sample_rate": 16000, "channels": 3}, "^contracts: audio has 1 or 2 channels, not 3$"),
    ],
)
def test_a_frame_is_whole_samples(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(InputError, match=message):
        AudioFrame.model_validate(kwargs)
```

  `tests/unit/test_events.py`:

```python
from pydantic import TypeAdapter

from rfnry_senses.events import Closed, Event, Heard, Said
from rfnry_senses.models import Identity

ANA = Identity(role="user", id="ana", name="Ana")


def test_events_round_trip_by_kind() -> None:
    adapter: TypeAdapter[Event] = TypeAdapter(Event)
    for event in (
        Heard(scope="acme", text="a table for two", final=True, speaker=ANA),
        Said(scope="acme", text="For two.", heard="For two.", interrupted=False, triggered_by=None),
        Closed(scope="acme", reason="hung_up"),
    ):
        assert adapter.validate_json(event.model_dump_json()) == event


def test_every_event_carries_its_scope_and_time() -> None:
    dumped = Heard(scope="acme", text="hi", final=False, speaker=ANA).model_dump(mode="json")
    assert dumped["scope"] == "acme" and dumped["kind"] == "heard" and dumped["at"].endswith("Z")
```

- [ ] **Step 2: implement** both modules as above. **Gate. Commit** `feat: the model contracts and
  the session's events`.

## Task 4 — Transport, `Played`, `Loopback`, and the test fakes

**Files:** create `src/rfnry_senses/transport.py`, `tests/unit/fakes.py`,
`tests/unit/test_loopback.py`; modify `tests/unit/conftest.py`.

**Interfaces** — `transport.py`, complete:

```python
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from rfnry_senses.contracts import AudioFrame
from rfnry_senses.models import Identity, Image


class Played(BaseModel):
    utterance: int
    ms: int


@runtime_checkable
class Transport(Protocol):
    identity: Identity
    scope: str

    def inbound(self) -> AsyncIterator[AudioFrame | Image | Played]: ...
    async def send_audio(self, frame: AudioFrame, utterance: int) -> None: ...
    async def send_control(self, control: dict[str, Any]) -> None: ...
    def clear_audio(self) -> None: ...
    async def close(self) -> None: ...


class Loopback:
    def __init__(
        self,
        identity: Identity,
        scope: str,
        *,
        played: Callable[[int, float], int] | None = None,
        answer: bool = True,
    ) -> None:
        self.identity = identity
        self.scope = scope
        self.audio: list[AudioFrame] = []
        self.controls: list[dict[str, Any]] = []
        self.closed = False
        self._played = played
        self._answer = answer
        self._sent: dict[int, float] = {}
        self._inbox: asyncio.Queue[AudioFrame | Image | Played | None] = asyncio.Queue()

    async def speak(self, frame: AudioFrame) -> None:
        await self._inbox.put(frame)

    async def show(self, jpeg: bytes) -> None:
        await self._inbox.put(Image(media_type="image/jpeg", data=jpeg))

    async def hang_up(self) -> None:
        await self._inbox.put(None)

    async def inbound(self) -> AsyncIterator[AudioFrame | Image | Played]:
        while (item := await self._inbox.get()) is not None:
            yield item

    async def send_audio(self, frame: AudioFrame, utterance: int) -> None:
        self.audio.append(frame)
        self._sent[utterance] = self._sent.get(utterance, 0.0) + frame.duration_ms

    async def send_control(self, control: dict[str, Any]) -> None:
        self.controls.append(control)
        if self._answer and control["type"] in ("stop", "silent"):
            utterance = control["utterance"]
            sent = self._sent.get(utterance, 0.0)
            ms = self._played(utterance, sent) if self._played is not None else int(sent)
            self._inbox.put_nowait(Played(utterance=utterance, ms=ms))

    def clear_audio(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True
        self._inbox.put_nowait(None)
```

`tests/unit/fakes.py`, complete — every later unit test uses these:

```python
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

from rfnry_senses.contracts import (
    AudioFrame,
    LiveAudio,
    LiveDone,
    LiveEvent,
    LiveSaid,
    LiveStarted,
    Transcript,
    UtteranceEnd,
)
from rfnry_senses.models import CheckAnswer, Content, Identity, Image, Question, State, ToolSpec

ANA = Identity(role="user", id="ana", name="Ana")
HOST = Identity(role="assistant", id="host", name="Host")
JPEG = b"\xff\xd8\xff\xe0frame\xff\xd9"


def pcm(ms: int, rate: int = 16000) -> AudioFrame:
    return AudioFrame(data=bytes(rate * ms // 1000 * 2), sample_rate=rate)


def response(*words: str, ms: int = 100) -> list[LiveEvent]:
    events: list[LiveEvent] = [LiveStarted()]
    for word in words:
        events += [LiveSaid(text=word), LiveAudio(frame=pcm(ms, 24000))]
    return [*events, LiveDone()]


async def until(condition: Callable[[], bool], timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.001)


async def next_of[T](events: AsyncIterator[Any], kind: type[T], timeout: float = 1.0) -> T:
    async with asyncio.timeout(timeout):
        async for event in events:
            if isinstance(event, kind):
                return event
    raise AssertionError(f"no {kind.__name__} before the stream ended")


class ScriptedConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.audio: list[AudioFrame] = []
        self.images: list[Image] = []
        self.results: list[tuple[str, Content, bool]] = []
        self.interrupts: list[int] = []
        self.refuse_interrupt = False
        self.closed = False
        self._inbox: asyncio.Queue[LiveEvent | None] = asyncio.Queue()

    def emit(self, *events: LiveEvent) -> None:
        for event in events:
            self._inbox.put_nowait(event)

    async def send_audio(self, frame: AudioFrame) -> None:
        self.audio.append(frame)

    async def send_image(self, image: Image) -> None:
        self.images.append(image)

    async def send_text(self, text: str, *, respond: bool) -> None:
        await asyncio.sleep(0)
        self.calls.append(("text", text, respond))

    async def say(self, text: str) -> None:
        await asyncio.sleep(0)
        self.calls.append(("say", text))

    async def interrupt(self, played_ms: int) -> None:
        if self.refuse_interrupt:
            raise RuntimeError("cannot cut a response")
        self.interrupts.append(played_ms)

    async def submit_result(self, call_id: str, result: Content, is_error: bool) -> None:
        self.results.append((call_id, result, is_error))

    async def events(self) -> AsyncIterator[LiveEvent]:
        while (event := await self._inbox.get()) is not None:
            yield event

    async def close(self) -> None:
        self.closed = True
        self._inbox.put_nowait(None)


class ScriptedLive:
    def __init__(self) -> None:
        self.connection = ScriptedConnection()
        self.opened: dict[str, Any] = {}

    async def open(self, *, tools: list[ToolSpec], instructions: str | None) -> ScriptedConnection:
        self.opened = {"tools": tools, "instructions": instructions}
        return self.connection


class ScriptedTranscription:
    def __init__(self) -> None:
        self.fed: list[AudioFrame] = []
        self._inbox: asyncio.Queue[Transcript | UtteranceEnd | None] = asyncio.Queue()

    def stream(self) -> ScriptedTranscription:
        return self

    def hear(self, *texts: str, final: bool = True, end: bool = True) -> None:
        for text in texts:
            self._inbox.put_nowait(Transcript(text=text, final=final))
        if end:
            self._inbox.put_nowait(UtteranceEnd())

    async def feed(self, frame: AudioFrame) -> None:
        self.fed.append(frame)

    async def events(self) -> AsyncIterator[Transcript | UtteranceEnd]:
        while (event := await self._inbox.get()) is not None:
            yield event

    async def close(self) -> None:
        self._inbox.put_nowait(None)


class Tone:
    def __init__(self, ms_per_word: int = 100) -> None:
        self.ms = ms_per_word
        self.spoken: list[str] = []
        self.held: str | None = None
        self.release = asyncio.Event()
        self.fail = False

    async def speak(self, text: str) -> AsyncIterator[AudioFrame]:
        self.spoken.append(text)
        if self.fail:
            raise RuntimeError("synthesis failed")
        if text == self.held:
            await self.release.wait()
        for _ in text.split():
            yield pcm(self.ms)


class Done:
    def __init__(self, *probabilities: float) -> None:
        self.probabilities = list(probabilities)
        self.states: list[State] = []

    async def decide(self, state: State, questions: dict[str, Question]) -> dict[str, CheckAnswer]:
        self.states.append(state)
        return {"done": CheckAnswer(probability=self.probabilities.pop(0))}
```

`tests/unit/conftest.py`:

```python
import pytest


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr("rfnry_senses.session.monotonic", fake)
    monkeypatch.setattr("rfnry_senses.window.monotonic", fake)
    return fake
```

(The fixture is used from Task 6 on, when both modules exist.)

- [ ] **Step 1: failing tests** — `tests/unit/test_loopback.py`:

```python
from rfnry_senses.models import Image
from rfnry_senses.transport import Loopback, Played, Transport

from .fakes import ANA, JPEG, pcm


async def test_the_loopback_is_a_transport_that_answers_stop_with_what_it_received() -> None:
    link = Loopback(ANA, "acme")
    assert isinstance(link, Transport)
    await link.send_audio(pcm(100), 1)
    await link.send_audio(pcm(100), 1)
    await link.send_control({"type": "stop", "utterance": 1})
    await link.show(JPEG)
    await link.hang_up()
    received = [item async for item in link.inbound()]
    assert received[0] == Played(utterance=1, ms=200)
    assert isinstance(received[1], Image) and received[1].media_type == "image/jpeg"


async def test_the_loopback_plays_what_it_is_told_or_stays_silent() -> None:
    told = Loopback(ANA, "acme", played=lambda utterance, sent: 70)
    await told.send_control({"type": "silent", "utterance": 4})
    silent = Loopback(ANA, "acme", answer=False)
    await silent.send_control({"type": "stop", "utterance": 4})
    await told.hang_up()
    await silent.hang_up()
    assert [item async for item in told.inbound()] == [Played(utterance=4, ms=70)]
    assert [item async for item in silent.inbound()] == []
```

- [ ] **Step 2: implement** `transport.py`, `fakes.py`, `conftest.py` as above. **Gate. Commit**
  `feat: the transport protocol and the loopback`.

## Task 5 — `sentences`, `Ledger`, `FrameWindow`

**Files:** create `src/rfnry_senses/voice.py`, `src/rfnry_senses/window.py`,
`tests/unit/test_voice.py`, `tests/unit/test_window.py`.

**Interfaces** — `voice.py`, complete (sentence cutting adapted from
`rfnry_speech_server/session/cascaded.py:_cut_sentences`):

```python
from __future__ import annotations

from rfnry_senses.contracts import AudioFrame


def sentences(text: str) -> list[str]:
    found: list[str] = []
    start = i = 0
    n = len(text)
    while i < n:
        if text[i] in ".!?":
            end = i + 1
            while end < n and text[end] in ".!?":
                end += 1
            if end < n and text[end].isspace():
                while end < n and text[end].isspace():
                    end += 1
                found.append(text[start:end])
                start = i = end
                continue
        i += 1
    if text[start:].strip():
        found.append(text[start:])
    return found


class Ledger:
    def __init__(self) -> None:
        self.sent_ms = 0.0
        self.started_at: float | None = None
        self._marks: list[tuple[str, float]] = []
        self._ahead: list[str] = []

    def sent(self, frame: AudioFrame, now: float) -> None:
        if self.started_at is None:
            self.started_at = now
        self.sent_ms += frame.duration_ms
        self._marks += [(text, self.sent_ms) for text in self._ahead]
        self._ahead = []

    def mark(self, text: str, *, ahead: bool = False) -> None:
        if ahead:
            self._ahead.append(text)
        else:
            self._marks.append((text, self.sent_ms))

    def heard(self, played_ms: float | None) -> str:
        if played_ms is None:
            return "".join([*(text for text, _ in self._marks), *self._ahead])
        return "".join(text for text, end in self._marks if end <= played_ms + 1)

    def estimate(self, now: float) -> float:
        if self.started_at is None:
            return 0.0
        return min(self.sent_ms, (now - self.started_at) * 1000)

    def remaining(self, now: float) -> float:
        if self.started_at is None:
            return 0.0
        return max(0.0, self.sent_ms / 1000 - (now - self.started_at))
```

`window.py`, complete (adapted from `rfnry_vision_server/session/frames.py`):

```python
from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from time import monotonic

from rfnry_senses.models import Image


class FrameWindow:
    def __init__(self, seconds: float, max_bytes: int) -> None:
        self.seconds = seconds
        self.max_bytes = max_bytes
        self._frames: deque[tuple[float, datetime, Image]] = deque()
        self._bytes = 0

    def append(self, image: Image) -> None:
        self._frames.append((monotonic(), datetime.now(UTC), image))
        self._bytes += len(image.data or b"")
        self._evict()

    def newest(self, count: int) -> list[tuple[datetime, Image]]:
        self._evict()
        return [(at, image) for _, at, image in list(self._frames)[-count:]]

    def _evict(self) -> None:
        now = monotonic()
        while self._frames and now - self._frames[0][0] > self.seconds:
            self._drop()
        while len(self._frames) > 1 and self._bytes > self.max_bytes:
            self._drop()

    def _drop(self) -> None:
        _, _, image = self._frames.popleft()
        self._bytes -= len(image.data or b"")
```

- [ ] **Step 1: failing tests** — `tests/unit/test_voice.py`:

```python
import pytest

from rfnry_senses.voice import Ledger, sentences

from .fakes import pcm


def test_text_is_cut_into_sentences_keeping_their_spacing() -> None:
    assert sentences("Your table is ready. Shall I order the usual?") == ["Your table is ready. ", "Shall I order the usual?"]
    assert sentences("Wait... what?! Fine") == ["Wait... ", "what?! ", "Fine"]
    assert sentences("v1.2 is out") == ["v1.2 is out"]


def test_heard_keeps_whole_sentences_played() -> None:
    ledger = Ledger()
    for sentence in ("One. ", "Two. "):
        ledger.sent(pcm(100), now=0.0)
        ledger.mark(sentence)
    assert ledger.heard(98) == ""
    assert ledger.heard(99) == ledger.heard(100) == "One. "
    assert ledger.heard(150) == "One. "
    assert ledger.heard(200) == ledger.heard(None) == "One. Two. "


def test_live_marks_end_with_the_audio_that_follows_them() -> None:
    ledger = Ledger()
    ledger.mark("Let me ", ahead=True)
    ledger.sent(pcm(100), now=0.0)
    ledger.mark("check. ", ahead=True)
    assert ledger.heard(100) == "Let me "
    assert ledger.heard(None) == "Let me check. "


def test_the_estimate_is_capped_by_what_was_sent() -> None:
    ledger = Ledger()
    assert ledger.estimate(5.0) == 0.0
    ledger.sent(pcm(300), now=10.0)
    assert ledger.estimate(10.15) == pytest.approx(150.0)
    assert ledger.estimate(99.0) == 300.0
    assert ledger.remaining(10.1) == pytest.approx(0.2)
```

  `tests/unit/test_window.py`:

```python
from rfnry_senses.models import Image
from rfnry_senses.window import FrameWindow

from .conftest import Clock


def jpeg(size: int) -> Image:
    return Image(media_type="image/jpeg", data=bytes(size))


def test_frames_leave_the_window_by_the_clock(clock: Clock) -> None:
    window = FrameWindow(10.0, 1_000_000)
    window.append(jpeg(10))
    clock.advance(6)
    window.append(jpeg(20))
    assert [len(image.data or b"") for _, image in window.newest(5)] == [10, 20]
    clock.advance(5)
    assert [len(image.data or b"") for _, image in window.newest(5)] == [20]


def test_the_byte_cap_keeps_the_newest_frame(clock: Clock) -> None:
    window = FrameWindow(10.0, 25)
    for size in (10, 10, 10):
        window.append(jpeg(size))
    assert len(window.newest(5)) == 2
    window.append(jpeg(100))
    assert [len(image.data or b"") for _, image in window.newest(5)] == [100]
```

  Until Task 6 creates `session.py`, the `clock` fixture patches only
  `rfnry_senses.window.monotonic`; Task 6 adds the `rfnry_senses.session.monotonic` line.
- [ ] **Step 2: implement.** **Gate. Commit** `feat: sentences, the ledger of what was heard, the
  frame window`.

## Task 6 — The session core, live mode

**Files:** create `src/rfnry_senses/session.py`, `tests/unit/test_session_live.py`.

**Interfaces:**

```python
class Session:
    scope: str
    person: Identity
    def __init__(self, transport: Transport, *, live: LiveModel | None = None, tools: ToolsLike | None = None,
                 instructions: str | None = None, transcription: TranscriptionModel | None = None,
                 speech: SpeechModel | None = None, turns: DecisionModel | None = None, video: bool = False,
                 window: float = 10.0, window_bytes: int = 8 * 1024 * 1024, turn_threshold: float = 0.5,
                 turn_timeout: float = 3.0, tool_timeout: float | None = 30.0) -> None
    async def __aenter__(self) -> Session
    async def __aexit__(self, *exc: object) -> None
    def events(self) -> Subscription
    async def say(self, text: str, *, triggered_by: Identity) -> Said
    async def prompt(self, text: str, *, triggered_by: Identity) -> Said
    async def inform(self, text: str, *, triggered_by: Identity) -> None
    async def close(self) -> None
```

(`interrupt`, `mute`, `unmute` arrive in Task 7, the cascaded branch of `say` and its streamed
`text: str | AsyncIterator[str]` in Task 8, `look` in Task 10.)

- [ ] **Step 1: failing tests** — `tests/unit/test_session_live.py`:

```python
import asyncio
from typing import Any

import pytest

from rfnry_senses.contracts import LiveDone, LiveFailed, LiveHeard, LiveToolCall
from rfnry_senses.errors import ConfigurationError, InputError, ModelError, TransportError
from rfnry_senses.events import Closed, Heard, Informed, Said, Silent, Speaking, ToolCalled
from rfnry_senses.models import Image, Text
from rfnry_senses.session import Session
from rfnry_senses.tools import Tool, Tools
from rfnry_senses.transport import Loopback

from .fakes import ANA, HOST, ScriptedLive, ScriptedTranscription, Tone, next_of, pcm, response, until


def session(link: Loopback, live: ScriptedLive, **kwargs: Any) -> Session:
    return Session(link, live=live, **kwargs)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"live": ScriptedLive(), "speech": Tone()}, "^session: a live model hears and speaks itself"),
        ({"transcription": ScriptedTranscription(), "turns": object(), "speech": Tone(), "window": 0}, "^session: window must be positive$"),
        ({"speech": Tone(), "turns": object()}, "^session: turns need a transcription model$"),
        ({"speech": Tone(), "tools": Tools()}, "^session: tools and instructions are for a live model$"),
        ({"live": ScriptedLive(), "turn_threshold": 0}, r"^session: turn_threshold must be in \(0, 1\]$"),
        ({"live": ScriptedLive(), "tool_timeout": 0}, "^session: tool_timeout must be positive or None$"),
    ],
)
def test_a_session_that_cannot_work_is_refused(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        Session(Loopback(ANA, "acme"), **kwargs)


async def test_the_live_model_is_opened_with_the_tools_and_hears_the_person() -> None:
    async def now(args: dict[str, Any]) -> str:
        return "12:00"

    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live, tools=Tools([Tool("now", "The time", body=now)]), instructions="Be brief.") as s:
        assert s.scope == "acme" and s.person == ANA
        await link.speak(pcm(20))
        await until(lambda: len(live.connection.audio) == 1)
    assert [spec.name for spec in live.opened["tools"]] == ["now"] and live.opened["instructions"] == "Be brief."
    assert link.controls[0] == {"type": "ready"} and live.connection.closed


async def test_the_person_is_heard_as_the_model_transcribes_them() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        live.connection.emit(LiveHeard(text=" "), LiveHeard(text="a table for two", final=True))
        heard = await next_of(events, Heard)
    assert heard.text == "a table for two" and heard.final and heard.speaker == ANA and heard.scope == "acme"


async def test_say_speaks_verbatim_and_returns_what_was_heard() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        saying = asyncio.create_task(s.say("For two, at eight.", triggered_by=HOST))
        await until(lambda: live.connection.calls == [("say", "For two, at eight.")])
        live.connection.emit(*response("For two, ", "at eight."))
        said = await saying
        kinds = [type(await anext(events)) for _ in range(3)]
    assert said.text == "For two, at eight." and said.heard == "For two, at eight."
    assert not said.interrupted and said.triggered_by == HOST
    assert kinds == [Speaking, Silent, Said] and len(link.audio) == 2
    assert [c for c in link.controls if c["type"] in ("speaking", "silent")] == [
        {"type": "speaking", "utterance": 1},
        {"type": "silent", "utterance": 1},
    ]


async def test_prompt_returns_the_models_own_words_and_inform_asks_for_no_reply() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        prompting = asyncio.create_task(s.prompt("Tell Ana her table is ready.", triggered_by=HOST))
        await until(lambda: bool(live.connection.calls))
        live.connection.emit(*response("Ana, ", "your table is ready."))
        said = await prompting
        await s.inform("The terrace closes at nine.", triggered_by=HOST)
        informed = await next_of(events, Informed)
    assert said.text == "Ana, your table is ready." and said.triggered_by == HOST
    assert live.connection.calls == [("text", "Tell Ana her table is ready.", True), ("text", "The terrace closes at nine.", False)]
    assert informed.triggered_by == HOST
    assert not [c for c in link.controls if c["type"] == "event" and c["event"]["kind"] == "informed"]


async def test_concurrent_say_and_prompt_keep_their_causes_in_order() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        first = asyncio.create_task(s.say("Hello.", triggered_by=HOST))
        second = asyncio.create_task(s.prompt("Greet Ana.", triggered_by=ANA))
        await until(lambda: len(live.connection.calls) == 2)
        live.connection.emit(*response("Hello."), *response("Hi ", "Ana."), *response("By the way."))
        said_first, said_second = await first, await second
        saids = [await next_of(events, Said) for _ in range(3)]
    assert live.connection.calls == [("say", "Hello."), ("text", "Greet Ana.", True)]
    assert said_first.triggered_by == HOST and said_second.triggered_by == ANA
    assert said_second.text == "Hi Ana."
    assert saids[2].text == "By the way." and saids[2].triggered_by is None


async def test_the_models_tool_calls_are_dispatched_and_answered() -> None:
    gate = asyncio.Event()

    async def terrace(args: dict[str, Any]) -> dict[str, bool]:
        return {"open": True}

    async def photo(args: dict[str, Any]) -> list[Any]:
        return [Image(media_type="image/png", data=b"png"), Text(text="the terrace")]

    async def slow(args: dict[str, Any]) -> str:
        await gate.wait()
        return "late"

    tools = Tools([Tool("terrace", "Open?", body=terrace), Tool("photo", "A photo", body=photo), Tool("slow", "Slow", body=slow)])
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live, tools=tools, tool_timeout=0.05) as s:
        events = s.events()
        live.connection.emit(
            LiveToolCall(call_id="c1", name="terrace"),
            LiveToolCall(call_id="c2", name="photo"),
            LiveToolCall(call_id="c3", name="ghost"),
            LiveToolCall(call_id="c4", name="slow"),
        )
        called = [await next_of(events, ToolCalled) for _ in range(4)]
    results = {call_id: (result, is_error) for call_id, result, is_error in live.connection.results}
    assert results["c1"] == ('{"open": true}', False)
    assert results["c2"][0] == [Image(media_type="image/png", data=b"png"), Text(text="the terrace")]
    assert results["c3"] == ("InputError: tools: unknown tool 'ghost'", True)
    assert results["c4"] == ("timed out after 0.05s", True)
    assert {c.name for c in called} == {"terrace", "photo", "ghost", "slow"}
    assert not [c for c in link.controls if c["type"] == "event" and c["event"]["kind"] == "tool_called"]


async def test_a_failing_model_closes_the_session_and_fails_its_waiters() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        prompting = asyncio.create_task(s.prompt("Hello?", triggered_by=HOST))
        await until(lambda: bool(live.connection.calls))
        live.connection.emit(LiveFailed(message="connection lost"))
        closed = await next_of(events, Closed)
        with pytest.raises(ModelError, match="^session: the live model failed: connection lost$"):
            await prompting
    assert closed.reason == "failed" and closed.message == "session: the live model failed: connection lost"
    assert link.closed


async def test_a_response_that_ends_before_it_starts_breaks_the_contract() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        live.connection.emit(LiveDone())
        closed = await next_of(events, Closed)
    assert closed.message == "session: a response ended before it started"


async def test_hanging_up_closes_the_session_and_its_verbs() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        await link.hang_up()
        closed = await next_of(events, Closed)
        with pytest.raises(TransportError, match="^session: the session is closed$"):
            await s.say("Hello.", triggered_by=HOST)
        late = [event async for event in s.events()]
    assert closed.reason == "hung_up" and late == []


async def test_every_subscription_sees_every_event() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        one, two = s.events(), s.events()
        live.connection.emit(LiveHeard(text="hi", final=True))
        assert (await next_of(one, Heard)).text == (await next_of(two, Heard)).text == "hi"
        with pytest.raises(InputError, match="^session: text is empty$"):
            await s.say("  ", triggered_by=HOST)
        with pytest.raises(ConfigurationError, match="^session: look needs video=True$"):
            await s.look(triggered_by=HOST)
```

  Note the last test calls `look`, which Task 10 adds; until then keep that `pytest.raises` block
  out and add it in Task 10.
- [ ] **Step 2: implement `session.py`** — complete for this task:

```python
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
from typing import Any

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
from rfnry_senses.errors import ConfigurationError, InputError, ModelError, SensesException, TransportError
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
        return _PARTS.validate_python([p.model_dump() if hasattr(p, "model_dump") else p for p in result])
    raise ModelError(f"session: a tool returned {type(result).__name__}; a tool returns text, a dict or parts")


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
        if live is not None and (transcription is not None or speech is not None or turns is not None):
            raise ConfigurationError("session: a live model hears and speaks itself; give it no transcription, speech or turn models")
        if live is None and (tools is not None or instructions is not None):
            raise ConfigurationError("session: tools and instructions are for a live model")
        if turns is not None and transcription is None:
            raise ConfigurationError("session: turns need a transcription model")
        for name, value in (("window", window), ("window_bytes", window_bytes), ("turn_timeout", turn_timeout)):
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
            specs = [ToolSpec.model_validate(d) for d in self._tools.definitions()] if self._tools is not None else []
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
                await self._transport.send_control({"type": "event", "event": event.model_dump(mode="json")})

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
                    await self._emit(Heard(scope=self.scope, text=event.text, final=event.final, speaker=self.person))
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
            played = await self._await_played(u, u.ledger.remaining(monotonic()) + PLAYBACK_SLACK, u.ledger.sent_ms)
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
        said = Said(scope=self.scope, text=u.text, heard=heard, interrupted=interrupted, triggered_by=u.triggered_by)
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
            result = await asyncio.wait_for(self._tools.dispatch(call.name, call.args), self._tool_timeout)
            output, is_error = as_content(result), False
        except TimeoutError:
            output, is_error = f"timed out after {self._tool_timeout:g}s", True
        except Exception as exc:
            output, is_error = f"{type(exc).__name__}: {exc}", True
        await self._connection.submit_result(call.call_id, output, is_error)
        await self._emit(ToolCalled(scope=self.scope, name=call.name, input=call.args, output=output, is_error=is_error))

    async def _close(self, reason: str, message: str | None, error: SensesException | None) -> None:
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
        await self._emit(Closed(scope=self.scope, reason=reason, message=message))  # type: ignore[arg-type]
        for queue in self._subscribers:
            queue.put_nowait(None)
        with contextlib.suppress(Exception):
            await self._transport.close()
```

  Type `reason` as `Literal["closed", "hung_up", "failed"]` in `_close`'s signature instead of the
  ignore shown above (rule 4: no ignore comments in the final code). Keep the `AudioFrame`,
  `Image` and `SpeechModel`/`TranscriptionModel`/`DecisionModel` imports: the later tasks use them.
- [ ] **Step 3: gate. Commit** `feat: the session — live mode, attribution, tools, events`.

## Task 7 — Cutting the voice: `interrupt`, barge-in in live mode, `mute`

**Files:** modify `src/rfnry_senses/session.py`; create `tests/unit/test_interrupt_live.py`.

**Interfaces:**

```python
async def interrupt(self, *, triggered_by: Identity) -> Interrupted | None
async def mute(self, *, triggered_by: Identity) -> None
async def unmute(self, *, triggered_by: Identity) -> None
```

- [ ] **Step 1: failing tests** — `tests/unit/test_interrupt_live.py`:

```python
import asyncio
from typing import Any

import pytest

from rfnry_senses import session as session_module
from rfnry_senses.contracts import LiveAudio, LiveDone, LiveInterrupted, LiveSaid, LiveStarted, LiveToolCall
from rfnry_senses.events import Interrupted, Muted, Said, ToolCalled
from rfnry_senses.session import Session
from rfnry_senses.tools import Tool, Tools
from rfnry_senses.transport import Loopback

from .conftest import Clock
from .fakes import ANA, HOST, ScriptedLive, next_of, pcm, response, until


async def test_the_person_cutting_in_truncates_to_what_they_heard() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme", played=lambda utterance, sent: 100)
    async with Session(link, live=live) as s:
        events = s.events()
        live.connection.emit(LiveStarted(), LiveSaid(text="Your table "), LiveAudio(frame=pcm(100, 24000)),
                             LiveSaid(text="is ready."), LiveAudio(frame=pcm(100, 24000)), LiveInterrupted())
        cut = await next_of(events, Interrupted)
        said = await next_of(events, Said)
    assert cut.heard == "Your table " and cut.triggered_by == ANA
    assert said.interrupted and said.heard == "Your table " and said.triggered_by is None
    assert live.connection.interrupts == [100]
    assert {"type": "stop", "utterance": 1} in link.controls


async def test_audio_after_the_cut_never_reaches_the_person() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with Session(link, live=live) as s:
        events = s.events()
        live.connection.emit(LiveStarted(), LiveAudio(frame=pcm(100, 24000)), LiveInterrupted(),
                             LiveAudio(frame=pcm(100, 24000)), LiveSaid(text="late"), LiveDone(), *response("Next."))
        await next_of(events, Interrupted)
        nxt = await next_of(events, Said)
        nxt = nxt if nxt.text == "Next." else await next_of(events, Said)
    assert len(link.audio) == 2 and nxt.text == "Next." and not nxt.interrupted


async def test_a_silent_client_is_estimated_from_the_clock(clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_module, "PLAYED_GRACE", 0.01)
    live, link = ScriptedLive(), Loopback(ANA, "acme", answer=False)
    async with Session(link, live=live) as s:
        events = s.events()
        live.connection.emit(LiveStarted(), LiveSaid(text="One. "), LiveAudio(frame=pcm(100, 24000)),
                             LiveSaid(text="Two. "), LiveAudio(frame=pcm(100, 24000)))
        await until(lambda: len(link.audio) == 2)
        clock.advance(0.15)
        live.connection.emit(LiveInterrupted())
        cut = await next_of(events, Interrupted)
    assert cut.heard == "One. " and live.connection.interrupts == [150]


async def test_a_silent_client_at_the_natural_end_heard_everything(clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_module, "PLAYBACK_SLACK", 0.01)
    live, link = ScriptedLive(), Loopback(ANA, "acme", answer=False)
    async with Session(link, live=live) as s:
        events = s.events()
        live.connection.emit(*response("One. ", "Two. "))
        await until(lambda: len(link.audio) == 2)
        clock.advance(10)
        said = await next_of(events, Said)
    assert said.heard == "One. Two. " and not said.interrupted


async def test_a_tool_call_made_by_an_interrupted_response_is_answered_once() -> None:
    gate = asyncio.Event()

    async def terrace(args: dict[str, Any]) -> dict[str, bool]:
        await gate.wait()
        return {"open": True}

    live, link = ScriptedLive(), Loopback(ANA, "acme", played=lambda utterance, sent: 100)
    async with Session(link, live=live, tools=Tools([Tool("terrace", "Open?", body=terrace)])) as s:
        events = s.events()
        live.connection.emit(LiveStarted(), LiveSaid(text="Let me check. "), LiveAudio(frame=pcm(100, 24000)),
                             LiveToolCall(call_id="c1", name="terrace"), LiveInterrupted())
        cut = await next_of(events, Interrupted)
        gate.set()
        await next_of(events, ToolCalled)
        live.connection.emit(LiveDone())
        await asyncio.sleep(0.01)
    assert live.connection.results == [("c1", '{"open": true}', False)]
    assert cut.heard == "Let me check. " and live.connection.interrupts == [100]


async def test_no_result_is_submitted_after_close() -> None:
    async def forever(args: dict[str, Any]) -> str:
        await asyncio.Event().wait()
        return "never"

    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with Session(link, live=live, tools=Tools([Tool("forever", "Never", body=forever)]), tool_timeout=None):
        live.connection.emit(LiveToolCall(call_id="c1", name="forever"))
        await asyncio.sleep(0.01)
    assert live.connection.results == []


async def test_interrupt_by_code_and_the_refusing_vendor() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme", played=lambda utterance, sent: 0)
    live.connection.refuse_interrupt = True
    async with Session(link, live=live) as s:
        assert await s.interrupt(triggered_by=HOST) is None
        live.connection.emit(LiveStarted(), LiveSaid(text="Hello. "), LiveAudio(frame=pcm(100, 24000)))
        await until(lambda: len(link.audio) == 1)
        cut = await s.interrupt(triggered_by=HOST)
    assert cut is not None and cut.heard == "" and cut.triggered_by == HOST


async def test_muting_cuts_the_voice_and_gates_what_follows() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme", played=lambda utterance, sent: 100)
    async with Session(link, live=live) as s:
        events = s.events()
        live.connection.emit(LiveStarted(), LiveSaid(text="Hello. "), LiveAudio(frame=pcm(100, 24000)))
        await until(lambda: len(link.audio) == 1)
        await s.mute(triggered_by=HOST)
        muted = await next_of(events, Muted)
        cut = await next_of(events, Interrupted)
        cut_said = await next_of(events, Said)
        live.connection.emit(LiveDone(), *response("Unheard."))
        await until(lambda: live.connection.interrupts == [100, 0])
        gated = await s.say("Hello?", triggered_by=HOST)
        await s.unmute(triggered_by=HOST)
        unmuted = await next_of(events, Muted)
    assert muted.muted and muted.triggered_by == HOST and cut.triggered_by == HOST
    assert cut_said.heard == "Hello. " and cut_said.interrupted
    assert gated.heard == "" and gated.interrupted and ("say", "Hello?") not in live.connection.calls
    assert not unmuted.muted and len(link.audio) == 1
```

- [ ] **Step 2: implement** — add to `session.py` (imports: `Interrupted`, `Muted`,
  `LiveInterrupted`):

```python
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
```

  In `_live_events`, before the `LiveFailed` branch:

```python
            elif isinstance(event, LiveInterrupted):
                await self._cut(self.person)
```

  At the end of `_response_started`, replacing its last line:

```python
        u = self._causes.popleft() if self._causes else Utterance(None)
        self._start(u)
        if self._muted:
            self._utterance = None
            await self._interrupt_model(0)
            await self._finish(u, "", interrupted=True)
```

  At the top of `say` and `prompt`, after building `u`:

```python
        if self._muted:
            await self._finish(u, "", interrupted=True)
            return await done
```

  (`done` is already resolved there; `await` returns the `Said`.)
- [ ] **Step 3: gate. Commit** `feat: interrupt, mute, and live barge-in cut to what was heard`.

## Task 8 — Cascaded mode: hearing, turns, speaking

**Files:** modify `src/rfnry_senses/session.py`, `src/rfnry_senses/voice.py`,
`tests/unit/test_voice.py`, `tests/unit/test_session_live.py`; create
`tests/unit/test_session_cascaded.py`.

**Interfaces:** no new public verb; `say` gains its cascaded branch and takes streamed text;
`TurnEnded` is emitted.

```python
async def say(self, text: str | AsyncIterator[str], *, triggered_by: Identity) -> Said
async def sentences_from(text: str | AsyncIterator[str]) -> AsyncGenerator[str]      # voice.py
```

- [ ] **Step 1: failing tests** — `tests/unit/test_session_cascaded.py`:

```python
import asyncio
from typing import Any

import pytest

from rfnry_senses.errors import ConfigurationError, ModelError
from rfnry_senses.events import Heard, TurnEnded
from rfnry_senses.session import Session
from rfnry_senses.transport import Loopback

from .fakes import ANA, HOST, Done, ScriptedTranscription, Tone, next_of, pcm, until


def cascaded(**kwargs: Any) -> tuple[Session, Loopback, ScriptedTranscription, Tone]:
    link, stt, tts = Loopback(ANA, "acme"), ScriptedTranscription(), Tone()
    return Session(link, transcription=stt, speech=tts, **kwargs), link, stt, tts


async def test_the_person_is_heard_and_their_turn_ends_with_the_utterance() -> None:
    s, link, stt, _ = cascaded()
    async with s:
        events = s.events()
        await link.speak(pcm(20))
        stt.hear("a table", final=False, end=False)
        stt.hear("A table for two.", "At eight.")
        heard = [await next_of(events, Heard) for _ in range(3)]
        turn = await next_of(events, TurnEnded)
        await until(lambda: len(stt.fed) == 1)
    assert [h.final for h in heard] == [False, True, True]
    assert turn.text == "A table for two. At eight." and turn.speaker == ANA and turn.probability is None


async def test_a_trailing_partial_belongs_to_the_turn() -> None:
    s, _, stt, _ = cascaded()
    async with s:
        events = s.events()
        stt.hear("book a", final=False)
        assert (await next_of(events, TurnEnded)).text == "book a"


async def test_a_decision_model_holds_a_turn_the_person_has_not_finished() -> None:
    dm = Done(0.2, 0.9)
    s, _, stt, _ = cascaded(turns=dm)
    async with s:
        events = s.events()
        stt.hear("I would like")
        await until(lambda: len(dm.states) == 1)
        stt.hear("a table for two.")
        turn = await next_of(events, TurnEnded)
    assert turn.text == "I would like a table for two." and turn.probability == 0.9
    assert dm.states == [{"said": "", "heard": "I would like"}, {"said": "", "heard": "I would like a table for two."}]


async def test_a_held_turn_ends_after_the_timeout_and_a_failed_check_ends_it_at_once() -> None:
    s, _, stt, _ = cascaded(turns=Done(0.2), turn_timeout=0.05)
    async with s:
        events = s.events()
        stt.hear("Hmm")
        assert (await next_of(events, TurnEnded)).probability == 0.2
        stt.hear("And also")
        turn = await next_of(events, TurnEnded)
    assert turn.text == "And also" and turn.probability is None


async def test_say_speaks_sentence_by_sentence_and_in_order() -> None:
    s, link, _, tts = cascaded()
    async with s:
        first = asyncio.create_task(s.say("One two. Three.", triggered_by=HOST))
        second = asyncio.create_task(s.say("Four.", triggered_by=ANA))
        said_first, said_second = await first, await second
    assert tts.spoken == ["One two. ", "Three.", "Four."]
    assert said_first.heard == "One two. Three." and said_first.triggered_by == HOST and not said_first.interrupted
    assert said_second.heard == "Four." and len(link.audio) == 4
    kinds = [(c["type"], c.get("utterance")) for c in link.controls if c["type"] in ("speaking", "silent")]
    assert kinds == [("speaking", 1), ("silent", 1), ("speaking", 2), ("silent", 2)]


async def test_a_failing_speech_model_fails_only_its_say() -> None:
    s, _, _, tts = cascaded()
    async with s:
        tts.fail = True
        with pytest.raises(ModelError, match="^session: the speech model failed: synthesis failed$"):
            await s.say("Hello.", triggered_by=HOST)
        tts.fail = False
        assert (await s.say("Hello again.", triggered_by=HOST)).heard == "Hello again."


async def test_each_model_is_optional() -> None:
    stt = ScriptedTranscription()
    async with Session(Loopback(ANA, "acme"), transcription=stt) as listener:
        with pytest.raises(ConfigurationError, match="^session: say needs a live model or a speech model$"):
            await listener.say("Hello.", triggered_by=HOST)
        with pytest.raises(ConfigurationError, match="^session: prompt needs a live model$"):
            await listener.prompt("Hello.", triggered_by=HOST)
        await stt.close()
        await asyncio.sleep(0.01)
        with pytest.raises(ConfigurationError, match="^session: interrupt needs a live model or a speech model$"):
            await listener.interrupt(triggered_by=HOST)
    async with Session(Loopback(ANA, "acme"), speech=Tone()) as announcer:
        said = await announcer.say("Doors close in one minute.", triggered_by=HOST)
    assert said.heard == "Doors close in one minute."


async def test_what_was_heard_last_reaches_the_turn_check() -> None:
    dm = Done(0.9)
    s, _, stt, _ = cascaded(turns=dm)
    async with s:
        events = s.events()
        await s.say("Shall I order the usual?", triggered_by=HOST)
        stt.hear("Yes please.")
        await next_of(events, TurnEnded)
    assert dm.states == [{"said": "Shall I order the usual?", "heard": "Yes please."}]


async def test_say_speaks_a_stream_as_its_sentences_complete() -> None:
    s, link, _, tts = cascaded()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def reply() -> AsyncIterator[str]:
        while (chunk := await queue.get()) is not None:
            yield chunk

    async with s:
        saying = asyncio.create_task(s.say(reply(), triggered_by=HOST))
        queue.put_nowait("One two")
        queue.put_nowait(". Thr")
        await until(lambda: tts.spoken == ["One two. "])
        queue.put_nowait("ee.")
        queue.put_nowait(None)
        said = await saying
    assert tts.spoken == ["One two. ", "Three."] and len(link.audio) == 3
    assert said.text == said.heard == "One two. Three." and not said.interrupted and said.triggered_by == HOST


async def test_an_empty_stream_says_nothing_and_a_failing_one_fails_only_its_say() -> None:
    async def empty() -> AsyncIterator[str]:
        nothing: tuple[str, ...] = ()
        for chunk in nothing:
            yield chunk

    async def broken() -> AsyncIterator[str]:
        yield "One. "
        raise RuntimeError("the writer failed")

    s, _, _, tts = cascaded()
    async with s:
        nothing = await s.say(empty(), triggered_by=HOST)
        with pytest.raises(RuntimeError, match="^the writer failed$"):
            await s.say(broken(), triggered_by=HOST)
        after = await s.say("Still here.", triggered_by=HOST)
    assert nothing.text == nothing.heard == "" and not nothing.interrupted
    assert after.heard == "Still here." and tts.spoken == ["Still here."]
```

  (`test_session_cascaded.py` also imports `AsyncIterator` from `collections.abc`.) Add to
  `tests/unit/test_voice.py` (imports `asyncio`, `AsyncIterator`, `sentences_from`):

```python
async def chunks(*parts: str) -> AsyncIterator[str]:
    for part in parts:
        yield part


async def test_a_stream_is_cut_into_sentences_in_order() -> None:
    pieces = sentences_from(chunks("Your table", " is ready. Sh", "all I order", " the usual? ", "Fine"))
    assert [s async for s in pieces] == ["Your table is ready. ", "Shall I order the usual? ", "Fine"]


async def test_a_sentence_is_ready_as_soon_as_text_follows_it() -> None:
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def feed() -> AsyncIterator[str]:
        while (chunk := await queue.get()) is not None:
            yield chunk

    pieces = sentences_from(feed())
    queue.put_nowait("One two. Th")
    assert await anext(pieces) == "One two. "
    queue.put_nowait("ree.")
    queue.put_nowait(None)
    assert [s async for s in pieces] == ["Three."]
```

  Add to `tests/unit/test_session_live.py` (imports `AsyncIterator`):

```python
async def test_a_live_say_joins_a_stream_and_sends_it_once() -> None:
    async def reply() -> AsyncIterator[str]:
        for chunk in ("For two, ", "at eight."):
            yield chunk

    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        saying = asyncio.create_task(s.say(reply(), triggered_by=HOST))
        await until(lambda: bool(live.connection.calls))
        live.connection.emit(*response("For two, ", "at eight."))
        said = await saying
    assert live.connection.calls == [("say", "For two, at eight.")]
    assert said.text == said.heard == "For two, at eight." and said.triggered_by == HOST
```

- [ ] **Step 2: implement** — in `voice.py`, after `sentences` (imports `AsyncGenerator`,
  `AsyncIterator`; it returns an `AsyncGenerator` because `_play` calls its `aclose()`):

```python
async def sentences_from(text: str | AsyncIterator[str]) -> AsyncGenerator[str]:
    if isinstance(text, str):
        for sentence in sentences(text):
            yield sentence
        return
    held = ""
    async for chunk in text:
        held += chunk
        *complete, held = sentences(held) or [""]
        for sentence in complete:
            yield sentence
    if held.strip():
        yield held
```

  In `session.py` add imports (`AsyncIterator`, `Transcript`, `UtteranceEnd`, `TurnEnded`,
  `Check`, `CheckAnswer`, `as_decided`, `sentences_from`), the field `source: str |
  AsyncIterator[str] = ""` on `Utterance`, and:

```python
DONE = Check(
    instructions="Has the person finished their turn, so that it is now the other side's turn to speak?",
    yes="They finished what they meant to say and expect an answer or a reaction",
    no="They paused mid-thought and are about to go on",
)
```

  In `__aenter__`, after the live branch:

```python
        if self._transcription is not None:
            self._stream = self._transcription.stream()
            self._spawn(self._transcripts())
        if self._speech is not None:
            self._spawn(self._speak())
```

  `say` becomes (Task 7's muted check kept):

```python
    async def say(self, text: str | AsyncIterator[str], *, triggered_by: Identity) -> Said:
        if isinstance(text, str):
            self._ready(text)
        else:
            self._check_open()
        done: asyncio.Future[Said] = asyncio.get_running_loop().create_future()
        u = Utterance(triggered_by, text if isinstance(text, str) else "", verbatim=True, done=done, source=text)
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
```

  `_hear` becomes:

```python
    async def _hear(self, frame: AudioFrame) -> None:
        if self._connection is not None:
            await self._connection.send_audio(frame)
        elif self._stream is not None:
            await self._stream.feed(frame)
```

  New methods:

```python
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
                await self._emit(Heard(scope=self.scope, text=event.text, final=event.final, speaker=self.person))
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
        await self._emit(TurnEnded(scope=self.scope, text=text, speaker=self.person, probability=probability))

    async def _done(self, text: str) -> float | None:
        assert self._turns is not None
        try:
            decided = as_decided(await self._turns.decide({"said": self._last_heard, "heard": text}, {"done": DONE}))
        except Exception as exc:
            log.warning("session: the turn check failed; the turn ends: %s", exc)
            return None
        answer = decided.answers.get("done")
        if not isinstance(answer, CheckAnswer):
            log.warning("session: the decision model did not answer 'done' with a check; the turn ends")
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
```

  The outer `except` is the consumer's stream failing: that `say` raises the stream's own error.
  A barge-in cancels `_playing` wherever it waits — on the speech model or inside the consumer's
  stream — and the `finally` closes the stream; a queued stream that is never spoken is never
  read.

- [ ] **Step 3: gate. Commit** `feat: cascaded sessions — transcripts, turns, speech, streamed say`.

## Task 9 — Cascaded barge-in

**Files:** modify `src/rfnry_senses/session.py`; create `tests/unit/test_barge_in.py`.

- [ ] **Step 1: failing tests** — `tests/unit/test_barge_in.py`:

```python
import asyncio

from rfnry_senses.events import Heard, Interrupted
from rfnry_senses.session import Session
from rfnry_senses.transport import Loopback

from .fakes import ANA, HOST, ScriptedTranscription, Tone, next_of, until


async def test_the_person_talking_over_the_voice_cuts_it_and_the_queue() -> None:
    link, stt, tts = Loopback(ANA, "acme"), ScriptedTranscription(), Tone()
    tts.held = "Three four."
    async with Session(link, transcription=stt, speech=tts) as s:
        events = s.events()
        first = asyncio.create_task(s.say("One two. Three four.", triggered_by=HOST))
        queued = asyncio.create_task(s.say("Five.", triggered_by=HOST))
        await until(lambda: len(link.audio) == 2 and "Three four." in tts.spoken)
        stt.hear("wait", final=False, end=False)
        cut = await next_of(events, Interrupted)
        heard = await next_of(events, Heard)
        said_first, said_queued = await first, await queued
    assert cut.heard == "One two. " and cut.triggered_by == ANA and heard.text == "wait"
    assert said_first.interrupted and said_first.heard == "One two. "
    assert said_queued.interrupted and said_queued.heard == "" and "Five." not in tts.spoken
    assert {"type": "stop", "utterance": 1} in link.controls


async def test_a_sentence_finished_after_barge_in_is_never_sent() -> None:
    link, stt, tts = Loopback(ANA, "acme"), ScriptedTranscription(), Tone()
    tts.held = "Three four."
    async with Session(link, transcription=stt, speech=tts) as s:
        events = s.events()
        saying = asyncio.create_task(s.say("One two. Three four.", triggered_by=HOST))
        await until(lambda: "Three four." in tts.spoken)
        stt.hear("stop", end=False)
        await next_of(events, Interrupted)
        tts.release.set()
        said = await saying
        await asyncio.sleep(0.02)
    assert len(link.audio) == 2 and said.heard == "One two. "
    assert all(c.get("utterance") == 1 for c in link.controls if c["type"] in ("speaking", "stop"))


async def test_the_voice_speaks_again_after_a_barge_in() -> None:
    link, stt, tts = Loopback(ANA, "acme"), ScriptedTranscription(), Tone()
    tts.held = "Three four."
    async with Session(link, transcription=stt, speech=tts) as s:
        events = s.events()
        asyncio.create_task(s.say("One two. Three four.", triggered_by=HOST))
        await until(lambda: "Three four." in tts.spoken)
        stt.hear("sorry")
        await next_of(events, Interrupted)
        said = await s.say("Go ahead.", triggered_by=HOST)
    assert said.heard == "Go ahead." and not said.interrupted
    assert {"type": "speaking", "utterance": 2} in link.controls


async def test_a_barge_in_mid_stream_stops_reading_the_stream() -> None:
    link, stt, tts = Loopback(ANA, "acme"), ScriptedTranscription(), Tone()
    queue: asyncio.Queue[str] = asyncio.Queue()
    pulled: list[str] = []
    closed = asyncio.Event()

    async def reply() -> AsyncIterator[str]:
        try:
            while True:
                chunk = await queue.get()
                pulled.append(chunk)
                yield chunk
        finally:
            closed.set()

    async with Session(link, transcription=stt, speech=tts) as s:
        events = s.events()
        saying = asyncio.create_task(s.say(reply(), triggered_by=HOST))
        queue.put_nowait("One two. ")
        queue.put_nowait("Three")
        await until(lambda: len(link.audio) == 2)
        stt.hear("wait", final=False, end=False)
        cut = await next_of(events, Interrupted)
        said = await saying
        queue.put_nowait(" four.")
        await asyncio.wait_for(closed.wait(), 1.0)
    assert cut.heard == said.heard == said.text == "One two. " and said.interrupted
    assert pulled == ["One two. ", "Three"] and tts.spoken == ["One two. "]
```

  (Imports `AsyncIterator` from `collections.abc`. The stream test needs no code of its own:
  Task 8's `_play` stops reading and closes the stream when the cut cancels it.)
- [ ] **Step 2: implement** — in `_transcripts`, before `await self._emit(Heard(...))`:

```python
                if self._utterance is not None or self._waiting:
                    await self._cut(self.person)
                    await self._drop_waiting()
```

- [ ] **Step 3: gate. Commit** `feat: cascaded barge-in — the voice stops for the person`.

## Task 10 — Seeing: the window, `look`, frames to a live model

**Files:** modify `src/rfnry_senses/session.py`, `tests/unit/test_session_live.py` (add the `look`
assertion held back in Task 6); create `tests/unit/test_seeing.py`.

**Interfaces:**

```python
async def look(self, frames: int = 1, *, triggered_by: Identity) -> list[Part]
```

- [ ] **Step 1: failing tests** — `tests/unit/test_seeing.py`:

```python
import asyncio
import re

import pytest

from rfnry_senses.errors import ConfigurationError, InputError, NotFoundError
from rfnry_senses.events import Looked
from rfnry_senses.models import Image, Text
from rfnry_senses.session import Session
from rfnry_senses.transport import Loopback

from .conftest import Clock
from .fakes import ANA, HOST, JPEG, ScriptedConnection, ScriptedLive, next_of, until


async def test_look_returns_the_newest_frames_as_parts(clock: Clock) -> None:
    link = Loopback(ANA, "acme")
    async with Session(link, video=True) as s:
        events = s.events()
        for n in range(3):
            await link.show(JPEG + bytes([n]))
            clock.advance(1)
        await asyncio.sleep(0.01)
        parts = await s.look(frames=2, triggered_by=HOST)
        looked = await next_of(events, Looked)
    assert [type(p) for p in parts] == [Image, Text, Image, Text]
    assert isinstance(parts[0], Image) and parts[0].data == JPEG + b"\x01" and parts[0].media_type == "image/jpeg"
    assert isinstance(parts[1], Text) and re.fullmatch(r"frame at \d\d:\d\d:\d\d\.\d{3}", parts[1].text)
    assert looked.frames == 2 and looked.triggered_by == HOST
    assert any(c["type"] == "event" and c["event"]["kind"] == "looked" for c in link.controls)


async def test_a_stalled_camera_leaves_an_empty_window(clock: Clock) -> None:
    link = Loopback(ANA, "acme")
    async with Session(link, video=True) as s:
        await link.show(JPEG)
        await asyncio.sleep(0.01)
        assert len(await s.look(triggered_by=HOST)) == 2
        clock.advance(10.5)
        with pytest.raises(NotFoundError, match="^session: no frame in the last 10 s$"):
            await s.look(triggered_by=HOST)
        with pytest.raises(InputError, match="^session: frames must be at least 1$"):
            await s.look(frames=0, triggered_by=HOST)


async def test_without_video_frames_are_dropped_and_look_is_refused() -> None:
    link = Loopback(ANA, "acme")
    async with Session(link) as s:
        await link.show(JPEG)
        with pytest.raises(ConfigurationError, match="^session: look needs video=True$"):
            await s.look(triggered_by=HOST)


class Slow(ScriptedConnection):
    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def send_image(self, image: Image) -> None:
        self.images.append(image)
        await self.gate.wait()


async def test_a_live_model_gets_the_newest_frame_when_it_falls_behind() -> None:
    live, link, slow = ScriptedLive(), Loopback(ANA, "acme"), Slow()
    live.connection = slow
    async with Session(link, live=live, video=True):
        await link.show(JPEG + b"\x00")
        await until(lambda: len(slow.images) == 1)
        for n in range(1, 4):
            await link.show(JPEG + bytes([n]))
        await asyncio.sleep(0.01)
        slow.gate.set()
        await until(lambda: len(slow.images) == 2)
    assert [i.data for i in slow.images] == [JPEG + b"\x00", JPEG + b"\x03"]
```

  `Session(link)` with no model and no video is legal and useless; the test only proves frames are
  dropped. In the last test, frame 0 is sent alone first: the inbound loop drains a ready queue
  without yielding, so frames sent together would all land before the forwarder runs.
- [ ] **Step 2: implement** — imports `Looked`, `Text`, `NotFoundError`. In `__aenter__`, after the
  live branch's `_spawn(self._live_events())`:

```python
            if self._window is not None:
                self._spawn(self._forward_frames())
```

  Replace `_see` and add:

```python
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
```

- [ ] **Step 3: gate. Commit** `feat: seeing — the frame window, look, frames to a live model`.

## Task 11 — `SensesTools`

**Files:** create `src/rfnry_senses/senses_tools.py`, `tests/unit/test_senses_tools.py`.

**Interfaces:**

```python
class SensesTools(Tools):
    def __init__(self, session: Session, *, triggered_by: Identity) -> None
```

- [ ] **Step 1: failing tests** — `tests/unit/test_senses_tools.py`:

```python
import asyncio

import pytest

from rfnry_senses.errors import InputError, NotFoundError
from rfnry_senses.models import Image, Text
from rfnry_senses.senses_tools import SensesTools
from rfnry_senses.session import Session
from rfnry_senses.transport import Loopback

from .fakes import ANA, HOST, JPEG, ScriptedLive, ScriptedTranscription, Tone, response, until


def names(tools: SensesTools) -> list[str]:
    return [d["name"] for d in tools.definitions()]


async def test_the_tools_offered_follow_what_the_session_can_do() -> None:
    async with Session(Loopback(ANA, "acme"), live=ScriptedLive(), video=True) as live:
        assert names(SensesTools(live, triggered_by=HOST)) == ["look", "say", "prompt", "inform", "interrupt"]
    async with Session(Loopback(ANA, "acme"), transcription=ScriptedTranscription(), speech=Tone()) as cascaded:
        assert names(SensesTools(cascaded, triggered_by=HOST)) == ["say", "interrupt"]
    async with Session(Loopback(ANA, "acme"), video=True) as camera:
        assert names(SensesTools(camera, triggered_by=HOST)) == ["look"]


async def test_the_tools_act_for_their_cause() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with Session(link, live=live, video=True) as s:
        tools = SensesTools(s, triggered_by=HOST)
        await link.show(JPEG)
        await asyncio.sleep(0.01)
        seen = await tools.dispatch("look", {})
        saying = asyncio.create_task(tools.dispatch("say", {"text": "Hello."}))
        await until(lambda: bool(live.connection.calls))
        live.connection.emit(*response("Hello."))
        said = await saying
        prompting = asyncio.create_task(tools.dispatch("prompt", {"text": "Greet Ana."}))
        await until(lambda: len(live.connection.calls) == 2)
        live.connection.emit(*response("Hi Ana."))
        prompted = await prompting
        informed = await tools.dispatch("inform", {"text": "Ana is vegetarian."})
        cut = await tools.dispatch("interrupt", {})
        with pytest.raises(InputError, match="^tools: invalid arguments for say"):
            await tools.dispatch("say", {})
    assert isinstance(seen, list) and isinstance(seen[0], Image) and isinstance(seen[1], Text)
    assert said == {"heard": "Hello.", "interrupted": False}
    assert prompted == {"said": "Hi Ana.", "heard": "Hi Ana.", "interrupted": False}
    assert informed == "informed" and cut == {"heard": ""}


async def test_a_failing_tool_raises_the_librarys_error() -> None:
    async with Session(Loopback(ANA, "acme"), video=True) as s:
        with pytest.raises(NotFoundError, match="^session: no frame in the last 10 s$"):
            await SensesTools(s, triggered_by=HOST).dispatch("look", {"frames": 2})
```

- [ ] **Step 2: implement** `senses_tools.py`, complete:

```python
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from rfnry_senses.models import Identity, Part
from rfnry_senses.session import Session
from rfnry_senses.tools import Tool, Tools


class LookArgs(BaseModel):
    frames: int = Field(default=1, ge=1, le=10, description="How many of the newest frames to see")


class TextArgs(BaseModel):
    text: str = Field(description="The words")


class NoArgs(BaseModel):
    pass


class SensesTools(Tools):
    def __init__(self, session: Session, *, triggered_by: Identity) -> None:
        self.session = session
        self.triggered_by = triggered_by
        tools: list[Tool] = []
        if session._window is not None:
            tools.append(Tool("look", "See the camera: the newest frames, oldest first, each followed by when it was taken.", body=self._look, args=LookArgs))
        if session._voice:
            tools.append(Tool("say", "Speak these exact words to the person. Returns what they heard before any interruption.", body=self._say, args=TextArgs))
        if session._live is not None:
            tools.append(Tool("prompt", "Have the live voice say this, in its own words, now. Returns what it said and what was heard.", body=self._prompt, args=TextArgs))
            tools.append(Tool("inform", "Tell the live voice something it should know, without it speaking.", body=self._inform, args=TextArgs))
        if session._voice:
            tools.append(Tool("interrupt", "Stop speaking now. Returns what the person heard.", body=self._interrupt, args=NoArgs))
        super().__init__(tools)

    async def _look(self, args: dict[str, Any]) -> list[Part]:
        return await self.session.look(args.get("frames", 1), triggered_by=self.triggered_by)

    async def _say(self, args: dict[str, Any]) -> dict[str, Any]:
        said = await self.session.say(args["text"], triggered_by=self.triggered_by)
        return {"heard": said.heard, "interrupted": said.interrupted}

    async def _prompt(self, args: dict[str, Any]) -> dict[str, Any]:
        said = await self.session.prompt(args["text"], triggered_by=self.triggered_by)
        return {"said": said.text, "heard": said.heard, "interrupted": said.interrupted}

    async def _inform(self, args: dict[str, Any]) -> str:
        await self.session.inform(args["text"], triggered_by=self.triggered_by)
        return "informed"

    async def _interrupt(self, args: dict[str, Any]) -> dict[str, Any]:
        cut = await self.session.interrupt(triggered_by=self.triggered_by)
        return {"heard": cut.heard if cut is not None else ""}
```

  The tool descriptions are the model-facing contract; keep them as written unless the docs
  change. The `say` tool passes `args["text"]`, a string: a model's tool arguments are text, and a
  streamed `say` is for code (decision 10).
- [ ] **Step 3: gate. Commit** `feat: SensesTools — the room, for a text mind`.

## Task 12 — The wire: binary frames, control frames, golden fixtures

**Files:** create `src/rfnry_senses/wire.py`, `tests/unit/test_wire.py`, and (generated by the
test) `fixtures/wire/*.json`, `fixtures/wire/*.hex` at the repository root.

**Interfaces** — `wire.py`, complete (fresh; the old ws-pcm hid JSON+base64 inside the channel):

```python
from __future__ import annotations

import json
import struct
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from rfnry_senses.contracts import AudioFrame
from rfnry_senses.errors import InputError
from rfnry_senses.models import Image
from rfnry_senses.transport import Played

PROTOCOL = 1
AUDIO = 1
IMAGE = 2
_AUDIO = struct.Struct("<BBII")


class Hello(BaseModel):
    type: Literal["hello"]
    protocol: int


class _Played(BaseModel):
    type: Literal["played"]
    utterance: int = Field(ge=0)
    ms: int = Field(ge=0)


def encode_audio(frame: AudioFrame, utterance: int) -> bytes:
    return _AUDIO.pack(AUDIO, frame.channels, frame.sample_rate, utterance) + frame.data


def encode_image(jpeg: bytes) -> bytes:
    return bytes([IMAGE]) + jpeg


def decode(data: bytes) -> AudioFrame | Image:
    if not data:
        raise InputError("wire: an empty binary frame")
    if data[0] == AUDIO:
        if len(data) < _AUDIO.size:
            raise InputError("wire: an audio frame shorter than its header")
        _, channels, sample_rate, _ = _AUDIO.unpack_from(data)
        return AudioFrame(data=data[_AUDIO.size :], sample_rate=sample_rate, channels=channels)
    if data[0] == IMAGE:
        if len(data) == 1:
            raise InputError("wire: an image frame with no image")
        return Image(media_type="image/jpeg", data=data[1:])
    raise InputError(f"wire: unknown binary frame kind {data[0]}")


def parse_hello(text: str) -> Hello:
    try:
        return Hello.model_validate_json(text)
    except ValidationError as exc:
        raise InputError(f"wire: the first frame must be a hello: {exc.errors(include_url=False, include_input=False)}") from exc


def parse_played(text: str) -> Played:
    try:
        played = _Played.model_validate_json(text)
    except ValidationError as exc:
        raise InputError(f"wire: not a played frame: {exc.errors(include_url=False, include_input=False)}") from exc
    return Played(utterance=played.utterance, ms=played.ms)


def control(frame: dict[str, Any]) -> str:
    return json.dumps(frame, separators=(",", ":"))
```

  The frame layout, for both clients: audio `[0x01][channels u8][sample_rate u32 LE][utterance u32
  LE][PCM16 LE…]` (header 10 bytes; the client sends utterance 0); camera `[0x02][JPEG…]`.

- [ ] **Step 1: failing tests** — `tests/unit/test_wire.py`:

```python
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from rfnry_senses.contracts import AudioFrame
from rfnry_senses.errors import InputError
from rfnry_senses.events import Closed, Heard, Interrupted, Looked, Muted, Said, Silent, Speaking, TurnEnded
from rfnry_senses.models import Image
from rfnry_senses.transport import Played
from rfnry_senses.wire import decode, encode_audio, encode_image, parse_hello, parse_played

from .fakes import ANA, HOST

FIXTURES = Path(__file__).parents[4] / "fixtures" / "wire"
AT = datetime(2026, 9, 24, 12, 3, 4, 200000, tzinfo=UTC)


def controls() -> dict[str, dict[str, Any]]:
    events = {
        "heard": Heard(scope="acme", at=AT, text="a table for two", final=True, speaker=ANA),
        "turn_ended": TurnEnded(scope="acme", at=AT, text="A table for two.", speaker=ANA, probability=0.93),
        "said": Said(scope="acme", at=AT, text="Your table is ready. Shall I order the usual?", heard="Your table is ready. ", interrupted=True, triggered_by=HOST),
        "interrupted": Interrupted(scope="acme", at=AT, heard="Your table is ready. ", triggered_by=ANA),
        "speaking": Speaking(scope="acme", at=AT),
        "silent": Silent(scope="acme", at=AT),
        "looked": Looked(scope="acme", at=AT, frames=1, triggered_by=HOST),
        "muted": Muted(scope="acme", at=AT, muted=True, triggered_by=HOST),
        "closed": Closed(scope="acme", at=AT, reason="hung_up"),
    }
    return {
        "hello": {"type": "hello", "protocol": 1},
        "ready": {"type": "ready"},
        "played": {"type": "played", "utterance": 3, "ms": 1200},
        "speaking": {"type": "speaking", "utterance": 3},
        "silent": {"type": "silent", "utterance": 3},
        "stop": {"type": "stop", "utterance": 3},
        "error": {"type": "error", "message": "transport: protocol 2 is not supported; this server speaks 1"},
        **{f"event-{kind}": {"type": "event", "event": event.model_dump(mode="json")} for kind, event in events.items()},
    }


BINARY = {
    "audio": encode_audio(AudioFrame(data=b"\x00\x01\xff\x7f", sample_rate=16000), 3),
    "image": encode_image(b"\xff\xd8\xff\xd9"),
}


def test_the_golden_fixtures_are_what_the_code_writes() -> None:
    if os.environ.get("RFNRY_WRITE_FIXTURES") == "1":
        FIXTURES.mkdir(parents=True, exist_ok=True)
        for name, frame in controls().items():
            (FIXTURES / f"{name}.json").write_text(json.dumps(frame, indent=2) + "\n", encoding="utf-8")
        for name, data in BINARY.items():
            (FIXTURES / f"{name}.hex").write_text(data.hex() + "\n", encoding="utf-8")
    for name, frame in controls().items():
        assert json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8")) == frame, name
    for name, data in BINARY.items():
        assert (FIXTURES / f"{name}.hex").read_text(encoding="utf-8").strip() == data.hex(), name
    assert BINARY["audio"].hex() == "010180" + "3e0000" + "03000000" + "0001ff7f"


def test_frames_decode_to_what_was_encoded() -> None:
    frame = decode(BINARY["audio"])
    assert isinstance(frame, AudioFrame) and frame.sample_rate == 16000 and frame.data == b"\x00\x01\xff\x7f"
    image = decode(BINARY["image"])
    assert isinstance(image, Image) and image.data == b"\xff\xd8\xff\xd9"
    assert parse_hello(json.dumps(controls()["hello"])).protocol == 1
    assert parse_played(json.dumps(controls()["played"])) == Played(utterance=3, ms=1200)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "^wire: an empty binary frame$"),
        (b"\x01\x01", "^wire: an audio frame shorter than its header$"),
        (b"\x02", "^wire: an image frame with no image$"),
        (b"\x09abc", "^wire: unknown binary frame kind 9$"),
    ],
)
def test_bad_binary_frames_are_refused(data: bytes, message: str) -> None:
    with pytest.raises(InputError, match=message):
        decode(data)


def test_bad_control_frames_are_refused() -> None:
    with pytest.raises(InputError, match="^wire: the first frame must be a hello"):
        parse_hello('{"type": "played"}')
    with pytest.raises(InputError, match="^wire: not a played frame"):
        parse_played('{"type": "played", "utterance": 1}')
```

- [ ] **Step 2: implement** `wire.py`; run once with `RFNRY_WRITE_FIXTURES=1 uv run pytest
  tests/unit/test_wire.py`, read every written file, then run without the variable.
- [ ] **Step 3: gate. Commit** `feat: the wire — frames, controls, golden fixtures`.

## Task 13 — The WebSocket transport and `serve`

**Files:** create `src/rfnry_senses/websocket.py`, `tests/unit/test_websocket.py`,
`tests/integration/socket_adapter.py`, `tests/integration/test_websocket.py`.

**Interfaces:**

```python
class Connection(Protocol):
    async def receive(self) -> str | bytes: ...
    async def send_text(self, data: str) -> None: ...
    async def send_bytes(self, data: bytes) -> None: ...
    async def close(self) -> None: ...

async def serve(connection: Connection, *, identity: Identity, scope: str) -> WebSocketTransport | None

class WebSocketTransport:            # a Transport
    identity: Identity
    scope: str
```

- [ ] **Step 1: failing unit tests** — `tests/unit/test_websocket.py`:

```python
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
        (json.dumps({"type": "hello", "protocol": 2}), "transport: protocol 2 is not supported; this server speaks 1"),
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
    assert all(isinstance(a, AudioFrame) for a in audio) and len(audio) == 2 and said.heard == "Hi there."
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
    assert isinstance(socket.sent[0], bytes) and json.loads(socket.sent[1]) == {"type": "stop", "utterance": 1}
```

  (`caplog` needs the `rfnry_senses` logger to propagate, which it does by default. `Socket(None)`
  puts the disconnect first: `receive` raises, as a real socket does when the client leaves.)
  `serve` takes the person and scope from the consumer (decision 11); no header or cookie reaches
  senses, and none should be added to `Connection`. There is no hello deadline: the socket was
  authenticated on its upgrade, and a client that leaves makes `receive` raise.
- [ ] **Step 2: implement** `websocket.py`, complete:

```python
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from rfnry_senses.contracts import AudioFrame
from rfnry_senses.errors import InputError
from rfnry_senses.models import Identity, Image
from rfnry_senses.transport import Played
from rfnry_senses.wire import PROTOCOL, control, decode, encode_audio, parse_hello, parse_played

log = logging.getLogger("rfnry_senses")


@runtime_checkable
class Connection(Protocol):
    async def receive(self) -> str | bytes: ...
    async def send_text(self, data: str) -> None: ...
    async def send_bytes(self, data: bytes) -> None: ...
    async def close(self) -> None: ...


async def serve(connection: Connection, *, identity: Identity, scope: str) -> WebSocketTransport | None:
    if not scope:
        raise InputError("transport: scope is empty")
    try:
        first = await connection.receive()
        if not isinstance(first, str):
            raise InputError("wire: the first frame must be a hello")
        check_hello(first)
    except InputError as exc:
        await _refuse(connection, str(exc))
        return None
    except Exception:
        return None
    return WebSocketTransport(connection, identity, scope)


def check_hello(text: str) -> None:
    hello = parse_hello(text)
    if hello.protocol != PROTOCOL:
        raise InputError(f"transport: protocol {hello.protocol} is not supported; this server speaks {PROTOCOL}")


async def _refuse(connection: Connection, message: str) -> None:
    with contextlib.suppress(Exception):
        await connection.send_text(control({"type": "error", "message": message}))
    with contextlib.suppress(Exception):
        await connection.close()


class WebSocketTransport:
    def __init__(self, connection: Connection, identity: Identity, scope: str) -> None:
        self.identity = identity
        self.scope = scope
        self._connection = connection
        self._pending: deque[str | bytes] = deque()
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._writer: asyncio.Task[None] | None = None
        self._closed = False

    async def inbound(self) -> AsyncIterator[AudioFrame | Image | Played]:
        while not self._closed:
            try:
                message = await self._connection.receive()
            except Exception:
                return
            try:
                item = decode(message) if isinstance(message, bytes) else parse_played(message)
            except InputError as exc:
                log.warning("transport: dropped a frame: %s", exc)
                continue
            yield item

    async def send_audio(self, frame: AudioFrame, utterance: int) -> None:
        self._put(encode_audio(frame, utterance))

    async def send_control(self, control_frame: dict[str, Any]) -> None:
        self._put(control(control_frame))

    def clear_audio(self) -> None:
        self._pending = deque(item for item in self._pending if isinstance(item, str))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._idle.wait(), 1.0)
        if self._writer is not None:
            self._writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._writer
        with contextlib.suppress(Exception):
            await self._connection.close()

    def _put(self, item: str | bytes) -> None:
        if self._closed:
            return
        self._pending.append(item)
        self._idle.clear()
        self._wake.set()
        if self._writer is None:
            self._writer = asyncio.create_task(self._write())

    async def _write(self) -> None:
        while True:
            while not self._pending:
                self._idle.set()
                self._wake.clear()
                await self._wake.wait()
            item = self._pending.popleft()
            try:
                if isinstance(item, bytes):
                    await self._connection.send_bytes(item)
                else:
                    await self._connection.send_text(item)
            except Exception:
                self._pending.clear()
                self._idle.set()
                return
```

  Note the session's `_close` emits `Closed` (a control) before closing the transport, and
  `close()` waits up to a second for the queue to drain, so the client sees why it ended.
- [ ] **Step 3: integration** — `tests/integration/socket_adapter.py` holds the docs' `Socket`
  class verbatim, with its imports (`from fastapi import WebSocket, WebSocketDisconnect`);
  Task 15's docs test asserts the two stay identical. `tests/integration/test_websocket.py`
  (`pytestmark = pytest.mark.integration`):
  1. a `websockets` server (`websockets.asyncio.server.serve` on `127.0.0.1`, port 0) whose handler
     adapts the connection (`receive = recv`, `send_text = send_bytes = send`, `close`), calls
     `serve`, and runs a cascaded `Session` over `ScriptedTranscription`/`Tone`; a
     `websockets.asyncio.client.connect` client sends `hello`, reads `ready`, sends one audio
     frame, receives the `heard` event after the test feeds the transcription, receives audio
     frames with utterance 1 after `say`, answers `silent` with `played`, and sees `closed` after
     it hangs up;
  2. the same through FastAPI with the docs' adapter (the transcription fake hears when it is fed,
     because the test client runs the app on another thread's loop): an app whose route is the docs' Getting
     started endpoint with `stt`/`tts`/`engine` replaced by fakes (an `engine` whose `run` returns
     an object with `reply="For two."`) and `signed_in` a dependency that reads an `authorization`
     header (403 without it — the consumer's own check, shown refusing the upgrade), driven by
     `fastapi.testclient.TestClient` — `websocket_connect(..., headers=...)`,
     `websocket_connect`, `send_text(hello)`, `receive_json()` → `ready`, `send_bytes(audio)`,
     and after the scripted turn, binary audio frames and a `said` event with `heard == "For
     two."`.
- [ ] **Step 4: gate** plus `uv run poe test:integration`. **Commit** `feat: the WebSocket
  transport and serve`.

## Task 14 — The WebRTC transport (extra)

**Files:** create `src/rfnry_senses/webrtc.py`, `tests/integration/test_webrtc.py`.

**Interfaces:**

```python
async def accept_offer(sdp: str, *, identity: Identity, scope: str, sample_rate: int = 16000,
                       ice_servers: list[dict[str, Any]] | None = None) -> WebRTCTransport

class WebRTCTransport:               # a Transport
    identity: Identity
    scope: str
    answer: str
```

- [ ] **Step 1: failing test** — `tests/integration/test_webrtc.py` (`pytestmark =
  pytest.mark.webrtc`; `pytest.importorskip("aiortc")`): an in-process client
  `RTCPeerConnection` adds a silence audio track (a `MediaStreamTrack` subclass returning 20 ms
  48 kHz s16 mono `av.AudioFrame`s, paced with `asyncio.sleep(0.02)`, pts advancing by 960) and
  aiortc's `VideoStreamTrack`, creates data channel `"senses"`, sends `{"type": "hello",
  "protocol": 1}` when it opens, records every JSON message it receives and answers `stop`/`silent` with `{"type": "played", "utterance": n, "ms": 0}` /
  the full length, and collects frames from the answered audio track. Then:

```python
    link = await accept_offer(client.localDescription.sdp, identity=ANA, scope="acme")
    assert link.scope == "acme"
    await client.setRemoteDescription(RTCSessionDescription(sdp=link.answer, type="answer"))
    stt, tts = ScriptedTranscription(), Tone()
    async with Session(link, transcription=stt, speech=tts, video=True) as s:
        await until(lambda: len(stt.fed) > 10, timeout=10)
        assert all(frame.sample_rate == 16000 and frame.channels == 1 for frame in stt.fed)
        await until(lambda: s._window is not None and bool(s._window.newest(1)), timeout=10)
        parts = await s.look(triggered_by=HOST)
        said = await s.say("Hello there.", triggered_by=HOST)
        await until(lambda: len(received_audio) > 5, timeout=5)
    assert isinstance(parts[0], Image) and (parts[0].data or b"")[:2] == b"\xff\xd8"
    assert said.heard == "Hello there." and {"type": "ready"} in messages
```

  A second test: a client whose channel sends `{"type": "hello", "protocol": 2}` receives
  `{"type": "error", "message": "transport: protocol 2 is not supported; this server speaks 1"}`
  and the session over it closes `hung_up`; `accept_offer(sdp, identity=ANA, scope="")` raises
  `InputError("transport: scope is empty")`. The offer endpoint is the consumer's own HTTP
  endpoint, authenticated by the consumer before it calls `accept_offer` (decision 11). The camera track
  arrives at the client's `fps` (its `maxFramerate`, Task 19) and every frame is encoded to JPEG,
  so a live session receives frames continuously — the video path the docs explain.
- [ ] **Step 2: implement** `webrtc.py`, complete (adapted as the table at the top says):

```python
from __future__ import annotations

import asyncio
import contextlib
import fractions
import logging
import time
from collections.abc import AsyncIterator, Coroutine
from typing import Any

import av
from aiortc import MediaStreamTrack, RTCConfiguration, RTCDataChannel, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError

from rfnry_senses.contracts import AudioFrame
from rfnry_senses.errors import InputError, TransportError
from rfnry_senses.models import Identity, Image
from rfnry_senses.transport import Played
from rfnry_senses.websocket import check_hello
from rfnry_senses.wire import control, parse_played

__all__ = ["WebRTCTransport", "accept_offer"]

log = logging.getLogger("rfnry_senses")


class _Voice(MediaStreamTrack):
    kind = "audio"
    FRAME_MS = 20
    PREBUFFER_S = 0.12
    GAP_S = 0.1

    def __init__(self) -> None:
        super().__init__()
        self._queue: asyncio.Queue[AudioFrame] = asyncio.Queue()
        self._rest = b""
        self._pts = 0
        self._start: float | None = None
        self._samples = 0

    def push(self, frame: AudioFrame) -> None:
        self._rest += frame.data
        size = frame.sample_rate * self.FRAME_MS // 1000 * 2 * frame.channels
        while len(self._rest) >= size:
            chunk, self._rest = self._rest[:size], self._rest[size:]
            self._queue.put_nowait(AudioFrame(data=chunk, sample_rate=frame.sample_rate, channels=frame.channels))

    def clear(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
        self._rest = b""
        self._start = None
        self._samples = 0

    async def recv(self) -> av.AudioFrame:
        frame = await self._queue.get()
        out = av.AudioFrame(format="s16", layout="mono" if frame.channels == 1 else "stereo", samples=len(frame.data) // (2 * frame.channels))
        out.planes[0].update(frame.data)
        out.sample_rate = frame.sample_rate
        out.pts = self._pts
        out.time_base = fractions.Fraction(1, frame.sample_rate)
        self._pts += out.samples
        now = time.monotonic()
        due = None if self._start is None else self._start + self._samples / out.sample_rate
        if due is None or now - due > self.GAP_S + self.PREBUFFER_S:
            self._start, self._samples, due = now - self.PREBUFFER_S, 0, now - self.PREBUFFER_S
        await asyncio.sleep(max(0.0, due - now))
        self._samples += out.samples
        return out


class WebRTCTransport:
    def __init__(self, identity: Identity, scope: str, *, sample_rate: int = 16000, ice_servers: list[dict[str, Any]] | None = None) -> None:
        self.identity = identity
        self.scope = scope
        self.answer = ""
        self._rate = sample_rate
        self._pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(**server) for server in ice_servers or []]))
        self._voice = _Voice()
        self._pc.addTrack(self._voice)
        self._inbox: asyncio.Queue[AudioFrame | Image | Played | None] = asyncio.Queue()
        self._channel: RTCDataChannel | None = None
        self._unsent: list[str] = []
        self._readers: set[asyncio.Task[None]] = set()
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)
        self._jpeg: Any = None
        self._jpeg_size = (0, 0)
        self._greeted = False
        self._closed = False

        @self._pc.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            self._read(self._hear(track) if track.kind == "audio" else self._see(track))

        @self._pc.on("datachannel")
        def on_channel(channel: RTCDataChannel) -> None:
            self._channel = channel
            for text in self._unsent:
                channel.send(text)
            self._unsent.clear()

            @channel.on("message")
            def on_message(message: Any) -> None:
                if not isinstance(message, str):
                    return
                if not self._greeted:
                    try:
                        check_hello(message)
                    except InputError as exc:
                        channel.send(control({"type": "error", "message": str(exc)}))
                        self._inbox.put_nowait(None)
                        return
                    self._greeted = True
                    return
                try:
                    self._inbox.put_nowait(parse_played(message))
                except InputError as exc:
                    log.warning("transport: dropped a frame: %s", exc)

        @self._pc.on("connectionstatechange")
        def on_state() -> None:
            if self._pc.connectionState in ("failed", "closed"):
                self._inbox.put_nowait(None)

    async def inbound(self) -> AsyncIterator[AudioFrame | Image | Played]:
        while (item := await self._inbox.get()) is not None:
            yield item

    async def send_audio(self, frame: AudioFrame, utterance: int) -> None:
        self._voice.push(frame)

    async def send_control(self, control_frame: dict[str, Any]) -> None:
        text = control(control_frame)
        if self._channel is not None and self._channel.readyState == "open":
            self._channel.send(text)
        else:
            self._unsent.append(text)

    def clear_audio(self) -> None:
        self._voice.clear()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for reader in self._readers:
            reader.cancel()
        with contextlib.suppress(Exception):
            await self._pc.close()
        self._inbox.put_nowait(None)

    def _read(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self._readers.add(task)
        task.add_done_callback(self._readers.discard)

    async def _hear(self, track: MediaStreamTrack) -> None:
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError:
                return
            for out in self._resampler.resample(frame):
                self._inbox.put_nowait(AudioFrame(data=bytes(out.planes[0])[: out.samples * 2], sample_rate=self._rate))

    async def _see(self, track: MediaStreamTrack) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError:
                return
            jpeg = await loop.run_in_executor(None, self._encode, frame)
            if jpeg:
                self._inbox.put_nowait(Image(media_type="image/jpeg", data=jpeg))

    def _encode(self, frame: av.VideoFrame) -> bytes:
        if self._jpeg is None or self._jpeg_size != (frame.width, frame.height):
            codec = av.CodecContext.create("mjpeg", "w")
            codec.width, codec.height = frame.width, frame.height
            codec.pix_fmt = "yuvj420p"
            codec.time_base = fractions.Fraction(1, 1000)
            codec.options = {"qmin": "5", "qmax": "5"}
            self._jpeg, self._jpeg_size = codec, (frame.width, frame.height)
        return b"".join(bytes(packet) for packet in self._jpeg.encode(frame.reformat(format="yuvj420p")))


async def accept_offer(
    sdp: str,
    *,
    identity: Identity,
    scope: str,
    sample_rate: int = 16000,
    ice_servers: list[dict[str, Any]] | None = None,
) -> WebRTCTransport:
    if not scope:
        raise InputError("transport: scope is empty")
    transport = WebRTCTransport(identity, scope, sample_rate=sample_rate, ice_servers=ice_servers)
    try:
        await transport._pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        await transport._pc.setLocalDescription(await transport._pc.createAnswer())
    except Exception as exc:
        await transport.close()
        raise TransportError(f"webrtc: the offer could not be answered: {exc}") from exc
    transport.answer = transport._pc.localDescription.sdp
    return transport
```

  PyAV 17 ships type stubs, so mypy checks it: `_hear` and `_see` skip anything `track.recv()`
  returns that is not an `av.AudioFrame`/`av.VideoFrame`, and `_encode` narrows the created codec
  with `isinstance(codec, av.VideoCodecContext)` (raising `TransportError("webrtc: this build of av
  has no jpeg encoder")` otherwise) — both found in Task 14. The test says "Hello there, how are
  you today?": two words of `Tone` are only ten 20 ms frames, too few to count on after Opus.
  Check against the installed PyAV that `av.AudioFrame(..., samples=n)` allocates with
  `align=1` (so `planes[0].update` takes exactly `n * 2 * channels` bytes); if the installed
  version pads, pass `align=1` explicitly. The inbound `bytes(out.planes[0])[: out.samples * 2]`
  already trims padding. `webrtc.py` is only imported by `from rfnry_senses.webrtc import …`;
  `rfnry_senses/__init__.py` never imports it.
- [ ] **Step 3: gate** plus `uv run poe test:webrtc`. **Commit** `feat: the WebRTC transport`.

## Task 15 — Public names, the docs pin, the docs examples

**Files:** modify `src/rfnry_senses/__init__.py`; create `tests/unit/test_public_api.py`,
`tests/unit/test_docs.py`, `tests/unit/test_docs_examples.py`.

- [ ] **Step 1: `__init__.py`** — import and list exactly:

```python
__all__ = [
    "Session", "SensesTools",
    "Event", "Heard", "TurnEnded", "Said", "Interrupted", "Speaking", "Silent", "Looked", "Muted", "Informed", "ToolCalled", "Closed",
    "LiveModel", "LiveConnection", "LiveEvent", "LiveAudio", "LiveHeard", "LiveSaid", "LiveStarted", "LiveDone",
    "LiveToolCall", "LiveInterrupted", "LiveFailed",
    "TranscriptionModel", "Transcription", "Transcript", "UtteranceEnd", "SpeechModel", "AudioFrame",
    "DecisionModel", "Check", "CheckAnswer", "Decided",
    "Text", "Image", "Audio", "Document", "Part", "Content", "ToolSpec",
    "Tool", "Tools",
    "serve", "Connection", "Transport", "Played", "Loopback", "Identity",
    "SensesException", "ConfigurationError", "InputError", "NotFoundError", "ModelError", "TransportError",
]
```

  (One name per line after `ruff format`.)
- [ ] **Step 2: tests.** `test_public_api.py`: `set(rfnry_senses.__all__) == EXPECTED` (the list
  above as a set); every name resolves; `{n for n in vars(Session) if not n.startswith("_")} ==
  {"say", "prompt", "inform", "interrupt", "mute", "unmute", "look", "events", "close"}` (the
  attributes `scope` and `person` are set in `__init__`); with `pytest.importorskip("aiortc")`,
  `rfnry_senses.webrtc.__all__ == ["WebRTCTransport", "accept_offer"]`.

  `test_docs.py`:

```python
import re
from pathlib import Path

import rfnry_senses

ROOT = Path(__file__).parents[4]
DOCS = ROOT / "docs" / "senses.md"
VENDORS = re.compile(r"\b(anthropic|claude|openai|gpt|gemini|google|deepgram|elevenlabs|cartesia|twilio|azure|aws|voyage|typesafe)\b", re.I)


def test_every_public_name_is_documented() -> None:
    text = DOCS.read_text(encoding="utf-8")
    names = [*rfnry_senses.__all__, "WebRTCTransport", "accept_offer"]
    assert [name for name in names if name not in text] == []


def test_no_vendor_is_named_in_the_library_or_its_contract() -> None:
    files = [DOCS, *(ROOT / "packages/python/src").rglob("*.py"), *(ROOT / "packages/typescript/src").rglob("*.ts")]
    assert [str(f) for f in files if VENDORS.search(f.read_text(encoding="utf-8"))] == []
```

  `test_docs_examples.py` — modelled on `../engine/packages/python/tests/unit/test_docs_examples.py`:
  extract the ```` ```python ```` blocks; each block's first line names it.
  - `SIGNATURES`: `"class LiveModel(Protocol):"`, `"class DecisionModel(Protocol):"`, `"class
    Tools:"`. Parse per class (`class (\w+)` sets the class, `def (\w+)\(([^)]*)\)` a method),
    strip nested brackets and a bare `*`, and compare the parameter names with
    `inspect.signature` of `getattr(rfnry_senses, class_name)`'s method.
  - `ILLUSTRATIVE` (need a web framework or a network): `@app.websocket("/senses")`, `class
    Socket:`, `@app.post("/senses/offer")`. For `class Socket:`, assert instead that the block
    equals the class in `tests/integration/socket_adapter.py`.
  - Every other block runs, compiled with `ast.PyCF_ALLOW_TOP_LEVEL_AWAIT`, inside
    `asyncio.timeout(5)`, in a namespace of: every `rfnry_senses` public name; the docs' `class
    Script:` block executed first (it defines `Script` and `Tone`); `frame` (`pcm(20)`); `ana`,
    `host`, `assistant` identities; `link` (a fresh `Loopback(ana, "acme")`); `live` (an
    auto-answering `ScriptedLive` whose connection replies to `say`/`send_text(respond=True)` with
    `response(...)` of the words); `stt` (`Script("A table for two.")`); `tts` (`Tone()`); `dm`
    (`Done(0.9)`); `tools` (a `Tools` with a `now` tool); `now` (its body); `engine` (a fake whose
    `run(agent, scope=, input=, tools=None, on_event=None)` awaits `on_event(TextDelta(text=w))`
    for each word of `"For two, at eight."` (spaces kept) when given one, then returns an object
    with `reply="For two, at eight."`, and whose `tool(agent, scope=, name=, description=)`
    returns a one-tool `Tools` named `name`); `TextDelta` (a stand-in with a `text` field — the
    docs name the engine's event, the harness never imports rfnry-engine); `asyncio`; `ThreadTools` (a function returning an empty `Tools`); `client`, `thread`;
    `session` (an opened live `Session` over its own `Loopback`, closed after the block); `post`
    (records its calls). A block that iterates `session.events()` until the session ends is
    ended by the harness hanging its link up 0.2 s after the block starts; the `Seeing` block's
    link is shown one frame before the block runs. Blocks whose first line appears in no set
    run as they are; a new block that cannot run this way must be added to one set, never
    skipped silently: `test_every_named_block_is_in_the_docs` asserts the set names all exist and
    first lines are unique, as the engine's test does.
- [ ] **Step 3: gate. Commit** `feat: the public surface, pinned by the docs`.

## Task 16 — The TypeScript package and its wire

**Files:** create `packages/typescript/{package.json,tsconfig.json,tsconfig.build.json,biome.json,vitest.config.ts,README.md}`,
`packages/typescript/src/{index.ts,errors.ts,wire.ts}`, `packages/typescript/tests/wire.test.ts`.

- [ ] **Step 1: scaffold.** `package.json`: `"name": "@rfnry/senses"`, `"type": "module"`,
  `"exports": {".": {"types": "./dist/index.d.ts", "default": "./dist/index.js"}}`, `"files":
  ["dist"]`, `"sideEffects": false`, `"engines": {"node": ">=22"}`, no `dependencies`;
  `devDependencies` `typescript`, `vitest`, `@biomejs/biome` (install with `npm i -D`, keep the
  lockfile). Scripts: `build` (`tsc -p tsconfig.build.json`), `typecheck` (`tsc --noEmit`),
  `format` (`biome format --write .`), `check` (`biome check .`), `test` (`vitest run`),
  `test:integration` (`vitest run --config vitest.integration.config.ts`). `tsconfig.json`:
  `strict`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, `target`/`lib` ES2022 +
  `DOM` + `DOM.Iterable`, `module` ESNext, `moduleResolution` Bundler, `types: ["node"]` only for
  tests (put `@types/node` in dev dependencies; `tsconfig.build.json` excludes `tests`). Biome:
  single quotes, no semicolons, line width 120, recommended lint rules.
- [ ] **Step 2: failing test** — `tests/wire.test.ts`:

```ts
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { SensesError } from '../src/errors'
import { decodeAudio, encodeAudio, encodeImage, hello, parseControl, played } from '../src/wire'

const FIXTURES = join(__dirname, '../../../fixtures/wire')
const json = (name: string): unknown => JSON.parse(readFileSync(join(FIXTURES, `${name}.json`), 'utf8'))
const hex = (name: string): Uint8Array => Uint8Array.from(Buffer.from(readFileSync(join(FIXTURES, `${name}.hex`), 'utf8').trim(), 'hex'))

describe('the wire', () => {
  it('writes the client frames the server expects', () => {
    expect(JSON.parse(hello())).toEqual(json('hello'))
    expect(JSON.parse(played(3, 1200))).toEqual(json('played'))
    expect(encodeAudio(Uint8Array.of(0x00, 0x01, 0xff, 0x7f), 16000, 1, 3)).toEqual(hex('audio'))
    expect(encodeImage(Uint8Array.of(0xff, 0xd8, 0xff, 0xd9))).toEqual(hex('image'))
  })

  it('reads every server frame', () => {
    for (const name of ['ready', 'speaking', 'silent', 'stop', 'error']) {
      expect(parseControl(JSON.stringify(json(name)))).toEqual(json(name))
    }
    for (const kind of ['heard', 'turn_ended', 'said', 'interrupted', 'speaking', 'silent', 'looked', 'muted', 'closed']) {
      const control = parseControl(JSON.stringify(json(`event-${kind}`)))
      expect(control.type === 'event' && control.event.kind).toBe(kind)
    }
    const audio = decodeAudio(hex('audio').buffer as ArrayBuffer)
    expect(audio).toMatchObject({ utterance: 3, sampleRate: 16000, channels: 1 })
    expect(Array.from(audio.pcm)).toEqual([0x00, 0x01, 0xff, 0x7f])
  })

  it('refuses what it cannot read', () => {
    expect(() => parseControl('{"type":"nope"}')).toThrow(new SensesError('wire: unknown control frame nope'))
    expect(() => decodeAudio(new Uint8Array([2, 1]).buffer)).toThrow(new SensesError('wire: not an audio frame'))
  })
})
```

- [ ] **Step 3: implement.** `src/errors.ts`:

```ts
export class SensesError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'SensesError'
  }
}
```

  `src/wire.ts`, complete:

```ts
import { SensesError } from './errors'

export const PROTOCOL = 1
const AUDIO = 1
const IMAGE = 2
const HEADER = 10

export type Identity = { role: 'user' | 'assistant' | 'system'; id: string; name: string | null; metadata: Record<string, unknown> }

type Stamp = { scope: string; at: string }

export type SensesEvent =
  | (Stamp & { kind: 'heard'; text: string; final: boolean; speaker: Identity })
  | (Stamp & { kind: 'turn_ended'; text: string; speaker: Identity; probability: number | null })
  | (Stamp & { kind: 'said'; text: string; heard: string; interrupted: boolean; triggered_by: Identity | null })
  | (Stamp & { kind: 'interrupted'; heard: string; triggered_by: Identity })
  | (Stamp & { kind: 'speaking' })
  | (Stamp & { kind: 'silent' })
  | (Stamp & { kind: 'looked'; frames: number; triggered_by: Identity })
  | (Stamp & { kind: 'muted'; muted: boolean; triggered_by: Identity })
  | (Stamp & { kind: 'closed'; reason: 'closed' | 'hung_up' | 'failed'; message: string | null })

export type ServerControl =
  | { type: 'ready' }
  | { type: 'speaking' | 'silent' | 'stop'; utterance: number }
  | { type: 'event'; event: SensesEvent }
  | { type: 'error'; message: string }

export type AudioOut = { utterance: number; sampleRate: number; channels: number; pcm: Uint8Array }

const CONTROLS = new Set(['ready', 'speaking', 'silent', 'stop', 'event', 'error'])

export function hello(): string {
  return JSON.stringify({ type: 'hello', protocol: PROTOCOL })
}

export function played(utterance: number, ms: number): string {
  return JSON.stringify({ type: 'played', utterance, ms: Math.max(0, Math.round(ms)) })
}

export function encodeAudio(pcm: Uint8Array, sampleRate: number, channels = 1, utterance = 0): Uint8Array {
  const out = new Uint8Array(HEADER + pcm.byteLength)
  const view = new DataView(out.buffer)
  view.setUint8(0, AUDIO)
  view.setUint8(1, channels)
  view.setUint32(2, sampleRate, true)
  view.setUint32(6, utterance, true)
  out.set(pcm, HEADER)
  return out
}

export function encodeImage(jpeg: Uint8Array): Uint8Array {
  const out = new Uint8Array(1 + jpeg.byteLength)
  out[0] = IMAGE
  out.set(jpeg, 1)
  return out
}

export function decodeAudio(data: ArrayBuffer): AudioOut {
  const view = new DataView(data)
  if (data.byteLength < HEADER || view.getUint8(0) !== AUDIO) throw new SensesError('wire: not an audio frame')
  return {
    channels: view.getUint8(1),
    sampleRate: view.getUint32(2, true),
    utterance: view.getUint32(6, true),
    pcm: new Uint8Array(data, HEADER),
  }
}

export function parseControl(text: string): ServerControl {
  const frame: unknown = JSON.parse(text)
  const type = typeof frame === 'object' && frame !== null ? (frame as { type?: unknown }).type : undefined
  if (typeof type !== 'string' || !CONTROLS.has(type)) throw new SensesError(`wire: unknown control frame ${String(type)}`)
  return frame as ServerControl
}
```

  `src/index.ts` exports `SensesError` and the types `SensesEvent`, `Identity` for now.
- [ ] **Step 4: gate** (TypeScript gate). **Commit** `feat(ts): @rfnry/senses — the package and
  its wire`.

## Task 17 — Capture and playback

**Files:** create `packages/typescript/src/{microphone.ts,playback.ts}`,
`packages/typescript/tests/playback.test.ts`, `packages/typescript/tests/microphone.test.ts`.

- [ ] **Step 1: failing tests** — `tests/playback.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import { type AudioContextLike, Playback } from '../src/playback'

class Node {
  buffer: unknown = null
  onended: (() => void) | null = null
  started = -1
  stopped = false
  connect(): void {}
  start(at: number): void {
    this.started = at
  }
  stop(): void {
    this.stopped = true
  }
}

class Context {
  currentTime = 0
  destination = {} as AudioNode
  nodes: Node[] = []
  createBuffer(_channels: number, length: number, rate: number): AudioBuffer {
    return { duration: length / rate, getChannelData: () => new Float32Array(length) } as unknown as AudioBuffer
  }
  createBufferSource(): AudioBufferSourceNode {
    const node = new Node()
    this.nodes.push(node)
    return node as unknown as AudioBufferSourceNode
  }
  async close(): Promise<void> {}
}

const frame = (utterance: number, ms = 100) => ({ utterance, sampleRate: 16000, channels: 1, pcm: new Uint8Array(32 * ms) })

function setup() {
  const context = new Context()
  const reports: Array<[number, number]> = []
  const playback = new Playback(context as unknown as AudioContextLike, (u, ms) => reports.push([u, ms]))
  return { context, reports, playback }
}

describe('playback', () => {
  it('reports what played when an utterance ends', () => {
    const { context, reports, playback } = setup()
    playback.push(frame(1))
    playback.push(frame(1))
    expect(context.nodes.map((n) => n.started)).toEqual([0, 0.1])
    playback.silent(1)
    expect(reports).toEqual([])
    for (const node of context.nodes) node.onended?.()
    expect(reports).toEqual([[1, 200]])
  })

  it('stops mid-utterance and reports how far it got', () => {
    const { context, reports, playback } = setup()
    for (let i = 0; i < 3; i++) playback.push(frame(1))
    context.nodes[0]?.onended?.()
    context.currentTime = 0.15
    playback.stop(1)
    expect(reports).toEqual([[1, 150]])
    expect(context.nodes.slice(1).every((n) => n.stopped)).toBe(true)
  })

  it('drops audio of a stopped utterance', () => {
    const { context, reports, playback } = setup()
    playback.push(frame(1))
    playback.stop(1)
    playback.push(frame(1))
    expect(context.nodes).toHaveLength(1)
    playback.push(frame(2))
    expect(context.nodes).toHaveLength(2)
    playback.stop(5)
    expect(reports).toEqual([[1, 0], [2, 0], [5, 0]])
  })
})
```

  `tests/microphone.test.ts`: the worklet's resampler (exported as `downsample` from
  `microphone.ts` for the test, and inlined into the worklet source by `String(downsample)`) turns
  48 kHz into 16 kHz in the ratio 3:1 and keeps its fractional position across calls; `toPcm16`
  clips to ±1 and writes little-endian (`[1, -1, 0.5]` → `ff7f 0080 ff3f`).
- [ ] **Step 2: implement** `src/playback.ts`, complete (adapted from
  `../rfnry-speech/packages/client-react/src/audio/playback.ts`; the per-utterance map and the fence
  are fresh):

```ts
import type { AudioOut } from './wire'

export type AudioContextLike = Pick<AudioContext, 'currentTime' | 'destination' | 'createBuffer' | 'createBufferSource' | 'close'>

type Scheduled = { utterance: number; start: number; duration: number; node: AudioBufferSourceNode }

export class Playback {
  private next = 0
  private fenced = 0
  private readonly scheduled = new Set<Scheduled>()
  private readonly played = new Map<number, number>()
  private readonly ending = new Set<number>()
  private readonly reported = new Set<number>()

  constructor(
    private readonly context: AudioContextLike,
    private readonly report: (utterance: number, ms: number) => void,
  ) {}

  speaking(utterance: number): void {
    if (utterance > this.fenced && !this.played.has(utterance) && !this.reported.has(utterance)) this.played.set(utterance, 0)
  }

  push(frame: AudioOut): void {
    if (frame.utterance <= this.fenced) return
    this.speaking(frame.utterance)
    const samples = frame.pcm.byteLength / 2 / frame.channels
    if (samples === 0) return
    const buffer = this.context.createBuffer(frame.channels, samples, frame.sampleRate)
    const view = new DataView(frame.pcm.buffer, frame.pcm.byteOffset, frame.pcm.byteLength)
    for (let channel = 0; channel < frame.channels; channel++) {
      const data = buffer.getChannelData(channel)
      for (let i = 0; i < samples; i++) {
        const value = view.getInt16((i * frame.channels + channel) * 2, true)
        data[i] = value < 0 ? value / 0x8000 : value / 0x7fff
      }
    }
    const node = this.context.createBufferSource()
    node.buffer = buffer
    node.connect(this.context.destination)
    const start = Math.max(this.context.currentTime, this.next)
    const entry: Scheduled = { utterance: frame.utterance, start, duration: buffer.duration, node }
    node.onended = () => {
      this.scheduled.delete(entry)
      this.add(entry.utterance, entry.duration * 1000)
      this.settle(entry.utterance)
    }
    node.start(start)
    this.next = start + buffer.duration
    this.scheduled.add(entry)
  }

  silent(utterance: number): void {
    this.ending.add(utterance)
    this.settle(utterance)
  }

  stop(utterance: number): void {
    this.speaking(utterance)
    this.fenced = Math.max(this.fenced, utterance)
    const now = this.context.currentTime
    for (const entry of this.scheduled) {
      entry.node.onended = null
      entry.node.stop()
      this.add(entry.utterance, Math.min(Math.max(now - entry.start, 0), entry.duration) * 1000)
    }
    this.scheduled.clear()
    this.next = now
    for (const number of [...this.played.keys()].sort((a, b) => a - b)) if (number <= utterance) this.finish(number)
  }

  async close(): Promise<void> {
    this.stop(Number.MAX_SAFE_INTEGER)
    await this.context.close()
  }

  private add(utterance: number, ms: number): void {
    const sum = this.played.get(utterance)
    if (sum !== undefined) this.played.set(utterance, sum + ms)
  }

  private settle(utterance: number): void {
    if (!this.ending.has(utterance)) return
    for (const entry of this.scheduled) if (entry.utterance === utterance) return
    this.finish(utterance)
  }

  private finish(utterance: number): void {
    const ms = this.played.get(utterance)
    if (ms === undefined) return
    this.played.delete(utterance)
    this.ending.delete(utterance)
    this.reported.add(utterance)
    this.report(utterance, Math.round(ms))
  }
}
```

  `close()` must not report `MAX_SAFE_INTEGER`: `speaking` adds it to `played` — guard `stop` so
  it only calls `speaking(utterance)` when `utterance !== Number.MAX_SAFE_INTEGER`, or give
  `close` its own loop; the executor picks the smaller change and covers it with a test.

  `src/microphone.ts` — adapt `capture-worklet.ts` and `capture.ts` from
  `../rfnry-speech/packages/client-react/src/audio/`: the worklet source (a `LinearStreamResampler`
  and the 320-sample framer, 20 ms at 16 kHz) is built from a template string and loaded through a
  `Blob` URL; `class Microphone { start(sampleRate: number): Promise<MediaStream>; onChunk(listener:
  (pcm: Uint8Array) => void): () => void; set enabled(on: boolean); stop(): Promise<void> }`, with
  `getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } })`.
  `enabled = false` sets the track's `enabled` and stops posting chunks. Drop the old
  `clearAfterMs` reset (it existed for a React remount path that is gone).
- [ ] **Step 3: gate. Commit** `feat(ts): microphone capture and playback that counts what played`.

## Task 18 — Camera, the WebSocket link, `connect`

**Files:** create `packages/typescript/src/{camera.ts,websocket.ts,senses.ts}`; modify
`src/index.ts`; create `packages/typescript/tests/senses.test.ts`.

**Interfaces** (public, exported from `index.ts`):

```ts
export type ConnectOptions = {
  url: string
  transport: 'websocket' | 'webrtc'
  protocols?: string | string[]
  headers?: Record<string, string>
  microphone?: boolean
  camera?: false | { fps?: number; width?: number; quality?: number }
  sampleRate?: number
  iceServers?: RTCIceServer[]
}
export type SensesStatus = 'open' | 'closed'
export type SensesListener = (event: SensesEvent | { kind: 'status'; status: SensesStatus }) => void
export interface Senses {
  readonly status: SensesStatus
  subscribe(listener: SensesListener): () => void
  mute(): void
  unmute(): void
  pauseCamera(): void
  resumeCamera(): void
  setFps(fps: number): void
  close(): Promise<void>
}
export function connect(options: ConnectOptions): Promise<Senses>
export { SensesError }
export type { SensesEvent, Identity }
```

  Defaults: `microphone: true`, `camera: false`, `camera.fps: 1`, `camera.width: 640`,
  `camera.quality: 0.7`, `sampleRate: 16000`.

- [ ] **Step 1: failing tests** — `tests/senses.test.ts`, with `vi.stubGlobal` for `WebSocket`
  (a fake that records sent frames and lets the test deliver messages and `close`) and
  `AudioContext` (the `Context` fake from `playback.test.ts`, moved to `tests/fakes.ts`):
  1. `connect({ url, transport: 'websocket', protocols: ['bearer', 't'], microphone: false })`
     opens the socket with those subprotocols, sends exactly `hello()` on open and resolves only
     after the server's `ready`;
  2. an `error` control before `ready` rejects with `new SensesError('transport: protocol 2 is not
     supported; this server speaks 1')`;
  3. `subscribe` receives `{ kind: 'status', status: 'open' }` first, then every `event` control's
     event; the returned function unsubscribes;
  4. binary audio is played, and after `silent {utterance: 1}` and the fake nodes ending, the
     socket receives `played(1, 200)`; after `stop {utterance: 2}` it receives `played(2, …)` at
     once, and later utterance-2 audio creates no node;
  5. with `microphone: true` and stubbed `navigator.mediaDevices.getUserMedia`,
     `AudioWorkletNode` and `audioWorklet.addModule`, chunks posted by the fake worklet port are
     sent as `encodeAudio(pcm, 16000)` until `mute()`, and again after `unmute()`;
  6. with `camera: { fps: 2 }`, a stubbed `getUserMedia` video stream and a stubbed canvas whose
     `toBlob` yields a fixed JPEG, fake timers advance 1 s and the socket receives two
     `encodeImage` frames; `setFps(4)` then gives four frames in the next second; `pauseCamera()`
     stops them; `setFps(0)` throws `senses: fps must be positive`, and `setFps(2)` on a connection
     without a camera throws `senses: setFps needs a camera`;
  7. the server's `closed` event, or the socket closing, sets `status` to `'closed'` and tells
     listeners; `close()` closes the socket and the audio context.
- [ ] **Step 2: implement.** `websocket.ts`:

```ts
import { SensesError } from './errors'
import { type AudioOut, decodeAudio, encodeAudio, encodeImage, hello, parseControl, played, type ServerControl } from './wire'

export type LinkHandlers = { control(frame: ServerControl): void; audio(frame: AudioOut): void; closed(): void }

export class WebSocketLink {
  private constructor(private readonly socket: WebSocket) {}

  static open(url: string, protocols: string | string[] | undefined, handlers: LinkHandlers): Promise<WebSocketLink> {
    return new Promise((resolve, reject) => {
      const socket = new WebSocket(url, protocols)
      socket.binaryType = 'arraybuffer'
      let ready = false
      socket.onopen = () => socket.send(hello())
      socket.onmessage = (message: MessageEvent) => {
        if (message.data instanceof ArrayBuffer) return handlers.audio(decodeAudio(message.data))
        const frame = parseControl(String(message.data))
        if (!ready && frame.type === 'ready') {
          ready = true
          resolve(new WebSocketLink(socket))
        } else if (!ready && frame.type === 'error') {
          reject(new SensesError(frame.message))
        } else {
          handlers.control(frame)
        }
      }
      socket.onclose = () => {
        if (!ready) reject(new SensesError('transport: the connection closed before it was ready'))
        handlers.closed()
      }
    })
  }

  sendAudio(pcm: Uint8Array, sampleRate: number): void {
    if (this.socket.readyState === WebSocket.OPEN) this.socket.send(encodeAudio(pcm, sampleRate))
  }

  sendImage(jpeg: Uint8Array): void {
    if (this.socket.readyState === WebSocket.OPEN) this.socket.send(encodeImage(jpeg))
  }

  played(utterance: number, ms: number): void {
    if (this.socket.readyState === WebSocket.OPEN) this.socket.send(played(utterance, ms))
  }

  close(): void {
    this.socket.close()
  }
}
```

  `camera.ts` — adapted from `../rfnry-vision/packages/client-react/src/video/capture.ts` and the
  canvas path of `encoder.ts`: `class Camera { constructor(options: {fps, width, quality}, send:
  (jpeg: Uint8Array) => void); start(): Promise<MediaStream>; pause(): void; resume(): void;
  setFps(fps: number): void; stop(): void }` (`setFps` clears and restarts the interval); a hidden `<video muted playsinline>` plays the stream, a `setInterval(1000 /
  fps)` draws it scaled to `width` on a canvas and `toBlob('image/jpeg', quality)` sends the bytes;
  a capture still in flight skips the next tick. Frames flow for as long as the camera runs, at
  `camera.fps`: that continuous stream is what a live session forwards to its model (Task 10) —
  "seeing as video" in the docs.

  `senses.ts` — `connect()` dispatches on `transport`; the WebSocket path:

```ts
async function overWebSocket(options: ConnectOptions): Promise<Senses> {
  const listeners = new Set<SensesListener>()
  const rate = options.sampleRate ?? 16000
  let status: SensesStatus = 'open'
  let playback: Playback | null = null
  const tell = (event: Parameters<SensesListener>[0]) => {
    for (const listener of listeners) listener(event)
  }
  const end = () => {
    if (status === 'closed') return
    status = 'closed'
    tell({ kind: 'status', status })
  }
  const link = await WebSocketLink.open(options.url, options.protocols, {
    control(frame) {
      if (frame.type === 'speaking') playback?.speaking(frame.utterance)
      else if (frame.type === 'silent') playback?.silent(frame.utterance)
      else if (frame.type === 'stop') playback?.stop(frame.utterance)
      else if (frame.type === 'event') {
        tell(frame.event)
        if (frame.event.kind === 'closed') end()
      }
    },
    audio(frame) {
      playback?.push(frame)
    },
    closed: end,
  })
  const context = new AudioContext()
  playback = new Playback(context, (utterance, ms) => link.played(utterance, ms))
  const microphone = options.microphone === false ? null : new Microphone()
  microphone?.onChunk((pcm) => link.sendAudio(pcm, rate))
  await microphone?.start(rate)
  const camera = options.camera ? new Camera(cameraOptions(options.camera), (jpeg) => link.sendImage(jpeg)) : null
  await camera?.start()
  return {
    get status() {
      return status
    },
    subscribe(listener) {
      listeners.add(listener)
      listener({ kind: 'status', status })
      return () => listeners.delete(listener)
    },
    mute: () => microphone && (microphone.enabled = false),
    unmute: () => microphone && (microphone.enabled = true),
    pauseCamera: () => camera?.pause(),
    resumeCamera: () => camera?.resume(),
    setFps(fps) {
      if (!(fps > 0)) throw new SensesError('senses: fps must be positive')
      if (camera === null) throw new SensesError('senses: setFps needs a camera')
      camera.setFps(fps)
    },
    async close() {
      camera?.stop()
      await microphone?.stop()
      link.close()
      await playback?.close()
      end()
    },
  }
}
```

  Audio or controls that arrive between `ready` and `playback` being set are dropped by the `?.`;
  the server sends nothing but events before the person speaks, so create `playback` before
  awaiting `WebSocketLink.open` if a test shows otherwise (the `AudioContext` needs no await).
  Make the `mute`/`unmute` arrow bodies statements (Biome flags assignments in expressions).
- [ ] **Step 3: gate. Commit** `feat(ts): connect over a WebSocket — microphone, camera, playback,
  events`.

## Task 19 — The browser's WebRTC path

**Files:** create `packages/typescript/src/{webrtc.ts,remote-playback.ts}`; modify
`src/senses.ts`; create `packages/typescript/tests/webrtc.test.ts`.

- [ ] **Step 1: failing tests** — `tests/webrtc.test.ts`, with stubbed `RTCPeerConnection` (records
  `addTrack`, `createDataChannel('senses')`, offers a fixed SDP, completes ICE gathering
  immediately, exposes the channel to the test), `fetch`, `getUserMedia`:
  1. `connect({ url: '/senses/offer', transport: 'webrtc', headers: { authorization: 'Bearer t' } })`
     posts `{ sdp }` as JSON with those headers (and `credentials: 'same-origin'`), sets the answer
     from `{ sdp }`, sends `hello()` when the channel opens, and resolves when the channel
     delivers `ready`;
  2. a 401 rejects with `new SensesError('webrtc: the offer was refused with status 401')`;
  3. with a camera, the video sender's parameters get `maxFramerate` = `fps`; `pauseCamera()`
     disables the video track, `mute()` the audio track; `setFps(5)` calls the camera track's
     `applyConstraints({ frameRate: 5 })`;
  4. `RemotePlayback`: given receiver stats (`inbound-rtp` with `jitterBufferEmittedCount`,
     `concealedSamples`, and a codec `clockRate` 48000), `speaking(1)` takes a baseline, `stop(1)`
     reports `(emitted − concealed) / 48` ms since it and mutes the element; `silent(1)` reports
     once two polls 100 ms apart see the emitted count unchanged; `speaking(2)` unmutes.
- [ ] **Step 2: implement.** `remote-playback.ts` — adapt
  `../rfnry-speech/packages/client-react/src/audio/remotePlayback.ts` (`_readCounts`, the
  emitted−concealed arithmetic, the muted element), keyed by utterance: `speaking(u)`,
  `silent(u)`, `stop(u)`, each reporting through the same `report(utterance, ms)` callback as
  `Playback`. `webrtc.ts` — adapt `../rfnry-speech/packages/client-react/src/transport/webrtc.ts`
  (offer, ICE gathering wait) and `../rfnry-vision/packages/client-react/src/transport/webrtc.ts`
  (the video sender): one `RTCPeerConnection({ iceServers: options.iceServers ?? [] })`, the
  data channel created before the offer, the microphone track (same constraints as
  `Microphone`), the camera track when asked with `sender.setParameters` setting `maxFramerate`,
  `ontrack` feeding `RemotePlayback`, the offer posted as `{ sdp }` to `options.url` with
  `options.headers`, `hello()` sent when the channel opens, data channel messages parsed with
  `parseControl` and routed exactly as in the WebSocket path; `setFps` applies `{ frameRate }` to
  the camera track. The
  WebRTC path never sends audio frames or JPEG itself: the tracks carry them. `senses.ts` calls
  it for `transport: 'webrtc'` and returns the same `Senses` shape.
- [ ] **Step 3: gate. Commit** `feat(ts): connect over WebRTC`.

## Task 20 — Both clients against one server, offline

**Files:** create `packages/python/tests/integration/serve_for_client.py`,
`packages/typescript/vitest.integration.config.ts`,
`packages/typescript/tests/integration/python-server.test.ts`.

- [ ] **Step 1:** `serve_for_client.py` — a `websockets` server on `127.0.0.1:<port from argv>`
  that runs `serve` (identity Ana, scope `acme`; the test server authenticates nobody) + a cascaded
  `Session` over scripted models: a transcription that yields
  `Transcript("hello", final=True)` and `UtteranceEnd()` once it has been fed ten frames, a
  `Tone` speech model, and a loop that answers each `TurnEnded` with `say("Hi there.",
  triggered_by=event.speaker)`. When `RFNRY_LIVE_ADAPTERS` names a module, it takes
  `transcription()` and `speech()` from it instead (Task 22). It prints `listening` when ready.
- [ ] **Step 2: failing test** — `python-server.test.ts` spawns `uv run python
  tests/integration/serve_for_client.py 0` from `packages/python` (skipped when `uv` is not on
  `PATH`), waits for `listening <port>`, opens a `WebSocketLink` with Node 22's global
  `WebSocket`, sends 20 audio frames of silence, and asserts, in order: a `heard` event with text
  `hello`, a `turn_ended` event, audio frames with `utterance: 1`, a `said` event whose `heard` is
  `Hi there.` after a `Playback` over the fake context reports `played` for utterance 1; then
  `link.close()` leads the server to log the session `hung_up`.
- [ ] **Step 3: gate** — `npm run test:integration`. **Commit** `test: the TypeScript client
  against the Python server`.

## Task 21 — Examples

**Files:** create `examples/README.md` and one folder per example, each a standalone uv project
like `../engine/examples/*`: `pyproject.toml` (`[tool.uv] package = false`, `rfnry-senses` and
`rfnry-engine` as editable path sources — `../../packages/python`, `../../../engine/packages/python`),
`providers.py` (the only files that name vendors), `main.py` read top to bottom, and for browser
examples `static/index.html` loading `../../packages/typescript/dist/index.js` (served by a
`StaticFiles` mount; `npm run build` first). Keys come from `../../../.env.examples` through
`uv run --env-file`; nothing with a key is ever written in the repository. An example that needs a
microphone also runs headless (its default when no browser is asked for): it synthesizes the
person's words with the speech model at runtime and feeds them through a `Loopback`, paced in real
time, so each example runs once live from a terminal; no third-party audio is committed.

What each vendor can provide, with the keys this workspace has (`ANTHROPIC_API_KEY`,
`GOOGLE_API_KEY`, `OPENAI_API_KEY`, `VOYAGE_API_KEY`, `DECISION_API_KEY`):

| contract | provider in `providers.py` | key | note |
| --- | --- | --- | --- |
| `LiveModel` | `GoogleLive` — Gemini Live (`google-genai`, `client.aio.live.connect`) | `GOOGLE_API_KEY` | `say` sends "Say exactly this, nothing else: …" as a completed turn; `send_text(respond=False)` sends an incomplete client turn; `interrupt(played_ms)` has no truncation call to make and raises (senses logs it) — the vendor cuts its own context when it detects the interruption |
| `TranscriptionModel` | `OpenAIProvider` — streaming realtime transcription: a Realtime transcription session (`wss://api.openai.com/v1/realtime?intent=transcription`, `session.update` with `type: "transcription"`, 24 kHz `audio/pcm`, `server_vad` turn detection); `…input_audio_transcription.delta` → a running `Transcript(final=False)`, `…completed` → `Transcript(final=True)` then `UtteranceEnd()`; the adapter resamples 16 kHz to 24 kHz | `OPENAI_API_KEY` | model `gpt-4o-transcribe`-class with server VAD (`gpt-live-transcribe` streams too but takes no turn detection, so the adapter would have to commit turns itself); the exact model is confirmed on the first live run and named in the commit |
| `SpeechModel` | `GoogleSpeech` — Gemini text-to-speech, one request per sentence, 24 kHz PCM | `GOOGLE_API_KEY` | not streaming within a sentence; senses already speaks sentence by sentence |
| `DecisionModel` (turns) | `TypeSafeProvider`, byte-identical with the engine examples' class | `DECISION_API_KEY` | |
| generation (engine agents) | `AnthropicProvider` / `GoogleProvider`, byte-identical with the engine examples | `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` | Anthropic has no audio models: it is only ever the text mind |

OpenAI provides only transcription here; Gemini keeps the live model and speech. Every provider
class is byte-identical across the folders that carry it (the rule of the engine's examples).

| example | what it shows | vendors |
| --- | --- | --- |
| `barge_in` | offline, no keys: `Loopback` with scripted models; a `say` cut after 1.2 s by the person talking; prints `Said.heard` for `played` at 0.4 s, 1.2 s and never (the clock estimate) | none |
| `cascaded_voice_agent` | the Getting started endpoint for real: FastAPI WebSocket authenticated on the upgrade, `serve`, a cascaded session with turns, an engine agent answering each `TurnEnded`, the browser page from the TS client with captions from `subscribe`; headless by default | OpenAI transcription, Google speech, Anthropic agent, decision turns |
| `live_voice_consult` | a live session whose tools are `engine.tool("core", scope=…, name="consult")`; `ToolCalled` printed; `inform` pushing a fact mid-call | Google live, Anthropic core agent |
| `camera_look` | a camera-only session over WebSocket JPEG at 1 fps; an engine agent with `SensesTools` (`look` only) answers typed questions about what the camera sees | Anthropic agent |
| `two_minds_thread` | live front, engine agent behind through `SensesTools` (`say`/`prompt`/`inform`/`look`), `ThreadTools`, and the `mirror` glue from the docs posting `Heard`/`Said` into a thread | Google live, Anthropic core; **needs rfnry-thread** — the folder lands when thread's `ThreadTools` does |
| `webrtc_voice` | `live_voice_consult` over WebRTC: the offer endpoint, `accept_offer`, the browser on `transport: 'webrtc'` | Google live, Anthropic core |

- [ ] **Step 1:** `barge_in` first (it needs no key), then the others in the table's order. Each
  `main.py` runs with `uv run --env-file ../../../.env.examples main.py` (browser examples print
  the URL to open). Lint each with its own ruff (`uv run ruff format . && uv run ruff check .`).
- [ ] **Step 2:** `examples/README.md` — the table above (without the vendor-capability table's
  keys column), how to run, and which contracts each provider implements.
- [ ] **Step 3: commit** `docs: examples — barge-in, cascaded, live with consult, camera, WebRTC`
  (and `two_minds_thread` in its own commit once rfnry-thread lands).

## Task 22 — The live tier

**Files:** create `packages/python/tests/e2e/conftest.py`, `packages/python/tests/e2e/test_live.py`;
modify `packages/typescript/tests/integration/python-server.test.ts`.

Never run before Tasks 13, 14 and 20 pass offline (engineering rule 6).

- [ ] **Step 1:** `conftest.py` — an `adapters` fixture importing the module named by
  `RFNRY_LIVE_ADAPTERS` (skip without it), as `../engine/packages/python/tests/e2e/conftest.py`
  does. The module exposes `live()`, `transcription()`, `speech()`, `decision()` factories; the
  examples' `providers.py` classes are the implementation (a small `live_adapters.py` beside
  them builds them from the environment).
- [ ] **Step 2:** `test_live.py` (`pytestmark = pytest.mark.live`):
  1. **live** — `Session(Loopback, live=adapters.live())`: `say("Good morning.")` returns a
     `Said` with non-empty `heard`; `prompt("Say one short sentence about the weather.")` returns
     non-empty `text`; the audio frames reached the loopback;
  2. **voice round trip** — `speech()` speaks "A table for two at eight." into a `Loopback`; a
     second, cascaded session with `transcription()` hears those frames played back as the
     person (paced in real time) and reports a `Heard` containing "table" and a `TurnEnded`;
  3. **turns** — with `decision()`, "I would like a table for two at eight." ends the turn with
     `probability > 0.5`; the probability for "I would like" is recorded in the test output, not
     asserted;
  4. **barge-in** — a live `say` of a long paragraph cut by `interrupt()` after 1 s: `Said.heard`
     is a proper prefix of what the model said.
- [ ] **Step 3:** the TypeScript integration test gains a `live` variant (run when
  `RFNRY_LIVE_ADAPTERS` is set) against `serve_for_client.py` on real transcription and speech
  models: the round trip of step 2 through the browser client's WebSocket link.
- [ ] **Step 4:** run `uv run poe test:e2e` and `RFNRY_LIVE_ADAPTERS=… npm run test:integration`
  with the workspace keys; record costs and timings in the commit message. **Commit** `test: the
  live tier`.

## Self-review against the spec

- §1 scope: every stripped item is absent — no handler registry, stores, recorder, codecs,
  telemetry, capability protocols, `TurnDetector`, React packages; the fixture generator is a test.
- §2 family shapes: Task 2, pinned against engine, memory and (when present) thread.
- §3 contracts: Task 3; `say` on the connection and the event names are the corrections recorded
  in the design.
- §4 session: live and cascaded (Tasks 6–9), optional modalities (Task 8's
  `test_each_model_is_optional`, Task 10), every primitive, `say` returning what was heard, a
  streamed `say` (Task 8, its barge-in in Task 9), barge-in, the frame window, attribution (an
  unsolicited response is `triggered_by=None`) — each with a named test.
- §5 events: Task 3 and every session test; `Heard` regardless of an answer (Task 8).
- §6 SensesTools: Task 11, definitions by mode; `say` stays text.
- §7 two minds: docs + `live_voice_consult`, `two_minds_thread`.
- §8 transports: Tasks 12–14; golden fixtures shared (Tasks 12, 16); hello-token authentication
  on both (decision 11); the video path — JPEG frames over WebSocket, a track over WebRTC turned
  into JPEG, both at the client's `fps` (Tasks 14, 18, 19).
- §9 TypeScript client: Tasks 16–19; no framework, no store, no visualizer.
- §10 engine addition: referenced, planned in the engine repository.
- §11 layout, docs test, examples: Tasks 1, 15, 21. §12 tests: three tiers, Tasks 1–22.

**Checked while writing.** The Python code of Tasks 2–13 was assembled from this plan exactly as
written (the family blocks cut from the engine checkout, the session edits applied in task order)
and run: the 81 unit tests of those tasks pass three runs in a row with `RuntimeWarning` as an
error, `mypy --strict` is clean, and `ruff check` is clean after `--fix` sorts imports. The family
cut markers match between rfnry-engine and rfnry-memory for every block listed against both. The
TypeScript `wire.ts` and `playback.ts` with their tests typecheck under the strict settings of
Task 16 and pass against the fixtures the Python test wrote. The streamed-`say` additions to Tasks
8 and 9 (decision 10) were written after that run and have not been run. Not run: the WebSocket and WebRTC
integration tests, the rest of the TypeScript client, the docs-examples harness, the examples and
the live tier.
