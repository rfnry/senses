from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator

from rfnry_senses.contracts import AudioFrame


def sentences(text: str) -> list[str]:
    found: list[str] = []
    start = i = 0
    n = len(text)
    while i < n:
        if text[i] in ".!?":
            end = i + 1
            while end < n and text[end] in ".!?":
                end += 1
            if end < n and text[end].isspace():
                while end < n and text[end].isspace():
                    end += 1
                found.append(text[start:end])
                start = i = end
                continue
        i += 1
    if text[start:].strip():
        found.append(text[start:])
    return found


async def sentences_from(text: str | AsyncIterator[str]) -> AsyncGenerator[str]:
    if isinstance(text, str):
        for sentence in sentences(text):
            yield sentence
        return
    held = ""
    async for chunk in text:
        held += chunk
        *complete, held = sentences(held) or [""]
        for sentence in complete:
            yield sentence
    if held.strip():
        yield held


class Ledger:
    def __init__(self) -> None:
        self.sent_ms = 0.0
        self.started_at: float | None = None
        self._marks: list[tuple[str, float]] = []
        self._ahead: list[str] = []

    def sent(self, frame: AudioFrame, now: float) -> None:
        if self.started_at is None:
            self.started_at = now
        self.sent_ms += frame.duration_ms
        self._marks += [(text, self.sent_ms) for text in self._ahead]
        self._ahead = []

    def mark(self, text: str, *, ahead: bool = False) -> None:
        if ahead:
            self._ahead.append(text)
        else:
            self._marks.append((text, self.sent_ms))

    def heard(self, played_ms: float | None) -> str:
        if played_ms is None:
            return "".join([*(text for text, _ in self._marks), *self._ahead])
        return "".join(text for text, end in self._marks if end <= played_ms + 1)

    def estimate(self, now: float) -> float:
        if self.started_at is None:
            return 0.0
        return min(self.sent_ms, (now - self.started_at) * 1000)

    def remaining(self, now: float) -> float:
        if self.started_at is None:
            return 0.0
        return max(0.0, self.sent_ms / 1000 - (now - self.started_at))
