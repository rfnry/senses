# rfnry-senses

rfnry-senses provides vision and speech interfaces for human-machine interaction.

It is the microphone, camera and voice between a person and a model — a model that hears and speaks
natively, or transcription and speech models around any text model — and only that: it never decides
what to say. The assistant is a participant, not a responder: it can speak first, look when it has a
reason to, and be interrupted.

A **session** connects a person's microphone and camera to a model, and the model's voice back to
the person. It runs one of two ways. **Live**: a model that hears and speaks natively runs inside
the session; senses pipes the person's audio and camera frames in, the model's voice out, and runs
the model's tool calls. **Cascaded**: a transcription model hears, a speech model speaks, and any
text model — an engine agent, a plain model call, your own loop — decides what to say in between.
Either way the assistant is a first-class presence: it can speak first, look when it has a reason
to, and be interrupted like anyone in a conversation. Acting first and answering are the same call.

A session is input and output only. It hears, sees, speaks and reports what happened; it never
decides what to say. A framework-free TypeScript client captures the microphone and camera in a
browser and plays the voice back.

Every public name, with its signature, is in [the API reference](api.md).

## Philosophy

**The senses, not the mind.** The loop that decides — your code, an engine agent, a live model's
own reasoning — lives outside the session. What senses owns is what no model provides for itself:
moving audio and frames across a network in real time, cutting the voice off when the person talks,
knowing what they actually heard, and saying who caused what.

**The model decides when the person is done.** When a turn ends is the transcription or live
model's call — its own turn detection, which improves as models do. Senses does not second-guess it.

**What was heard is what counts.** When the person cuts in, playback stops, the client reports how
much it played, and both the session's report and the live model's context are cut to it. `say`
returns what got through.

**Models are named for what they produce.** Live, transcription, speech. Each is optional: a feature says which it needs and the rest degrade cleanly. A camera-only session has no
model at all.

**Attribution is declared, never assumed.** Every action names who caused it. When a live model
speaks on its own, or answers what it heard, nothing is invented: the cause is empty.

**The library runs; it does not take over.** You bring the models, the authentication, the
endpoint, and any record you want to keep. There is no handler registry, no background loop of
ours in your application, no storage. Every verb is a function you call.

**Tenancy by construction.** Every session belongs to the scope your endpoint names, every event
carries it, and nothing crosses sessions.

## Models

The library speaks to models through protocols. Any object with the right methods is a model;
adapters to vendors are yours to write and never ship here.

```python
class LiveModel(Protocol):
    async def open(self, *, tools: list[ToolSpec], instructions: str | None) -> LiveConnection: ...

class LiveConnection(Protocol):
    duplex: bool

    async def send_audio(self, frame: AudioFrame) -> None: ...
    async def send_image(self, image: Image) -> None: ...
    async def send_text(self, text: str, *, respond: bool) -> str | None: ...
    async def say(self, text: str) -> str | None: ...
    async def interrupt(self, played_ms: int, *, heard: str, response: str | None) -> None: ...
    async def submit_result(self, call_id: str, result: Content, is_error: bool, *, respond: bool) -> None: ...
    def events(self) -> AsyncIterator[LiveEvent]: ...
    async def close(self) -> None: ...

class TranscriptionModel(Protocol):
    def stream(self) -> Transcription: ...

class Transcription(Protocol):
    async def feed(self, frame: AudioFrame) -> None: ...
    def events(self) -> AsyncIterator[Transcript | UtteranceEnd]: ...
    async def close(self) -> None: ...

class SpeechModel(Protocol):
    def speak(self, text: str) -> AsyncIterator[AudioFrame]: ...
```

Audio is `AudioFrame(data, sample_rate, channels)`: 16-bit little-endian PCM, one or two channels;
`duration_ms` is how long it plays. Frames carry their rate, so an adapter whose vendor wants
another rate converts in the adapter.
Images are the family's `Image` part — frames travel as JPEG.

A live connection reports what happens as `LiveEvent`s. On a turn-based connection every response
the model gives is bracketed by `LiveStarted` and `LiveDone`, and an adapter whose vendor has no such
markers makes them; a duplex connection can leave them out:

```python
LiveAudio(frame=frame)                                   # the model's voice
LiveHeard(text="a table for", final=False)                # the person, as the model hears them
LiveSaid(text="Booked for eight. ")                       # the model's own words, as it speaks them
LiveStarted(id="r1", requested=True), LiveDone()          # one response: its id, and whether you asked for it
LiveToolCall(call_id="c1", name="consult", args={"input": "Is the terrace open?"})
LiveToolCancelled(call_ids=["c1"])                        # the model withdrew the call
LiveInterrupted()                                         # the model heard the person cut in
LiveFailed(message="connection lost")                     # the connection is over
```

`send_text(text, respond=True)` asks the model to answer now, in its own words; `respond=False`
adds context without a reply, and `submit_result` takes the same switch for a tool's answer.
`say(text)` speaks exactly those words. The text `send_text` and `say` carry is your code addressing
the model — an instruction, a fact, a line to speak — whoever caused it: `triggered_by` names the
cause, never a speaker, and the person reaches the model only as audio. An adapter sends it on the
vendor's operator channel where there is one. Senses sends one at a time and never cancels the send
of a `say` or `prompt` whose caller gave up: it runs to its end, so one that never returns holds
every later `say`, `prompt` and `inform` until `close`, and an adapter bounds its own send. An
`inform` is sent in its caller's own task, so its send goes when the caller does.

`interrupt(played_ms, heard=, response=)` stops what the model is saying and cuts its context to
what the person heard: the response the cut landed in keeps `heard`, the first `played_ms` of its
audio, and nothing the model said after it was heard. `response` is the `id` that response's
`LiveStarted` carried, or `None`; an adapter whose vendor cannot cut its context by time tells the
model `heard` in words. An adapter that can tell whether a response answers its own request says so
with `requested`, and names the response with the vendor's `id`; one that cannot leaves both `None`.
A response to a tool result or to the person answers no `say` or `prompt`: it is `requested=False`.

An adapter that can name the request a response answers returns a tag from `send_text(respond=True)`
and `say` — the vendor's id for the request, or one of its own — and puts it on that response:
`LiveStarted(requested=tag)`. Without tags, responses take their requests in order, and the order
slips when the vendor answers a request senses ended after its send began — by `mute`, the person
cutting in, or a caller that gave up while it was being sent: the vendor's late answer is credited
to the next request. A tag removes that limit: the late answer is cut before a word of it plays,
and the next request keeps its own. A tag goes on its response only once the call that returned it
has returned: a `LiveStarted` that carries it sooner reads as the vendor's own, senses logs a
warning, and the request waits for an answer that already came until the person cuts in, `mute` or
`close`.

A connection declares `duplex`, which says how its model converses: `False` for a model that takes
turns, `True` for one that listens while it speaks and decides when to talk. It has no default, and
`start` reads it once. A duplex connection takes input at any time: `say`, `prompt` and `inform` go
at once, one at a time. `say` and `prompt` wait for a cut to finish; `inform` waits for nothing, as on
a turn-based connection. A `send_text(respond=True)` that returns a tag is paired with the response carrying it, as above. One that returns `None` cannot be
paired, because the model's reply cannot be told from the rest of its speech: `prompt` returns `None`
once it is sent, and senses reports `Informed`, so the action still leaves an event naming its cause.
`say` must return a tag, and the adapter brackets the speech with `LiveStarted(requested=tag)` and
`LiveDone` and reports its words as `LiveSaid`, so `Said` is exact: a `say` that returns `None` raises
`ModelError`, and an adapter with no way to speak exact words raises from `say`.

On a duplex connection an adapter sends the voice as it comes, and the session makes an utterance
from each stretch of voice. `LiveAudio` with no response open starts one, with `triggered_by=None`.
It ends `voice_gap` seconds after its last voice has played; `voice_gap` is a `Session` argument, 1.0
by default. Audio can arrive ahead of playback, and a model may stream continuously, sending silence
as zeros, so audio arriving says nothing about whether it is speaking. Only the session can tell: it
reads voice from the samples, and what has played from its own count. A frame of pure silence is not
voice: it starts no stretch and does not extend one, though once a stretch has voice its silence is
still sent, to keep the timing of its pauses. Each stretch reports `Speaking`, `Silent` and `Said` as
a response does, and `quiet` returns between stretches. A `LiveSaid` that comes after
its stretch went quiet but before its `Said` still joins it, at its end, and one with no utterance
open starts a stretch, so a model's words are never lost.

An adapter can still bracket speech itself. `LiveStarted` ends any open stretch and opens a response
as usual, and audio belongs to that response until `LiveDone`, cut or not. A `LiveDone` during a
stretch ends the stretch, and one with nothing open is ignored, so a model that marks only where
speech ends fits too. This is how `say` works, how a model with its own markers fits, and how a tag
claims a `prompt`. A boundary is knowledge about the model, so an adapter that knows better than
a gap in the voice brackets the speech. Silence inside a response is sent as any frame is.

The person's final `LiveHeard` comes before the `LiveToolCall` it led to, so a tool reads what they
said. `LiveFailed` means the connection is over; a vendor error that ends nothing is the adapter's
to absorb. A connection implements every method; a vendor that cannot do one raises from its
adapter, and the verb that needed it raises to its caller. `duplex` says how a model converses, not
what it can do, so there is no capability matrix: every other limit raises from the adapter.

A transcription stream reports `Transcript(text, final)` — the running words of the current
segment, then the segment committed — and `UtteranceEnd()` when the person stops talking.

Turn completion has no protocol of its own: the turn ends when the transcription model reports
`UtteranceEnd`. How eagerly it decides the person is done — a silence timer, or a model that judges
the words — is the adapter's configuration.

Fakes are enough to run everything in tests:

```python
class Script:
    def __init__(self, *lines): self.lines = lines
    def stream(self): return self
    async def feed(self, frame): pass
    async def events(self):
        for line in self.lines:
            yield Transcript(text=line, final=True)
            yield UtteranceEnd()
    async def close(self): pass

class Tone:
    async def speak(self, text):
        for _ in text.split():
            yield AudioFrame(data=bytes(6400), sample_rate=16000, channels=1)
```

A scripted live model is the same idea: `open` returns a connection whose `events()` yields a
script and whose other methods record what they were given.

## Getting started

```python
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
```

Authentication is yours, on the upgrade: your framework knows who opened the socket — a cookie, a
header, a token — before senses sees it, and `serve` takes the person and their scope from you. It
reads the browser's hello, which carries only the protocol version, and returns the transport. The
session hears the person, emits a `TurnEnded` when they finish, and your loop — here an engine
agent — decides what to say. `Socket` adapts FastAPI's socket in a few lines
([Transports](#transports)). In the browser:

```ts
const senses = await connect({ url: 'wss://example.com/senses', transport: 'websocket' })
```

The Python package needs Python 3.14 or later.

## Core features

### Live sessions

```python
session = Session(link, live=live, instructions="You are the concierge at Acme.")
await session.start(tools=tools)
async with session:
    async for event in session.events():
        print(event)
```

The model runs inside its vendor's session. `start` opens it with the definitions of `tools` and the
instructions, sends it the person's audio, and plays its voice. The tools are fixed from then on:
vendors take tool definitions when their session opens. They are given to `start`, not to
`Session`, so a tool can hold the session it runs in ([Two minds](#two-minds)). `async with` closes
the session at the end, and starts it without tools if it has not started; when that start fails, it
closes the session as `failed`, so the client is told and the transport closes, and raises the error.
A start cancelled there, by a deadline or a handler torn down, closes it as `closed`. A `start` you
call yourself that raises before it sends `ready` leaves the session open, to be tried again: `close`
it when you will not. A transport that fails the `ready` closes the session as `failed`, and there
is nothing left to try. When the model calls a
tool, senses dispatches the call through `tools` — any family `Tools`: an engine agent as a tool, rfnry-thread's
`ThreadTools`, rfnry-memory's `MemoryTools`, your own — and submits the result. A string or a list of
parts goes back as it is, a dict as JSON; an exception goes back as an error the model reads,
`InputError: tools: unknown tool 'ghost'`. Each call is bounded by `tool_timeout` (30 s by default,
`None` for none) and reported as a `ToolCalled` event. Every call the model makes is answered
exactly once, even when the person interrupts the response that made it — unless the model
withdraws it with `LiveToolCancelled`: then the call is stopped and nothing goes back. A tool whose result should
start no reply — ending the call, handing it over — is named in `start(tools=tools, inform=["end_call"])`:
its result goes back the way `inform` speaks, as something the model knows, and the model is not
asked to say more.

Your code stops the tool calls still running with `cancel`, as `interrupt` stops the voice — the
person moved on, the call is ending. Each one is answered `"cancelled"`, without asking for a reply,
and `Cancelled` names who stopped them; when to cancel is yours:

```python
cancelled = await session.cancel(triggered_by=host)   # Cancelled(tools=[...]), or None when none ran
```

Two more verbs reach a live model. `prompt` asks it to speak now, in its own words, and returns what
it said; `inform` gives it something to know without a reply — the fact that arrived after it was
connected, which its connect-time instructions could not hold:

```python
said = await session.prompt("Tell the guest their table is ready.", triggered_by=host)
await session.inform("The terrace closes at nine tonight.", triggered_by=host)
```

`say` speaks exactly the given words, through the same voice; given a stream of text, it joins the
chunks and sends them once the stream ends, since a live model's verbatim speech needs the whole
text. A cascaded session has no model to prompt or inform; both are a `ConfigurationError` there, and so
is `start(tools=)` — without a live model, tools belong to your own loop.

A live model gives one response at a time, so `say` and `prompt` take turns: each goes to the model
once no response is open and none is still owed to the last one sent — after the model's own reply,
and after the response to the request before. The person cutting in, or `mute`, ends them, sent or
not; `interrupt` leaves them, and they go once the cut response ends. One still waiting ends at
once; one whose send is under way returns when its send does, so a vendor's refusal still reaches
its caller. A `say` or `prompt` given up on before its send returns ends there, and a send under way
still runs to its end before the next goes; one given up on after that is still answered. A request
ended after its send began may still be answered by the vendor; how senses tells that answer from
the next request's is in
[Models](#models). A vendor that never answers a request holds the ones behind it until the person
cuts in, `mute` or `close`. `inform` waits for no response, only for a send under way.
A `say` or `prompt` awaited inside a tool body waits behind the response that made the call, and on a
vendor that holds that response open until the result, the tool times out first: spawn it there.

On a `duplex` connection `say` and `prompt` are held behind no speech and no answer, only behind a
cut that has not finished, and they still go one at a time. `inform` is as it is on any connection. A
`prompt` the adapter cannot pair returns `None` once it is sent ([Models](#models)).

### Cascaded sessions

```python
async with Session(link, transcription=stt, speech=tts) as session:
    async for event in session.events():
        if isinstance(event, TurnEnded):
            run = await engine.run("concierge", scope=session.scope, input=event.text)
            await session.say(run.reply, triggered_by=event.speaker)
```

Senses feeds the person's audio to a transcription stream and reports every `Heard`, partial and
final, whether or not anyone answers — the person's words are never lost because the loop chose
silence. When the stream reports `UtteranceEnd`, the utterance's words become a turn and senses emits
`TurnEnded`.

`say` synthesizes the text sentence by sentence, so the first sentence plays while the rest is
still being made. A sentence ends at `. ! ? ؟ ।` followed by whitespace, or at `。 ！ ？` with or
without it; a chunk ending in a bare `.` waits for the next, so `3.` then `5 apples. ` is one
sentence. Calls queue: a second `say` speaks after the first, a person talking over the voice
drops the queued ones before the cut waits on the far end, and one given up on while it waits is
never spoken. A stream that is dropped this way is closed unread. Each model is optional — a
session with only `speech=` can announce and never listens; one with only `transcription=` listens
and never speaks, and `say` there is a `ConfigurationError`.

`say` also takes an `AsyncIterator[str]` — a reply still being written. Senses cuts it into
sentences as the chunks arrive, so the first sentence plays while the model is still writing the
rest; when the person cuts in, senses stops reading the stream and closes it. With rfnry-engine, a
run's `on_event` hands over its text deltas:

```python
async def reply(text, scope):
    chunks = asyncio.Queue()

    async def forward(event):
        if isinstance(event, TextDelta):
            chunks.put_nowait(event.text)

    async def run():
        try:
            await engine.run("concierge", scope=scope, input=text, on_event=forward)
        finally:
            chunks.put_nowait(None)

    running = asyncio.create_task(run())
    while (chunk := await chunks.get()) is not None:
        yield chunk
    await running

said = await session.say(reply("Is the terrace open?", session.scope), triggered_by=host)
```

`Said.text` is what the voice was given and `Said.heard` what the person heard, as with a string. A
stream that yields no text speaks nothing and returns an empty `Said`.

### Speaking and being interrupted

`say` and `prompt` return when the utterance ends, with what the person heard:

```python
said = await session.say("Your table is ready. Shall I order the usual?", triggered_by=host)
said.heard          # "Your table is ready. "
said.interrupted    # True
```

When the person starts talking over the assistant — a cascaded transcript arrives, or the live
model reports `LiveInterrupted` — senses stops the voice: queued audio is dropped, the client is told
to stop and fades the voice out over 60 ms rather than cutting it mid-sample, and it answers with
how many milliseconds it played. Senses cuts the utterance's words to
that point — whole sentences in a cascaded session, the model's own transcript in a live one — emits
`Interrupted`, and, in a live session, calls `interrupt` once so the model's context holds only
what was heard. Every utterance is numbered, and audio from an interrupted one — a sentence that
finished synthesizing a moment too late, a frame already on the wire — is dropped, on the server and
in the client. Queued `say` and `prompt` calls end too, as the person cuts in and before `Interrupted`
is reported: the person is talking, and they return `Said(heard="", interrupted=True)`. On a live
model a call that arrives during the cut ends the same way, and none of them reaches the model. A
cascaded `say` that arrives then speaks if it reaches the voice before the cut ends, and ends the
same way if it is still queued.

A live model can start its next response while the last one is still playing. The client plays them
back to back, one stretch of voice: `Speaking` when it starts after quiet, `Silent` when all of it
has played or been cut, and never between the two. Each utterance still ends on its own, when the
client reports how much of it played, so the first one's `Said` can come after the second has begun.
A cut stops everything still playing: the utterance it lands in is cut where it was, the ones queued
behind it were not heard at all, and `Interrupted.heard` is what the person heard of the utterance
it landed in — the response `interrupt` names to the model. `Said` events come oldest first, and `Silent` comes right before the `Said` of the
utterance that left the voice quiet.

If the client never reports, senses estimates from its own clock — the audio sent, capped by the
time since it began to play, after anything queued ahead of it — after a quarter of a second, so an
interruption never waits on a silent client. A live model whose vendor cannot cut its context raises from `interrupt`; senses logs that
as a warning and goes on — the voice is already stopped.

On a duplex connection a cut ends a stretch of voice as it ends a response: `interrupt`, `mute` and
`LiveInterrupted` drop what is queued and cut the utterance to what was heard, and `interrupt` is
called with `response=None`. Stopping the model is `connection.interrupt`'s job, and only the adapter
knows how its vendor behaves, so after a cut an adapter silences what its model still sends until the
model has stopped. Audio that arrives during an `interrupt` or `mute` is dropped. An adapter that sends
`LiveInterrupted` silences its model from that moment: senses reads no event until that cut ends, so
what queued behind it would be new voice. Any voice after a cut is a new stretch, so the answer to a
barge-in or a `prompt` is never swallowed, and while muted audio starts no stretch.

A model that gives no barge-in event leaves the session nothing to do: the voice plays on, and the
transport still counts what played. An adapter sends `LiveInterrupted` only on a clear signal, never
on a backchannel such as "mm-hm". How much queued audio plays over the person is the model's to
offer.

Your code can cut the voice too, or close the gate on it:

```python
cut = await session.interrupt(triggered_by=host)     # Interrupted(heard=...), or None when silent
await session.mute(triggered_by=host)                # nothing reaches the person
await session.unmute(triggered_by=host)
```

`interrupt` stops what is playing and cuts it to what the person heard, and ends nothing else:
unlike the person cutting in, it leaves the `say` and `prompt` calls queued behind it, and they play
next. `mute` is the output gate: while muted, no audio reaches the person. It stops and cuts
anything being said, and it ends the calls queued behind it with nothing heard. A `say` or `prompt`
while muted returns at once with nothing heard, and a response a live model starts while muted is
cut before a word of it plays. The session still hears and sees. A product's floor policy or mandate
gate is built on these two verbs.

A cut finishes whatever happens to the code that asked for it. An `interrupt` or `mute` given up on
— a timeout, a cancelled task, a tool call the model withdrew — still waits for the client's report,
tells the model what was heard, and ends each utterance it cut. If the session closes during a cut,
the calls it cut raise `TransportError`, as every `say` and `prompt` still waiting does. That
includes a `stop` the transport fails: the session closes as `failed`, and no `Interrupted` is
reported, since nothing says what the person heard.

`quiet` waits until the voice has nothing left to say — no response open, no `say` or `prompt`
waiting, every utterance played or cut — so a call can end after its last words were heard:

```python
await session.quiet()
await session.close()
```

It does not wait for a tool still running, and from inside a tool body it can wait on the response
that is waiting for that tool: spawn it there instead.

`close(triggered_by=)` names who ended the call on `Closed`; a hang-up names the person, and a failure or the end of `async with` names no one.

`close` runs to the end once it begins. It fails every `say` and `prompt` still waiting first and
lets `quiet` return, then waits at most 2 seconds for what it stopped and at most 2 seconds for the
final `Closed` send, so a tool that ignores being cancelled or a send that hangs delays it and
cannot hold it. A task that has not stopped by then is left running and logged, a `Closed` send
that has not finished is cancelled and logged, and the model connections and the transport close
all the same. Subscribers have `Closed` before that send goes. A `cancel` still waits for the tools
it stopped, and an `interrupt` or `mute` for a `stop` send that will not end, so those calls can
outlast the close. A caller cancelled while it waits — a deadline, a
cancel scope, a handler torn down — stops waiting, but the model connections and the transport still
close and `Closed` is still reported; a second `close` returns once the first is done. A tool that ends
the call by calling `close` is stopped with the other tool calls: its `close` returns at once, as
does one a tool calls while the close is stopping it, and nothing more goes to the model.

### Seeing

```python
async with Session(link, video=True) as session:
    parts = await session.look(frames=2, triggered_by=host)
    run = await engine.run("inspector", scope=session.scope, input=[Text(text="Is the valve open?"), *parts])
```

With `video=True` the session keeps the camera's frames for a short window — `window` seconds (10)
and `window_bytes` (8 MB), whichever is smaller — and drops the oldest by its own clock, so a client
that stalls leaves an empty window rather than a stale picture. Senses never samples, describes or
transcodes a frame; the client sends JPEG at the rate models consume (1 frame per second by
default). In a live session every frame also streams to the model; when the model falls behind it
gets the newest frame and the ones in between are skipped.

That stream is how a live model sees as video. Today's live models take video as a sequence of
image frames, so seeing as video is frames arriving continuously at the rate you set — the client's
`camera.fps`, changed while it runs with `setFps`. Over a WebSocket each frame is a JPEG: light, and ideal at 1–2 frames a second. Over
WebRTC the camera is a real video track, with inter-frame compression and adaptive bitrate — the
right path when your product raises the frame rate; the `webrtc` extra turns it back into JPEG
frames on the server. A model that takes true video input would add `send_video` to the live
connection, alongside what is there.

The frames can be the person's screen instead of their camera — "where do I click to save?" — and
the session does not tell them apart: a frame is a frame, and a model that sees a screen knows it is
looking at one. The browser client chooses the source ([The TypeScript client](#the-typescript-client)),
one at a time. A shared screen goes to the model you chose, as the camera does; the browser asks the
person which screen or window to share, and shows that it is being shared.

A live model that takes no images (its `send_image` raises) is logged once and no longer sent frames; the window, `look` and the session go on. `look` returns the newest frames, oldest first, each an `Image` followed by the `Text` of when it was
taken — `frame at 12:03:04.200` — ready to hand any model that sees. With nothing in the window it
raises `NotFoundError`. The model sees the frame itself; there is no vision model in between. Without
`video=True`, `look` is a `ConfigurationError` and frames are dropped on arrival.

### Events

`events()` yields everything the session reports, from the moment it is called; call it twice and
both streams see every event, and a stream you stop holding is let go. Each event carries the
session's `scope` and the time `at`:

| event | fields | when |
| --- | --- | --- |
| `Heard` | `text, final, speaker` | the person's words, partial and final |
| `TurnEnded` | `text, speaker` | a cascaded turn ended |
| `Said` | `text, heard, interrupted, triggered_by` | an utterance ended: once each, when what was heard of it is final |
| `Interrupted` | `heard, triggered_by` | the voice was cut |
| `Speaking` / `Silent` | — | the voice started after quiet / went quiet: all of it played, or was cut |
| `Looked` | `frames, triggered_by` | someone looked through the camera |
| `Muted` | `muted, triggered_by` | the output gate closed or opened |
| `Informed` | `text, triggered_by` | a live model was told something |
| `ToolCalled` | `name, input, output, is_error, cancelled` | a live model's tool call ran, or was stopped |
| `Cancelled` | `tools, triggered_by` | `cancel` stopped the tool calls running |
| `Closed` | `reason, message, triggered_by` | the session ended: `closed`, `hung_up` or `failed`; `triggered_by` names who: the cause given to `close`, the person on a hang-up, no one on a failure |

```python
async for event in session.events():
    match event:
        case Heard(final=True, text=text, speaker=speaker):
            print(f"{speaker.name}: {text}")
        case Said(heard=heard, interrupted=True):
            print(f"cut after: {heard}")
        case Closed(reason=reason):
            print(reason)
```

Every action leaves an event naming its cause. `Said.triggered_by` is the identity passed to the
`say` or `prompt` that started the utterance. A live model's responses take their causes in order:
each `say` and `prompt` joins a queue, each response the model starts takes the oldest, and a
response started with the queue empty — the model answering what it heard, or speaking on its own —
has `triggered_by=None`. So does one the adapter reports with `LiveStarted(requested=False)`, which
the vendor started on its own while a request waited; the request keeps its place for the next. A
response that carries a request's tag takes that request's cause instead, and one whose tag senses
never handed out is the vendor's own. A `prompt` that cannot be paired has no response to carry its
cause, so `Informed` carries it. The client receives every event except `Informed`,
`ToolCalled` and `Cancelled`, which are between you and the model.

Every identity the library takes — `serve`, `accept_offer`, `Loopback`, a transport's `identity`,
each verb's `triggered_by=`, `SensesTools(triggered_by=)` — is an `IdentityLike`, read by its
fields: any object with `role`, `id`, `name` and `metadata` will do, such as rfnry-thread's
`Identity`, which has the same source. The values are validated as strictly as an `Identity`'s;
one that does not fit is an `InputError`.

### Two minds

A live model in front for the conversation, an engine agent behind for the thinking:

```python
session = Session(link, live=live, video=True)
consult = engine.tool("core", scope=session.scope, name="consult", description="Ask the reasoning mind.", tools=SensesTools(session, triggered_by=assistant).only("look", "inform"))
await session.start(tools=consult + ThreadTools(client, thread_id=thread, triggered_by=ana))
```

The live model asks the engine agent with `consult` — rfnry-engine's `engine.tool`, an agent as a
tool. The engine agent reaches the room through `SensesTools`, built before the session starts: it
needs the session only when a tool is called, and the session needs `consult` only when it starts,
so the circle closes in three lines. Inside `consult` the agent gets `look`, which shows it the
camera directly, and `inform`, which keeps the live model current; its reply is what the voice says.
It gets no `prompt` or `say` there: the voice is waiting on that call, and a `prompt` or `say` from
inside it waits its turn behind the response that made the call. Those two are for the agent acting on its own, outside a
consult: the full `SensesTools`, passed to its own `engine.run`, where `say` speaks its words
verbatim and `prompt` has the live voice say them in its own words. The transcript both minds read
is the thread, mirrored from `events()` ([Storage](#storage)). A persona, a floor policy or a
mandate gate stays in your product, built on `mute`, `unmute`, `interrupt` and tools of your own.

A live model that hands work off, rather than calling a tool by name, fits the same shape. The
adapter reports the hand-off as a `LiveToolCall` to `consult`; when the vendor sends no words with
it, the adapter builds the call's input from the conversation it already reads. The result goes back
through `submit_result`, spoken with `respond=True` and silent with `respond=False`, and a hand-off
can happen many times in a call. The voice stays on the line while the other mind works, and the
person can keep talking to it. Covering a slow tool is the model's job, steered by its instructions.

There are only ever two minds, and each keeps its own instructions. The voice needs its role even
when the other mind does the thinking: `instructions` reach `LiveModel.open`, and a voice given none
falls back to its vendor's defaults and talks as a generic assistant. What a vendor requires around
that role is the adapter's to add, so your role text stays neutral.

Inside a call to a duplex model, `prompt` and `say` no longer wait behind the response, so they go at
once from a tool body too.

### Three ways to use the screen

A shared screen or camera fits the family in three shapes. The engine agent is the same in all
three; only the session around it changes.

**Two minds.** A live model watches continuously, frames streaming at `fps`, and holds the
conversation; for anything that needs thought it asks the engine agent through `consult`. The agent
looks at the screen itself with `look` from its `SensesTools` — the same current frame, as an image,
never the live model's description of it — and its reply is what the live voice says; `inform`
keeps the voice current. Best for fluid help while the person works; the live model is billed for
every frame it watches.

**Cascaded, one mind.** Transcription, then an engine agent on a strong model, then speech; the
agent calls `look` when it needs to see. Turns take seconds and nothing watches between them, but
it is one model with one set of rules, every step is recorded by the engine's refinery, and frames
cost only when they are looked at. Best for step-by-step guidance.

**Chat plus screen.** A video-only session — no audio models — beside an rfnry-thread thread: the
person types, the engine agent answers in the thread and calls `look` on the shared screen. Best for
support and internal tools, where people already work in a chat:

```python
async with Session(link, video=True) as screen:
    run = await engine.run("support", scope=screen.scope, input="Where do I click to save?", tools=SensesTools(screen, triggered_by=assistant))
```

| shape | live feel | depth | cost | moving parts |
| --- | --- | --- | --- | --- |
| two minds | fluid, talks while it watches | the agent's, on request | every frame streamed | live model, engine agent, `consult` |
| one mind | turn by turn, seconds a turn | one strong model throughout | frames only when looked at | transcription, engine agent, speech |
| chat plus screen | typed, at chat pace | one strong model throughout | frames only when looked at | thread, engine agent, video-only session |

### Transports

A session runs on a `Transport`. Two ship: a WebSocket, in the core, and WebRTC, with the `webrtc`
extra. The transport is your choice per endpoint, never negotiated.

`serve(connection, identity=, scope=)` takes any object with four methods — `receive()` returning
text or bytes, `send_text`, `send_bytes`, `close` — and the person and scope your endpoint
authenticated. It reads the hello, checks its protocol version, and returns the transport, or
`None` after telling the client why it was refused; a connection that drops before its hello gets
`None` too, with no error frame. Senses never sees a credential: who opened the
socket is decided by your framework on the upgrade, as in rfnry-thread. Authenticating a WebSocket
is yours, and a short checklist covers it:

- Serve it over `wss://` only.
- If you authenticate with cookies, check the `Origin` header on the upgrade against your own
  origins. Browsers send cookies on a cross-site upgrade and WebSockets have no CORS, so without
  the check any site the person visits can open the socket as them (cross-site WebSocket
  hijacking).
- Prefer short-lived tokens: a browser cannot set headers on a WebSocket, so a token rides as a
  subprotocol (`connect({ protocols: ['bearer', token] })`, accepted with
  `ws.accept(subprotocol="bearer")`) or in a cookie.

FastAPI's socket adapts like this:

```python
class Socket:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.send_text = ws.send_text
        self.send_bytes = ws.send_bytes
        self.close = ws.close

    async def receive(self) -> str | bytes:
        message = await self.ws.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        return message["text"] if message.get("text") is not None else message["bytes"]
```

The server's liveness is the `Connection`'s: senses sends no keepalive of its own, so a client that
stalls without closing ends the session only when the adapter's ping does (the WebSocket server's
ping, for one), and `receive()` then raises or returns nothing more.

Media has its own socket, apart from any thread socket. Audio and camera frames are binary, each
behind a small header; control frames are JSON: `hello` (the protocol version) and `played` from
the client; `ready`, `speaking`, `silent`, `stop`, `event` and `error` from the server. `silent`
says an utterance has all been sent; `stop` names the newest utterance spoken and stops every one
through it. Over WebRTC, `speaking` and `silent` also carry `sent_ms`: the milliseconds of voice this
connection had handed to the sender when the frame left the queue, which for `speaking` is where the
utterance's first frame starts and for `silent` where its last frame ends, always a multiple of 20.
Over a WebSocket they do not. The client answers each utterance once, with one `played`, when it has
played all of it or been stopped. The frames are pinned by golden fixtures both clients' tests read.

WebRTC is the transport for browser voice: Opus bandwidth, loss concealment, and the echo
cancellation that lets the person interrupt over speakers. One peer connection carries the audio,
an optional camera track at the client's `fps` and a data channel for the control frames. You host
the signalling endpoint — your own HTTP endpoint, authenticated like any other; the client posts
its offer to it, and `accept_offer` answers it for the person and scope you name:

```python
tasks: set[asyncio.Task[None]] = set()


async def converse(link: WebRTCTransport) -> None:
    if not await link.greeted():
        return
    async with Session(link, transcription=stt, speech=tts) as session:
        async for event in session.events():
            if isinstance(event, TurnEnded):
                run = await engine.run("concierge", scope=session.scope, input=event.text)
                await session.say(run.reply, triggered_by=event.speaker)


@app.post("/senses/offer")
async def offer(body: dict, user: User = Depends(signed_in)) -> dict:
    link = await accept_offer(body["sdp"], identity=user.identity, scope=user.org)
    task = asyncio.create_task(converse(link))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return {"sdp": link.answer}
```

The server sends every utterance whole, at any rate, as 48 kHz mono with its last 20 ms frame padded with silence. Its control frames wait for the client's hello: a refused hello gets only an `error`, and no `ready`. `speaking` and `silent` leave with the utterance's first and last audio, stamped with `sent_ms` as they leave; a `silent` that comes after its audio has left keeps the stamp of its last frame, and a `silent` for an utterance a cut took carries the count when it is sent.

`accept_offer` lives in `rfnry_senses.webrtc` and returns a `WebRTCTransport`. The offer is answered
before the client can say hello, so `converse` waits for it: `greeted` is what `serve` is to a
WebSocket, `True` once the hello is accepted, and `False`, with the transport closed, when it is
refused or the connection ends first. Then no session opens, and no live model opens for a client
that is turned away. A peer that connects and never says hello keeps `greeted` waiting until it
leaves; to stop waiting sooner, put a timeout on it and `close` the link. The endpoint keeps each
conversation's task in `tasks` until it ends, since the event loop holds a task only weakly and one
nobody references can be collected mid-conversation. The inbound audio is resampled to
`sample_rate` (16 kHz) and camera frames are encoded to JPEG at the client's rate, inside the extra.
`ice_servers=` takes STUN and TURN servers in the shape of the browser's `iceServers`.

`Loopback` is an in-process transport for tests — and for anything that is not a browser:

```python
link = Loopback(Identity(role="user", id="ana", name="Ana"), scope="acme")
async with Session(link, transcription=Script("A table for two."), speech=Tone()) as session:
    await link.speak(AudioFrame(data=bytes(640), sample_rate=16000, channels=1))
    said = await session.say("For two, at eight.", triggered_by=link.identity)
    said.heard, len(link.audio)
```

`speak` and `show(jpeg)` are the person's microphone and camera, and `hang_up()` is them leaving.
It records the audio (`link.audio`) and control frames (`link.controls`) the person would receive,
and answers each utterance once, as the browser does — on its `silent`, or on a `stop` through it —
with a `Played`: all of it by default, what `played=` says, or nothing with `answer=False`. A telephone line, a meeting bot's bridge or
a robot's microphone is a `Transport` of your own: an `identity`, a `scope`, `inbound()` yielding
`AudioFrame`s, JPEG `Image`s and `Played` reports, `send_audio`, `send_control`, `clear_audio` and
`close`. A send method does not raise: a far end that is gone ends `inbound()`, and a send the session makes that raises (audio, the `speaking`, `silent`, `stop` and `ready` controls, `clear_audio`) closes the session as `failed` with a `TransportError`; the events it reports to the client are best effort, and `inbound()` ending is how a gone far end is seen. It answers each utterance once with a `Played`, as the client does; a far end that reports
progress as it plays is counted by the transport, which answers at once. `clear_audio` drops only
what the transport has not sent yet: a far end that buffers audio is told to flush when `stop`
arrives.

### The TypeScript client

`@rfnry/senses` is framework-free and has no dependencies:

```ts
import { connect } from '@rfnry/senses'

const senses = await connect({ url: 'wss://example.com/senses', transport: 'websocket', camera: { fps: 1 } })
const unsubscribe = senses.subscribe((event) => {
  if (event.kind === 'heard' && event.final) show(event.speaker, event.text)
  if (event.kind === 'said') show('assistant', event.heard)
})
senses.setFps(4)
senses.mute()
senses.pauseCamera()
await senses.close()
```

The screen is the same video, from another source:

```ts
shareButton.onclick = () => senses.shareScreen()
senses.subscribe((event) => {
  if (event.kind === 'video') shareButton.hidden = event.source === 'screen'
})
```

It captures the microphone with echo cancellation and noise suppression and sends PCM at
`sampleRate` (16 kHz), filtered from the browser's own rate so nothing above it folds back into the
speech, or an Opus track over WebRTC (`transport: 'webrtc'`, with `url` the offer
endpoint and optional `iceServers`); `microphone: false` connects to listen only. It authenticates as your endpoint expects: same-origin cookies go with the request on their own,
a WebSocket token rides in `protocols`, and the WebRTC offer's request takes `headers`, which an
offer endpoint on another origin needs. With
`camera` it sends frames at `fps` for as long as the camera runs: JPEG at `quality` (0.7) over a
WebSocket, a video track capped at `fps` over WebRTC. `setFps(fps)` changes the rate while it runs — the sampling
interval over a WebSocket, the track's frame rate and its cap over WebRTC; without a camera, or for
an `fps` that is not positive, it throws a `SensesException`.

`camera` is the video the client sends, from one source at a time: the camera, or the screen with
`camera: { source: 'screen' }`. `shareScreen()` switches to the screen while it runs and
`useCamera()` back; both need the `camera` option, since a WebRTC connection negotiates its video
once and a switch replaces the track in place. The browser asks the person what to share, and a
cancelled picker rejects `shareScreen` and leaves the camera on. When the person stops sharing from
the browser's own controls, the client returns to the camera if it had one before the share, or
sends no video until `useCamera()`. Each switch is told to `subscribe` as
`{ kind: 'video', source: 'camera' | 'screen' | null }`. A screen's detail is small text: give it
room with `width` (1280 reads a window; 640, the default, suits a face, and blurs a menu's words),
and raise `setFps` while the model guides the person through clicks, then lower it. The client marks
a screen's track as text (`contentHint`), so over WebRTC the encoder keeps its resolution and drops
frames instead when the network tightens. Screen sharing is a desktop-browser
feature; mobile browsers mostly have no `getDisplayMedia`.

Which camera is `camera.facing` — `'environment'`, the back of a phone, or `'user'`, the front — or
`camera.deviceId`, from your own picker over `navigator.mediaDevices.enumerateDevices()`; one or the
other, never both. Mobile browsers open the front camera unless asked, so a phone pointed at a
machine needs `facing: 'environment'`:

```ts
const senses = await connect({ url, transport: 'websocket', camera: { facing: 'environment', width: 1280, fps: 1 } })
await senses.useCamera({ facing: 'user' })
```

`facing` is a preference, so a laptop with one camera still opens it; a `deviceId` is exact, so a
device that is gone rejects. `useCamera` takes the same two and switches in place — the next frame
comes from the new camera over a WebSocket, and the track is replaced over WebRTC — and without them
returns to the last camera chosen, as the client does when a screen share ends. Both at once reject
with a `SensesException`.

It plays the voice, counts what it played, and answers `stop` with it on its own. Over a WebSocket
each stretch of voice starts 120 ms after its first audio arrives; over WebRTC the server sends the
first 120 ms of it at once, ahead of the clock. Either way uneven first chunks play without a hole.
Only audio that has played is counted, as
far as the browser can tell. Over a WebSocket a stop counts up to `currentTime` less the context's
`outputLatency`, what has reached the speakers. Over WebRTC the count is the receiver's
`jitterBufferEmittedCount`, which leaves out the concealment it plays while no audio comes. It stops
short of the speakers, so a cut there does not subtract the output latency, and it cannot see a
muted output or an element the browser would not play: those count as played. A packet lost or too
late to play also counts, because the person heard it concealed and one lost packet should not cost
a sentence. Concealment only covers short losses: a long outage counts the same way, so the reply
reads whole and `heard` claims words the person never got. The line between utterances is the
server's stamp, exact on a busy page. A count that stops within 60 ms of the end reads whole, so a
lost final packet does not shorten a play. Packets lost at the very start of a session are never
counted: each cut in that session reads short by them, and more than three of them also delay each
natural end by up to two seconds, after which it reads short by them unless more voice came first.
`mute()`/`unmute()` silence the microphone, `pauseCamera()`/`resumeCamera()` the video, whichever
source it comes from; `subscribe` receives the session's events and
`{ kind: 'status', status: 'open' | 'closed' }`, and `senses.status` is the latest. Device pickers,
visualizers and framework bindings are yours.

A connection has one stop. `close()` runs it, and so does every end from the far side: the session's
`closed` event, the socket or data channel closing, the peer failing. It runs once, so the person's
microphone and camera never outlive the call, and `close()` after that, or twice, does nothing more.
The order is fixed. A `closed` event reaches subscribers first, with the devices still live; then
the microphone, the camera, the voice and the connection are released, all in that same turn; then
`status` turns `'closed'` and subscribers are told. A subscriber that calls `close()` from its
`closed` handler runs that release early, so the subscribers after it hear the status first and the
`closed` event with the devices gone. A subscriber that throws stops none of it: every
other subscriber is still told, `close()` still resolves, and the error surfaces on its own as an
uncaught error, for `window.onerror`. Voice still playing stops with the rest, so a call that should
end on its last words waits on `quiet` before it closes. A peer that is only `disconnected` is no
end, since it can recover. A `connect` that loses the connection partway rejects at once, even with
a permission prompt still open, and a device the person grants after that is stopped as it arrives.
It rejects with the server's reason when a failing session gave one.

`connect` waits as long as your endpoint does, so a page that gives up on it passes `signal`, an
`AbortSignal`: an abort before `connect` resolves rejects with a `SensesException` and releases
everything the same way, including an offer request that never answered. A signal that has already
aborted rejects at once and takes nothing, and once `connect` has resolved the signal does nothing:
`close()` ends a call.

## Tools

A **tool** is a name, a description, an input schema and a body; a **`Tools`** is a set of them —
the family block, the same class as in rfnry-engine, rfnry-memory and rfnry-thread:

```python
class Tools:
    def definitions(self) -> list[dict]                 # [{name, description, input_schema}]
    async def dispatch(self, name: str, args: dict) -> str | dict | list[Part]
    def __add__(self, other) -> Tools                    # union; a duplicate name is refused
    def only(self, *names) -> Tools
    def without(self, *names) -> Tools
```

A live model receives any `Tools` as `session.start(tools=)`. `Tool("now", "The current time", body=now)`
makes one of your own; `engine.tool(agent, scope=…)` makes an engine agent one, returned as a
`Tools` so it adds to the rest.

`SensesTools(session, triggered_by=)` is the room, for a *text* mind — an engine agent or any tool
loop — pinned to one session and one cause:

| tool | returns | offered when |
| --- | --- | --- |
| `look(frames)` | the newest `frames`, at least 1 and at most what the window holds, as parts: `[Image, Text("frame at 12:03:04.200")]` | `video=True` |
| `say(text)` | `{"heard": …, "interrupted": …}` — text only; a streamed `say` is for code | the session has a voice |
| `prompt(text)` | `{"said": …, "heard": …, "interrupted": …}`, or `"prompted"` when the adapter cannot pair the reply | live |
| `inform(text)` | `"informed"` | live |
| `interrupt()` | `{"heard": …}` — empty when nothing was being said | the session has a voice |

```python
tools = SensesTools(session, triggered_by=host)
[d["name"] for d in tools.definitions()]
await tools.dispatch("look", {"frames": 1})
```

There is no `listen` tool — a loop is started with what was said — and no `watch` tool: when to look
is your trigger. A call that fails raises the library's own error, as any `Tools` does, and the loop
that ran it decides what the model reads.

## Storage

Senses stores nothing. The session holds the camera window and the words of the utterance in
flight, in memory, and nothing else; it never writes to disk and never sees a credential. The
conversation's record is a thread's job and the agent's record the engine refinery's; a third log
would repeat them.

To keep a record, mirror the events — a few lines of your own, and neither library imports the
other. With rfnry-thread, `post(who, text)` sends `text` with `Client.send(thread_id, text)` on the
client connected as `who`:

```python
async def mirror(session, post):
    async for event in session.events():
        match event:
            case Heard(final=True):
                await post(event.speaker, event.text)
            case Said(heard=heard) if heard:
                await post(assistant, heard)
```

Post `heard`, not `text`: the record should hold what the person heard.

## Tenancy

`scope` is the tenant: your endpoint names it with the person, the transport carries it, the
session takes it from the transport (`session.scope`, with the person as `session.person`), and
every event carries it. Nothing is
shared between sessions: each has its own model connections, window and queues. Tools pinned to a
tenant — a thread, a memory corpus, an engine agent — are built for the session's scope.

## At a glance

Everything the package exports, by role.

- **Run a session:** `Session` — `start(tools=)` or `async with` — and `SensesTools`.
- **What a session reports:** `Event`; `Heard`, `TurnEnded`, `Said`, `Interrupted`, `Speaking`, `Silent`, `Looked`, `Muted`, `Informed`, `ToolCalled`, `Cancelled`, `Closed`.
- **Speak to models:** `LiveModel`, `LiveConnection`, `LiveEvent`, `LiveAudio`, `LiveHeard`, `LiveSaid`, `LiveStarted`, `LiveDone`, `LiveToolCall`, `LiveToolCancelled`, `LiveInterrupted`, `LiveFailed`; `TranscriptionModel`, `Transcription`, `Transcript`, `UtteranceEnd`; `SpeechModel`; `AudioFrame`; parts `Text`, `Image`, `Audio`, `Document`, `Part`, `Content`; `ToolSpec`.
- **Give models tools:** `Tool`, `Tools`.
- **Connect people:** `serve`, `Connection`, `Transport`, `Played`, `Loopback`, `Identity`, `IdentityLike`; with the `webrtc` extra, `accept_offer` and `WebRTCTransport` from `rfnry_senses.webrtc`.
- **Fail loudly:** `SensesException`, `ConfigurationError`, `InputError`, `NotFoundError`, `ModelError`, `TransportError`.

The TypeScript package exports `connect` and its `ConnectOptions`, with `CameraChoice` for the
camera's `facing` or `deviceId`; the `Senses` it returns, with `SensesStatus`; `SensesListener`,
`SensesEvent`, `Identity` and `VideoSource` for what `subscribe` receives; and one error class,
`SensesException`, whose messages have the same shape.

## Errors

`ConfigurationError` for a session that cannot do what is asked: a live model given with
transcription or speech models; `instructions=` or `start(tools=)` without a live model; `start`
on a session already started; `say` or `interrupt` without a voice, `prompt` or `inform` without a
live model, `look` without `video=True`; a `window`, `window_bytes`, `tool_timeout` or `voice_gap` that is not
positive. `InputError` for a call the library
refuses: empty text, `frames` below 1, a media part with both or neither of `data` and `url`, an
audio frame whose sample rate is not positive, whose channels are not one or two, or whose bytes
do not fill its samples, a tool called with bad arguments, an
identity whose fields do not fit, an empty scope given to `serve` or `accept_offer`, or a transport
with an empty scope given to `Session`. `NotFoundError` when `look` finds no frame in the
window. `ModelError` when a live model reports `LiveFailed` or breaks the contract — a response
that ends before it starts on a turn-based connection, or starts before the last one is done — and the session closes; a
`say` or `prompt` waiting on it raises it. A `say` on a duplex connection that returns no tag raises
`ModelError` from that one `say`, and the session goes on. A speech model that fails raises `ModelError` from the
one `say` that needed it; a tool definition that is not valid raises `InputError` from `start`. `TransportError`
for a send the transport failed (the session closes as `failed`), a verb on a session not yet started or closed, `start` on a closed one, for a `say` or `prompt` whose session closed before it was
spoken, and for an offer `accept_offer` cannot answer. A client that sends a malformed frame after
its hello has the frame dropped with a warning, never the session.

All under `SensesException` — the root takes the `Exception` suffix, every child ends in `Error`,
the family's shape. Every message names the module that raised it, then says what went wrong in
lowercase: `session: prompt needs a live model`, `session: no frame in the last 10 s`,
`transport: protocol 1 is not supported; this server speaks 2`.
