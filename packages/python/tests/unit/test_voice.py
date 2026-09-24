import pytest

from rfnry_senses.voice import Ledger, sentences

from .fakes import pcm


def test_text_is_cut_into_sentences_keeping_their_spacing() -> None:
    assert sentences("Your table is ready. Shall I order the usual?") == [
        "Your table is ready. ",
        "Shall I order the usual?",
    ]
    assert sentences("Wait... what?! Fine") == ["Wait... ", "what?! ", "Fine"]
    assert sentences("v1.2 is out") == ["v1.2 is out"]


def test_heard_keeps_whole_sentences_played() -> None:
    ledger = Ledger()
    for sentence in ("One. ", "Two. "):
        ledger.sent(pcm(100), now=0.0)
        ledger.mark(sentence)
    assert ledger.heard(98) == ""
    assert ledger.heard(99) == ledger.heard(100) == "One. "
    assert ledger.heard(150) == "One. "
    assert ledger.heard(200) == ledger.heard(None) == "One. Two. "


def test_live_marks_end_with_the_audio_that_follows_them() -> None:
    ledger = Ledger()
    ledger.mark("Let me ", ahead=True)
    ledger.sent(pcm(100), now=0.0)
    ledger.mark("check. ", ahead=True)
    assert ledger.heard(100) == "Let me "
    assert ledger.heard(None) == "Let me check. "


def test_the_estimate_is_capped_by_what_was_sent() -> None:
    ledger = Ledger()
    assert ledger.estimate(5.0) == 0.0
    ledger.sent(pcm(300), now=10.0)
    assert ledger.estimate(10.15) == pytest.approx(150.0)
    assert ledger.estimate(99.0) == 300.0
    assert ledger.remaining(10.1) == pytest.approx(0.2)
