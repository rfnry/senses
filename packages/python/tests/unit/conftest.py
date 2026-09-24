import pytest


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr("rfnry_senses.session.monotonic", fake)
    monkeypatch.setattr("rfnry_senses.window.monotonic", fake)
    return fake
