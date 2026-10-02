"""Community detection and summarisation run only when the graph changed.

They are the expensive tail of every add_documents(): under an LLM extractor,
summarising is one model call per community, and it was paid again on every
message even when nothing new was extracted.
"""

import pytest

from vectrixdb.core.graphrag import ExtractorType, GraphRAGConfig, create_pipeline
from vectrixdb.core.graphrag import pipeline as pipeline_module

DOCS = [
    "Marie Curie discovered radium in Paris.",
    "Pierre Curie worked with Marie Curie on radioactivity.",
]


@pytest.fixture
def pipeline(tmp_path):
    config = GraphRAGConfig(enabled=True, extractor=ExtractorType.NLP)
    return create_pipeline(config=config, path=tmp_path / "graph")


class TestReuse:
    def test_first_build_detects(self, pipeline):
        stats = pipeline.add_documents(DOCS)
        assert not stats.hierarchy_reused
        assert pipeline.hierarchy is not None

    def test_an_unchanged_graph_skips_detection(self, pipeline, monkeypatch):
        pipeline.add_documents(DOCS)
        before = pipeline.hierarchy

        def must_not_run(*args, **kwargs):
            raise AssertionError("detect_communities ran on an unchanged graph")

        monkeypatch.setattr(pipeline_module, "detect_communities", must_not_run)
        stats = pipeline.add_documents(DOCS)

        assert stats.hierarchy_reused
        assert pipeline.hierarchy is before, "the previous hierarchy is kept, not rebuilt"
        assert stats.communities_detected == before.total_communities

    def test_a_new_entity_runs_detection_again(self, pipeline, monkeypatch):
        pipeline.add_documents(DOCS)
        calls = []
        real = pipeline_module.detect_communities

        def counting(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        monkeypatch.setattr(pipeline_module, "detect_communities", counting)
        # A structural change now runs detection through the incremental path,
        # which imports the detector by its own name.
        import vectrixdb.core.graphrag.graph.incremental as incremental_module

        monkeypatch.setattr(incremental_module, "detect_communities", counting)
        stats = pipeline.add_documents(["The Nobel Prize was awarded in Stockholm."])
        assert not stats.hierarchy_reused
        assert calls

    def test_progress_callback_still_reports_the_skipped_stages(self, pipeline):
        pipeline.add_documents(DOCS)
        stages = []
        pipeline.add_documents(DOCS, on_progress=lambda cur, tot, stage: stages.append(stage))
        assert "detecting_communities" in stages and "summarizing" in stages


class TestExtractorConstruction:
    """LLM and hybrid extractors were unconstructable through the pipeline.

    _init_extractor passed entity_types= and relationship_types= to two
    constructors that do not accept them, so ExtractorType.LLM and HYBRID
    raised TypeError before any document was seen. mypy found it.
    """

    @pytest.mark.parametrize(
        "extractor_type,class_name",
        [(ExtractorType.LLM, "LLMExtractor"), (ExtractorType.HYBRID, "HybridExtractor")],
    )
    def test_only_config_is_passed(self, tmp_path, monkeypatch, extractor_type, class_name):
        received = {}

        class Fake:
            def __init__(self, **kwargs):
                received.update(kwargs)

        monkeypatch.setattr(pipeline_module, class_name, Fake)
        config = GraphRAGConfig(enabled=True, extractor=extractor_type)
        pipeline_module.GraphRAGPipeline(config=config)
        assert set(received) == {"config"}
        assert received["config"] is config
