# rfnry-senses API

The reference: every public name, its signature, what it returns and what it raises. How and why to use them is in [the guide](senses.md).

## Models

### `LiveModel`

```python
class LiveModel(Protocol):
    async def open(self, *, tools: list[ToolSpec], instructions: str | None) -> LiveConnection: ...
```

A model that hears and speaks natively. `Session.start` calls `open` once, with the tools' definitions and the session's instructions, and returns the connection the session runs on.

### `LiveConnection`

```python
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
```

One open live model. `duplex` says how it converses, and `Session.start` reads it once: `False` for a model that takes turns, `True` for one that listens while it speaks and decides when to talk. It has no default, so a connection without it fails `start` with `ConfigurationError`: `session: a live connection must declare duplex, True or False`. A `send_image` that raises is logged once and frames stop going to the model; the window and `look` go on, and so does the session. `send_text(text, respond=True)` asks for an answer in the model's own words and `respond=False` adds context without one; `say` speaks exactly `text`. Both may return a tag for the response they asked for — the vendor's id for the request, or the adapter's own — which the adapter then puts on that response's `LiveStarted(requested=tag)`, reported only once the call has returned it; an adapter that cannot tag returns `None`, and `respond=False` always does. The session sends one at a time and never cancels the `say` or `send_text(respond=True)` of a call whose caller gave up: it runs to its end, so one that never returns holds every later `say`, `prompt` and `inform` until `close`, and an adapter bounds its own send. `send_text(respond=False)` runs in the task of the `inform` that called it, and is cancelled with it. `interrupt(played_ms, heard=, response=)` stops what the model is saying and cuts its context to what the person heard: the response the cut landed in — named by the `id` its `LiveStarted` carried, `None` without one — keeps `heard`, the first `played_ms` of its audio, and nothing the model said after it was heard; `submit_result` answers a tool call, and `respond=False` adds the result without asking for a reply, as `send_text` does. The text of `send_text` and `say` is your code addressing the model, whoever caused it; an adapter sends it on the vendor's operator channel where there is one. `events()` reports what happens as `LiveEvent`s; a method the vendor cannot do raises from the adapter, and the verb that needed it raises to its caller. On a duplex connection `say` must return a tag, and a `send_text(respond=True)` that returns `None` is one the adapter cannot pair with a response.

### `LiveEvent`

```python
LiveEvent = (
    LiveAudio
    | LiveHeard
    | LiveSaid
    | LiveStarted
    | LiveDone
    | LiveToolCall
    | LiveToolCancelled
    | LiveInterrupted
    | LiveFailed
)
```

What `LiveConnection.events()` yields. On a turn-based connection every response is bracketed by `LiveStarted` and `LiveDone`; on a duplex connection an adapter can leave them out, and the session makes an utterance from each stretch of voice.

### `LiveAudio`

```python
class LiveAudio(BaseModel):
    frame: AudioFrame
```

A piece of the model's voice. On a turn-based connection it plays only inside a response. On a duplex connection one with no response open starts a stretch of voice, with `triggered_by=None`, unless the session is muted or a cut has not finished, when it is dropped. There, a frame whose loudest sample is at or below `voice_floor` is not voice: outside a response it is dropped, and inside a stretch that already has voice it is sent, to keep the timing of the pauses, but does not extend the stretch. Inside a response it is sent as any frame is.

### `LiveHeard`

```python
class LiveHeard(BaseModel):
    text: str
    final: bool = False
```

The person's words as the model hears them, partial or final. The session reports them as `Heard`; blank text is dropped. A final one comes before the `LiveToolCall` it led to.

### `LiveSaid`

```python
class LiveSaid(BaseModel):
    text: str
```

The model's own words as it speaks them, marked against its audio so a cut knows what was heard. A `prompt`'s `Said.text` is these words, joined. On a duplex connection one with no response open starts a stretch of voice, so words that come just before their voice stay with it.

### `LiveStarted`

```python
class LiveStarted(BaseModel):
    id: str | None = None
    requested: bool | str | None = None
```

One response begins. A `str` is the tag `send_text` or `say` returned for the request it answers, and the response takes that request's cause. A response that carries the tag of a request the session already ended — by `mute`, the person cutting in, or a caller that gave up while it was being sent — is cut at once, as one started while muted is: `interrupt(0, heard="", response=id)`, and no `Said`. One whose tag the session never handed out is the vendor's own, and takes no cause. `requested=False` says it answers no `say` or `prompt` — the vendor started it on its own, or it answers a tool result or the person — and it takes no cause; `True` and `None` take the oldest `say` or `prompt` waiting for a response, or none. `None` is for an adapter that cannot tell. Without a tag, a request the session ended after its send began shifts that order when the vendor still answers it: the answer takes the next request's cause. `id` is the vendor's id for the response, handed back to `interrupt`. The session fails with `ModelError` when a response starts before the last one is done. On a duplex connection an open stretch of voice ends first, with what played, and no error: audio then belongs to this response until `LiveDone`.

### `LiveDone`

```python
class LiveDone(BaseModel):
    pass
```

One response is over; its utterance ends when the client reports how much of it played. On a duplex connection it also ends an open stretch of voice, and with nothing open it is ignored, so a model that marks only where speech ends fits too. On a turn-based connection the session fails with `ModelError` when a response ends before it starts.

### `LiveToolCall`

```python
class LiveToolCall(BaseModel):
    call_id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
```

The model calls a tool. The session dispatches it through the tools given to `start`, bounded by `tool_timeout`, answers it with `submit_result` exactly once unless the model withdraws it, and reports `ToolCalled`.

### `LiveToolCancelled`

```python
class LiveToolCancelled(BaseModel):
    call_ids: list[str]
```

The model withdrew these calls. The session cancels each one still running and submits nothing for it; each reports `ToolCalled` with `cancelled=True`. An id it is not running is ignored.

### `LiveInterrupted`

```python
class LiveInterrupted(BaseModel):
    pass
```

The model heard the person cut in. The session stops the voice, cuts the utterance to what was heard, and ends the `say` and `prompt` calls still queued with nothing heard. An adapter on a duplex connection sends it only on a clear signal, never on a backchannel; audio after the cut starts a new stretch of voice.

### `LiveFailed`

```python
class LiveFailed(BaseModel):
    message: str
```

The connection is over — only then: a vendor error that ends nothing is the adapter's to absorb. The session closes with reason `failed`, and a `say` or `prompt` waiting on it raises `ModelError`; an `events()` that simply ends does the same.

### `TranscriptionModel`

```python
class TranscriptionModel(Protocol):
    def stream(self) -> Transcription: ...
```

A model that turns speech into text. `Session.start` opens one stream for the session.

### `Transcription`

```python
class Transcription(Protocol):
    async def feed(self, frame: AudioFrame) -> None: ...
    def events(self) -> AsyncIterator[Transcript | UtteranceEnd]: ...
    async def close(self) -> None: ...
```

One transcription stream. The session feeds it the person's audio and reads `Transcript`s and `UtteranceEnd`s from `events()`; when `events()` ends, the session stops hearing and goes on.

### `Transcript`

```python
class Transcript(BaseModel):
    text: str
    final: bool = False
```

The running words of the current segment (`final=False`), then the segment committed (`final=True`). The session reports each as `Heard`, and one that arrives while the assistant speaks cuts it off.

### `UtteranceEnd`

```python
class UtteranceEnd(BaseModel):
    pass
```

The person stopped talking. The utterance's words become a `TurnEnded`; how eagerly this is decided is the adapter's configuration.

### `SpeechModel`

```python
class SpeechModel(Protocol):
    def speak(self, text: str) -> AsyncIterator[AudioFrame]: ...
```

A model that turns text into speech. The session calls `speak` once per sentence and plays the frames as they arrive; one that fails raises `ModelError` from the `say` that needed it.

## Session

### `Session`

```python
class Session:
    scope: str
    person: Identity

    def __init__(
        self,
        transport: Transport,
        *,
        live: LiveModel | None = None,
        instructions: str | None = None,
        transcription: TranscriptionModel | None = None,
        speech: SpeechModel | None = None,
        video: bool = False,
        window: float = 10.0,
        window_bytes: int = 8 * 1024 * 1024,
        tool_timeout: float | None = 30.0,
        voice_gap: float = 1.0,
        voice_floor: int = 128,
    ) -> None: ...
    async def __aenter__(self) -> Session: ...
    async def __aexit__(self, *exc: object) -> None: ...
```

One person on one transport, and the models that hear, see and speak with them: live with `live=`, cascaded with `transcription=` and `speech=`, each optional. With `video=True` it keeps the camera's frames for `window` seconds and `window_bytes` bytes, whichever is smaller; `scope` and `person` come from the transport. On a duplex connection a stretch of voice ends `voice_gap` seconds after its last voice has played, and a frame whose loudest 16-bit sample is at or below `voice_floor` is not voice: 0 leaves only exact zeros as silence. `async with` starts it without tools if it has not started, and closes it at the end; when that start raises, it closes the session with reason `failed` and raises the error, and when that start is cancelled, it closes the session with reason `closed`.
Raises `ConfigurationError` when a live model is given with transcription or speech models, `instructions` without a live model, a `window`, `window_bytes`, `tool_timeout` or `voice_gap` that is not positive: `session: voice_gap must be positive`, or a `voice_floor` outside 0 to 32767: `session: voice_floor must be between 0 and 32767`.
Raises `InputError` when the transport's scope is empty or its identity does not fit.

### `Session.start`

```python
async def start(self, tools: ToolsLike | None = None, *, inform: Collection[str] = ()) -> None: ...
```

Opens the live model with the definitions of `tools` and the instructions, starts hearing, seeing and speaking, and tells the client the session is ready. `tools` is any `Tools`, or any object with `definitions()` and `dispatch()`; it is fixed from then on. The tools named in `inform` answer their calls the way `inform` speaks: the result goes back to the model with `respond=False`, and it is not asked to reply. A start that raises before it sends `ready` leaves the session open, so it can be tried again; call `close()` after one you will not retry.
Raises `TransportError` when the session is closed, or closes while the live model opens; the connection it opened is then closed. A `ready` the transport fails closes the session with reason `failed`, and raises `TransportError` too.
Raises `ConfigurationError` when the session has already started, `tools` is given without a live model, or `inform` names something that is not one of the tools.
Raises `InputError` when a tool definition is not valid: `session: a tool definition is not valid`.

### `Session.say`

```python
async def say(self, text: str | AsyncIterator[str], *, triggered_by: IdentityLike) -> Said: ...
```

Speaks exactly `text` and returns, when the utterance ends, the `Said` with what the person heard. Given an `AsyncIterator[str]`, a cascaded session speaks it sentence by sentence as it arrives and a live one joins it first; a stream with no text returns an empty `Said`. Calls queue; on a live model each is sent once no response is open and none is owed to the last one sent. On a duplex connection each is sent at once, held only by a cut that has not finished, and the adapter brackets the speech with `LiveStarted(requested=tag)` and `LiveDone`. While muted it returns at once with nothing heard.
Raises `InputError` when `text` is empty or `triggered_by` does not fit.
Raises `ConfigurationError` when the session has neither a live model nor a speech model.
Raises `TransportError` when the session is not started or is closed, or closes before the words are spoken.
Raises `ModelError` when the speech model fails, the live model fails while the call waits, or a duplex connection returns no tag: `session: a say on a duplex model needs a tag`.

### `Session.prompt`

```python
async def prompt(self, text: str, *, triggered_by: IdentityLike) -> Said | None: ...
```

Asks the live model to speak now, in its own words, and returns the `Said`: `text` is what it said and `heard` what the person heard. It queues like `say`, and while muted it returns at once with nothing heard. On a duplex connection it is sent at once, held only by a cut that has not finished; when the adapter returns a tag it waits for the response carrying that tag, and when the adapter returns `None` it returns `None` once sent and reports `Informed`. A turn-based connection never returns `None` here.
Raises `InputError` when `text` is empty or `triggered_by` does not fit.
Raises `ConfigurationError` when the session has no live model.
Raises `TransportError` when the session is not started or is closed, or closes before the words are spoken.
Raises `ModelError` when the live model fails while the call waits.

### `Session.inform`

```python
async def inform(self, text: str, *, triggered_by: IdentityLike) -> None: ...
```

Gives the live model `text` to know, without a reply, and reports `Informed`.
Raises `InputError` when `text` is empty or `triggered_by` does not fit.
Raises `ConfigurationError` when the session has no live model.
Raises `TransportError` when the session is not started or is closed.

### `Session.interrupt`

```python
async def interrupt(self, *, triggered_by: IdentityLike) -> Interrupted | None: ...
```

Stops the voice, cuts what is playing to what the person heard, and has a live model cut its context to the same point. Returns the `Interrupted` it reports, or `None` when nothing was being said; calls queued behind the cut still play. The cut finishes even when its caller gives up.
Raises `InputError` when `triggered_by` does not fit.
Raises `ConfigurationError` when the session has neither a live model nor a speech model.
Raises `TransportError` when the session is not started or is closed, or closes before the cut ends. A `stop` the transport fails closes the session with reason `failed`, and the calls it cut raise that error, with no `Interrupted` reported.

### `Session.cancel`

```python
async def cancel(self, *, triggered_by: IdentityLike) -> Cancelled | None: ...
```

Stops every tool call still running, except the one that calls it, and answers each with `"cancelled"` as an error and `respond=False`, so the model knows and is not asked to reply. Returns the `Cancelled` it reports once each call is answered, or `None` when nothing was running.
Raises `InputError` when `triggered_by` does not fit.
Raises `ConfigurationError` when the session has no live model.
Raises `TransportError` when the session is not started or is closed.

### `Session.mute`

```python
async def mute(self, *, triggered_by: IdentityLike) -> None: ...
```

Closes the output gate: queued calls end with nothing heard, what is playing is cut as by `interrupt`, and no audio reaches the person until `unmute`. Reports `Muted`; the session still hears and sees, and muting a muted session does nothing. The gate closes and the cut finishes even when its caller gives up.
Raises `InputError` when `triggered_by` does not fit.
Raises `TransportError` when the session is not started or is closed, or closes before the cut ends. A `stop` the transport fails closes the session with reason `failed`.

### `Session.unmute`

```python
async def unmute(self, *, triggered_by: IdentityLike) -> None: ...
```

Opens the output gate and reports `Muted` with `muted=False`. Unmuting a session that is not muted does nothing.
Raises `InputError` when `triggered_by` does not fit.
Raises `TransportError` when the session is not started or is closed.

### `Session.look`

```python
async def look(self, frames: int = 1, *, triggered_by: IdentityLike) -> list[Part]: ...
```

Returns the newest `frames` from the camera window (at most what the window holds), oldest first, each an `Image` followed by the `Text` of when it was taken (`frame at 12:03:04.200`), and reports `Looked`.
Raises `InputError` when `frames` is below 1 or `triggered_by` does not fit.
Raises `ConfigurationError` when the session was made without `video=True`.
Raises `NotFoundError` when there is no frame in the window.
Raises `TransportError` when the session is not started or is closed.

### `Session.events`

```python
def events(self) -> Subscription: ...
```

Returns a new async iterator of every `Event` the session reports from now on. Every stream sees every event and ends after `Closed`; one made after the session closed ends at once, and one nobody holds is let go.

### `Session.quiet`

```python
async def quiet(self) -> None: ...
```

Returns once the voice has nothing left to say: no response is open, no `say` or `prompt` is waiting, and every utterance has played or been cut. On a duplex connection it returns between stretches of voice, so a model that never marks its speech still goes quiet. It also returns when the session closes, or has closed. A tool still running is not waited for; its result may start another response.
Raises `TransportError` when the session is not started.

### `Session.close`

```python
async def close(self, *, triggered_by: IdentityLike | None = None) -> None: ...
```

Ends the session: `say` and `prompt` calls still waiting raise `TransportError`, tool calls still running are stopped, including one whose tool called `close`, the model connections and the transport close, and `Closed` is reported with reason `closed`, naming `triggered_by` when given: who ended it, a person or your own code. An utterance nobody is waiting on reports `Silent` and its `Said` before `Closed`, with `heard` from the session's own clock and `interrupted` true unless all of it had played. Those calls fail first and `quiet` returns; the close then waits at most 2 seconds for the tasks and tool calls it stopped, leaving what has not stopped running and logging it, and at most 2 seconds for the `Closed` send, cancelling it and logging that. A `cancel` still waits for the tools it stopped, and an `interrupt` or `mute` for a `stop` send that will not end, so those calls can outlast the close. Once begun, the close runs to the end even when its caller is cancelled, and a `close` while one is in progress returns once that one is done; from a tool call the close is stopping, `close` returns at once. Closing a closed session does nothing.
Raises `InputError` when `triggered_by` does not fit.

## Events

### `Event`

```python
Event = Annotated[
    Heard
    | TurnEnded
    | Said
    | Interrupted
    | Speaking
    | Silent
    | Looked
    | Muted
    | Informed
    | ToolCalled
    | Cancelled
    | Closed,
    Field(discriminator="kind"),
]
```

Everything `Session.events()` yields, told apart by `kind`. Every event is frozen and carries the session's `scope` and the time `at`; the client receives all but `Informed`, `ToolCalled` and `Cancelled`.

### `Heard`

```python
class Heard(BaseModel):
    scope: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: Literal["heard"] = "heard"
    text: str
    final: bool
    speaker: Identity
```

The person's words, partial and final, whether or not anyone answers. `speaker` is the transport's person.

### `TurnEnded`

```python
class TurnEnded(BaseModel):
    scope: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: Literal["turn_ended"] = "turn_ended"
    text: str
    speaker: Identity
```

A cascaded turn ended: the utterance's words, when the transcription model reported `UtteranceEnd`.

### `Said`

```python
class Said(BaseModel):
    scope: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: Literal["said"] = "said"
    text: str
    heard: str
    interrupted: bool
    triggered_by: Identity | None
```

An utterance ended, once each, when what was heard of it is final. `text` is what the voice was given and `heard` what the person heard; `triggered_by` is `None` when a live model spoke on its own or answered what it heard.

### `Interrupted`

```python
class Interrupted(BaseModel):
    scope: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: Literal["interrupted"] = "interrupted"
    heard: str
    triggered_by: Identity
```

The voice was cut, by the person talking over it or by a verb. `heard` is what the person heard of the utterance the cut landed in.

### `Speaking`

```python
class Speaking(BaseModel):
    scope: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: Literal["speaking"] = "speaking"
```

The voice started after quiet.

### `Silent`

```python
class Silent(BaseModel):
    scope: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: Literal["silent"] = "silent"
```

The voice went quiet: all of it played, or was cut. It comes right before the `Said` of the utterance that left the voice quiet.

### `Looked`

```python
class Looked(BaseModel):
    scope: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: Literal["looked"] = "looked"
    frames: int
    triggered_by: Identity
```

Someone looked through the camera; `frames` is how many frames they got.

### `Muted`

```python
class Muted(BaseModel):
    scope: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: Literal["muted"] = "muted"
    muted: bool
    triggered_by: Identity
```

The output gate closed (`muted=True`) or opened.

### `Informed`

```python
class Informed(BaseModel):
    scope: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: Literal["informed"] = "informed"
    text: str
    triggered_by: Identity
```

A live model was told `text`, by `inform` or by a `prompt` the adapter could not pair with a response. It is not sent to the client.

### `ToolCalled`

```python
class ToolCalled(BaseModel):
    scope: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: Literal["tool_called"] = "tool_called"
    name: str
    input: dict[str, Any]
    output: Content
    is_error: bool
    cancelled: bool
```

A live model's tool call ran; `output` is what went back to the model. `cancelled` is true when the model withdrew the call — nothing went back; `output` is what the tool returned, empty when it was stopped first — or `Session.cancel` stopped it. It is not sent to the client.

### `Cancelled`

```python
class Cancelled(BaseModel):
    scope: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: Literal["cancelled"] = "cancelled"
    tools: list[str]
    triggered_by: Identity
```

`Session.cancel` stopped the tool calls that were running, named in `tools`; each still reports its own `ToolCalled`. It is not sent to the client.

### `Closed`

```python
class Closed(BaseModel):
    scope: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: Literal["closed"] = "closed"
    reason: Literal["closed", "hung_up", "failed"]
    message: str | None = None
    triggered_by: Identity | None = None
```

The session ended: `closed` by `close()`, `hung_up` when the person left, `failed` with the error's `message`. `triggered_by` says who ended it: the cause given to `close(triggered_by=)`, the person for `hung_up`, and no one (`None`) for `failed` and for the end of `async with`. It is the last event.

## Tools

### `Tool`

```python
@dataclass(frozen=True)
class Tool:
    name: str
    description: str = ""
    body: Body | None = None
    args: type[BaseModel] | None = None
    input_schema: dict[str, Any] | None = None
```

One tool. `body` is an async function from the arguments dict to a `Result`, which is `str | dict[str, Any] | list[Part]`; `args` is a pydantic model that validates the arguments and gives the schema.

### `Tool.schema`

```python
def schema(self) -> dict[str, Any]: ...
```

Returns `input_schema` when given, else the JSON schema of `args`, else `{"type": "object", "properties": {}}`.

### `Tool.definition`

```python
def definition(self) -> dict[str, Any]: ...
```

Returns `{"name": …, "description": …, "input_schema": …}`.

### `Tool.call`

```python
async def call(self, args: dict[str, Any]) -> Result: ...
```

Validates `args` against `args` when given, passes on only the fields that were set, and returns what `body` returns.
Raises `ConfigurationError` when the tool has no body.
Raises `InputError` when the arguments do not validate.

### `Tools`

```python
class Tools:
    def __init__(self, tools: Iterable[Tool | ToolsLike] = ()) -> None: ...
    @property
    def names(self) -> list[str]: ...
    def __add__(self, other: Tools | ToolsLike) -> Tools: ...
    def __contains__(self, name: str) -> bool: ...
    def __len__(self) -> int: ...
```

A set of tools, the family's shared block, built from `Tool`s and any objects with `definitions()` and `dispatch()`, such as another `Tools`. `a + b` is their union, `name in tools` and `len(tools)` work, and `names` lists the tools in order.
Raises `ConfigurationError` when two tools share a name, here or in `+`.

### `Tools.definitions`

```python
def definitions(self) -> list[dict[str, Any]]: ...
```

Returns a copy of every tool's definition, `{name, description, input_schema}`, in order.

### `Tools.dispatch`

```python
async def dispatch(self, name: str, args: dict[str, Any]) -> Result: ...
```

Calls the tool named `name` with `args` and returns its result; anything the tool raises passes through.
Raises `InputError` when no tool has that name.

### `Tools.only`

```python
def only(self, *names: str) -> Tools: ...
```

Returns a `Tools` of just `names`, in that order.
Raises `InputError` when a name is not one of the tools.

### `Tools.without`

```python
def without(self, *names: str) -> Tools: ...
```

Returns a `Tools` without `names`; a name that is not one of the tools is ignored.

### `SensesTools`

```python
class SensesTools(Tools):
    session: Session
    triggered_by: Identity

    def __init__(self, session: Session, *, triggered_by: IdentityLike) -> None: ...
```

The session's verbs as tools for a text mind, pinned to one session and one cause: `look(frames)` with `video=True`, `say(text)` and `interrupt()` when the session has a voice, `prompt(text)` and `inform(text)` with a live model. They return the parts, `{"heard", "interrupted"}`, `{"heard"}`, `{"said", "heard", "interrupted"}` and `"informed"`; `prompt` returns `"prompted"` when `Session.prompt` returns `None`. A call that fails raises the session verb's error.
Raises `InputError` when `triggered_by` does not fit.

## Transports

### `serve`

```python
async def serve(connection: Connection, *, identity: IdentityLike, scope: str) -> Transport | None: ...
```

Reads the client's hello from `connection`, checks its protocol version, and returns a `Transport` over the socket for the person and scope you name. Returns `None` when the hello is refused, after sending the client an `error` frame and closing, or when the connection ends before a hello.
Raises `InputError` when `identity` does not fit or `scope` is empty.

### `Connection`

```python
class Connection(Protocol):
    async def receive(self) -> str | bytes: ...
    async def send_text(self, data: str) -> None: ...
    async def send_bytes(self, data: bytes) -> None: ...
    async def close(self) -> None: ...
```

The socket `serve` takes: any object with these four methods. `receive` returns the next text or binary message; when it raises, the person has left.

### `Transport`

```python
class Transport(Protocol):
    @property
    def identity(self) -> IdentityLike: ...
    @property
    def scope(self) -> str: ...
    def inbound(self) -> AsyncIterator[AudioFrame | Image | Played]: ...
    async def send_audio(self, frame: AudioFrame, utterance: int) -> None: ...
    async def send_control(self, control: dict[str, Any]) -> None: ...
    def clear_audio(self) -> None: ...
    async def close(self) -> None: ...
```

What a session runs on: the person, the scope, and a two-way link to them. `inbound()` yields the person's audio, JPEG camera frames and `Played` reports and ends when they leave; a far end that is gone ends `inbound()`, and a send method does not raise for it. A send the session makes that does raise (audio, the `speaking`, `silent`, `stop` and `ready` controls, `clear_audio`) closes the session as `failed` with a `TransportError`; the events it reports to the client are best effort, and `inbound()` ending is how a gone far end is seen; `clear_audio` drops audio not yet sent; stopping what the far end already has is the `stop` control frame, so a transport whose far end buffers audio flushes it when `stop` arrives. `serve`, `accept_offer` and `Loopback` make one; a telephone line or a robot's microphone is one of your own.

### `Played`

```python
class Played(BaseModel):
    utterance: int
    ms: int
```

The client's report of how many milliseconds of an utterance it played, yielded by `Transport.inbound()`: one per utterance, once it has played all of it after a `silent`, or when a `stop` names it or a later utterance. A transport that learns how much played as it goes keeps the count and answers with it at once. A packet lost or too late to play counts as played, so loss alone does not shorten a full play.

### `Loopback`

```python
class Loopback:
    identity: Identity
    scope: str
    audio: list[AudioFrame]
    controls: list[dict[str, Any]]
    closed: bool

    def __init__(
        self,
        identity: IdentityLike,
        scope: str,
        *,
        played: Callable[[int, float], int] | None = None,
        answer: bool = True,
    ) -> None: ...
```

An in-process transport, for tests and anything that is not a browser. It records the audio and control frames the person would receive in `audio` and `controls`, and answers each utterance once with a `Played`, on its `silent` or on a `stop` naming it or a later one: all the audio sent by default, `played(utterance, sent_ms)` when given, nothing with `answer=False`.
Raises `InputError` when `identity` does not fit.

### `Loopback.speak`

```python
async def speak(self, frame: AudioFrame) -> None: ...
```

Delivers `frame` to the session as the person's microphone.

### `Loopback.show`

```python
async def show(self, jpeg: bytes) -> None: ...
```

Delivers `jpeg` to the session as a camera frame, an `Image` of type `image/jpeg`.

### `Loopback.hang_up`

```python
async def hang_up(self) -> None: ...
```

Ends `inbound()`, as the person leaving; the session closes with reason `hung_up`.

### `Loopback.inbound`

```python
async def inbound(self) -> AsyncIterator[AudioFrame | Image | Played]: ...
```

Yields what `speak` and `show` delivered and the `Played` answers, in order, until `hang_up` or `close`.

### `Loopback.send_audio`

```python
async def send_audio(self, frame: AudioFrame, utterance: int) -> None: ...
```

Appends `frame` to `audio` and counts it toward `utterance`.

### `Loopback.send_control`

```python
async def send_control(self, control: dict[str, Any]) -> None: ...
```

Appends `control` to `controls`. A `silent` is answered with a `Played` for its utterance, and a `stop` with one for every utterance through it not yet answered, oldest first, unless made with `answer=False`.

### `Loopback.clear_audio`

```python
def clear_audio(self) -> None: ...
```

Does nothing; what `audio` recorded stays.

### `Loopback.close`

```python
async def close(self) -> None: ...
```

Sets `closed` and ends `inbound()`.

## WebRTC

### `accept_offer`

```python
async def accept_offer(sdp: str, *, identity: IdentityLike, scope: str, sample_rate: int = 16000, ice_servers: list[dict[str, Any]] | None = None) -> WebRTCTransport: ...
```

Answers the browser's SDP offer for the person and scope you name and returns the transport, with the answer in its `answer`. It lives in `rfnry_senses.webrtc` and needs the `webrtc` extra; `ice_servers` takes STUN and TURN servers in the shape of the browser's `iceServers`.
The voice is Opus, which browsers offer first: an offer that lists another codec ahead of it is answered with Opus alone, and an offer without it, from a G.711 or G.722 peer, is refused.
Raises `InputError` when `identity` does not fit or `scope` is empty.
Raises `TransportError` when the offer has no Opus or cannot be answered.

### `WebRTCTransport`

```python
class WebRTCTransport:
    identity: Identity
    scope: str
    answer: str

    def __init__(
        self,
        identity: IdentityLike,
        scope: str,
        *,
        sample_rate: int = 16000,
        ice_servers: list[dict[str, Any]] | None = None,
    ) -> None: ...
```

A `Transport` over one peer connection: the microphone and an optional camera track in, the voice out, the control frames on a data channel. It lives in `rfnry_senses.webrtc`, needs the `webrtc` extra, and is made by `accept_offer`.
Raises `InputError` when `identity` does not fit.

### `WebRTCTransport.greeted`

```python
async def greeted(self) -> bool: ...
```

Waits for the client's hello. `True` once it is accepted; `False`, with the transport closed, when it is refused or the transport ends first. Call it first and open no session on `False`, as with a `None` from `serve`, so a client that is turned away opens no live model. A peer that stays connected and never says hello keeps it waiting: to stop sooner, put a timeout on it and call `close`.

### `WebRTCTransport.inbound`

```python
async def inbound(self) -> AsyncIterator[AudioFrame | Image | Played]: ...
```

Yields the microphone resampled to mono PCM at `sample_rate`, camera frames encoded to JPEG, and `Played` reports. It ends when the hello is refused, the peer connection fails or closes, or `close` is called.

### `WebRTCTransport.send_audio`

```python
async def send_audio(self, frame: AudioFrame, utterance: int) -> None: ...
```

Queues `frame` on the voice track, resampled to 48 kHz mono whatever its rate or channels, in 20 ms frames. The last frame of an utterance is padded with silence, so it is sent whole. The first 120 ms of each stretch of voice go at once, ahead of the clock, and its timestamps show the silence before it, so the far end's jitter buffer does not take a pause for delay.

### `WebRTCTransport.send_control`

```python
async def send_control(self, control_frame: dict[str, Any]) -> None: ...
```

Sends the control frame on the data channel, holding it until the client's hello is accepted; a refused hello sends only the `error`. `speaking` leaves with the utterance's first audio frame and `silent` with its last, each stamped with `sent_ms` as it leaves; `stop` and `event` frames go at once.

### `WebRTCTransport.clear_audio`

```python
def clear_audio(self) -> None: ...
```

Drops the queued voice, fading out over its next 60 ms instead of stopping mid-sample. For each utterance whose `speaking` had not yet reached the client, it puts `Played(utterance, 0)` on `inbound()` at once and ignores the client's later report for that utterance.

### `WebRTCTransport.close`

```python
async def close(self) -> None: ...
```

Closes the peer connection and ends `inbound()`. Closing it again does nothing.

## Records

### `Identity`

```python
class Identity(BaseModel):
    role: Literal["user", "assistant", "system"]
    id: str
    name: str | None = None
    metadata: dict[str, Any] = {}
```

Who is speaking or who caused something, the family's shared block. Every identity the library takes is an `IdentityLike`, read by these fields, and it comes back as an `Identity`.

### `IdentityLike`

```python
class IdentityLike(Protocol):
    @property
    def role(self) -> Literal["user", "assistant", "system"]: ...
    @property
    def id(self) -> str: ...
    @property
    def name(self) -> str | None: ...
    @property
    def metadata(self) -> dict[str, Any]: ...
```

What everything that takes an identity accepts: `serve`, `accept_offer`, `Loopback`, a transport's `identity`, each verb's `triggered_by`, `SensesTools`. Any object with these four fields will do, such as an `Identity` or rfnry-thread's; its values are validated as strictly as an `Identity`'s, and one that does not fit raises `InputError` from the call that took it.

### `Text`

```python
class Text(BaseModel):
    type: Literal["text"] = "text"
    text: str
```

A text part.

### `Image`

```python
class Image(BaseModel):
    type: Literal["image"] = "image"
    media_type: str
    data: bytes | None = None
    url: str | None = None
```

An image part, carrying `data` or a `url`; `data` travels as base64 in JSON. Camera frames are JPEG images.
Raises `InputError` when it has both or neither of `data` and `url`.

### `Audio`

```python
class Audio(BaseModel):
    type: Literal["audio"] = "audio"
    media_type: str
    data: bytes | None = None
    url: str | None = None
```

An audio part, carrying `data` or a `url`; `data` travels as base64 in JSON.
Raises `InputError` when it has both or neither of `data` and `url`.

### `Document`

```python
class Document(BaseModel):
    type: Literal["document"] = "document"
    media_type: str
    data: bytes | None = None
    url: str | None = None
    name: str | None = None
```

A document part, carrying `data` or a `url`; `data` travels as base64 in JSON.
Raises `InputError` when it has both or neither of `data` and `url`.

### `Part`

```python
Part = Annotated[Text | Image | Audio | Document, Field(discriminator="type")]
```

Any part, told apart by `type`.

### `Content`

```python
Content = str | list[Part]
```

Text, or a list of parts: what a tool result carries.

### `AudioFrame`

```python
class AudioFrame(BaseModel):
    data: bytes
    sample_rate: int
    channels: int = 1

    @property
    def duration_ms(self) -> float: ...
```

Audio as 16-bit little-endian PCM at `sample_rate`, one or two channels; frozen. `duration_ms` is how long it plays.
Raises `InputError` when `sample_rate` is not positive, `channels` is not 1 or 2, or `data` does not fill whole samples.

### `ToolSpec`

```python
class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
```

A tool's definition as `LiveModel.open` receives it.

## Errors

### `SensesException`

```python
class SensesException(Exception): ...
```

The root of every error the library raises. Every message starts with the module that raised it, then says what went wrong in lowercase: `session: prompt needs a live model`.

### `ConfigurationError`

```python
class ConfigurationError(SensesException): ...
```

A session asked for what it was not set up to do: models that do not go together, `instructions` or `start(tools=)` without a live model, a second `start`, a verb without the model or `video=True` it needs, a limit that is not positive.

### `InputError`

```python
class InputError(SensesException): ...
```

A call the library refuses: empty text, `frames` below 1, a media part with both or neither of `data` and `url`, an audio frame that does not fit, an unknown tool or bad tool arguments, an identity whose fields do not fit, an empty scope.

### `NotFoundError`

```python
class NotFoundError(SensesException): ...
```

`look` found no frame in the camera window.

### `ModelError`

```python
class ModelError(SensesException): ...
```

A model failed: a live model reported `LiveFailed`, ended its events or broke the response contract, and the session closed; or a speech model failed during the `say` that needed it.

### `TransportError`

```python
class TransportError(SensesException): ...
```

A send the transport failed (the session closes with reason `failed`), a verb on a session not yet started or already closed, `start` on a closed session, a `say` or `prompt` whose session closed before it was spoken, or an offer `accept_offer` cannot answer.

## TypeScript client

### `connect`

```ts
function connect(options: ConnectOptions): Promise<Senses>
```

Connects to your senses endpoint, starts the microphone and, with `camera`, the video, and resolves with the `Senses` once the server is ready.
Rejects with `SensesException` when the server refuses the hello, the connection closes or fails before `connect` resolves, the session fails before it, with the server's reason, the WebRTC offer is refused, `camera` names both `facing` and `deviceId`, or `signal` aborts. A connection that ends during a permission prompt rejects at once. Whatever `connect` took is released when it rejects, and a device granted after that is stopped as it arrives.

### `ConnectOptions`

```ts
type ConnectOptions = {
  url: string
  transport: 'websocket' | 'webrtc'
  protocols?: string | string[]
  headers?: Record<string, string>
  microphone?: boolean
  camera?:
    | false
    | ({ source?: VideoSource; fps?: number; width?: number; quality?: number } & CameraChoice)
  sampleRate?: number
  iceServers?: RTCIceServer[]
  signal?: AbortSignal
}
```

`url` is the WebSocket endpoint, or the offer endpoint over WebRTC; `protocols` goes with the WebSocket and `headers` with the offer's request. `microphone: false` connects to listen only, and `sampleRate` is the microphone's PCM rate over a WebSocket (16000), filtered and resampled from the browser's own rate. `camera` sends video from `source` (`'camera'`) at `fps` (1) and `width` (640), as JPEG at `quality` (0.7) over a WebSocket. `signal` gives up on `connect`, on either transport: an abort at any point before it resolves rejects with a `SensesException` and releases everything taken, the offer's request included. One that has already aborted rejects at once and takes nothing, and after `connect` has resolved it does nothing.

### `CameraChoice`

```ts
type CameraChoice = { facing?: 'user' | 'environment'; deviceId?: string }
```

Which camera: `facing`, a preference, or `deviceId`, exact; one or the other, never both.

### `VideoSource`

```ts
type VideoSource = 'camera' | 'screen'
```

Where the video comes from.

### `Senses`

```ts
interface Senses {
  readonly status: SensesStatus
  subscribe(listener: SensesListener): () => void
  mute(): void
  unmute(): void
  shareScreen(): Promise<void>
  useCamera(choice?: CameraChoice): Promise<void>
  pauseCamera(): void
  resumeCamera(): void
  setFps(fps: number): void
  close(): Promise<void>
}
```

A connection, from `connect`. `status` is the latest status.

### `Senses.subscribe`

```ts
subscribe(listener: SensesListener): () => void
```

Calls `listener` with the current status at once, then with every event the session sends, each video switch and each status change. Returns a function that unsubscribes. A listener that throws on an event stops no other listener and nothing the client is doing: the error is rethrown on its own, as an uncaught error for `window.onerror`.

### `Senses.mute`

```ts
mute(): void
```

Stops sending the microphone; the connection stays open.

### `Senses.unmute`

```ts
unmute(): void
```

Sends the microphone again.

### `Senses.shareScreen`

```ts
shareScreen(): Promise<void>
```

Switches the video to a screen the person picks. A cancelled picker rejects and leaves the camera on.
Rejects with `SensesException` when connected without the `camera` option.

### `Senses.useCamera`

```ts
useCamera(choice?: CameraChoice): Promise<void>
```

Switches the video to the camera `choice` names, or to the last camera chosen. A `deviceId` that is gone rejects.
Rejects with `SensesException` when connected without the `camera` option, or when `choice` names both `facing` and `deviceId`.

### `Senses.pauseCamera`

```ts
pauseCamera(): void
```

Stops sending video, from either source, until `resumeCamera`.

### `Senses.resumeCamera`

```ts
resumeCamera(): void
```

Sends video again.

### `Senses.setFps`

```ts
setFps(fps: number): void
```

Changes the frame rate while the video runs: the sampling interval over a WebSocket, the track's frame rate and its cap over WebRTC.
Throws `SensesException` when `fps` is not positive or there is no camera.

### `Senses.close`

```ts
close(): Promise<void>
```

Stops the microphone, the video and playback, closes the connection, and sets `status` to `'closed'`. Resolves once all of it is released.

Every end from the far side runs this same stop on its own: the server's `closed` event, the socket or data channel closing, or the peer going `failed`. It runs at most once, so `close` after that, or a second `close`, resolves and does nothing, and no verb reopens a device. A `closed` event reaches subscribers first, then everything is released, then `status` changes and subscribers are told.

### `SensesStatus`

```ts
type SensesStatus = 'open' | 'closed'
```

`'open'` until the connection ends, by `close()` or from the far side. When it turns `'closed'` the microphone and the video have stopped.

### `SensesListener`

```ts
type SensesListener = (
  event:
    | SensesEvent
    | { kind: 'status'; status: SensesStatus }
    | { kind: 'video'; source: VideoSource | null }
) => void
```

What `subscribe` takes: the session's events, status changes, and each switch of the video source, `null` when no video is sent.

### `SensesEvent`

```ts
type Stamp = { scope: string; at: string }

type SensesEvent =
  | (Stamp & { kind: 'heard'; text: string; final: boolean; speaker: Identity })
  | (Stamp & { kind: 'turn_ended'; text: string; speaker: Identity })
  | (Stamp & {
      kind: 'said'
      text: string
      heard: string
      interrupted: boolean
      triggered_by: Identity | null
    })
  | (Stamp & { kind: 'interrupted'; heard: string; triggered_by: Identity })
  | (Stamp & { kind: 'speaking' })
  | (Stamp & { kind: 'silent' })
  | (Stamp & { kind: 'looked'; frames: number; triggered_by: Identity })
  | (Stamp & { kind: 'muted'; muted: boolean; triggered_by: Identity })
  | (Stamp & {
      kind: 'closed'
      reason: 'closed' | 'hung_up' | 'failed'
      message: string | null
      triggered_by: Identity | null
    })
```

The session's events as the client receives them: every `Event` but `Informed`, `ToolCalled` and `Cancelled`, with `at` as an ISO 8601 string.

### `Identity`

```ts
type Identity = {
  role: 'user' | 'assistant' | 'system'
  id: string
  name: string | null
  metadata: Record<string, unknown>
}
```

An identity as the events carry it.

### `SensesException`

```ts
class SensesException extends Error {
  constructor(message: string)
}
```

The client's one error class. Its messages have the server's shape: the module, then what went wrong.
