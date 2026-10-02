"""A sentence a printer split across pages reaches the chunker whole, and a
figure is one chunk with its caption, whatever the strategy.

The page join is the rule the Azure search sample settled on after enough
PDFs: a break is a paragraph unless the sentence plainly runs on. It is a
pure function here, so it is tested without a PDF parser.
"""

from __future__ import annotations

import pytest

from vectrixdb.citations import citation_for
from vectrixdb.ingest import (
    FIGURE_LINE,
    LoadedDocument,
    _load_html,
    chunk,
    figure_line,
    join_pages,
    normalise_figures,
)


class TestJoinPages:
    def test_a_running_sentence_is_joined_with_a_space(self):
        text, pages = join_pages(["The covenant was tested at the end of the", "quarter and it held. Next topic."])
        assert "end of the quarter and it held." in text
        assert pages == [(0, 1), (len("The covenant was tested at the end of the") + 1, 2)]
        assert text[pages[1][0]:].startswith("quarter")

    def test_a_finished_sentence_keeps_the_paragraph_break(self):
        text, pages = join_pages(["First page about basalt.", "Second page about sourdough."])
        assert text == "First page about basalt.\n\nSecond page about sourdough."
        assert pages == [(0, 1), (len("First page about basalt.") + 2, 2)]

    def test_a_capital_after_no_punctuation_is_a_new_paragraph(self):
        text, _ = join_pages(["A heading with no full stop", "Then the body starts."])
        assert "\n\n" in text

    def test_a_page_number_page_is_dropped_and_still_counted(self):
        text, pages = join_pages(["Body one.", "12", "Page 3 of 3", "Body four."])
        assert text == "Body one.\n\nBody four."
        assert [p for _, p in pages] == [1, 2, 3, 4]
        assert pages[3][0] == len("Body one.\n\n")

    def test_offsets_index_the_text(self):
        parts = ["alpha runs into the", "beta line. Gamma.", "Delta."]
        text, pages = join_pages(parts)
        for (start, number), part in zip(pages, parts):
            assert text[start : start + len(part)] == part, number


class TestChunksAcrossPages:
    def test_a_sentence_across_pages_lands_in_one_chunk_with_both_pages(self):
        text, pages = join_pages(
            ["Basalt forms when lava cools. The covenant was tested at the end of the", "quarter and it held. Granite cools slowly."]
        )
        doc = LoadedDocument(text=text, pages=pages)
        chunks = chunk(doc, "sentence", size=70, overlap=0)
        crossing = next(c for c in chunks if "end of the quarter" in c.text)
        assert crossing.page == 1 and crossing.page_end == 2
        assert crossing.metadata()["page_end"] == 2
        assert "covenant was tested at the end of the quarter and it held." in crossing.text

    def test_page_end_is_absent_when_the_chunk_stays_on_one_page(self):
        doc = LoadedDocument(text="One page only. Two sentences.", pages=[(0, 1)])
        (c,) = chunk(doc, "sentence", size=100, overlap=0)
        assert c.page == 1 and c.page_end == 1 and "page_end" not in c.metadata()


class TestFigures:
    def test_markdown_images_become_figure_lines(self):
        text = normalise_figures("Revenue grew.\n![Revenue by region](q3.png)\nCosts were flat. ![](chart-2.png)")
        assert "[Figure: Revenue by region]" in text
        assert "[Figure: chart-2.png]" in text
        assert FIGURE_LINE.match("[Figure: Revenue by region]").group(1) == "Revenue by region"

    def test_figure_line_strips_brackets_and_names_untitled(self):
        assert figure_line("[Q3] chart") == "[Figure: Q3 chart]"
        assert figure_line("", None) == "[Figure: untitled]"

    @pytest.mark.parametrize("strategy", ["recursive", "sentence", "markdown"])
    def test_every_strategy_keeps_a_figure_as_its_own_chunk(self, strategy):
        text = normalise_figures(
            "# Results\n\nRevenue grew in every region this quarter, which the chart shows clearly.\n\n"
            "![Revenue by region](q3.png)\n\nCosts were flat across the same period.\n"
        )
        chunks = chunk(text, strategy, size=60, overlap=0)
        figs = [c for c in chunks if c.figure]
        assert len(figs) == 1
        assert figs[0].text.strip() == "[Figure: Revenue by region]"
        assert figs[0].metadata()["figure"] == "Revenue by region"
        assert all("[Figure" not in c.text for c in chunks if not c.figure)

    def test_html_img_and_figure_with_caption(self):
        doc = _load_html(
            "<h1>Results</h1><p>Revenue grew.</p><img src='q3.png' alt='Revenue by region'>"
            "<figure><img src='c.png' alt='ignored when captioned'><figcaption>Costs by <b>quarter</b></figcaption></figure>"
            "<p>Costs were flat.</p><figure><img src='d.png'></figure>"
        )
        assert "[Figure: Revenue by region]" in doc.text
        assert "[Figure: Costs by quarter]" in doc.text
        assert "[Figure: d.png]" in doc.text
        assert "ignored when captioned" not in doc.text
        assert [h[1] for h in doc.headings] == ["Results"]

    def test_a_figure_is_cited_by_page_and_caption(self):
        assert citation_for("report.pdf", "d", page=3, figure="Revenue by region") == "report.pdf#page=3(Revenue by region)"
        assert citation_for("guide", "d", heading="Results", figure="(a) [b]") == "guide#Results(a b)"
        assert citation_for("guide", "d", figure="") == "guide"


class TestThroughVectrix:
    def test_a_figure_chunk_is_searchable_by_its_caption(self, tmp_path):
        from vectrixdb import Vectrix

        db = Vectrix("figs", path=str(tmp_path), mode="dense")
        try:
            md = tmp_path / "q3.md"
            md.write_text(
                "# Results\n\nRevenue grew in every region.\n\n![Revenue by region](q3.png)\n\nCosts were flat.\n",
                encoding="utf-8",
            )
            db.add_document(md, chunk="markdown")
            hits = db.search("chart of revenue by region", limit=5)
            fig = next(h for h in hits if h.metadata.get("figure"))
            assert fig.metadata["figure"] == "Revenue by region"
            assert fig.citation == "q3.md#Results(Revenue by region)"
        finally:
            db.close()
