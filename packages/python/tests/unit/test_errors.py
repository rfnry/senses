import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from rfnry_senses.contracts import AudioFrame
from rfnry_senses.errors import (
    ConfigurationError,
    InputError,
    ModelError,
    NotFoundError,
    SensesException,
    TransportError,
)
from rfnry_senses.models import ToolSpec
from rfnry_senses.session import Session
from rfnry_senses.transport import Loopback

from .fakes import ANA, HOST, ScriptedConnection, ScriptedLive, ScriptedTranscription, Tone


@pytest.mark.parametrize(
    "error", [ConfigurationError, InputError, ModelError, NotFoundError, TransportError]
)
def test_every_error_is_a_senses_error(error: type[Exception]) -> None:
    assert issubclass(error, SensesException) and error.__name__.endswith("Error")


def test_the_root_takes_the_exception_suffix() -> None:
    assert SensesException.__name__ == "SensesException" and SensesException.__bases__ == (
        Exception,
    )


class Refusing(ScriptedConnection):
    def __init__(self, refusal: Exception) -> None:
        super().__init__()
        self.refusal: Exception | None = refusal

    def _refuse(self) -> None:
        if self.refusal is not None:
            raise self.refusal

    async def say(self, text: str) -> str | None:
        self._refuse()
        return await super().say(text)

    async def send_text(self, text: str, *, respond: bool) -> str | None:
        self._refuse()
        return await super().send_text(text, respond=respond)


class Unreachable(ScriptedLive):
    def __init__(self, refusal: Exception) -> None:
        super().__init__()
        self.refusal = refusal

    async def open(self, *, tools: list[ToolSpec], instructions: str | None) -> ScriptedConnection:
        raise self.refusal


class Unheard(ScriptedTranscription):
    def __init__(self, refusal: Exception) -> None:
        super().__init__()
        self.refusal = refusal

    def stream(self) -> ScriptedTranscription:
        raise self.refusal


class Voiceless(Tone):
    def __init__(self, refusal: Exception) -> None:
        super().__init__()
        self.refusal = refusal

    def speak(self, text: str) -> AsyncIterator[AudioFrame]:
        raise self.refusal


class Breaking(Tone):
    def __init__(self, refusal: Exception) -> None:
        super().__init__()
        self.refusal = refusal

    async def speak(self, text: str) -> AsyncIterator[AudioFrame]:
        raise self.refusal
        yield


VERBS: dict[str, Callable[[Session], Awaitable[object]]] = {
    "say": lambda s: s.say("Your table is ready.", triggered_by=HOST),
    "prompt": lambda s: s.prompt("Greet the guest.", triggered_by=HOST),
    "inform": lambda s: s.inform("The guest is vegetarian.", triggered_by=HOST),
}


def refusing(refusal: Exception) -> tuple[Session, Refusing]:
    live = ScriptedLive()
    live.connection = connection = Refusing(refusal)
    return Session(Loopback(ANA, "acme"), live=live), connection


def opening(model: str, refusal: Exception) -> Session:
    models: dict[str, Any] = (
        {"live": Unreachable(refusal)}
        if model == "live"
        else {"transcription": Unheard(refusal), "speech": Tone()}
    )
    return Session(Loopback(ANA, "acme"), **models)


@pytest.mark.parametrize("verb", VERBS)
async def test_a_live_model_that_fails_a_verb_raises_model_error_from_it(verb: str) -> None:
    refusal = ConnectionError("the vendor hung up")
    s, connection = refusing(refusal)
    async with s:
        with pytest.raises(
            ModelError, match="^session: the live model failed: the vendor hung up$"
        ) as raised:
            await VERBS[verb](s)
        connection.refusal = None
        await s.inform("Still here.", triggered_by=HOST)
    assert raised.value.__cause__ is refusal
    assert connection.calls == [("text", "Still here.", False)]


@pytest.mark.parametrize("verb", VERBS)
async def test_a_library_error_a_live_model_raises_reaches_the_verb_unchanged(verb: str) -> None:
    refusal = InputError("adapter: the voice is not one the vendor has")
    s, _ = refusing(refusal)
    async with s:
        with pytest.raises(InputError) as raised:
            await VERBS[verb](s)
    assert raised.value is refusal


@pytest.mark.parametrize("model", ["live", "transcription"])
async def test_a_model_that_fails_to_open_raises_model_error_from_start(model: str) -> None:
    refusal = ConnectionError("the vendor hung up")
    s = opening(model, refusal)
    with pytest.raises(
        ModelError, match=f"^session: the {model} model failed: the vendor hung up$"
    ) as raised:
        await s.start()
    await s.close()
    assert raised.value.__cause__ is refusal


@pytest.mark.parametrize("model", ["live", "transcription"])
async def test_a_library_error_a_model_raises_as_it_opens_reaches_start_unchanged(
    model: str,
) -> None:
    refusal = ConfigurationError("adapter: the model is not one the vendor has")
    s = opening(model, refusal)
    with pytest.raises(ConfigurationError) as raised:
        await s.start()
    await s.close()
    assert raised.value is refusal


@pytest.mark.parametrize("speech", [Voiceless, Breaking])
async def test_a_speech_model_that_fails_raises_model_error_from_say(
    speech: type[Voiceless | Breaking],
) -> None:
    refusal = ConnectionError("the vendor hung up")
    s = Session(
        Loopback(ANA, "acme"), transcription=ScriptedTranscription(), speech=speech(refusal)
    )
    async with s:
        with pytest.raises(
            ModelError, match="^session: the speech model failed: the vendor hung up$"
        ) as raised:
            await s.say("Your table is ready.", triggered_by=HOST)
    assert raised.value.__cause__ is refusal


@pytest.mark.parametrize("speech", [Voiceless, Breaking])
async def test_a_library_error_a_speech_model_raises_reaches_say_unchanged(
    speech: type[Voiceless | Breaking],
) -> None:
    refusal = InputError("speech: the voice is not known")
    s = Session(
        Loopback(ANA, "acme"), transcription=ScriptedTranscription(), speech=speech(refusal)
    )
    async with s:
        with pytest.raises(InputError) as raised:
            await s.say("Your table is ready.", triggered_by=HOST)
    assert raised.value is refusal


class Dropping(ScriptedConnection):
    def __init__(self, refusal: Exception) -> None:
        super().__init__()
        self.refusal = refusal
        self.asked = asyncio.Event()

    async def say(self, text: str) -> str | None:
        self.asked.set()
        return await super().say(text)

    async def events(self) -> AsyncIterator[Any]:
        await self.asked.wait()
        raise self.refusal
        yield


async def test_a_live_model_that_fails_in_the_background_ends_the_say_with_its_cause() -> None:
    refusal = ConnectionError("the vendor hung up")
    live = ScriptedLive()
    live.connection = Dropping(refusal)
    s = Session(Loopback(ANA, "acme"), live=live)
    async with s:
        with pytest.raises(ModelError, match="^session: the vendor hung up$") as raised:
            await s.say("Your table is ready.", triggered_by=HOST)
    assert raised.value.__cause__ is refusal
