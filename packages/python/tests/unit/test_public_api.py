import annotationlib
import inspect
import re
import tomllib
from pathlib import Path

import pytest

import rfnry_senses
from rfnry_senses import Session, models

SOURCE = annotationlib.Format.STRING

EXPECTED = {
    "Session",
    "SensesTools",
    "Event",
    "Heard",
    "TurnEnded",
    "Said",
    "Interrupted",
    "Speaking",
    "Silent",
    "Looked",
    "Muted",
    "Informed",
    "ToolCalled",
    "Cancelled",
    "Closed",
    "LiveModel",
    "LiveConnection",
    "LiveEvent",
    "LiveAudio",
    "LiveHeard",
    "LiveSaid",
    "LiveStarted",
    "LiveDone",
    "LiveToolCall",
    "LiveToolCancelled",
    "LiveInterrupted",
    "LiveFailed",
    "TranscriptionModel",
    "Transcription",
    "Transcript",
    "UtteranceEnd",
    "SpeechModel",
    "AudioFrame",
    "Text",
    "Image",
    "Audio",
    "Document",
    "Part",
    "Content",
    "ToolSpec",
    "Tool",
    "Tools",
    "serve",
    "Connection",
    "Transport",
    "Played",
    "Loopback",
    "Identity",
    "IdentityLike",
    "SensesException",
    "ConfigurationError",
    "InputError",
    "NotFoundError",
    "ModelError",
    "TransportError",
}


def test_the_package_exports_exactly_its_public_names() -> None:
    assert set(rfnry_senses.__all__) == EXPECTED
    assert all(getattr(rfnry_senses, name) is not None for name in rfnry_senses.__all__)


def test_the_session_has_exactly_its_verbs() -> None:
    public = {n for n in vars(Session) if not n.startswith("_")}
    assert public == {
        "start",
        "say",
        "prompt",
        "inform",
        "interrupt",
        "cancel",
        "mute",
        "unmute",
        "look",
        "events",
        "quiet",
        "close",
    }


def test_the_identity_the_verbs_take_is_exported() -> None:
    assert rfnry_senses.IdentityLike is models.IdentityLike
    for verb in ("say", "prompt", "inform", "interrupt", "mute", "unmute", "look"):
        cause = inspect.signature(getattr(Session, verb), annotation_format=SOURCE).parameters[
            "triggered_by"
        ]
        assert cause.annotation == "IdentityLike"


def test_serve_returns_a_type_the_package_exports() -> None:
    returned = inspect.signature(rfnry_senses.serve, annotation_format=SOURCE).return_annotation
    assert set(re.findall(r"\w+", returned)) - {"None"} <= set(rfnry_senses.__all__)


def test_the_webrtc_extra_exports_its_names() -> None:
    pytest.importorskip("aiortc")
    import rfnry_senses.webrtc

    assert rfnry_senses.webrtc.__all__ == ["WebRTCTransport", "accept_offer"]


def test_the_version_is_the_one_the_package_is_built_with() -> None:
    project = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    assert rfnry_senses.__version__ == project["project"]["version"]
