import ast
import dataclasses
import inspect
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

import rfnry_senses

from .test_docs import TYPESCRIPT, typescript_exports

ROOT = Path(__file__).parents[4]
API = ROOT / "docs" / "api.md"
SRC = Path(rfnry_senses.__file__).parent
PYTHON_PART, TYPESCRIPT_PART = API.read_text(encoding="utf-8").split("\n## TypeScript client\n")

Shape = tuple[bool, bool, list[tuple[str, str, str | None, object]], str | None]


def entries(part: str) -> list[tuple[str, str]]:
    found = []
    for section in re.split(r"^### ", part, flags=re.M)[1:]:
        heading = re.match(r"`([\w.]+)`\n", section)
        block = re.search(r"```(?:python|ts)\n(.*?)```", section, re.S)
        assert heading is not None and block is not None, section[:80]
        found.append((heading.group(1), block.group(1)))
    return found


PYTHON = entries(PYTHON_PART)
TS = entries(TYPESCRIPT_PART)


def webrtc_names() -> list[str]:
    tree = ast.parse((SRC / "webrtc.py").read_text(encoding="utf-8"))
    return next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and [ast.unparse(t) for t in node.targets] == ["__all__"]
    )


def public() -> dict[str, Any]:
    objects = {name: getattr(rfnry_senses, name) for name in rfnry_senses.__all__}
    try:
        from rfnry_senses import webrtc
    except ImportError:
        return objects
    return objects | {name: getattr(webrtc, name) for name in webrtc.__all__}


def is_protocol(obj: object) -> bool:
    return inspect.isclass(obj) and bool(getattr(obj, "_is_protocol", False))


def is_core(obj: object) -> bool:
    return (
        inspect.isclass(obj)
        and not issubclass(obj, BaseModel | BaseException)
        and not is_protocol(obj)
    )


def verbs(cls: type) -> list[str]:
    return [
        name
        for name, member in vars(cls).items()
        if not name.startswith("_") and inspect.isfunction(member)
    ]


def spelled(annotation: object) -> str | None:
    if annotation is inspect.Parameter.empty:
        return None
    text = annotation if isinstance(annotation, str) else inspect.formatannotation(annotation)
    return ast.unparse(ast.parse(text, mode="eval"))


def value(node: ast.expr) -> object:
    return eval(compile(ast.Expression(node), str(API), "eval"), {"__builtins__": {}})


def from_code(member: object) -> Shape:
    function = member.fget if isinstance(member, property) else member
    assert callable(function)
    signature = inspect.signature(function)
    return (
        isinstance(member, property),
        inspect.iscoroutinefunction(function) or inspect.isasyncgenfunction(function),
        [
            (p.name, p.kind.name, spelled(p.annotation), p.default)
            for p in signature.parameters.values()
        ],
        spelled(signature.return_annotation),
    )


def from_doc(node: ast.FunctionDef | ast.AsyncFunctionDef) -> Shape:
    a = node.args
    empty = inspect.Parameter.empty
    positional = [*a.posonlyargs, *a.args]
    defaults = [empty] * (len(positional) - len(a.defaults)) + [value(d) for d in a.defaults]
    params = [
        (arg, "POSITIONAL_ONLY" if arg in a.posonlyargs else "POSITIONAL_OR_KEYWORD", default)
        for arg, default in zip(positional, defaults, strict=True)
    ]
    if a.vararg is not None:
        params.append((a.vararg, "VAR_POSITIONAL", empty))
    params += [
        (arg, "KEYWORD_ONLY", empty if default is None else value(default))
        for arg, default in zip(a.kwonlyargs, a.kw_defaults, strict=True)
    ]
    if a.kwarg is not None:
        params.append((a.kwarg, "VAR_KEYWORD", empty))
    return (
        any(ast.unparse(d) == "property" for d in node.decorator_list),
        isinstance(node, ast.AsyncFunctionDef),
        [
            (arg.arg, kind, spelled(ast.unparse(arg.annotation)) if arg.annotation else None, d)
            for arg, kind, d in params
        ],
        spelled(ast.unparse(node.returns)) if node.returns else None,
    )


def declared(block: str, name: str) -> ast.stmt:
    tree = ast.parse(block)
    found = [
        node
        for node in tree.body
        if getattr(node, "name", None) == name
        or (isinstance(node, ast.Assign) and [ast.unparse(t) for t in node.targets] == [name])
    ]
    assert len(found) == 1, f"the block declares {name} once"
    return found[0]


def annotations(cls: type) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for c in reversed(cls.__mro__):
        found |= inspect.get_annotations(c)
    return found


def check_fields(cls: type, fields: list[ast.AnnAssign]) -> None:
    names = [ast.unparse(f.target) for f in fields]
    if issubclass(cls, BaseModel):
        assert names == list(cls.model_fields)
        required = {n: info.is_required() for n, info in cls.model_fields.items()}
        defaults = {n: info.default for n, info in cls.model_fields.items()}
    elif dataclasses.is_dataclass(cls):
        assert names == [f.name for f in dataclasses.fields(cls)]
        missing = dataclasses.MISSING
        required = {
            f.name: f.default is missing and f.default_factory is missing
            for f in dataclasses.fields(cls)
        }
        defaults = {f.name: f.default for f in dataclasses.fields(cls)}
    else:
        source = inspect.getsource(cls)
        assert [n for n in names if not re.search(rf"self\.{n}\s*[:=]", source)] == []
        return
    code = annotations(cls)
    for field, name in zip(fields, names, strict=True):
        assert spelled(ast.unparse(field.annotation)) == spelled(code[name]), name
        assert (field.value is None) == required[name], name
        if field.value is not None:
            try:
                literal = ast.literal_eval(field.value)
            except ValueError:
                continue
            assert literal == defaults[name], name


def check_class(cls: type, node: ast.ClassDef) -> None:
    assert {ast.unparse(b) for b in node.bases} <= {c.__name__ for c in cls.__mro__}
    functions = [n for n in node.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    for function in functions:
        assert from_doc(function) == from_code(inspect.getattr_static(cls, function.name)), (
            function.name
        )
    check_fields(cls, [n for n in node.body if isinstance(n, ast.AnnAssign)])
    if is_protocol(cls):
        members = {
            n
            for n, m in vars(cls).items()
            if not n.startswith("_") and (inspect.isfunction(m) or isinstance(m, property))
        }
        assert {f.name for f in functions} == members
    if is_core(cls) and not dataclasses.is_dataclass(cls):
        assert "__init__" in {f.name for f in functions}


def assignment(name: str) -> str:
    for path in sorted(SRC.glob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and [ast.unparse(t) for t in node.targets] == [name]:
                return ast.unparse(node.value)
    raise AssertionError(f"no assignment to {name}")


def test_every_public_name_has_one_entry() -> None:
    headings = [heading for heading, _ in PYTHON]
    names = [*rfnry_senses.__all__, *webrtc_names()]
    assert [n for n in names if headings.count(n) != 1] == []


def test_every_python_entry_names_something_public() -> None:
    names = {*rfnry_senses.__all__, *webrtc_names()}
    objects = public()
    stale = []
    for heading, _ in PYTHON:
        owner, _, verb = heading.partition(".")
        known = owner in names and not (
            verb and owner in objects and verb not in verbs(objects[owner])
        )
        if not known:
            stale.append(heading)
    assert stale == []


def test_every_verb_of_a_core_class_has_an_entry_right_after_its_class() -> None:
    headings = [heading for heading, _ in PYTHON]
    missing = [
        f"{name}.{verb}"
        for name, obj in public().items()
        if is_core(obj)
        for verb in verbs(obj)
        if f"{name}.{verb}" not in headings
    ]
    assert missing == []
    owner = ""
    for heading in headings:
        if "." in heading:
            assert heading.split(".")[0] == owner, heading
        else:
            owner = heading


@pytest.mark.parametrize(("heading", "block"), PYTHON, ids=[h for h, _ in PYTHON])
def test_each_python_signature_matches_the_code(heading: str, block: str) -> None:
    objects = public()
    owner, _, verb = heading.partition(".")
    if owner not in objects:
        pytest.skip(f"{owner} needs an extra that is not installed")
    obj = objects[owner]
    name = verb or owner
    node = declared(block, name)
    if verb:
        assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        assert from_doc(node) == from_code(inspect.getattr_static(obj, verb))
    elif inspect.isfunction(obj):
        assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        assert from_doc(node) == from_code(obj)
    elif inspect.isclass(obj):
        assert isinstance(node, ast.ClassDef)
        check_class(obj, node)
    else:
        assert isinstance(node, ast.Assign)
        assert ast.unparse(node.value) == assignment(name)


def senses_methods() -> set[str]:
    source = (TYPESCRIPT / "room.ts").read_text(encoding="utf-8")
    body = source.split("export interface Senses {", 1)[1].split("\n}", 1)[0]
    return set(re.findall(r"^\s+(\w+)\(", body, re.M))


def test_every_typescript_export_and_senses_method_has_one_entry() -> None:
    headings = [heading for heading, _ in TS]
    exports = typescript_exports((TYPESCRIPT / "index.ts").read_text(encoding="utf-8"))
    expected = exports | {f"Senses.{method}" for method in senses_methods()}
    assert {"Senses.useCamera", "Senses.close"} <= expected
    assert sorted(h for h in headings if headings.count(h) > 1) == []
    assert sorted(expected - set(headings)) == []
    assert sorted(set(headings) - expected) == []


def test_each_typescript_block_is_in_the_source() -> None:
    source = "\n".join(p.read_text(encoding="utf-8") for p in sorted(TYPESCRIPT.glob("*.ts")))
    stale = [
        (heading, line.strip())
        for heading, block in TS
        for line in block.splitlines()
        if line.strip() not in source
    ]
    assert stale == []
