"""skills/vectrixdb/SKILL.md, the skill a coding agent loads, names only what the code has.

An agent does what a skill says, so a name the code does not have is a bug it
copies into someone's project. Every name in backticks is looked up: a command
and its options, an extra in pyproject.toml, a module, class or function by
importing it, a method, parameter or field of Vectrix and its results, a search
mode, a pick of the evaluation report, a setting. The Python blocks are parsed,
not run: what they import exists, every method and field they use is there, and
every keyword they pass is a parameter of what they call.
"""

from __future__ import annotations

import ast
import builtins
import dataclasses
import importlib
import inspect
import re
import shlex
import typing
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 and 3.10
    import tomli as tomllib  # type: ignore[no-redef]

import typer.main

from vectrixdb import Vectrix
from vectrixdb._eval_report import build_report
from vectrixdb.cli import app
from vectrixdb.easy import Result, SearchMode
from vectrixdb.evaluation import evaluate
from vectrixdb.settings import known

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "vectrixdb" / "SKILL.md"
SITE = "https://knowusuboaky.github.io/VectrixDB/"
FENCE = re.compile(r"^```(\w*)\n(.*?)^```\n?", flags=re.S | re.M)
MODES = set(typing.get_args(SearchMode))
# What evaluate() returns: the report build_report makes, and how a result was counted right.
REPORT = set(build_report([], {}, question_ids=[])) | {"by"}
PICKS = set(build_report([], {}, question_ids=[])["picks"])
# The calls whose keywords a bare `name=` in the prose can be.
WRITING = (Vectrix.__init__, Vectrix.add, Vectrix.add_document, Vectrix.search)


def _split(text: str) -> Tuple[Dict[str, str], str]:
    """The frontmatter, as its fields, and the instructions under it."""
    assert text.startswith("---\n"), "a skill opens with YAML frontmatter"
    head, body = text[4:].split("\n---\n", 1)
    fields = {}
    for line in head.splitlines():
        key, value = line.split(": ", 1)
        fields[key] = value
    return fields, body


def _skill() -> Tuple[Dict[str, str], str]:
    return _split(SKILL.read_text(encoding="utf-8"))


def _spans(body: str) -> List[str]:
    """Every name in backticks outside the code blocks."""
    return re.findall(r"`([^`\n]+)`", FENCE.sub("", body))


def _extras() -> Set[str]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(pyproject["project"]["optional-dependencies"])


def _commands() -> Dict[str, Set[str]]:
    """Each command of the command line, with its options."""
    group = typer.main.get_command(app)
    return {
        name: {option for parameter in command.params for option in parameter.opts}
        for name, command in group.commands.items()
    }


def _imported(dotted: str) -> Any:
    """What a dotted name such as ``vectrixdb.evaluation.evaluate`` names, imported."""
    parts = dotted.split(".")
    for cut in range(len(parts), 0, -1):
        try:
            found: Any = importlib.import_module(".".join(parts[:cut]))
        except ImportError:
            continue
        for part in parts[cut:]:
            found = getattr(found, part)
        return found
    raise ImportError(f"no module {dotted}")


def _parameters(function: Any) -> Set[str]:
    return set(inspect.signature(function).parameters) - {"self"}


def _takes(function: Any, keyword: str) -> bool:
    kinds = {p.kind for p in inspect.signature(function).parameters.values()}
    return keyword in _parameters(function) or inspect.Parameter.VAR_KEYWORD in kinds


def _has(cls: Any, name: str) -> bool:
    """A method, property or field of a class, a dataclass's fields included."""
    fields = {f.name for f in dataclasses.fields(cls)} if dataclasses.is_dataclass(cls) else set()
    return name in fields or hasattr(cls, name)


def _words() -> Set[str]:
    """Every bare name the prose may use."""
    errors = {
        name
        for name, value in vars(builtins).items()
        if isinstance(value, type) and issubclass(value, BaseException)
    }
    return (
        set(dir(Vectrix))
        | {name for function in WRITING for name in _parameters(function)}
        | {f.name for f in dataclasses.fields(Result)}
        | set(dir(Result))
        | MODES
        | PICKS
        | _extras()
        | set(known())
        | errors
    )


def _missing(span: str) -> str:
    """Why a span in backticks names nothing in the code, or "" when it names something."""
    if span.startswith("pip install "):
        for requirement in shlex.split(span)[2:]:
            found = re.fullmatch(r"vectrixdb(?:\[([\w,-]+)\])?", requirement)
            if not found:
                return f"{requirement} is not vectrixdb or vectrixdb with extras"
            unknown = set(filter(None, (found.group(1) or "").split(","))) - _extras()
            if unknown:
                return f"pyproject.toml has no extra {', '.join(sorted(unknown))}"
        return ""
    if span.startswith("vectrixdb "):
        words, commands = shlex.split(span), _commands()
        if words[1] not in commands:
            return f"the command line has no {words[1]}"
        options = {w.split("=", 1)[0] for w in words[2:] if w.startswith("-")}
        extra = options - commands[words[1]]
        return f"vectrixdb {words[1]} has no {', '.join(sorted(extra))}" if extra else ""
    name = re.sub(r"\((\.\.\.)?\)$", "", span)
    if name == "vectrixdb" or name.startswith("vectrixdb."):
        try:
            _imported(name)
        except (ImportError, AttributeError) as exc:
            return str(exc)
        return ""
    if name.startswith("db."):
        return "" if _has(Vectrix, name[3:]) else "Vectrix has no such method"
    if name.startswith("result."):
        return "" if _has(Result, name[7:]) else "a result has no such field"
    keyword = re.fullmatch(r"(\w+)=(.*)", name)
    if keyword:
        parameter, value = keyword.groups()
        if not any(parameter in _parameters(function) for function in WRITING):
            return "not a parameter of Vectrix, add, add_document or search"
        if parameter == "mode" and value and ast.literal_eval(value) not in MODES:
            return "not a search mode"
        return ""
    if name != span:
        return "" if callable(getattr(Vectrix, name, None)) else "Vectrix has no such method"
    return "" if name in _words() else "names nothing in the code"


class _Block(ast.NodeVisitor):
    """What a Python block imports, calls and reads, looked up without running it."""

    def __init__(self) -> None:
        self.imported: Dict[str, Any] = {}
        # A variable, and what made it: the class it is an instance of, or the function it came from.
        self.held: Dict[str, Any] = {}
        self.assigned: Set[str] = set()
        self.wrong: List[str] = []

    def _callee(self, call: ast.Call) -> Any:
        """What a call calls: an imported name, or a method of a variable the block holds."""
        func = call.func
        if isinstance(func, ast.Name):
            return self.imported.get(func.id)
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            held = self.held.get(func.value.id)
            return getattr(held, func.attr, None) if inspect.isclass(held) else None
        return None

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = importlib.import_module(node.module or "")
        for alias in node.names:
            if hasattr(module, alias.name):
                self.imported[alias.asname or alias.name] = getattr(module, alias.name)
            else:
                self.wrong.append(f"{node.module} has no {alias.name}")

    def visit_Assign(self, node: ast.Assign) -> None:
        made = self._callee(node.value) if isinstance(node.value, ast.Call) else None
        for target in node.targets:
            if isinstance(target, ast.Name) and made is not None:
                self.held[target.id] = made
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        # Iterating a search gives its results.
        if isinstance(node.target, ast.Name) and isinstance(node.iter, ast.Call):
            if self._callee(node.iter) is Vectrix.search:
                self.held[node.target.id] = Result
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.assigned.add(node.id)
        elif (
            node.id not in self.imported
            and node.id not in self.assigned
            and not hasattr(builtins, node.id)
        ):
            self.wrong.append(f"{node.id} is used and never imported or assigned")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        held = self.held.get(node.value.id) if isinstance(node.value, ast.Name) else None
        if inspect.isclass(held) and not _has(held, node.attr):
            self.wrong.append(f"{held.__name__} has no {node.attr}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        callee = self._callee(node)
        if callee is not None:
            for keyword in node.keywords:
                if keyword.arg and not _takes(callee, keyword.arg):
                    self.wrong.append(f"{callee.__qualname__} takes no {keyword.arg}=")
                given = keyword.value.value if isinstance(keyword.value, ast.Constant) else None
                if keyword.arg == "mode" and given not in MODES:
                    self.wrong.append(f"mode={given!r} is not a search mode")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        key = node.slice
        if isinstance(node.value, ast.Name) and self.held.get(node.value.id) is evaluate:
            if isinstance(key, ast.Constant) and key.value not in REPORT:
                self.wrong.append(f"evaluate()'s report has no {key.value!r}")
        self.generic_visit(node)


def _wrong_in_blocks(body: str) -> List[str]:
    wrong: List[str] = []
    for language, code in FENCE.findall(body):
        if language == "python":
            block = _Block()
            block.visit(ast.parse(code))
            wrong += block.wrong
    return wrong


def test_it_opens_with_the_name_and_description_an_agent_reads():
    fields, body = _skill()
    assert set(fields) == {"name", "description"}
    assert fields["name"] == SKILL.parent.name == "vectrixdb"
    assert re.fullmatch(r"[a-z0-9-]{1,64}", fields["name"])
    # A plain YAML value: one line, nothing that YAML would read as a key or a comment.
    description = fields["description"]
    assert 0 < len(description) <= 1024
    assert ": " not in description and " #" not in description
    assert body.lstrip().startswith("# VectrixDB")


def test_every_name_in_backticks_is_in_the_code():
    _, body = _skill()
    spans = _spans(body)
    assert len(spans) > 20
    wrong = [f"`{span}`: {why}" for span in spans for why in [_missing(span)] if why]
    assert wrong == [], "names in skills/vectrixdb/SKILL.md the code does not have:\n" + "\n".join(
        wrong
    )


def test_the_python_blocks_use_only_what_the_code_has():
    _, body = _skill()
    assert [language for language, _ in FENCE.findall(body)].count("python") >= 2
    assert _wrong_in_blocks(body) == []


def test_its_links_to_the_docs_are_pages():
    _, body = _skill()
    links = re.findall(re.escape(SITE) + r"([\w/.-]*\w)", body)
    assert links
    for page in links:
        path = ROOT / "docs" / (page if "." in page else f"{page}.md")
        assert path.is_file(), page


def test_a_name_the_code_does_not_have_is_caught():
    for span in (
        "db.drop()",
        "result.source",
        "top_k=5",
        'mode="fast"',
        "vectrixdb launch",
        "vectrixdb mcp --collection docs",
        'pip install "vectrixdb[everything]"',
        "vectrixdb.evaluation.pick_mode",
        "best_overall",
    ):
        assert _missing(span), span
    body = (
        "```python\n"
        "from vectrixdb import Vectrix\n"
        "from vectrixdb.evaluation import evaluate\n"
        'db = Vectrix("docs", folder="./data")\n'
        'for result in db.search("refunds", top_k=5):\n'
        "    print(result.source)\n"
        'print(evaluate(db, "golden.jsonl")["picks"], evaluate(db, "g.jsonl", mode="fast"))\n'
        'report = evaluate(db, "golden.jsonl")\n'
        'print(report["winner"], db.drop(), Golden)\n'
        "```\n"
    )
    assert _wrong_in_blocks(body) == [
        "Vectrix takes no folder=",
        "Vectrix.search takes no top_k=",
        "Result has no source",
        "evaluate takes no mode=",
        "mode='fast' is not a search mode",
        "evaluate()'s report has no 'winner'",
        "Vectrix has no drop",
        "Golden is used and never imported or assigned",
    ]
