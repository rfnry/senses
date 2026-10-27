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
            raise InputError(
                f"contracts: {len(self.data)} bytes do not fill {self.channels}-channel 16-bit samples"
            )
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
    id: str | None = None
    requested: bool | None = None


class LiveDone(BaseModel):
    pass


class LiveToolCall(BaseModel):
    call_id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class LiveToolCancelled(BaseModel):
    call_ids: list[str]


class LiveInterrupted(BaseModel):
    pass


class LiveFailed(BaseModel):
    message: str


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
    async def interrupt(self, played_ms: int, *, heard: str, response: str | None) -> None: ...
    async def submit_result(
        self, call_id: str, result: Content, is_error: bool, *, respond: bool
    ) -> None: ...
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
