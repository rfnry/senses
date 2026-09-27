import ast
from pathlib import Path

import rfnry_senses

SRC = Path(rfnry_senses.__file__).parent

LOG_CALLS = {"debug", "info", "warning", "error", "exception", "critical", "log"}

PREFIXES: dict[str, tuple[str, ...]] = {
    "session.py": ("session", "tools"),
    "websocket.py": ("transport", "wire"),
    "webrtc.py": ("webrtc", "transport"),
}

ALLOWED: dict[tuple[str, str], str] = {
    ("session.py", "StopAsyncIteration"): "ends a subscription",
    ("session.py", "error"): "raises again what a speech task raised",
}


def _sources() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _prefixes(path: Path) -> tuple[str, ...]:
    return tuple(f"{name}: " for name in PREFIXES.get(path.name, (path.stem,)))


def _leading(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        text = ""
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                text += part.value
            else:
                return text + "{"
        return text
    return None


def _raises(path: Path) -> list[tuple[int, str, str | None]]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        exc = node.exc
        first = exc.args[0] if isinstance(exc, ast.Call) and exc.args else None
        found.append(
            (node.lineno, ast.unparse(exc), _leading(first) if first is not None else None)
        )
    return found


def _logs(path: Path) -> list[tuple[int, str, str | None]]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if not (isinstance(owner, ast.Name) and owner.id in {"log", "logger", "logging"}):
            continue
        if node.func.attr not in LOG_CALLS:
            continue
        args = node.args[1:] if node.func.attr == "log" else node.args
        found.append((node.lineno, ast.unparse(node), _leading(args[0]) if args else None))
    return found


def _key(path: Path, source: str) -> tuple[str, str]:
    return str(path.relative_to(SRC)), source


def test_every_error_message_starts_with_its_module_name() -> None:
    wrong = []
    for path in _sources():
        prefixes = _prefixes(path)
        for line, source, text in _raises(path):
            if text is None:
                if _key(path, source) not in ALLOWED:
                    wrong.append(f"{path.name}:{line}: no literal message: {source}")
                continue
            prefix = next((p for p in prefixes if text.startswith(p)), None)
            rest = text[len(prefix) :] if prefix is not None else ""
            if prefix is None or not rest or rest[0].isupper() or rest[0].isspace():
                wrong.append(f"{path.name}:{line}: {text!r}")
    assert wrong == []


def test_every_log_message_is_lowercase() -> None:
    wrong = []
    for path in _sources():
        for line, source, text in _logs(path):
            if text is None or not text or text[0].isupper():
                wrong.append(f"{path.name}:{line}: {source}")
    assert wrong == []


def test_the_allowlist_names_only_raises_that_exist() -> None:
    present = {
        _key(path, source)
        for path in _sources()
        for _, source, text in _raises(path)
        if text is None
    }
    assert set(ALLOWED) <= present


def test_the_prefix_map_names_only_files_that_exist() -> None:
    assert {name for name in PREFIXES if not (SRC / name).exists()} == set()
