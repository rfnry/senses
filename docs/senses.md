# rfnry-senses

rfnry-senses gives a model eyes, ears and a voice in real time.

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
    async def send_audio(self, frame: AudioFrame) -> None: ...
    async def send_image(self, image: Image) -> None: ...
    async def send_text(self, text: str, *, respond: bool) -> None: ...
    async def say(self, text: str) -> None: ...
    async def interrupt(self, played_ms: int) -> None: ...
    async def submit_result(self, call_id: str, result: Content, is_error: bool) -> None: ...
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

Audio is `AudioFrame(data, sample_rate, channels)`: 16-bit little-endian PCM, one or two channels.
Frames carry their rate, so an adapter whose vendor wants another rate converts in the adapter.
Images are the family's `Image` part — frames travel as JPEG.

A live connection reports what happens as `LiveEvent`s. Every response the model gives is
bracketed by `LiveStarted` and `LiveDone`; an adapter whose vendor has no such markers makes them:

```python
LiveAudio(frame=frame)                                   # the model's voice
LiveHeard(text="a table for", final=False)                # the person, as the model hears them
LiveSaid(text="Booked for eight. ")                       # the model's own words, as it speaks them
LiveStarted(), LiveDone()                                 # one response
LiveToolCall(call_id="c1", name="consult", args={"input": "Is the terrace open?"})
LiveInterrupted()                                         # the model heard the person cut in
LiveFailed(message="connection lost")                     # the connection is over
```

`send_text(text, respond=True)` asks the model to answer now, in its own words; `respond=False`
adds context without a reply. `say(text)` speaks exactly those words. `interrupt(played_ms)` stops
the current response and cuts the model's context to what the person heard. A connection
implements every method; a vendor that cannot do one raises from its adapter, and the verb that
needed it raises to its caller. There is no capability matrix.

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
the session at the end, and starts it without tools if it has not started. When the model calls a
tool, senses dispatches the call through `tools` — any family `Tools`: an engine agent as a tool, rfnry-thread's
`ThreadTools`, rfnry-memory's `MemoryTools`, your own — and submits the result. A string or a list of
parts goes back as it is, a dict as JSON; an exception goes back as an error the model reads,
`InputError: tools: unknown tool 'ghost'`. Each call is bounded by `tool_timeout` (30 s by default,
`None` for none) and reported as a `ToolCalled` event. Every call the model makes is answered
exactly once, even when the person interrupts the response that made it.

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
still being made. Calls queue: a second `say` speaks after the first. Each model is optional — a
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
`Interrupted`, and, in a live session, calls `interrupt(played_ms)` so the model's context holds only
what was heard. Every utterance is numbered, and audio from an interrupted one — a sentence that
finished synthesizing a moment too late, a frame already on the wire — is dropped, on the server and
in the client. Queued `say` and `prompt` calls end too: the person is talking, and they return
`Said(heard="", interrupted=True)`.

If the client never reports, senses estimates from its own clock — the audio sent, capped by the
time since it started — after a quarter of a second, so an interruption never waits on a silent
client. A live model whose vendor cannot cut its context raises from `interrupt`; senses logs that
as a warning and goes on — the voice is already stopped.

Your code can do the same:

```python
cut = await session.interrupt(triggered_by=host)     # Interrupted(heard=...), or None when silent
await session.mute(triggered_by=host)                # nothing reaches the person
await session.unmute(triggered_by=host)
```

`mute` is the output gate: while muted, no audio reaches the person, and anything being said is
stopped and cut as an interruption would be. A `say` or `prompt` while muted returns at once with
nothing heard, and a response a live model starts on its own is cut before a word of it plays. The
session still hears and sees. A product's floor policy or mandate gate is built on these two
verbs.

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

`look` returns the newest frames, oldest first, each an `Image` followed by the `Text` of when it was
taken — `frame at 12:03:04.200` — ready to hand any model that sees. With nothing in the window it
raises `NotFoundError`. The model sees the frame itself; there is no vision model in between. Without
`video=True`, `look` is a `ConfigurationError` and frames are dropped on arrival.

### Events

`events()` yields everything the session reports, from the moment it is called; call it twice and
both streams see every event. Each event carries the session's `scope` and the time `at`:

| event | fields | when |
| --- | --- | --- |
| `Heard` | `text, final, speaker` | the person's words, partial and final |
| `TurnEnded` | `text, speaker` | a cascaded turn ended |
| `Said` | `text, heard, interrupted, triggered_by` | an utterance ended |
| `Interrupted` | `heard, triggered_by` | the voice was cut |
| `Speaking` / `Silent` | — | the voice started / stopped |
| `Looked` | `frames, triggered_by` | someone looked through the camera |
| `Muted` | `muted, triggered_by` | the output gate closed or opened |
| `Informed` | `text, triggered_by` | a live model was told something |
| `ToolCalled` | `name, input, output, is_error` | a live model's tool call ran |
| `Closed` | `reason, message` | the session ended: `closed`, `hung_up` or `failed` |

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
has `triggered_by=None`. The client receives every event except `Informed` and `ToolCalled`, which
are between you and the model.

Every identity the library takes — `serve`, `accept_offer`, `Loopback`, a transport's `identity`,
each verb's `triggered_by=`, `SensesTools(triggered_by=)` — is read by its fields: any object with
`role`, `id`, `name` and `metadata` will do, such as rfnry-thread's `Identity`, which has the same
source. The values are validated as strictly as an `Identity`'s; one that does not fit is an
`InputError`.

### Two minds

A live model in front for the conversation, an engine agent behind for the thinking:

```python
session = Session(link, live=live, video=True)
consult = engine.tool("core", scope=session.scope, name="consult", description="Ask the reasoning mind.", tools=SensesTools(session, triggered_by=assistant))
await session.start(tools=consult + ThreadTools(client, thread_id=thread, triggered_by=ana))
```

The live model asks the engine agent with `consult` — rfnry-engine's `engine.tool`, an agent as a
tool. The engine agent reaches the room through `SensesTools`, built before the session starts: it
needs the session only when a tool is called, and the session needs `consult` only when it starts,
so the circle closes in three lines. Through them `say` speaks the agent's words verbatim, `prompt`
has the live voice say it in its own words, `inform` keeps the live model current, and `look` shows
it the camera directly. The same tools serve the agent when it acts first, passed to its own
`engine.run`. The transcript both minds read is the thread, mirrored from
`events()` ([Storage](#storage)). A persona, a floor policy or a mandate gate stays in your product,
built on `mute`, `unmute`, `interrupt` and tools of your own.

### Three ways to use the screen

A shared screen or camera fits the family in three shapes. The engine agent is the same in all
three; only the session around it changes.

**Two minds.** A live model watches continuously, frames streaming at `fps`, and holds the
conversation; for anything that needs thought it asks the engine agent through `consult`. The agent
looks at the screen itself with `look` from its `SensesTools` — the same current frame, as an image,
never the live model's description of it — and answers through the live voice with `say` or
`prompt`, or keeps it current with `inform`. Best for fluid help while the person works; the live
model is billed for every frame it watches.

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
`None` after telling the client why it was refused. Senses never sees a credential: who opened the
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

Media has its own socket, apart from any thread socket. Audio and camera frames are binary, each
behind a small header; control frames are JSON: `hello` (the protocol version) and `played` from
the client; `ready`, `speaking`, `silent`, `stop`, `event` and `error` from the server. The frames
are pinned by golden fixtures both clients' tests read.

WebRTC is the transport for browser voice: Opus bandwidth, loss concealment, and the echo
cancellation that lets the person interrupt over speakers. One peer connection carries the audio,
an optional camera track at the client's `fps` and a data channel for the control frames. You host
the signalling endpoint — your own HTTP endpoint, authenticated like any other; the client posts
its offer to it, and `accept_offer` answers it for the person and scope you name:

```python
@app.post("/senses/offer")
async def offer(body: dict, user: User = Depends(signed_in)) -> dict:
    link = await accept_offer(body["sdp"], identity=user.identity, scope=user.org)
    asyncio.create_task(converse(link))
    return {"sdp": link.answer}
```

`accept_offer` lives in `rfnry_senses.webrtc` and returns a `WebRTCTransport`; `converse` is the
same function the WebSocket endpoint runs over its `link`. The client sends its hello on the data
channel, and a refused one ends the transport. The inbound audio is resampled to
`sample_rate` (16 kHz) and camera frames are encoded to JPEG at the client's rate, inside the extra.

`Loopback` is an in-process transport for tests — and for anything that is not a browser:

```python
link = Loopback(Identity(role="user", id="ana", name="Ana"), scope="acme")
async with Session(link, transcription=Script("A table for two."), speech=Tone()) as session:
    await link.speak(AudioFrame(data=bytes(640), sample_rate=16000, channels=1))
    said = await session.say("For two, at eight.", triggered_by=link.identity)
    said.heard, len(link.audio)
```

It records the audio and control frames the person would receive, and answers every `stop` with a
`Played` — all of it by default, or what `played=` says. A telephone line, a meeting bot's bridge or
a robot's microphone is a `Transport` of your own: an `identity`, a `scope`, `inbound()` yielding
`AudioFrame`s, JPEG `Image`s and `Played` reports, `send_audio`, `send_control`, `clear_audio` and
`close`.

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

It captures the microphone with echo cancellation and noise suppression and sends 16 kHz PCM, or an
Opus track over WebRTC (`transport: 'webrtc'`, with `url` the offer endpoint and optional
`iceServers`). It authenticates as your endpoint expects: cookies go with the request on their own,
a WebSocket token rides in `protocols`, and the WebRTC offer's request takes `headers`. With
`camera` it sends frames at `fps` for as long as the camera runs: JPEG over a WebSocket, a video
track capped at `fps` over WebRTC. `setFps(fps)` changes the rate while it runs — the sampling
interval over a WebSocket, the track's frame rate and its cap over WebRTC; without a camera it throws.

`camera` is the video the client sends, from one source at a time: the camera, or the screen with
`camera: { source: 'screen' }`. `shareScreen()` switches to the screen while it runs and
`useCamera()` back; both need the `camera` option, since a WebRTC connection negotiates its video
once and a switch replaces the track in place. The browser asks the person what to share, and a
cancelled picker rejects `shareScreen` and leaves the camera on. When the person stops sharing from
the browser's own controls, the client returns to the camera if it had one before the share, or
sends no video until `useCamera()`. Each switch is told to `subscribe` as
`{ kind: 'video', source: 'camera' | 'screen' | null }`. A screen's detail is small text: give it
room with `width` (1280 reads a window; 640, the default, suits a face), and raise `setFps` while
the model guides the person through clicks, then lower it. Screen sharing is a desktop-browser
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

It plays the voice, counts what it played, and answers `stop` with it on its own.
`mute()`/`unmute()` silence the microphone, `pauseCamera()`/`resumeCamera()` the video, whichever
source it comes from; `subscribe` receives the session's events and
`{ kind: 'status', status: 'open' | 'closed' }`. Device pickers, visualizers and framework bindings
are yours.

## Tools

A **tool** is a name, a description, an input schema and a body; a **`Tools`** is a set of them —
the family block, the same class as in rfnry-engine and rfnry-memory:

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
| `look(frames)` | the frames as parts: `[Image, Text("frame at 12:03:04.200")]` | `video=True` |
| `say(text)` | `{"heard": …, "interrupted": …}` — text only; a streamed `say` is for code | the session has a voice |
| `prompt(text)` | `{"said": …, "heard": …, "interrupted": …}` | live |
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
other. With rfnry-thread, `post` is `ThreadClient.send`:

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
session takes it from the transport (`session.scope`), and every event carries it. Nothing is
shared between sessions: each has its own model connections, window and queues. Tools pinned to a
tenant — a thread, a memory corpus, an engine agent — are built for the session's scope.

## At a glance

Everything the package exports, by role.

- **Run a session:** `Session` — `start(tools=)` or `async with` — and `SensesTools`.
- **What a session reports:** `Event`; `Heard`, `TurnEnded`, `Said`, `Interrupted`, `Speaking`, `Silent`, `Looked`, `Muted`, `Informed`, `ToolCalled`, `Closed`.
- **Speak to models:** `LiveModel`, `LiveConnection`, `LiveEvent`, `LiveAudio`, `LiveHeard`, `LiveSaid`, `LiveStarted`, `LiveDone`, `LiveToolCall`, `LiveInterrupted`, `LiveFailed`; `TranscriptionModel`, `Transcription`, `Transcript`, `UtteranceEnd`; `SpeechModel`; `AudioFrame`; parts `Text`, `Image`, `Audio`, `Document`, `Part`, `Content`; `ToolSpec`.
- **Give models tools:** `Tool`, `Tools`.
- **Connect people:** `serve`, `Connection`, `Transport`, `Played`, `Loopback`, `Identity`; with the `webrtc` extra, `accept_offer` and `WebRTCTransport` from `rfnry_senses.webrtc`.
- **Fail loudly:** `SensesException`, `ConfigurationError`, `InputError`, `NotFoundError`, `ModelError`, `TransportError`.

The TypeScript package exports `connect` and its `ConnectOptions`, with `CameraChoice` for the
camera's `facing` or `deviceId`; the `Senses` it returns, with `SensesStatus`; `SensesListener`,
`SensesEvent`, `Identity` and `VideoSource` for what `subscribe` receives; and one error class,
`SensesException`, whose messages have the same shape.

## Errors

`ConfigurationError` for a session that cannot do what is asked: a live model given with
transcription or speech models; `instructions=` or `start(tools=)` without a live model; `start`
on a session already started; `say` or `interrupt` without a voice, `prompt` or `inform` without a
live model, `look` without `video=True`; a `window`, `window_bytes` or `tool_timeout` that is not
positive. `InputError` for a call the library
refuses: empty text, `frames` below 1, a media part with both or neither of `data` and `url`, an
audio frame whose bytes do not fill its samples, a tool called with bad arguments, an
identity whose fields do not fit, an empty scope given to `serve` or `accept_offer`. `NotFoundError` when `look` finds no frame in the
window. `ModelError` when a live model reports `LiveFailed` or breaks the contract — a response
that ends before it starts, or starts before the last one is done — and the session closes; a
`say` or `prompt` waiting on it raises it. A speech model that fails raises `ModelError` from the
one `say` that needed it. `TransportError`
for a verb on a session not yet started or closed, `start` on a closed one, for a `say` or `prompt` whose session closed before it was
spoken, and for an offer `accept_offer` cannot answer. A client that sends a malformed frame after
its hello has the frame dropped with a warning, never the session.

All under `SensesException` — the root takes the `Exception` suffix, every child ends in `Error`,
the family's shape. Every message names the module that raised it, then says what went wrong in
lowercase: `session: prompt needs a live model`, `session: no frame in the last 10 s`,
`transport: protocol 2 is not supported; this server speaks 1`.
