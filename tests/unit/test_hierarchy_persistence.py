"""Communities have to survive a restart, and so does search when there are none.

`save_hierarchy` returned early and then did nothing, above a comment claiming
the rows were "already saved during detection via save_community". Nothing in
the package ever called `save_community`, so the `communities` and
`community_members` tables were written by no code path at all.
`load_hierarchy` matched it by returning None unconditionally.

That was not a cosmetic gap. The pipeline gated its global and hybrid searchers
behind a truthy hierarchy, and `GraphSearchType.HYBRID` is the default, so every
reopened graph raised "Hybrid searcher not initialized" on search. The caller
swallowed it and returned vector-only results, which look entirely plausible.
"""

import logging
import sqlite3

import pytest

from vectrixdb import Vectrix
from vectrixdb.core.graphrag.extractor.base import Entity
from vectrixdb.core.graphrag.graph.community import Community, CommunityHierarchy
from vectrixdb.core.graphrag.graph.storage import GraphStorage

CORPUS = [
    "Marie Curie discovered radium in Paris.",
    "Pierre Curie worked with Marie Curie on radioactivity.",
    "Radium is a radioactive element used in early medicine.",
    "The Nobel Prize was awarded for research on radioactivity.",
    "Radioactivity research in Paris led to the Nobel Prize.",
]


def _store(tmp_path, entities=("e1", "e2", "e3", "e4")) -> GraphStorage:
    """A store with entities present, since load_hierarchy consults them."""
    storage = GraphStorage(str(tmp_path / "graph.db"))
    storage.save_entities([Entity(id=e, name=e.upper(), type="THING") for e in entities])
    return storage


def _two_levels() -> CommunityHierarchy:
    hierarchy = CommunityHierarchy()
    hierarchy.add_community(
        Community(
            id="community_L1_0",
            level=1,
            entity_ids=["e1", "e2", "e3", "e4"],
            summary="everything",
            importance=0.9,
        )
    )
    for index, members in enumerate((["e1", "e2"], ["e3", "e4"])):
        hierarchy.add_community(
            Community(
                id=f"community_L0_{index}",
                level=0,
                entity_ids=members,
                parent_id="community_L1_0",
                summary=f"group {index}",
                importance=0.5,
            )
        )
    return hierarchy


class TestRoundTrip:
    @pytest.fixture
    def reloaded(self, tmp_path) -> CommunityHierarchy:
        storage = _store(tmp_path)
        storage.save_hierarchy(_two_levels())
        storage.close()
        return GraphStorage(str(tmp_path / "graph.db")).load_hierarchy()

    def test_a_hierarchy_comes_back_at_all(self, reloaded):
        assert reloaded is not None
        assert reloaded.total_communities == 3

    def test_levels_are_preserved(self, reloaded):
        assert reloaded.num_levels == 2
        assert len(reloaded.get_level(0)) == 2
        assert len(reloaded.get_level(1)) == 1

    def test_members_are_preserved(self, reloaded):
        assert sorted(reloaded.get_community("community_L0_0").entity_ids) == ["e1", "e2"]

    def test_summary_and_importance_are_preserved(self, reloaded):
        top = reloaded.get_community("community_L1_0")
        assert top.summary == "everything"
        assert top.importance == pytest.approx(0.9)

    def test_the_entity_lookup_is_rebuilt(self, reloaded):
        """Not stored: add_community derives it, which is why the rows suffice."""
        assert reloaded.get_community_for_entity("e3", level=0).id == "community_L0_1"

    def test_parent_links_survive(self, reloaded):
        assert reloaded.get_community("community_L0_1").parent_id == "community_L1_0"

    def test_children_are_derived_from_parents(self, reloaded):
        """children_ids is not a column, so it has to be reconstructed."""
        assert sorted(reloaded.get_community("community_L1_0").children_ids) == [
            "community_L0_0",
            "community_L0_1",
        ]


class TestStaleRows:
    """Community ids are positional and get reused across rebuilds."""

    def test_a_smaller_rebuild_leaves_no_phantoms(self, tmp_path):
        storage = _store(tmp_path)
        storage.save_hierarchy(_two_levels())

        smaller = CommunityHierarchy()
        smaller.add_community(Community(id="community_L0_0", level=0, entity_ids=["e1"]))
        storage.save_hierarchy(smaller)

        reloaded = storage.load_hierarchy()
        assert reloaded.total_communities == 1, "communities from the previous build survived"
        assert reloaded.get_community("community_L0_1") is None

    def test_members_of_a_reused_id_are_replaced_not_appended(self, tmp_path):
        storage = _store(tmp_path)
        storage.save_hierarchy(_two_levels())

        rebuilt = CommunityHierarchy()
        rebuilt.add_community(Community(id="community_L0_0", level=0, entity_ids=["e4"]))
        storage.save_hierarchy(rebuilt)

        assert storage.load_hierarchy().get_community("community_L0_0").entity_ids == ["e4"]

    def test_saving_an_empty_hierarchy_clears_the_table(self, tmp_path):
        storage = _store(tmp_path)
        storage.save_hierarchy(_two_levels())
        assert storage.load_communities(), "the first save wrote nothing, so this proves nothing"

        storage.save_hierarchy(CommunityHierarchy())
        assert storage.load_communities() == []


class _RecordingCursor:
    """Notes the SQL a cursor is asked to run, then runs it."""

    def __init__(self, cursor, statements):
        self._cursor = cursor
        self._statements = statements

    def execute(self, sql, params=()):
        self._statements.append((sql, len(params)))
        return self._cursor.execute(sql, params)

    def executemany(self, sql, seq):
        rows = list(seq)
        # One statement run per row, so the parameter count per call is the
        # width of a row, not the length of the sequence.
        self._statements.append((sql, len(rows[0]) if rows else 0))
        return self._cursor.executemany(sql, rows)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _RecordingConnection:
    def __init__(self, conn, statements):
        self._conn = conn
        self._statements = statements

    def cursor(self):
        return _RecordingCursor(self._conn.cursor(), self._statements)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestScale:
    """No statement may bind one parameter per community.

    An earlier cut of this deleted stale rows with
    `DELETE ... WHERE id NOT IN (?, ?, ...)`, one placeholder per surviving
    community. That raises `sqlite3.OperationalError: too many SQL variables`
    past SQLITE_LIMIT_VARIABLE_NUMBER, which is 32766 on SQLite 3.32 and later
    but only 999 before it. It failed on precisely the graphs big enough to
    care, and on a machine with a recent SQLite you would never see it.
    """

    @staticmethod
    def _hierarchy_of(count) -> CommunityHierarchy:
        hierarchy = CommunityHierarchy()
        for index in range(count):
            hierarchy.add_community(
                Community(id=f"community_L0_{index}", level=0, entity_ids=["e0"])
            )
        return hierarchy

    def test_parameter_count_does_not_grow_with_the_hierarchy(self, tmp_path):
        """Asserts the shape of the statements, so it holds on any Python.

        The end-to-end version of this needs `Connection.setlimit` (3.11) or a
        hierarchy bigger than the real limit, which costs ~20s to build. This
        measures the thing that actually matters and is instant.
        """
        storage = _store(tmp_path, entities=("e0",))
        statements = []
        storage._local.conn = _RecordingConnection(storage._conn, statements)

        storage.save_hierarchy(self._hierarchy_of(500))

        assert statements, "nothing was recorded, so this proves nothing"
        worst_sql, worst = max(statements, key=lambda s: s[1])
        assert worst <= 8, f"{worst} parameters bound in one statement: {worst_sql[:80]}"

    @pytest.mark.skipif(
        not hasattr(sqlite3.Connection, "setlimit"),
        reason="Connection.setlimit needs Python 3.11",
    )
    def test_it_survives_a_limit_far_below_the_community_count(self, tmp_path):
        """The same guarantee end to end, with the limit lowered to force it."""
        storage = _store(tmp_path, entities=("e0",))
        storage._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 20)

        storage.save_hierarchy(self._hierarchy_of(200))
        assert storage.load_hierarchy().total_communities == 200


class TestEmptyIsNotMissing:
    """A graph too small to form a community still has a hierarchy: an empty one."""

    def test_entities_without_communities_load_an_empty_hierarchy(self, tmp_path):
        reloaded = _store(tmp_path).load_hierarchy()
        assert reloaded is not None, "an unclustered graph is not an absent one"
        assert reloaded.total_communities == 0

    def test_a_store_with_nothing_in_it_loads_nothing(self, tmp_path):
        assert GraphStorage(str(tmp_path / "graph.db")).load_hierarchy() is None

    def test_none_leaves_what_is_already_stored_alone(self, tmp_path):
        """None means "nothing to save", not "clear the table".

        Started from an empty store, this could not tell the two apart.
        """
        storage = _store(tmp_path)
        storage.save_hierarchy(_two_levels())
        storage.save_hierarchy(None)
        assert storage.load_hierarchy().total_communities == 3


# Lowercase on purpose. Without spaCy the NLP extractor falls back to regex,
# which only matches capitalised phrases, so this yields no entities at all.
QUIET_CORPUS = [
    "how do i reset my password from the settings page",
    "the export button is greyed out when a filter is active",
    "billing runs on the first of the month for annual plans",
]


def _unavailable(caplog, level=None):
    return [
        r
        for r in caplog.records
        if "Graph search unavailable" in r.getMessage() and (level is None or r.levelname == level)
    ]


class TestEmptyGraphIsSearchable:
    """A graph with no entities is a graph that finds nothing, not a broken one.

    `_rebuild_searchers` returned early on an empty graph while
    `add_documents()` marked the pipeline built regardless, so a corpus that
    extracted nothing was "built" with no searchers and every query raised
    "Hybrid searcher not initialized". `easy.py` caught it and fell back to
    vector results, so the raise-and-catch happened on every single search
    and nobody saw it.
    """

    def test_zero_entities_returns_an_empty_result_instead_of_raising(self, tmp_path):
        db = Vectrix("quiet", path=str(tmp_path), tier="graph")
        db.add(QUIET_CORPUS)
        assert not db.graph.graph.nodes, "this corpus was supposed to extract nothing"

        result = db.graph.search("password reset", k=2)
        assert result.entities == []
        assert result.communities == []
        assert result.search_strategy == "hybrid"

    def test_zero_entities_still_gets_its_searchers(self, tmp_path):
        db = Vectrix("quiet", path=str(tmp_path), tier="graph")
        db.add(QUIET_CORPUS)
        pipeline = db.graph
        assert pipeline.local_searcher is not None
        assert pipeline.global_searcher is not None
        assert pipeline.hybrid_searcher is not None

    def test_nothing_added_yet_is_also_just_empty(self, tmp_path):
        """Searching before adding is what an empty vector collection does: nothing."""
        db = Vectrix("fresh", path=str(tmp_path), tier="graph")
        assert db.graph.search("anything", k=2).entities == []

    def test_the_fallback_is_never_reached(self, tmp_path, caplog):
        """The public call answers from the graph path, not the except clause."""
        caplog.set_level(logging.DEBUG, logger="vectrixdb")
        db = Vectrix("quiet", path=str(tmp_path), tier="graph")
        db.add(QUIET_CORPUS)
        for _ in range(5):
            assert list(db.search("password reset", mode="graph", limit=2))
        assert not _unavailable(caplog)


class TestDegradationIsAudibleOnce:
    """When graph search genuinely fails, the fallback must be heard, once.

    `_graph_search` catches everything and returns vector results, which is
    right: a broken graph should not fail a search. Logging it at debug is
    what let a restart silently stop using the graph, so it warns now.
    Warning on every query is the opposite mistake. The package installs no
    logging handler, so `logging.lastResort` puts warnings on stderr for
    anyone who never configured logging.

    An empty graph no longer reaches this clause, so these break the search
    deliberately.
    """

    @pytest.fixture
    def broken(self, tmp_path, monkeypatch):
        db = Vectrix("kg", path=str(tmp_path), tier="graph")
        db.add(CORPUS)

        def explode(*args, **kwargs):
            raise RuntimeError("searcher exploded")

        monkeypatch.setattr(type(db.graph), "search", explode)
        return db

    def test_repeated_searches_warn_once(self, broken, caplog):
        caplog.set_level(logging.DEBUG, logger="vectrixdb")
        for _ in range(10):
            assert list(broken.search("radioactivity", mode="graph", limit=2))

        assert len(_unavailable(caplog, "WARNING")) == 1
        assert _unavailable(caplog, "DEBUG"), "the repeats should still be traceable"

    def test_a_working_graph_says_nothing(self, tmp_path, caplog):
        caplog.set_level(logging.DEBUG, logger="vectrixdb")
        db = Vectrix("kg", path=str(tmp_path), tier="graph")
        db.add(CORPUS)
        list(db.search("radioactivity", mode="graph", limit=2))
        assert not _unavailable(caplog)

    def test_adding_documents_lets_it_speak_up_again(self, broken):
        """New documents may have fixed it, so a recurrence is news again."""
        list(broken.search("radioactivity", mode="graph", limit=2))
        assert broken._graph_warnings

        broken.add(["Radium glows faintly in the dark."])
        assert not broken._graph_warnings


class TestPipelineSurvivesRestart:
    """The behaviour all of the above exists to protect."""

    def test_communities_are_still_there_after_reopen(self, tmp_path):
        first = Vectrix("kg", path=str(tmp_path), tier="graph")
        first.add(CORPUS)
        built = first.graph.hierarchy.total_communities
        assert built > 0, "the corpus produced no communities, so this proves nothing"

        reopened = Vectrix("kg", path=str(tmp_path), tier="graph")
        assert reopened.graph.hierarchy is not None, "the hierarchy was lost on reopen"
        assert reopened.graph.hierarchy.total_communities == built

    def test_searchers_are_rebuilt_on_reopen(self, tmp_path):
        Vectrix("kg", path=str(tmp_path), tier="graph").add(CORPUS)
        pipeline = Vectrix("kg", path=str(tmp_path), tier="graph").graph
        assert pipeline.global_searcher is not None
        assert pipeline.hybrid_searcher is not None

    def test_graph_search_works_after_a_restart(self, tmp_path):
        Vectrix("kg", path=str(tmp_path), tier="graph").add(CORPUS)
        reopened = Vectrix("kg", path=str(tmp_path), tier="graph")
        assert list(reopened.search("who worked on radioactivity", mode="graph", limit=3))


class TestNoCommunitiesIsStillSearchable:
    """Entities but no communities, then reopen: the exact failure.

    Hybrid is the default search type and the hierarchy-backed searchers were
    gated on a truthy hierarchy, so a corpus below `min_community_size` came
    back from a restart with no hybrid searcher and raised on the first query.

    The first three assert against the pipeline rather than `Vectrix.search`,
    because `_graph_search` catches everything and falls back to vector results:
    going through the public call passes either way, which is precisely how this
    stayed invisible. The last one covers the public call anyway, to pin that the
    fallback keeps working.
    """

    @pytest.fixture
    def reopened(self, tmp_path):
        Vectrix("tiny", path=str(tmp_path), tier="graph").add(
            ["Marie Curie discovered radium in Paris."]
        )
        return Vectrix("tiny", path=str(tmp_path), tier="graph")

    def test_the_premise_holds(self, reopened):
        """If this corpus ever starts forming communities, the rest proves nothing."""
        assert reopened.graph.graph.nodes
        assert reopened.graph.hierarchy is not None
        assert reopened.graph.hierarchy.total_communities == 0

    def test_the_hierarchy_backed_searchers_exist_anyway(self, reopened):
        assert reopened.graph.hybrid_searcher is not None
        assert reopened.graph.global_searcher is not None

    def test_the_pipeline_answers_instead_of_raising(self, reopened):
        """It used to raise here. A real result object, not merely non-None."""
        result = reopened.graph.search("radium", k=2)
        assert result.search_strategy
        assert result.top_entities == []  # no communities, but the graph has entities
        assert isinstance(result.context, str)

    def test_the_public_search_returns_results(self, reopened):
        assert list(reopened.search("radium", mode="graph", limit=2))
