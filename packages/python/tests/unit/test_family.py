from pathlib import Path

import pytest

ROOT = Path(__file__).parents[4]
FAMILY = ROOT.parent
OWN = ROOT / "packages/python/src/rfnry_senses"

BLOCKS = {
    "state": ("models.py", "State = ", "\n", ("engine", "memory")),
    "parts": (
        "models.py",
        "class Text(BaseModel):",
        "Content = str | list[Part]\n",
        ("engine", "memory", "thread"),
    ),
    "tool spec": (
        "models.py",
        "class ToolSpec(BaseModel):",
        "    input_schema: dict[str, Any]\n",
        ("engine", "memory"),
    ),
    "usage": (
        "models.py",
        "class Usage(BaseModel):",
        "            cache_read=self.cache_read + other.cache_read,\n        )\n",
        ("engine", "memory"),
    ),
    "decision": (
        "models.py",
        "class Choice(BaseModel):",
        "    model: str | None = None\n",
        ("engine", "memory"),
    ),
    "as_decided": ("models.py", "def as_decided(", "from exc\n", ("engine", "memory")),
    "usage like": (
        "models.py",
        "class UsageLike(Protocol):",
        "    def cache_read(self) -> int: ...\n",
        ("engine", "memory"),
    ),
    "answers like": (
        "models.py",
        "class ChoiceAnswerLike(Protocol):",
        "    def model(self) -> str | None: ...\n",
        ("engine", "memory"),
    ),
    "decision model": (
        "models.py",
        "class DecisionModel(Protocol):",
        "| DecidedLike: ...\n",
        ("engine",),
    ),
    "tools": ("tools.py", "Result = ", "if n not in names})\n", ("engine", "memory", "thread")),
    "identity": (
        "models.py",
        "class Identity(BaseModel):",
        'raise InputError(f"models: not an identity: {exc.errors(include_url=False)}") from exc\n',
        ("thread",),
    ),
}

SOURCES = {
    "engine": {
        "tools.py": "engine/packages/python/src/rfnry_engine/tools/tools.py",
        "models.py": "engine/packages/python/src/rfnry_engine/models.py",
    },
    "memory": {
        "tools.py": "memory/packages/python/src/rfnry_memory/tools.py",
        "models.py": "memory/packages/python/src/rfnry_memory/models.py",
    },
    "thread": {
        "tools.py": "thread/packages/python/src/rfnry_thread",
        "models.py": "thread/packages/python/src/rfnry_thread",
    },
}


def cut(text: str, start: str, end: str) -> str | None:
    first = text.find(start)
    if first < 0:
        return None
    last = text.find(end, first + len(start))
    return text[first:] if last < 0 else text[first : last + len(end)]


def sibling(name: str, own_file: str, start: str) -> str | None:
    path = FAMILY / SOURCES[name][own_file]
    if path.is_dir():
        return next(
            (
                f.read_text(encoding="utf-8")
                for f in sorted(path.rglob("*.py"))
                if start in f.read_text(encoding="utf-8")
            ),
            None,
        )
    return path.read_text(encoding="utf-8") if path.exists() else None


PAIRS = [(block, name) for block, spec in BLOCKS.items() for name in spec[3]]


@pytest.mark.parametrize(("block", "name"), PAIRS, ids=[f"{b}-{n}" for b, n in PAIRS])
def test_the_shared_block_is_byte_identical_with_the_sibling(block: str, name: str) -> None:
    own_file, start, end, _ = BLOCKS[block]
    theirs = sibling(name, own_file, start)
    if theirs is None or cut(theirs, start, end) is None:
        pytest.skip(f"no {block} block in a {name} checkout at {FAMILY / name}")
    assert cut((OWN / own_file).read_text(encoding="utf-8"), start, end) == cut(theirs, start, end)
