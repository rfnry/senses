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
