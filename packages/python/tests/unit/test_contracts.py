import pytest

from rfnry_senses.contracts import AudioFrame
from rfnry_senses.errors import InputError


def test_a_frame_knows_its_duration() -> None:
    assert AudioFrame(data=bytes(640), sample_rate=16000).duration_ms == 20.0
    assert AudioFrame(data=bytes(960), sample_rate=24000, channels=2).duration_ms == 10.0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"data": bytes(3), "sample_rate": 16000},
            "^contracts: 3 bytes do not fill 1-channel 16-bit samples$",
        ),
        ({"data": bytes(4), "sample_rate": 0}, "^contracts: sample rate 0 is not positive$"),
        (
            {"data": bytes(6), "sample_rate": 16000, "channels": 3},
            "^contracts: audio has 1 or 2 channels, not 3$",
        ),
    ],
)
def test_a_frame_is_whole_samples(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(InputError, match=message):
        AudioFrame.model_validate(kwargs)
