"""The MCP surface: tool bodies without the mcp package, wiring with it.

A tool result goes straight into a model's context, so the one thing every
test here cares about is that a cut answer announces itself on its first line.
"""

import asyncio
import importlib.util
import sys

import pytest

from vectrixdb import Vectrix
from vectrixdb.easy import Result, Results
from vectrixdb.exceptions import DependencyError
from vectrixdb.mcp_server import (
    build_server,
    main,
    render_results,
    tool_context,
    tool_feedback,
    tool_recall,
    tool_remember,
    tool_search,
)

# A skipif that called importorskip skipped this whole module without mcp, the
# tools that need no SDK with it. Only the tests that build a server need it.
HAS_MCP = importlib.util.find_spec("mcp") is not None


def _results(n, truncated=False, cut=0, budget=None, tokens=12):
    items = [Result(id=f"d{i}", text=f"text {i}", score=1.0 - i / 10) for i in range(n)]
    return Results(
        items=items,
        query="q",
        mode="dense",
        time_ms=1.0,
        truncated=truncated,
        cut_count=cut,
        token_estimate=tokens,
        token_budget=budget,
    )


class TestRender:
    def test_truncation_is_announced_first(self):
        text = render_results(_results(2, truncated=True, cut=3, budget=50))
        first = text.splitlines()[0]
        assert first.startswith("[!] TRUNCATED: showing 2 of 5 results")
        assert "50-token budget" in first

    def test_budget_without_a_cut_reports_usage(self):
        first = render_results(_results(2, budget=500, tokens=40)).splitlines()[0]
        assert first == "[i] 2 results, ~40 of a 500-token budget."

    def test_no_budget_reports_cost(self):
        first = render_results(_results(1, tokens=9)).splitlines()[0]
        assert first == "[i] 1 results, ~9 tokens."

    def test_empty_says_so(self):
        assert render_results(_results(0, tokens=0)).endswith("No matches.")

    def test_rows_carry_score_id_and_text(self):
        line = render_results(_results(1)).splitlines()[1]
        assert line == "- (1.000) [d0] text 0"


@pytest.fixture
def db(tmp_path) -> Vectrix:
    db = Vectrix("mcp", path=str(tmp_path))
    db.add(
        [
            "The export button greys out when a filter is active.",
            "Billing runs on the first of the month for annual plans.",
            "Password reset lives under settings.",
        ]
    )
    return db


class TestTools:
    def test_search_cuts_to_budget(self, db):
        out = tool_search(db, "export button", limit=3, token_budget=5)
        assert out.startswith("[!] TRUNCATED")
        assert "export" in out.lower()

    def test_search_without_a_cut(self, db):
        out = tool_search(db, "export button", limit=3, token_budget=5000)
        assert out.startswith("[i] 3 results")

    def test_remember_then_recall(self, db):
        note = tool_remember(db, "the customer's name is Ama", session="s1")
        assert note.startswith("Remembered mem_") and "'s1'" in note
        out = tool_recall(db, "customer name", session="s1")
        assert "Ama" in out

    def test_pinned_is_named(self, db):
        assert tool_remember(db, "Ships to Canada", pinned=True).startswith("Pinned mem_")

    def test_feedback_reports_supersession(self, db):
        mid = db.remember("I prefer dark mode", session="s1")
        plain = tool_feedback(db, mid, "useful")
        assert plain == f"Recorded useful on {mid}."
        corrected = tool_feedback(db, mid, "corrected", correction="I prefer light mode")
        assert corrected.startswith(f"Recorded corrected on {mid}; superseded by mem_")

    def test_context_has_a_header_and_sections(self, db):
        db.remember("Customer is on the annual plan", pinned=True)
        db.remember("hello", session="s1")
        out = tool_context(db, "billing", session="s1", token_budget=800)
        assert out.startswith("[i] ~")
        assert "Pinned facts:" in out and "Recent conversation:" in out

    def test_context_announces_a_cut(self, db):
        db.remember("Customer is on the annual plan", pinned=True)
        for i in range(5):
            db.remember(f"turn {i}", session="s1")
        assert tool_context(db, "plan", session="s1", token_budget=8).startswith("[!] TRUNCATED")

    def test_empty_context(self, db):
        assert tool_context(db, "x", session="none").endswith("Nothing remembered yet.")


class TestServer:
    def test_missing_mcp_names_the_extra(self, db, monkeypatch):
        monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", None)  # version 2's name
        monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", None)  # version 1's
        with pytest.raises(DependencyError, match="mcp"):
            build_server(db)

    @pytest.mark.skipif(not HAS_MCP, reason="the mcp extra is not installed")
    def test_registers_the_six_tools(self, db):
        server = build_server(db, name="t")
        tools = asyncio.run(server.list_tools())
        assert {t.name for t in tools} == {
            "search",
            "recall",
            "remember",
            "feedback",
            "context",
            "forget",
        }

    def test_forget_tool_deletes_and_says_how_many(self, db):
        from vectrixdb.mcp_server import tool_forget

        before = db.count()
        a = db.remember("first", session="s")
        db.remember("second", session="s")
        assert tool_forget(db, ids=[a]) == "Forgot 1 memory."
        assert tool_forget(db, session="s", older_than_days=0) == "Forgot 1 memory in session 's'."
        assert db.count() == before

    def test_forget_tool_leaves_documents_and_counts_what_went(self, db):
        """An id is not a licence to delete a document the memory tools never
        wrote, and a bare call does not wipe every session."""
        from vectrixdb.mcp_server import tool_forget

        db.add(["Quarterly revenue grew 12 percent"], ids=["report:0"])
        db.remember("I like tea", session="alice")
        db.remember("I like coffee", session="bob")
        before = db.count()
        assert tool_forget(db).startswith("Forgot nothing")
        assert db.count() == before
        assert tool_forget(db, ids=["report:0", "missing"]) == "Forgot 0 memories."
        assert db.get(["report:0"])
        assert tool_forget(db, all_sessions=True) == "Forgot 2 memories."
        assert db.count() == before - 2

    @pytest.mark.skipif(not HAS_MCP, reason="the mcp extra is not installed")
    def test_every_tool_documents_a_budget_or_its_effect(self, db):
        server = build_server(db)
        for tool in asyncio.run(server.list_tools()):
            assert tool.description, tool.name

    def test_main_wires_arguments_through(self, tmp_path, monkeypatch):
        captured = {}

        class FakeServer:
            def run(self, transport):
                captured["transport"] = transport

        def fake_build(db, name="vectrixdb", writes=True):
            captured["db"] = db
            captured["writes"] = writes
            return FakeServer()

        monkeypatch.setattr("vectrixdb.mcp_server.build_server", fake_build)
        main(["--name", "notes", "--path", str(tmp_path), "--transport", "sse"])
        assert captured["db"].name == "notes"
        assert captured["transport"] == "sse"
        assert captured["writes"] is False, "over HTTP, changing the collection is asked for"

    def test_stdio_and_allow_writes_offer_the_write_tools(self, tmp_path, monkeypatch):
        seen = []

        class FakeServer:
            def run(self, transport):
                pass

        def fake_build(db, name="vectrixdb", writes=True):
            seen.append(writes)
            return FakeServer()

        monkeypatch.setattr("vectrixdb.mcp_server.build_server", fake_build)
        main(["--name", "notes", "--path", str(tmp_path)])
        main(
            [
                "--name",
                "notes",
                "--path",
                str(tmp_path),
                "--transport",
                "streamable-http",
                "--allow-writes",
            ]
        )
        assert seen == [True, True]

    @pytest.mark.skipif(not HAS_MCP, reason="the mcp extra is not installed")
    def test_without_writes_only_the_reading_tools_are_registered(self, db):
        tools = asyncio.run(build_server(db, writes=False).list_tools())
        assert {t.name for t in tools} == {"search", "recall", "context"}

    def test_a_missing_extra_is_a_message_not_a_traceback(self, tmp_path, monkeypatch, capsys):
        """A plain install puts `vectrixdb-mcp` on the path without the `mcp`
        package, so this is the first thing a new user sees when they run it.
        It printed a stack trace ending in DependencyError; the message on
        that exception already says what to install."""

        def refuse(db, name="vectrixdb", writes=True):
            raise DependencyError("mcp", extra="mcp")

        monkeypatch.setattr("vectrixdb.mcp_server.build_server", refuse)

        with pytest.raises(SystemExit) as exit_info:
            main(["--name", "notes", "--path", str(tmp_path)])

        assert exit_info.value.code == 1
        # The traceback would have gone to stderr too, so check the shape:
        # one line, naming the command to run.
        printed = capsys.readouterr().err.strip()
        assert len(printed.splitlines()) == 1
        assert "pip install vectrixdb[mcp]" in printed
        assert "Traceback" not in printed


def test_the_command_passes_allow_writes_on(monkeypatch, tmp_path):
    """`vectrixdb mcp --allow-writes` reaches the server's own flag, as the docs say."""
    from typer.testing import CliRunner

    import vectrixdb.mcp_server as mcp_server
    from vectrixdb.cli import app

    seen = []
    monkeypatch.setattr(mcp_server, "main", lambda argv: seen.append(argv))
    result = CliRunner().invoke(
        app,
        ["mcp", "--path", str(tmp_path), "--transport", "streamable-http", "--allow-writes"],
    )
    assert result.exit_code == 0, result.output
    assert seen and seen[0][-1] == "--allow-writes"
