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
        raise InputError(
            f"wire: the first frame must be a hello: {exc.errors(include_url=False, include_input=False)}"
        ) from exc


def parse_played(text: str) -> Played:
    try:
        played = _Played.model_validate_json(text)
    except ValidationError as exc:
        raise InputError(
            f"wire: not a played frame: {exc.errors(include_url=False, include_input=False)}"
        ) from exc
    return Played(utterance=played.utterance, ms=played.ms)


def control(frame: dict[str, Any]) -> str:
    return json.dumps(frame, separators=(",", ":"))
