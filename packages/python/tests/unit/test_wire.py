import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from rfnry_senses.contracts import AudioFrame
from rfnry_senses.errors import InputError
from rfnry_senses.events import (
    Closed,
    Heard,
    Interrupted,
    Looked,
    Muted,
    Said,
    Silent,
    Speaking,
    TurnEnded,
)
from rfnry_senses.models import Image
from rfnry_senses.transport import Played
from rfnry_senses.wire import decode, encode_audio, encode_image, parse_hello, parse_played

from .fakes import ANA, HOST

FIXTURES = Path(__file__).parents[1] / "fixtures" / "wire"
AT = datetime(2026, 9, 24, 12, 3, 4, 200000, tzinfo=UTC)


def controls() -> dict[str, dict[str, Any]]:
    events = {
        "heard": Heard(scope="acme", at=AT, text="a table for two", final=True, speaker=ANA),
        "turn_ended": TurnEnded(scope="acme", at=AT, text="A table for two.", speaker=ANA),
        "said": Said(
            scope="acme",
            at=AT,
            text="Your table is ready. Shall I order the usual?",
            heard="Your table is ready. ",
            interrupted=True,
            triggered_by=HOST,
        ),
        "interrupted": Interrupted(
            scope="acme", at=AT, heard="Your table is ready. ", triggered_by=ANA
        ),
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
        "error": {
            "type": "error",
            "message": "transport: protocol 2 is not supported; this server speaks 1",
        },
        **{
            f"event-{kind}": {"type": "event", "event": event.model_dump(mode="json")}
            for kind, event in events.items()
        },
    }


BINARY = {
    "audio": encode_audio(AudioFrame(data=b"\x00\x01\xff\x7f", sample_rate=16000), 3),
    "image": encode_image(b"\xff\xd8\xff\xd9"),
}


def test_the_golden_fixtures_are_what_the_code_writes() -> None:
    if os.environ.get("RFNRY_WRITE_FIXTURES") == "1":
        FIXTURES.mkdir(parents=True, exist_ok=True)
        for name, frame in controls().items():
            (FIXTURES / f"{name}.json").write_text(
                json.dumps(frame, indent=2) + "\n", encoding="utf-8"
            )
        for name, data in BINARY.items():
            (FIXTURES / f"{name}.hex").write_text(data.hex() + "\n", encoding="utf-8")
    for name, frame in controls().items():
        assert json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8")) == frame, name
    for name, data in BINARY.items():
        assert (FIXTURES / f"{name}.hex").read_text(encoding="utf-8").strip() == data.hex(), name
    assert BINARY["audio"].hex() == "010180" + "3e0000" + "03000000" + "0001ff7f"


def test_frames_decode_to_what_was_encoded() -> None:
    frame = decode(BINARY["audio"])
    assert (
        isinstance(frame, AudioFrame)
        and frame.sample_rate == 16000
        and frame.data == b"\x00\x01\xff\x7f"
    )
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
