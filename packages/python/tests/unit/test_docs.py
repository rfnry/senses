import re
from pathlib import Path

import rfnry_senses

ROOT = Path(__file__).parents[4]
DOCS = ROOT / "docs" / "senses.md"
VENDORS = re.compile(
    r"\b(anthropic|claude|openai|gpt|gemini|google|deepgram|elevenlabs|cartesia|twilio|azure|aws|voyage|typesafe)\b",
    re.I,
)


def test_every_public_name_is_documented() -> None:
    text = DOCS.read_text(encoding="utf-8")
    names = [*rfnry_senses.__all__, "WebRTCTransport", "accept_offer"]
    assert [name for name in names if name not in text] == []


def test_no_vendor_is_named_in_the_library_or_its_contract() -> None:
    files = [
        DOCS,
        *(ROOT / "packages/python/src").rglob("*.py"),
        *(ROOT / "packages/typescript/src").rglob("*.ts"),
    ]
    assert [str(f) for f in files if VENDORS.search(f.read_text(encoding="utf-8"))] == []
