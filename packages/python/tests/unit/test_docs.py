import re
from pathlib import Path

import rfnry_senses

ROOT = Path(__file__).parents[4]
DOCS = ROOT / "docs" / "senses.md"
TYPESCRIPT = ROOT / "packages" / "typescript" / "src"
VENDORS = re.compile(
    r"\b(anthropic|claude|openai|gpt|gemini|google|deepgram|elevenlabs|cartesia|twilio|azure|aws|voyage|typesafe)\b",
    re.I,
)


def test_every_public_name_is_documented() -> None:
    text = DOCS.read_text(encoding="utf-8")
    names = [*rfnry_senses.__all__, "WebRTCTransport", "accept_offer"]
    assert [name for name in names if name not in text] == []


def typescript_exports(index: str) -> set[str]:
    names = {
        name.strip().removeprefix("type ")
        for group in re.findall(r"export (?:type )?\{([^}]*)\} from", index)
        for name in group.split(",")
        if name.strip()
    }
    for module in re.findall(r"export (?:type )?\* from '\./([\w-]+)\.js'", index):
        source = (TYPESCRIPT / f"{module}.ts").read_text(encoding="utf-8")
        names |= set(
            re.findall(
                r"^export (?:type|interface|class|const|(?:async )?function) (\w+)", source, re.M
            )
        )
    return names


def test_every_typescript_export_is_documented() -> None:
    text = DOCS.read_text(encoding="utf-8")
    names = typescript_exports((TYPESCRIPT / "index.ts").read_text(encoding="utf-8"))
    assert sorted(name for name in names if f"`{name}`" not in text) == []


def test_the_typescript_exports_follow_an_export_star() -> None:
    assert {"SensesEvent", "ServerControl", "AudioOut"} <= typescript_exports(
        "export type * from './wire.js'\n"
    )


def typescript_fields(name: str) -> set[str]:
    declaration = f"export type {name} = {{"
    for path in sorted(TYPESCRIPT.glob("*.ts")):
        source = path.read_text(encoding="utf-8")
        if declaration in source:
            break
    start = source.index(declaration) + len(declaration) - 1
    depth = 0
    for end in range(start, len(source)):
        depth += {"{": 1, "}": -1}.get(source[end], 0)
        if depth == 0:
            break
    block = source[start : end + 1]
    names = set(re.findall(r"(\w+)\??:", block))
    for other in re.findall(r"& (\w+)", block):
        names |= typescript_fields(other)
    return names


def test_every_typescript_connect_option_is_documented() -> None:
    text = DOCS.read_text(encoding="utf-8")
    names = typescript_fields("ConnectOptions")
    assert {"microphone", "camera", "quality", "facing", "deviceId"} <= names
    assert sorted(n for n in names if not re.search(rf"`[^`\n]*\b{n}\b[^`\n]*`", text)) == []


def test_no_vendor_is_named_in_the_library_or_its_contract() -> None:
    files = [
        DOCS,
        *(ROOT / "packages/python/src").rglob("*.py"),
        *(ROOT / "packages/typescript/src").rglob("*.ts"),
    ]
    assert [str(f) for f in files if VENDORS.search(f.read_text(encoding="utf-8"))] == []
