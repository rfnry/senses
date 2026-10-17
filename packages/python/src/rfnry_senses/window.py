from collections import deque
from datetime import UTC, datetime
from time import monotonic

from rfnry_senses.models import Image


class FrameWindow:
    def __init__(self, seconds: float, max_bytes: int) -> None:
        self.seconds = seconds
        self.max_bytes = max_bytes
        self._frames: deque[tuple[float, datetime, Image]] = deque()
        self._bytes = 0

    def append(self, image: Image) -> None:
        self._frames.append((monotonic(), datetime.now(UTC), image))
        self._bytes += len(image.data or b"")
        self._evict()

    def newest(self, count: int) -> list[tuple[datetime, Image]]:
        self._evict()
        return [(at, image) for _, at, image in list(self._frames)[-count:]]

    def _evict(self) -> None:
        now = monotonic()
        while self._frames and now - self._frames[0][0] > self.seconds:
            self._drop()
        while len(self._frames) > 1 and self._bytes > self.max_bytes:
            self._drop()

    def _drop(self) -> None:
        _, _, image = self._frames.popleft()
        self._bytes -= len(image.data or b"")
