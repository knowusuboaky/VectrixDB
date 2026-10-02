"""Entity resolution: the same thing under different names must become one node.

Extraction yields "Marie Curie", then "Curie", then "M. Curie". Without
resolution that is three nodes, and every edge, community and centrality score
is computed over a graph that believes in three people.

The failure to avoid is the opposite one. Merging "Marie Curie" with "Pierre
Curie" fuses two real people into a single node, and nothing downstream can tell
that happened. Every test below that asserts a *non*-merge is guarding that.
"""

import pytest

from vectrixdb.core.graphrag.extractor.base import Entity
from vectrixdb.core.graphrag.graph import KnowledgeGraph
from vectrixdb.core.graphrag.graph.resolution import EntityResolver, _jaro_winkler


@pytest.fixture
def resolver() -> EntityResolver:
    return EntityResolver()


class TestShouldMerge:
    """Cases where two names denote one entity."""

    @pytest.mark.parametrize(
        "incoming,established,reason",
        [
            ("marie curie", "Marie Curie", "exact"),
            ("Curie", "Marie Curie", "token subset"),
            ("M. Curie", "Marie Curie", "initials"),
            ("Marie Curie", "M. Curie", "initials"),
            ("Acme Corp", "Acme Corporation", "exact"),
            ("OpenAI", "OpenAI Inc", "exact"),
        ],
    )
    def test_merges(self, resolver, incoming, established, reason):
        match = resolver.compare(incoming, established)
        assert match.matched, f"{incoming!r} should resolve to {established!r}"
        assert match.reason == reason

    def test_legal_suffixes_are_not_part_of_the_name(self, resolver):
        """A company is the same company with or without its legal form."""
        assert resolver.normalize("Acme Holdings Ltd") == "acme"


class TestShouldNotMerge:
    """Cases where merging would fuse two distinct entities."""

    @pytest.mark.parametrize(
        "a,b",
        [
            ("Marie Curie", "Pierre Curie"),  # a shared surname is not identity
            ("Phase 1", "Phase 2"),
            ("GPT-4", "GPT-5"),
            ("Paris", "Pierre Curie"),
            ("Paris", "Paris Agreement"),  # shares the first token, not the head
        ],
    )
    def test_stays_separate(self, a, b):
        resolver = EntityResolver()
        assert not resolver.compare(a, b).matched, f"{a!r} and {b!r} were wrongly merged"
        assert not resolver.compare(b, a).matched, f"{b!r} and {a!r} were wrongly merged"

    def test_differing_numbers_block_a_merge_outright(self, resolver):
        """Version-like names are near-identical as strings and never the same."""
        assert resolver.compare("Phase 1", "Phase 2").reason == "numeric tokens differ"

    def test_low_information_labels_are_refused(self, resolver):
        assert not resolver.compare("AB", "AC").matched

    def test_threshold_of_one_disables_fuzzy_matching(self):
        strict = EntityResolver(threshold=1.0)
        assert strict.compare("marie curie", "Marie Curie").matched  # exact still works
        assert not strict.compare("Curie", "Marie Curie").matched

    def test_subset_can_be_turned_off(self):
        no_subset = EntityResolver(allow_subset=False)
        assert not no_subset.compare("Curie", "Marie Curie").matched


class TestDirectionality:
    """Subset resolution folds the vaguer name into the more specific one."""

    def test_short_incoming_folds_into_established_long(self, resolver):
        assert resolver.compare("Curie", "Marie Curie").matched

    def test_long_incoming_does_not_fold_into_established_short(self, resolver):
        """The reverse would assert identity from a surname alone.

        If only "Curie" is known and "Pierre Curie" arrives, merging claims they
        are the same person. A second full name is evidence that the bare
        surname was ambiguous, not evidence of identity.
        """
        assert not resolver.compare("Pierre Curie", "Curie").matched


class TestJaroWinkler:
    """The bundled metric, so behaviour does not depend on rapidfuzz."""

    def test_identical(self):
        assert _jaro_winkler("radium", "radium") == 1.0

    def test_unrelated_scores_below_the_merge_threshold(self):
        """What matters is not an absolute figure but staying clear of merging."""
        from vectrixdb.core.graphrag.graph.resolution import _FUZZY_THRESHOLD

        assert _jaro_winkler("paris", "curie") < _FUZZY_THRESHOLD

    def test_shared_prefix_scores_above_plain_ratio(self):
        """A one-character tail difference stays close, which difflib does not.

        SequenceMatcher rates this pair 0.67, low enough to miss real typos.
        """
        assert _jaro_winkler("radium", "radion") > 0.85

    def test_empty_inputs(self):
        assert _jaro_winkler("", "") == 1.0
        assert _jaro_winkler("abc", "") == 0.0


class TestGraphMerging:
    """The resolver's effect on a real graph."""

    @staticmethod
    def _graph_of(names) -> KnowledgeGraph:
        graph = KnowledgeGraph()
        for name in names:
            graph.add_entity(
                Entity(id=f"e_{name}".replace(" ", "_").replace(".", ""), name=name, type="PERSON")
            )
        return graph

    def test_variants_collapse_to_one_node(self):
        graph = self._graph_of(["Marie Curie", "Curie", "M. Curie", "marie curie"])
        assert len(graph.nodes) == 1

    def test_two_people_sharing_a_surname_stay_apart(self):
        """The regression that matters: this used to produce a single node."""
        graph = self._graph_of(["Marie Curie", "Curie", "M. Curie", "Pierre Curie", "Paris"])
        names = {e.name for e in graph.nodes.values()}
        assert len(graph.nodes) == 3, f"expected 3 entities, got {names}"
        assert "Pierre Curie" in names

    @pytest.mark.parametrize(
        "order",
        [
            ["Marie Curie", "Curie", "M. Curie", "Pierre Curie", "Paris"],
            ["Paris", "Pierre Curie", "M. Curie", "Curie", "Marie Curie"],
            ["Pierre Curie", "Marie Curie", "Paris", "Curie", "M. Curie"],
        ],
    )
    def test_no_ordering_fuses_the_two_people(self, order):
        """Which name ends up canonical varies with arrival order; identity must not."""
        graph = self._graph_of(order)
        assert len(graph.nodes) >= 3, f"entities were fused for order {order}"

    def test_merged_variants_are_kept_as_aliases(self):
        graph = self._graph_of(["Marie Curie", "Curie"])
        entity = next(iter(graph.nodes.values()))
        assert entity.aliases, "the merged name should survive as an alias"

    def test_threshold_reaches_the_graph(self):
        """The config value was declared but never read; exact-only must work."""
        graph = KnowledgeGraph(similarity_threshold=1.0)
        for name in ("Marie Curie", "Curie"):
            graph.add_entity(Entity(id=f"e_{name}", name=name, type="PERSON"))
        assert len(graph.nodes) == 2


class TestConfidence:
    """Every relationship must say how it was arrived at.

    The regex fallback links two names because they appeared near each other.
    The LLM and REBEL paths link them because the text said so. Both used to
    produce an identical RELATED_TO edge, so retrieval weighted a guess exactly
    like a citation.
    """

    def test_regex_cooccurrence_is_labelled_ambiguous(self):
        from vectrixdb.core.graphrag.extractor.base import Confidence
        from vectrixdb.core.graphrag.extractor.nlp_extractor import NLPExtractor

        result = NLPExtractor().extract_single(
            "Marie Curie discovered radium with Pierre Curie in Paris."
        )
        assert result.relationships, "expected co-occurrence edges"
        assert all(r.confidence == Confidence.AMBIGUOUS for r in result.relationships)

    def test_default_is_inferred_not_extracted(self):
        """An extractor that says nothing has not earned the strongest label."""
        from vectrixdb.core.graphrag.extractor.base import Confidence, Relationship

        assert Relationship(id="r", source_id="a", target_id="b", type="X").confidence == (
            Confidence.INFERRED
        )

    def test_merging_keeps_the_better_supported_claim(self):
        """Two sightings, one stated and one guessed, is still a stated link."""
        from vectrixdb.core.graphrag.extractor.base import Confidence, Relationship

        guess = Relationship.create(
            "a", "b", "X", confidence=Confidence.AMBIGUOUS, source_unit_id="u1"
        )
        stated = Relationship.create(
            "a", "b", "X", confidence=Confidence.EXTRACTED, source_unit_id="u2"
        )
        assert guess.merge_with(stated).confidence == Confidence.EXTRACTED
        assert stated.merge_with(guess).confidence == Confidence.EXTRACTED


class TestMergingKeepsEdges:
    """Resolving two names into one entity must not discard that entity's edges.

    The pipeline discarded the id `add_entity` returns on merge, then added
    relationships using the extractor's pre-merge ids. `add_relationship` found
    the losing id absent from the graph and returned it unchanged, so the edge
    vanished while the caller saw a plausible id back.

    Entity resolution made this sharply worse, because it merges far more
    readily than the exact matching it replaced: every extra merge orphaned that
    entity's relationships. A fix to the node problem had quietly created an
    edge problem.
    """

    @staticmethod
    def _two_forms_and_a_place():
        from vectrixdb.core.graphrag.extractor.base import Entity, Relationship

        full = Entity(id="e_full", name="Marie Curie", type="PERSON")
        short = Entity(id="e_short", name="Curie", type="PERSON")
        place = Entity(id="e_place", name="Paris", type="PLACE")
        edges = [
            Relationship.create(full.id, place.id, "LOCATED_IN", source_unit_id="u1"),
            # This one references the id that loses the merge.
            Relationship.create(short.id, place.id, "LOCATED_IN", source_unit_id="u2"),
        ]
        return [full, short, place], edges

    def test_an_edge_on_the_merged_away_id_is_dropped_without_remapping(self):
        """Pins the underlying behaviour, so the pipeline's remap is load-bearing."""
        graph = KnowledgeGraph()
        entities, edges = self._two_forms_and_a_place()
        for e in entities:
            graph.add_entity(e)
        for r in edges:
            graph.add_relationship(r)
        assert len(graph.edges) == 1, "expected the un-remapped edge to be lost"

    def test_add_relationship_reports_a_drop_instead_of_faking_success(self):
        from vectrixdb.core.graphrag.extractor.base import Relationship

        graph = KnowledgeGraph()
        orphan = Relationship.create("nonexistent_a", "nonexistent_b", "X", source_unit_id="u")
        assert graph.add_relationship(orphan) is None

    def test_a_self_loop_from_merging_is_refused(self):
        """Two surface forms of one entity joined by an edge is not a real fact."""
        from vectrixdb.core.graphrag.extractor.base import Entity, Relationship

        graph = KnowledgeGraph()
        graph.add_entity(Entity(id="e1", name="Marie Curie", type="PERSON"))
        assert (
            graph.add_relationship(
                Relationship.create("e1", "e1", "RELATED_TO", source_unit_id="u")
            )
            is None
        )

    def test_the_pipeline_remaps_so_the_edge_survives(self, tmp_path):
        """The end-to-end guarantee: merging names keeps their relationships."""
        from vectrixdb import Vectrix

        db = Vectrix("edges", path=str(tmp_path), tier="graph")
        db.add(
            [
                "Marie Curie discovered radium in Paris.",
                "Curie worked with Pierre Curie on radioactivity.",
            ]
        )
        graph = db.graph.graph
        assert graph.edges, "every edge was lost to entity merging"
        for edge in graph.edges.values():
            assert edge.source_id in graph.nodes
            assert edge.target_id in graph.nodes
            assert edge.source_id != edge.target_id

    def test_merge_extraction_preserves_confidence(self):
        """Its Relationship rebuild omitted confidence, resetting EXTRACTED edges."""
        from vectrixdb.core.graphrag.extractor.base import (
            Confidence,
            Entity,
            ExtractionResult,
            Relationship,
        )

        graph = KnowledgeGraph()
        result = ExtractionResult(
            entities=[
                Entity(id="a", name="Alpha Corp", type="ORG"),
                Entity(id="b", name="Beta Institute", type="ORG"),
            ],
            relationships=[
                Relationship.create(
                    "a",
                    "b",
                    "PARTNERS_WITH",
                    confidence=Confidence.EXTRACTED,
                    source_unit_id="u",
                )
            ],
        )
        graph.merge_extraction(result)
        assert graph.edges
        assert all(e.confidence == Confidence.EXTRACTED for e in graph.edges.values())
