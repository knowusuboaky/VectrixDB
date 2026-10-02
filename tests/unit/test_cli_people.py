"""The people commands, run on the server beside the database, and what serve prints and starts.

What is being held to: `vectrixdb people add` makes a person and prints a
one-time set-up link whole on one line; it will not send somebody already set
up round again, and says to reset them instead; `people reset` forgets their
passkeys, authenticator and recovery codes, signs them out and prints a new
link, and for an address nobody has says how to add them; `people list` shows
each person's role, how they sign in and when they last did; `people remove`
takes them off; with sign-in off every one of them stops with code 2 and says
what to set, and with single sign-on alone the list works with no link and
only reset stops; a link behind a gateway is written the way people reach the
dashboard; and a link the command prints is one the server on the same
--path accepts, once. Also held: importing the server builds no app and makes
no vectrixdb_data folder where it was started; the app serve starts is built
on first use, with its sign-in file under VECTRIXDB_PATH and no dashboard when
VECTRIXDB_DASHBOARD=0; and the serve banner names the command that makes the
first admin, never a link.
"""

from __future__ import annotations

import datetime
import importlib
import json
import os
import re
import subprocess
import sys
import types
from contextlib import contextmanager
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

import vectrixdb
from vectrixdb.cli import app
from vectrixdb.signin import SignInStore, totp

SECRET = "k" * 48
PUBLIC = "https://vectors.example.test"
LINK = re.compile(r"https://vectors\.example\.test/dashboard/#/enrol\?token=([\w-]+)")
ROOT = Path(vectrixdb.__file__).resolve().parent.parent


def create_app(**kwargs):
    """The server as it is now. Another test drops and re-imports ``vectrixdb.api``,
    and a name bound at the top of this file would go on pointing at the old copy."""
    from vectrixdb.api.server import create_app as current

    return current(**kwargs)


@pytest.fixture(autouse=True)
def email_signin(monkeypatch):
    """The settings of a server with email sign-in, which the commands read the same way it does."""
    for name in [n for n in os.environ if n.startswith("VECTRIXDB_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("VECTRIXDB_SIGNIN", "email")
    monkeypatch.setenv("VECTRIXDB_SIGNIN_SECRET", SECRET)
    monkeypatch.setenv("VECTRIXDB_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def db(tmp_path) -> str:
    return str(tmp_path / "db")


@pytest.fixture
def wide(monkeypatch):
    """A console wide enough that no table cell wraps, whatever the widest address or role."""
    import vectrixdb.cli

    monkeypatch.setattr(vectrixdb.cli, "console", Console(width=200))


def people(runner, *args):
    return runner.invoke(app, ["people", *args])


def flat(output: str) -> str:
    """The output as one line. The console wraps a long sentence at its width."""
    return " ".join(output.split())


def link_in(output: str) -> str:
    """The token of the one link printed, which must sit whole on a line of its own."""
    lines = [line for line in output.splitlines() if "token=" in line]
    assert len(lines) == 1, output
    found = LINK.fullmatch(lines[0].strip())
    assert found is not None, f"the link is not whole on one line: {lines[0]!r}"
    return found.group(1)


def row(output: str, first_cell: str) -> list:
    """The cells of the table row that starts with this."""
    line = next(line for line in output.splitlines() if first_cell in line)
    return [cell for cell in re.split(r"\s*[│┃║|]\s*", line.strip()) if cell]


def open_store(db: str) -> SignInStore:
    return SignInStore(Path(db) / "auth" / "signin.db", [SECRET])


def stored(db: str, email: str):
    store = open_store(db)
    try:
        return store.person(email)
    finally:
        store.close()


def last_sign_in(db: str, email: str) -> str:
    return datetime.datetime.fromtimestamp(stored(db, email).last_sign_in).strftime("%d %b %Y %H:%M")


def set_up(db: str, email: str, *, authenticator: bool = True, passkeys: int = 0) -> None:
    """Somebody opens their link and finishes: their ways in, recovery codes, and a sign-in."""
    pytest.importorskip("cryptography", reason="the signin extra is not installed")
    store = open_store(db)
    try:
        if authenticator:
            secret = store.begin_enrolment(email)
            assert store.check_code(email, totp.code_at(secret, totp.step_now()), confirming=True)
        for n in range(passkeys):
            # A credential id is unique across everybody, as a real authenticator's is.
            store.add_passkey(email, f"{email}/credential-{n}".encode(), b"public-key", -7, 0, ["internal"], f"Laptop {n}")
        store.new_recovery_codes(email)
        role = store.person(email).role
        store.open_session(subject=email, email=email, name=None, role=role, principal=None, method="email", hours=8)
    finally:
        store.close()


@contextmanager
def server(db: str):
    """The server on the same path, set up from the same environment as the commands."""
    pytest.importorskip("fastapi", reason="the API extra is not installed")
    from fastapi.testclient import TestClient

    built = create_app(db_path=db, enable_dashboard=False)
    try:
        yield TestClient(built, base_url=PUBLIC)
    finally:
        built.state.signin.close()


def enrol(client, token: str) -> None:
    """The first visit from a set-up link: spend it, then confirm an authenticator code."""
    begun = client.post("/auth/email/enrol/begin", json={"token": token})
    assert begun.status_code == 200, begun.text
    data = begun.json()["data"]
    done = client.post("/auth/email/enrol/confirm", json={"ticket": data["ticket"], "code": totp.code_at(data["secret"], totp.step_now())})
    assert done.status_code == 200, done.text


def dashboard_mounted(built) -> bool:
    return any(getattr(route, "name", None) == "dashboard" for route in built.routes)


# ---------------------------------------------------------------------- add


class TestAdd:
    def test_it_makes_the_person_and_prints_a_one_time_link_on_one_line(self, runner, db):
        result = people(runner, "add", "Ada@Example.com", "--role", "admin", "--path", db)
        assert result.exit_code == 0, result.output
        assert "Added ada@example.com as an admin." in result.output
        assert "Open this to set up a passkey or an authenticator app. It works once, for 15 minutes:" in flat(result.output)
        link_in(result.output)
        person = stored(db, "ada@example.com")
        assert person.role == "admin" and not person.enrolled

    @pytest.mark.parametrize("role, said", [(None, "a viewer"), ("operator", "an operator"), ("admin", "an admin")])
    def test_the_role_is_viewer_unless_given_and_is_said_with_the_right_article(self, runner, db, role, said):
        result = people(runner, "add", "sam@example.com", "--path", db, *(["--role", role] if role else []))
        assert result.exit_code == 0, result.output
        assert f"Added sam@example.com as {said}." in result.output

    @pytest.mark.parametrize(
        "args, said",
        [(("x@example.com", "--role", "root"), "'root' is not a role"), (("not-an-address",), "'not-an-address' is not an email address")],
        ids=["a role that is not one", "an address that is not one"],
    )
    def test_a_mistake_is_refused_with_code_2_and_nobody_is_added(self, runner, db, args, said):
        result = people(runner, "add", *args, "--path", db)
        assert result.exit_code == 2 and said in flat(result.output)
        assert "token=" not in result.output
        store = open_store(db)
        try:
            assert store.people() == []
        finally:
            store.close()

    def test_somebody_already_set_up_is_not_sent_round_again_and_is_told_to_reset(self, runner, db):
        people(runner, "add", "ada@example.com", "--role", "admin", "--path", db)
        set_up(db, "ada@example.com")
        result = people(runner, "add", "ada@example.com", "--role", "viewer", "--path", db)
        assert result.exit_code == 1
        assert (
            "ada@example.com is already here, as admin, and set up. To send them round again: vectrixdb people reset ada@example.com"
        ) in flat(result.output)
        assert "token=" not in result.output
        person = stored(db, "ada@example.com")
        assert person.role == "admin" and person.enrolled, "nothing about them changed"


# -------------------------------------------------------------------- reset


class TestReset:
    def test_it_forgets_their_ways_in_signs_them_out_and_prints_a_new_link(self, runner, db):
        people(runner, "add", "ada@example.com", "--role", "admin", "--path", db)
        set_up(db, "ada@example.com", passkeys=1)
        result = people(runner, "reset", "ADA@example.com", "--path", db)
        assert result.exit_code == 0, result.output
        said = flat(result.output)
        assert "Removed the passkeys, authenticator and recovery codes for ada@example.com, and signed them out everywhere." in said
        assert "Open this to set up a new way in. It works once, for 15 minutes:" in said
        link_in(result.output)
        store = open_store(db)
        try:
            person = store.person("ada@example.com")
            assert person.role == "admin" and not person.enrolled and not person.authenticator and person.passkeys == 0
            assert store.recovery_codes_left("ada@example.com") == 0 and store.sessions_of("ada@example.com") == []
        finally:
            store.close()

    def test_an_address_nobody_has_is_told_how_to_add_them(self, runner, db):
        result = people(runner, "reset", "nobody@example.com", "--path", db)
        assert result.exit_code == 1
        assert "Nobody by the address nobody@example.com is on this server. Add them with: vectrixdb people add nobody@example.com" in flat(result.output)
        assert "token=" not in result.output


# --------------------------------------------------------------------- list


class TestList:
    def test_it_shows_each_persons_role_how_they_sign_in_and_when_they_last_did(self, runner, db, wide):
        for email, role in (("ada@example.com", "admin"), ("olu@example.com", "operator"), ("vi@example.com", "viewer")):
            people(runner, "add", email, "--role", role, "--path", db)
        set_up(db, "ada@example.com", passkeys=2)
        set_up(db, "olu@example.com", authenticator=False, passkeys=1)
        result = people(runner, "list", "--path", db)
        assert result.exit_code == 0, result.output
        assert row(result.output, "Email") == ["Email", "Role", "Signs in with", "Last sign-in"]
        assert row(result.output, "ada@example.com") == ["ada@example.com", "admin", "2 passkeys, authenticator app", last_sign_in(db, "ada@example.com")]
        assert row(result.output, "olu@example.com") == ["olu@example.com", "operator", "1 passkey", last_sign_in(db, "olu@example.com")]
        assert row(result.output, "vi@example.com") == ["vi@example.com", "viewer", "not set up yet", "never"]

    def test_somebody_disabled_is_marked_so(self, runner, db, wide):
        people(runner, "add", "sam@example.com", "--path", db)
        store = open_store(db)
        try:
            store.set_disabled("sam@example.com", True)
        finally:
            store.close()
        assert row(people(runner, "list", "--path", db).output, "sam@example.com")[1] == "viewer (disabled)"

    def test_with_nobody_yet_it_says_how_to_add_the_first_admin(self, runner, db):
        result = people(runner, "list", "--path", db)
        assert result.exit_code == 0
        assert "Nobody yet. Add the first admin with: vectrixdb people add you@company.com --role admin" in flat(result.output)


# ------------------------------------------------------------------- remove


class TestRemove:
    def test_it_takes_them_off_and_signs_them_out(self, runner, db):
        people(runner, "add", "ada@example.com", "--role", "admin", "--path", db)
        people(runner, "add", "vi@example.com", "--path", db)
        set_up(db, "vi@example.com")
        result = people(runner, "remove", "VI@example.com", "--path", db)
        assert result.exit_code == 0, result.output
        assert "Removed vi@example.com. They are signed out and can no longer sign in." in flat(result.output)
        store = open_store(db)
        try:
            assert store.person("vi@example.com") is None and store.sessions_of("vi@example.com") == []
            assert [p.email for p in store.people()] == ["ada@example.com"]
        finally:
            store.close()

    def test_the_only_admin_stays(self, runner, db):
        people(runner, "add", "ada@example.com", "--role", "admin", "--path", db)
        result = people(runner, "remove", "ada@example.com", "--path", db)
        assert result.exit_code == 1 and "This is the only admin." in flat(result.output)
        assert stored(db, "ada@example.com") is not None

    def test_an_address_nobody_has_is_said_plainly(self, runner, db):
        result = people(runner, "remove", "nobody@example.com", "--path", db)
        assert result.exit_code == 1 and "Nobody by the address nobody@example.com is on this server." in flat(result.output)


# ----------------------------------------------------- email sign-in is off

COMMANDS = [("add", "ada@example.com"), ("reset", "ada@example.com"), ("list",), ("remove", "ada@example.com")]


class TestWithSignInOff:
    @pytest.mark.parametrize("args", COMMANDS, ids=[command[0] for command in COMMANDS])
    def test_every_command_stops_with_code_2_and_says_what_to_set(self, runner, db, monkeypatch, args):
        monkeypatch.delenv("VECTRIXDB_SIGNIN")
        result = people(runner, *args, "--path", db)
        assert result.exit_code == 2, result.output
        said = flat(result.output)
        assert "sign-in is not on here" in said
        for setting in ("VECTRIXDB_SIGNIN=oidc,email", "VECTRIXDB_SIGNIN_SECRET", "VECTRIXDB_PUBLIC_URL"):
            assert setting in said, setting
        assert "token=" not in result.output and not (Path(db) / "auth").exists(), "and nothing was written"


class TestWithSingleSignOnAlone:
    """The People list is who may sign in whichever way, so it is managed here with single sign-on alone too."""

    @pytest.fixture(autouse=True)
    def sso_only(self, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_SIGNIN", "oidc")
        monkeypatch.setenv("VECTRIXDB_OIDC_ISSUER", "https://idp.example.test/tenant")
        monkeypatch.setenv("VECTRIXDB_OIDC_CLIENT_ID", "vectrixdb")
        monkeypatch.setenv("VECTRIXDB_OIDC_DEFAULT_ROLE", "viewer")

    def test_add_puts_them_on_the_list_and_prints_no_link(self, runner, db):
        result = people(runner, "add", "ada@example.com", "--role", "admin", "--path", db)
        assert result.exit_code == 0, result.output
        said = flat(result.output)
        assert "Added ada@example.com as an admin." in said and "They sign in with single sign-on." in said
        assert "token=" not in result.output, "a link sets up a way of the list's own, and there are none here"

    def test_list_and_remove_work_as_they_do_with_email(self, runner, db, wide):
        people(runner, "add", "ada@example.com", "--role", "admin", "--path", db)
        people(runner, "add", "sam@example.com", "--path", db)
        listed = flat(people(runner, "list", "--path", db).output)
        assert "ada@example.com" in listed and "sam@example.com" in listed
        removed = people(runner, "remove", "sam@example.com", "--path", db)
        assert removed.exit_code == 0 and "sam@example.com" not in flat(people(runner, "list", "--path", db).output)

    def test_reset_stops_with_code_2_since_nothing_is_kept_here_to_reset(self, runner, db):
        people(runner, "add", "ada@example.com", "--role", "admin", "--path", db)
        result = people(runner, "reset", "ada@example.com", "--path", db)
        assert result.exit_code == 2, result.output
        said = flat(result.output)
        assert "email sign-in is not on here" in said and "VECTRIXDB_SIGNIN=oidc,email" in said
        assert "token=" not in result.output

    def test_with_both_ways_on_the_link_comes_with_a_word_about_single_sign_on(self, runner, db, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_SIGNIN", "oidc,email")
        result = people(runner, "add", "ada@example.com", "--role", "admin", "--path", db)
        said = flat(result.output)
        assert "They sign in with single sign-on, then may add an authenticator app from their account." in said
        assert LINK.search(result.output), "and the link, for somebody who sets up a way of their own first"


class TestTheLinkBehindAGateway:
    def test_it_carries_the_dashboards_gateway_path_and_the_prefix(self, runner, db, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_PREFIX", "acme")
        monkeypatch.setenv("VECTRIXDB_GATEWAY_PATHS", "dashboard=/files/dash")
        result = people(runner, "add", "ada@example.com", "--role", "admin", "--path", db)
        assert re.search(r"https://vectors\.example\.test/files/dash/acme/dashboard/#/enrol\?token=[\w-]+", result.output), result.output


# ------------------------------------------------- the server takes the link


class TestTheServerOnTheSamePathTakesTheLink:
    def test_the_link_the_command_prints_is_accepted_once(self, runner, db):
        token = link_in(people(runner, "add", "ada@example.com", "--role", "admin", "--path", db).output)
        with server(db) as client:
            begun = client.post("/auth/email/enrol/begin", json={"token": token})
            assert begun.status_code == 200, begun.text
            assert begun.json()["data"]["email"] == "ada@example.com"
            assert client.post("/auth/email/enrol/begin", json={"token": token}).status_code == 400, "it works once"

    def test_the_first_visit_from_a_console_link_signs_them_in_with_their_role(self, runner, db):
        pytest.importorskip("cryptography", reason="the signin extra is not installed")
        token = link_in(people(runner, "add", "ada@example.com", "--role", "admin", "--path", db).output)
        with server(db) as client:
            enrol(client, token)
            me = client.get("/auth/me").json()["data"]["person"]
            assert me["email"] == "ada@example.com" and me["role"] == "admin"

    def test_a_reset_signs_them_out_of_the_server_and_its_new_link_works(self, runner, db):
        pytest.importorskip("cryptography", reason="the signin extra is not installed")
        token = link_in(people(runner, "add", "ada@example.com", "--role", "admin", "--path", db).output)
        with server(db) as browser:
            enrol(browser, token)
            assert browser.get("/auth/me").status_code == 200
            fresh = link_in(people(runner, "reset", "ada@example.com", "--path", db).output)
            assert browser.get("/auth/me").status_code == 401, "signed out everywhere"
            assert browser.post("/auth/email/enrol/begin", json={"token": fresh}).status_code == 200

    def test_adding_somebody_again_before_they_set_up_replaces_their_link(self, runner, db):
        first = link_in(people(runner, "add", "vi@example.com", "--path", db).output)
        second = link_in(people(runner, "add", "vi@example.com", "--role", "operator", "--path", db).output)
        assert first != second and stored(db, "vi@example.com").role == "operator"
        with server(db) as client:
            assert client.post("/auth/email/enrol/begin", json={"token": first}).status_code == 400
            assert client.post("/auth/email/enrol/begin", json={"token": second}).status_code == 200


class TestWhatTheConsoleDoesIsWrittenDown:
    def test_each_change_is_in_the_access_log_as_the_server_console_and_no_link_is(self, runner, db):
        printed = [
            people(runner, "add", "ada@example.com", "--role", "admin", "--path", db).output,
            people(runner, "add", "vi@example.com", "--path", db).output,
            people(runner, "reset", "vi@example.com", "--path", db).output,
        ]
        people(runner, "remove", "vi@example.com", "--path", db)
        log = (Path(db) / "auth" / "access.jsonl").read_text(encoding="utf-8")
        records = [json.loads(line) for line in log.splitlines()]
        assert [(r["event"], r["who"], r["by"]) for r in records] == [
            ("person_added", "ada@example.com", "server console"),
            ("person_added", "vi@example.com", "server console"),
            ("authenticator_reset", "vi@example.com", "server console"),
            ("person_removed", "vi@example.com", "server console"),
        ]
        assert not any(link_in(output) in log for output in printed)


# -------------------------------------------------------------------- serve


class TestTheServeBanner:
    @pytest.fixture
    def started(self, monkeypatch):
        """run_server replaced, so nothing listens. What it was asked to do is kept."""
        pytest.importorskip("fastapi", reason="the API extra is not installed")
        import vectrixdb.api.server as server_module

        calls = []
        monkeypatch.setattr(server_module, "run_server", lambda **kwargs: calls.append(kwargs))
        return calls

    def test_with_no_admin_it_prints_the_command_to_run_and_never_a_link(self, runner, db, started):
        result = runner.invoke(app, ["serve", "--path", db])
        assert result.exit_code == 0, result.output
        lines = [line.strip() for line in result.output.splitlines()]
        at = lines.index("No admin yet. To add the first one, run this on the server:")
        assert lines[at + 1] == "vectrixdb people add you@company.com --role admin"
        assert "token" not in result.output and "#/enrol" not in result.output and PUBLIC not in result.output
        assert len(started) == 1 and started[0]["db_path"] == db, "and the server is started all the same"

    def test_a_viewer_is_not_an_admin_and_once_there_is_one_the_line_goes(self, runner, db, started):
        people(runner, "add", "vi@example.com", "--path", db)
        assert "No admin yet" in runner.invoke(app, ["serve", "--path", db]).output
        people(runner, "add", "ada@example.com", "--role", "admin", "--path", db)
        result = runner.invoke(app, ["serve", "--path", db])
        assert result.exit_code == 0, result.output
        assert "No admin yet" not in result.output and "people add" not in result.output

    def test_an_admin_the_settings_name_is_added_at_the_start_so_there_is_no_such_line(self, runner, db, started, monkeypatch):
        monkeypatch.setenv("VECTRIXDB_SIGNIN_USERS", "pat@example.com:admin")
        result = runner.invoke(app, ["serve", "--path", db])
        assert result.exit_code == 0, result.output
        assert "No admin yet" not in result.output
        monkeypatch.setenv("VECTRIXDB_SIGNIN_USERS", "sam@example.com:viewer")
        assert "No admin yet" in runner.invoke(app, ["serve", "--path", db]).output, "a viewer is not an admin"

    def test_with_email_sign_in_off_there_is_no_such_line_and_nothing_is_written(self, runner, db, started, monkeypatch):
        monkeypatch.delenv("VECTRIXDB_SIGNIN")
        result = runner.invoke(app, ["serve", "--path", db])
        assert result.exit_code == 0, result.output
        assert "No admin yet" not in result.output and not (Path(db) / "auth").exists()


class TestImportingTheServerBuildsNothing:
    def test_importing_it_makes_no_app_and_no_folder_where_it_was_started(self, tmp_path):
        pytest.importorskip("fastapi", reason="the API extra is not installed")
        script = (
            "import os\n"
            "import vectrixdb.api\n"
            "import vectrixdb.api.server as server\n"
            "from vectrixdb.api import create_app\n"
            "print('app' in vars(server))\n"
            "print(sorted(os.listdir('.')))\n"
        )
        # Sign-in on, so an app built at import would have opened its sign-in file in ./vectrixdb_data.
        env = {name: value for name, value in os.environ.items() if not name.startswith("VECTRIXDB_")}
        env.update(
            VECTRIXDB_SIGNIN="email",
            VECTRIXDB_SIGNIN_SECRET=SECRET,
            VECTRIXDB_PUBLIC_URL=PUBLIC,
            VECTRIXDB_OFFLINE="1",
            PYTHONPATH=os.pathsep.join(p for p in (str(ROOT), os.environ.get("PYTHONPATH", "")) if p),
        )
        done = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
        assert done.returncode == 0, done.stderr
        assert done.stdout.splitlines() == ["False", "[]"], done.stdout
        assert not (tmp_path / "vectrixdb_data").exists()


@pytest.fixture
def unbuilt(monkeypatch, tmp_path):
    """The server module with no app built yet in this process, and whatever the test builds taken away after."""
    pytest.importorskip("fastapi", reason="the API extra is not installed")
    import vectrixdb.api.server as server_module

    monkeypatch.delitem(vars(server_module), "app", raising=False)
    # run_server writes these two straight into the environment. Setting them here puts them back afterwards.
    monkeypatch.setenv("VECTRIXDB_PATH", str(tmp_path / "unused"))
    monkeypatch.setenv("VECTRIXDB_DASHBOARD", "1")
    yield server_module
    built = vars(server_module).pop("app", None)
    if built is not None and built.state.signin is not None:
        built.state.signin.close()


@pytest.fixture
def uvicorn_loads(monkeypatch):
    """uvicorn replaced by the first thing it does: load the app from its import string. Nothing listens."""
    loaded = {}

    def run(target, **options):
        module, _, name = target.partition(":")
        loaded.update(target=target, options=options, app=getattr(importlib.import_module(module), name))

    fake = types.ModuleType("uvicorn")
    fake.run = run
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    return loaded


class TestTheAppServeStarts:
    @pytest.mark.parametrize("flag, mounted", [("--no-dashboard", False), ("--dashboard", True)])
    def test_serve_builds_it_on_first_use_with_the_path_and_the_dashboard_it_was_given(
        self, runner, tmp_path, unbuilt, uvicorn_loads, flag, mounted
    ):
        served = tmp_path / "served"
        result = runner.invoke(app, ["serve", "--path", str(served), flag])
        assert result.exit_code == 0, result.output
        assert uvicorn_loads["target"] == "vectrixdb.api.server:app"
        built = uvicorn_loads["app"]
        assert Path(built.state.signin.config.store_path) == served / "auth" / "signin.db"
        assert os.environ["VECTRIXDB_PATH"] == str(served) and os.environ["VECTRIXDB_DASHBOARD"] == ("1" if mounted else "0")
        assert dashboard_mounted(built) is mounted

    @pytest.mark.parametrize("value", ["0", "false", "no"])
    def test_the_app_is_built_on_first_use_from_the_environment_and_then_kept(self, tmp_path, monkeypatch, unbuilt, value):
        import vectrixdb.api

        monkeypatch.setenv("VECTRIXDB_PATH", str(tmp_path / "db"))
        monkeypatch.setenv("VECTRIXDB_DASHBOARD", value)
        assert "app" not in vars(unbuilt)
        built = unbuilt.app
        assert vars(unbuilt)["app"] is built and unbuilt.app is built and vectrixdb.api.app is built, "built once, then kept"
        assert Path(built.state.signin.config.store_path) == tmp_path / "db" / "auth" / "signin.db"
        assert not dashboard_mounted(built)
        with pytest.raises(AttributeError):
            unbuilt.not_a_setting  # noqa: B018 - only the app is built on demand
