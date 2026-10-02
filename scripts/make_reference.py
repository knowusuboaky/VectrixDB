"""Write the reference pages that are read off the code, so they cannot drift from it.

    python scripts/make_reference.py            # write settings.md, cli.md, rest-api.md, errors.md, openapi.json, golden.schema.json
    python scripts/make_reference.py --check    # exit 1 when one is out of date; the test suite runs this

Every setting from vectrixdb.settings, every command and option from the CLI,
and every documented route from the server with the sign-in action it needs
and the roles that hold it. Nothing is written by hand on these pages, so a
setting, an option or a route added to the code without its page fails a test
rather than going unmentioned.

``docs/reference/openapi.json`` is the same route table in the form a gateway
team imports: APIM and API Gateway both read it and make their operations from
it, so the endpoints they publish come from the code rather than from an email.
A running server serves the same document at ``/openapi.json``, carrying the
path it is served under; this copy is the one that ships with a release.

``docs/reference/errors.md`` is the error catalogue: every refusal a route can
send with its status and message, read from the source, every exception the
library defines. A
refusal added without its row fails the same test.

``docs/reference/golden.schema.json`` is what one line of a golden file may
hold, as JSON Schema, so a file can be checked in an editor or a pipeline
with the same rules ``check_golden`` holds it to before a run.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ============================================================================
# SETTINGS: where the pages go, and the mark every generated page carries
# ============================================================================
#
# docs/reference is where the pages land; every one opens with the same line
# saying it was written from the code, so nobody edits it by hand.

DOCS = ROOT / "docs" / "reference"
#: Actions a route also refuses to every key, the admin key included, beyond what the role table says.
ONLY_PEOPLE = {"evaluation.golden"}
GENERATED = "<!-- Written by scripts/make_reference.py from the code. Edit the code, then run it again. -->"


def _cell(text: Any) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ").strip()


# ============================================================================
# SETTINGS PAGE: every setting, from vectrixdb.settings
# ============================================================================
#
# INPUT   the settings module
# OUTPUT  settings.md: every setting, its default and what it is for
#
# Read from the module, so a setting added to the code without its row fails
# the reference test.


def settings_page() -> str:
    from vectrixdb.settings import SETTINGS

    groups: Dict[str, List[Any]] = {}
    for setting in SETTINGS:
        groups.setdefault(setting.group, []).append(setting)
    out = [
        GENERATED,
        "",
        "# Settings",
        "",
        f"Every setting VectrixDB reads, {len(SETTINGS)} of them, each an environment variable. "
        "`vectrixdb check --template` prints them as a file to fill in, and `vectrixdb check` "
        "tests a set before a start; see [Deploy the server](../how-to/deploy.md). A secret can "
        "also be given as `NAME_FILE`, naming a file that holds it, which is how Docker and "
        "Kubernetes secrets arrive; setting both is refused.",
        "",
    ]
    for group, rows in groups.items():
        out += [f"## {group}", "", "| Setting | What it does | Example or default |", "| --- | --- | --- |"]
        for s in rows:
            marks = []
            if s.secret:
                marks.append("a secret")
            if s.file_twin:
                marks.append(f"or `{s.name}_FILE`")
            note = f" ({', '.join(marks)})" if marks else ""
            example = f"`{_cell(s.example)}`" if s.example else ""
            out.append(f"| `{s.name}` | {_cell(s.means)}{note} | {example} |")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# ============================================================================
# CLI PAGE: every command and option, from the CLI itself
# ============================================================================
#
# INPUT   the CLI's parser
# OUTPUT  cli.md: every command and its options
#
# The parser is the source, so the page and the help text cannot disagree.


def _usage(name: str, command: Any) -> List[str]:
    # By what a parameter says it is: typer may carry its own copy of click, so
    # an isinstance check against the click installed beside it fails.
    lines = []
    args = [p for p in command.params if p.param_type_name == "argument"]
    opts = [p for p in command.params if p.param_type_name == "option" and p.name != "help"]
    shown = " ".join(f"{a.name.upper()}{'...' if a.nargs == -1 else ''}" if a.required else f"[{a.name.upper()}]" for a in args)
    lines.append(f"```text\nvectrixdb {name}{' ' + shown if shown else ''}{' [OPTIONS]' if opts else ''}\n```")
    help_text = (command.help or "").strip().split("\n\n")[0].replace("\n", " ")
    if help_text:
        lines += ["", help_text]
    if args or opts:
        lines += ["", "| Argument or option | What it does | Default |", "| --- | --- | --- |"]
        for a in args:
            lines.append(f"| `{a.name.upper()}` | {_cell(getattr(a, 'help', '') or '')} | {'required' if a.required else _cell(a.default)} |")
        for o in opts:
            names = ", ".join(f"`{n}`" for n in o.opts + o.secondary_opts)
            if o.is_flag:
                default = "on" if o.default else "off"
            elif o.required:
                default = "required"
            elif o.default is None or o.default == "":
                default = ""
            else:
                default = f"`{_cell(o.default)}`"
            lines.append(f"| {names} | {_cell(o.help)} | {default} |")
    return lines


def cli_page() -> str:
    import typer

    from vectrixdb.cli import app

    root = typer.main.get_command(app)
    out = [
        GENERATED,
        "",
        "# Command line",
        "",
        "`pip install vectrixdb` puts `vectrixdb` on the path. Every command that opens the data "
        "or the list of people takes `--env-file`, read before anything else, and finds the data "
        "at `--path`, else `VECTRIXDB_PATH`, else `./vectrixdb_data`, the way the server does.",
        "",
    ]
    for name in sorted(root.commands):  # type: ignore[attr-defined]
        command = root.commands[name]  # type: ignore[attr-defined]
        if getattr(command, "hidden", False):
            continue
        if hasattr(command, "commands"):
            out += [f"## {name}", "", (command.help or "").strip().split("\n\n")[0].replace("\n", " "), ""]
            for sub in sorted(command.commands):
                out += [f"### {name} {sub}", ""] + _usage(f"{name} {sub}", command.commands[sub]) + [""]
        else:
            out += [f"## {name}", ""] + _usage(name, command) + [""]
    return "\n".join(out).rstrip() + "\n"


# ============================================================================
# REST API PAGE AND OPENAPI: every route, with the action and roles it needs
# ============================================================================
#
# INPUT   the server's routes, and the role table
# OUTPUT  rest-api.md and openapi.json
#
# The same route table in two forms: one to read, one a gateway team imports.
# APIM and API Gateway both read openapi.json and make their operations from
# it, so the endpoints they publish come from the code rather than from an
# email.


def schema() -> Dict[str, Any]:
    """The OpenAPI document, from an application built with nothing set.

    Not app.routes: this lists every documented route whatever the FastAPI
    version keeps them in, and nothing it hides. The environment is put aside
    while it is built, so the file is the route table and does not move with
    the machine that generated it, a root path or a brand included.
    """
    import os

    from vectrixdb.api.server import create_app

    kept = {name: os.environ.pop(name) for name in list(os.environ) if name.startswith("VECTRIXDB_")}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            return create_app(db_path=tmp, enable_dashboard=False).openapi()
    finally:
        os.environ.update(kept)


def rest_page() -> str:
    from vectrixdb.api.signin import PUBLIC_PATHS, _PUBLIC_PREFIXES
    from vectrixdb.signin import roles

    document = schema()

    def who(method: str, path: str) -> str:
        if path in PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
            return "anybody"
        if path == "/auth/signout":
            return "whoever is signed in"  # let through by the sign-in layer itself
        action = roles.action_for(method, path)
        if action is None:
            return "admin (no action places it)"
        people = [r for r in roles.ROLES if roles.can(r, action)]
        # A key is nobody, so the actions about oneself are a person's alone.
        keys = [] if action.startswith("self.") else [k for k in roles.KEY_ROLES if roles.can(k, action)]
        said = ", ".join(people) if people else "nobody"
        if keys:
            said += f"; keys: {', '.join(keys)}"
        if roles.needs_step_up(action, method):
            said += "; a fresh check"
        if action in ONLY_PEOPLE:
            said += "; signed in as a person, never a key"
        return f"`{action}`: {said}"

    groups: Dict[str, List[str]] = {}
    for path in sorted(document.get("paths", {})):
        for method, spec in sorted(document["paths"][path].items()):
            method = method.upper()
            if method == "HEAD":
                continue
            described = next((line.strip() for line in (spec.get("description") or "").splitlines() if line.strip()), "")
            summary = described or spec.get("summary", "")
            tag = str((spec.get("tags") or ["other"])[0])
            groups.setdefault(tag, []).append(f"| `{method}` | `{path}` | {_cell(summary)} | {who(method, path)} |")
    total = sum(len(rows) for rows in groups.values())
    out = [
        GENERATED,
        "",
        "# REST API",
        "",
        f"Every documented route the server answers, {total} of them, grouped as the interactive "
        "docs at `/docs` group them. The last column is who may call it once sign-in is on: the "
        "action the role table places it under, the roles that hold that action, the key roles "
        "that do, and whether it needs a check from the last ten minutes. With sign-in off, the "
        "API key rules in [Run the REST API](../how-to/rest-api.md) apply instead. A route no "
        "action places is for admins only, so an endpoint added later is closed until somebody "
        "decides who it is for. Older aliases under `/api/` that the dashboard used are left out.",
        "",
    ]
    for tag in sorted(groups):
        out += [f"## {tag.capitalize()}", "", "| Method | Path | What it does | Who may call it |", "| --- | --- | --- | --- |"]
        out += groups[tag] + [""]
    return "\n".join(out).rstrip() + "\n"


def openapi_document() -> str:
    """The OpenAPI document, pretty printed and stable between runs.

    Built from an application with the dashboard off and no sign-in, so the
    file is the route table alone and does not move with the environment it
    was generated in. The version is the package's, which is what tells a
    gateway team that something changed.
    """
    import json

    spec = schema()
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def golden_schema_document() -> str:
    """The schema one line of a golden file is checked against, as the library holds it."""
    import json

    from vectrixdb.evaluation import GOLDEN_SCHEMA

    return json.dumps(GOLDEN_SCHEMA, indent=2, ensure_ascii=False) + "\n"



# ============================================================================
# ERRORS PAGE: every refusal and exception, read off the source
# ============================================================================
#
# INPUT   the API modules, read as syntax trees; the exceptions module
# OUTPUT  errors.md: every refusal a route can send with its status and words,
#         and every exception
#
# Read off the tree, so a refusal spread over lines, a conditional status or a
# constant reads as what it is, and a refusal added without its row fails the
# reference test.


#: Where refusals are raised, and the heading each file's rows go under.
_REFUSAL_FILES = [
    ("vectrixdb/api/signin.py", "Sign-in and the door"),
    ("vectrixdb/api/server.py", "Collections, points and search"),
    ("vectrixdb/api/documents.py", "Documents"),
    ("vectrixdb/api/inspection.py", "Inspection, provenance and the audit trail"),
    ("vectrixdb/api/evaluations.py", "Evaluations"),
    ("vectrixdb/api/replies.py", "The one answer for a collection that is not there"),
    ("vectrixdb/api/extraction.py", "The extraction service"),
]
#: The helpers a route refuses through, and where each keeps its status and its message.
_HELPERS = {"refusal": (0, 1), "_Refused": (0, 1), "_refuse": (0, 1), "again": (0, 1)}
#: What a message that is not a literal stands for, by the expression as the tree prints it.
_EXPRESSIONS = {
    "exc.reason": "the reason the provider gave",
    "decision.because": "why the policy refused, with its code in data",
    "message_of(detail)": "what did not validate, in words, with the fields in detail",
    "message": "the rate limit's words, with retry_after in data",
    "cut['why']": "why no cut is in force",
    "none": "which run there is no such of",
    "problem": "what is wrong with the password, in words",
}
#: A condition a message turns on, in words.
_TESTS = {"runtime.config.passwords": "passwords are on"}


def _words(node: Any, constants: Dict[str, str]) -> str:
    """A message or status expression as the page shows it."""
    import ast

    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.JoinedStr):
        return "".join(
            str(part.value) if isinstance(part, ast.Constant) else "{" + ast.unparse(part.value) + "}"
            for part in node.values
        )
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    if isinstance(node, ast.IfExp):
        yes, no = _words(node.body, constants), _words(node.orelse, constants)
        test = _TESTS.get(ast.unparse(node.test), ast.unparse(node.test))
        return f"{no}, or {yes} when {test}" if yes != no else yes
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "str":
        return "the error's own words"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id.endswith("Error"):
        return f"the error's own words, a {node.func.id}"
    printed = ast.unparse(node)
    return _EXPRESSIONS.get(printed, f"`{printed}`")


def _constants(tree: Any) -> Dict[str, str]:
    """Module-level strings a refusal names, so the page shows the words rather than the name."""
    import ast

    out: Dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if isinstance(node.value, (ast.Constant, ast.JoinedStr)) and not isinstance(getattr(node.value, "value", ""), (int, float, bool, type(None), bytes, tuple)):
                out[node.targets[0].id] = _words(node.value, out)
    return out


def _functions(tree: Any) -> List[Tuple[int, int, str]]:
    """Every function's line range and name, so a site is named by the route that raises it."""
    import ast

    return sorted(
        (node.lineno, node.end_lineno or node.lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def _named(line: int, functions: List[Tuple[int, int, str]]) -> str:
    inside = [name for start, end, name in functions if start <= line <= end]
    return inside[-1] if inside else "module"


def _site(node: Any) -> Any:
    """``(status node, message node)`` when a call is a refusal, else None."""
    import ast

    name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
    keywords = {k.arg: k.value for k in node.keywords}
    if name in _HELPERS:
        at_status, at_message = _HELPERS[name]
        if len(node.args) > at_message:
            return node.args[at_status], node.args[at_message]
    if name == "HTTPException":
        status = keywords.get("status_code", node.args[0] if node.args else None)
        message = keywords.get("detail", node.args[1] if len(node.args) > 1 else None)
        if status is not None and message is not None:
            return status, message
    if name == "JSONResponse" and node.args and isinstance(node.args[0], ast.Dict):
        for key, value in zip(node.args[0].keys, node.args[0].values):
            if isinstance(key, ast.Constant) and key.value == "message":
                status = keywords.get("status_code", node.args[1] if len(node.args) > 1 else None)
                # A reply that says a job started carries a message too, and is not a refusal.
                if status is not None and not (isinstance(status, ast.Constant) and int(status.value) < 400):
                    return status, value
    return None


def refusals_of(path: Path) -> List[Tuple[str, str, str]]:
    """Every refusal a file raises: ``(status, message, function)``, one a site, in file order."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    constants = _constants(tree)
    functions = _functions(tree)
    out: List[Tuple[str, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        site = _site(node)
        if site is None:
            continue
        status, message = site
        if isinstance(status, ast.IfExp):
            said = f"{_words(status.body, constants)} or {_words(status.orelse, constants)}"
        else:
            said = _words(status, constants)
        out.append((said, _words(message, constants), _named(node.lineno, functions)))
    return sorted(out, key=lambda row: 0) if False else out


def exception_classes() -> List[Tuple[str, str, str]]:
    """Every exception and warning the library defines: name, base, and its first line of explanation."""
    import ast

    source = (ROOT / "vectrixdb" / "exceptions.py").read_text(encoding="utf-8")
    out = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef):
            bases = ", ".join(getattr(base, "id", getattr(base, "attr", "?")) for base in node.bases)
            said = (ast.get_docstring(node) or "").strip().split("\n\n")[0]
            out.append((node.name, bases, " ".join(said.split())))
    return out


def errors_page() -> str:
    """The error catalogue: every refusal a route can send and every exception the library raises."""
    out = [
        GENERATED,
        "",
        "# Errors",
        "",
        "Every way the server and the extraction service say no, read off "
        "the code, so a refusal added without its row fails a test. Each refusal a route sends has "
        "the one shape, `{\"ok\": false, \"message\": ..., \"data\": ..., \"detail\": ...}`: "
        "`message` is one sentence a person can act on, `detail` is what the route gave, kept for "
        "clients that read it, and `data` carries what a client needs to go on, such as `signin` "
        "when the answer is to sign in, `retry_after` on a rate limit, or `code` when a policy "
        "refused. The status says what kind of no it is:",
        "",
        "| Status | What it means | What to do |",
        "| --- | --- | --- |",
        "| 400 | The request is well formed and one of its values is wrong. | Fix the value the message names. |",
        "| 401 | Nobody is signed in, or the key is wrong. | Sign in, or send the key the server was given. |",
        "| 403 | Somebody is here, and this is not theirs to do. | A role, a scope, a fresh check, or a collection they may not retrieve from. |",
        "| 404 | It is not there, or it is not yours to know about. | A collection that is private to others answers exactly as one that does not exist. |",
        "| 409 | It would clash with what is there already. | Read what is there, then decide. |",
        "| 413 | The file is too big. | `VECTRIXDB_MAX_UPLOAD_BYTES` says how big. |",
        "| 415 | The body is in a form this server cannot take. | Send the file as the request body, or give the server python-multipart. |",
        "| 422 | The request is not the shape the route takes, or a file could not be read. | `detail` names the field, or the file. |",
        "| 429 | Too many, too fast. | Wait `retry_after` seconds. |",
        "| 500 | The server's own fault, and it says what. | Read the message; it names the setting or the piece. |",
        "| 501 | Not on this server. | A reader this build does not include. |",
        "| 502 | A service the server called answered badly. | The message names the service and its status. |",
        "| 503 | A service or a store the server needs is not there right now, and nothing was done. | Try again; the message names what was missing. |",
        "",
        "Exceptions the library raises in Python are on [Exceptions](exceptions.md); the table at the "
        "end lists them. The server turns a `PolicyError` into 403 (or 503 for a records store that "
        "cannot be read), a route's `HTTPException` into its own status, and a body that is not the "
        "shape asked for into 422. The extraction service turns a `DependencyError` into 503, an "
        "`ExtractionError` into 502 when a service answered badly and 422 when the file could not be "
        "read, and a `ConfigurationError` in what was asked into 422.",
        "",
        "## What a route can send",
        "",
    ]
    total = 0
    for relative, heading in _REFUSAL_FILES:
        rows = refusals_of(ROOT / relative)
        if not rows:
            continue
        grouped: Dict[Tuple[str, str], List[str]] = {}
        for status, message, function in rows:
            grouped.setdefault((status, message), [])
            if function not in grouped[(status, message)]:
                grouped[(status, message)].append(function)
        total += len(grouped)
        out += [f"### {heading}", "", f"`{relative}`", "", "| Status | Message | Raised by |", "| --- | --- | --- |"]
        for (status, message), functions in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1].lower())):
            out.append(f"| {status} | {_cell(message)} | {', '.join(f'`{f}`' for f in functions)} |")
        out.append("")
    out[out.index("## What a route can send")] = f"## What a route can send, {total} refusals"
    out += ["## Exceptions the library raises", "", "| Exception | Derives from | What it means |", "| --- | --- | --- |"]
    for name, bases, said in exception_classes():
        out.append(f"| `{name}` | `{bases}` | {_cell(said)} |")
    return "\n".join(out).rstrip() + "\n"


# ============================================================================
# MAIN SCRIPT: the pages, written or checked
# ============================================================================
#
# INPUT   --check
# OUTPUT  the pages written; or exit 1 when one is out of date, which the test
#         suite runs
#
# One command writes them all; the same command with --check is what holds
# them to the code.

PAGES = {
    "settings.md": settings_page,
    "cli.md": cli_page,
    "rest-api.md": rest_page,
    "openapi.json": openapi_document,
    "golden.schema.json": golden_schema_document,
    "errors.md": errors_page,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--check", action="store_true", help="fail when a page is out of date, and write nothing")
    args = parser.parse_args(argv)
    stale = []
    for name, build in PAGES.items():
        page = build()
        path = DOCS / name
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current == page:
            continue
        if args.check:
            stale.append(name)
        else:
            path.write_text(page, encoding="utf-8")
            print(f"wrote {path.relative_to(ROOT)}")
    if stale:
        print(f"out of date: {', '.join(stale)}. Run python scripts/make_reference.py")
        return 1
    if args.check:
        print("the reference pages are current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
