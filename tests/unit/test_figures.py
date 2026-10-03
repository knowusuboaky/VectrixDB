"""A figure is a block: a caption that is cited, a description that is searched,
and an image that is kept. Every describer here is a fake.
"""

from __future__ import annotations

import json
import struct
import zlib

import numpy as np
import pytest

from vectrixdb.exceptions import ExtractionError
from vectrixdb.extract import HttpDescriber
from vectrixdb.ingest import (
    LoadedDocument,
    chunk,
    describe_figures,
    image_size,
    is_decorative,
    load,
    markdown_document,
)

REPORT = """## 4.2 Regional performance

Revenue grew in every region, as shown in Figure 3. Costs were flat.

![Figure 3: Revenue by region, FY2025](charts/revenue.png)

> **Figure 3.** Bar chart, USD thousands, three bars.
> AMER 2,100. EMEA 1,200. APAC 950.
> Text in image: "FY2025 actuals, unaudited".

Headcount did not change in the period, as Figure 31 in the appendix shows.
"""


def png(width: int, height: int, filler: int = 4000) -> bytes:
    """A PNG header with the size asked for, padded to look like a real file."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    head = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + ihdr
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    )
    return head + bytes(filler)


def embed(texts):
    out = np.zeros((len(texts), 8), dtype=np.float32)
    for i, text in enumerate(texts):
        for word in text.lower().split():
            out[i, hash(word) % 8] += 1.0
        out[i] /= np.linalg.norm(out[i]) or 1.0
    return out


def open_db(tmp_path, **options):
    from vectrixdb import Vectrix

    options.setdefault("embed_fn", embed)
    return Vectrix(
        "reports", path=str(tmp_path / "db"), dimension=8, embedding_cache=False, **options
    )


def rows(db):
    return sorted(db._collection._iter_documents_raw(), key=lambda r: r[2]["_vx_chunk"])


class TestTheBlock:
    def test_the_quote_under_the_image_is_the_description_and_the_block_is_one_chunk(self):
        doc = markdown_document(REPORT, pages=[(0, 4)])
        chunks = chunk(doc, "sentence", size=80, overlap=0)
        (figure,) = [c for c in chunks if c.figure]
        assert (
            figure.figure == "Figure 3: Revenue by region, FY2025"
            and figure.figure_src == "charts/revenue.png"
        )
        assert figure.text.splitlines() == [
            "[Figure: Figure 3: Revenue by region, FY2025]",
            "Figure 3. Bar chart, USD thousands, three bars.",
            "AMER 2,100. EMEA 1,200. APAC 950.",
            'Text in image: "FY2025 actuals, unaudited".',
        ]
        assert figure.page == 4 and figure.heading == "4.2 Regional performance"
        assert all(">" not in c.text and "![" not in c.text for c in chunks)

    def test_a_quote_further_down_is_an_ordinary_quote(self):
        doc = markdown_document(
            "![Chart](c.png)\n\nA paragraph in between.\n\n> Somebody said this.\n"
        )
        (figure,) = [c for c in chunk(doc, "markdown", size=500) if c.figure]
        assert figure.text == "[Figure: Chart]" and doc.figures[0][1]["described"] is False
        assert "Somebody said this." in doc.text

    def test_an_image_with_no_quote_is_still_a_figure(self):
        doc = markdown_document("Before.\n\n![Org chart](org.png)\n\nAfter.")
        assert doc.figures == [
            (
                doc.text.index("[Figure"),
                {"caption": "Org chart", "src": "org.png", "described": False},
            )
        ]


class TestDecorative:
    def test_sizes_are_read_from_the_header(self):
        assert image_size(png(640, 480)) == (640, 480)
        assert image_size(b"GIF89a" + struct.pack("<HH", 32, 16) + bytes(20)) == (32, 16)
        assert image_size(b"not an image at all") is None

    @pytest.mark.parametrize(
        "data, verdict",
        [
            (png(640, 480), False),
            (png(32, 32), True),
            (png(1200, 40), True),
            (png(640, 480, filler=100), True),
            (bytes(5000), False),
        ],
    )
    def test_small_thin_and_tiny_files_are_decoration(self, data, verdict):
        assert is_decorative(data) is verdict


class TestDescribeFigures:
    def _doc(self, text, images):
        doc = markdown_document(text, pages=[(0, 1)])
        doc.images.update(images)
        return doc

    def test_an_undescribed_figure_is_described_once_with_its_context(self):
        seen = []

        def describer(image, context):
            seen.append((image[:4], context))
            return "Bar chart of three regions. AMER is the largest."

        doc = self._doc(
            "# Results\n\nRevenue grew.\n\n![Revenue by region](rev.png)\n\nCosts were flat.",
            {"rev.png": png(640, 480)},
        )
        out = describe_figures(doc, describer, name="q3.pdf")
        assert (
            "[Figure: Revenue by region]\nBar chart of three regions. AMER is the largest."
            in out.text
        )
        assert out.figures[0][1]["described"] is True and out.text[out.figures[0][0] :].startswith(
            "[Figure:"
        )
        ((image, context),) = seen
        assert (
            image == b"\x89PNG"
            and context["caption"] == "Revenue by region"
            and context["name"] == "q3.pdf"
        )
        assert context["page"] == 1 and context["heading"] == "Results"
        assert context["before"].endswith("Revenue grew.") and context["after"].startswith(
            "Costs were flat."
        )
        again = describe_figures(out, lambda *a: pytest.fail("already described"), name="q3.pdf")
        assert again.text == out.text

    def test_a_picture_of_a_table_becomes_rows_and_a_better_caption_is_taken(self):
        doc = self._doc("![scan](t.png)", {"t.png": png(640, 480)})
        out = describe_figures(
            doc,
            lambda image, context: {
                "caption": "Table 2: Fees by tier",
                "description": "A fee schedule.",
                "table": [["Tier", "Fee"], ["Gold", "2%"], ["Silver", "3%"]],
            },
        )
        assert (
            out.text.strip()
            == "[Figure: Table 2: Fees by tier]\nA fee schedule.\nTier: Gold; Fee: 2%\nTier: Silver; Fee: 3%"
        )

    def test_decoration_is_taken_out_of_the_text(self):
        logo = png(40, 40)
        text = "![logo](logo.png)\n\nPage one text.\n\n![Revenue](rev.png)\n\nPage two text."
        out = describe_figures(
            self._doc(text, {"logo.png": logo, "rev.png": png(640, 480)}), lambda i, c: "A chart."
        )
        assert "logo" not in out.text and "logo.png" not in out.images
        assert [i["caption"] for _, i in out.figures] == ["Revenue"]

    def test_the_same_image_on_every_page_is_a_letterhead(self):
        banner = png(640, 480)
        text = "\n\n".join(f"![]({n}.png)\n\nPage {n} text." for n in (1, 2, 3))
        out = describe_figures(
            self._doc(text, {f"{n}.png": banner for n in (1, 2, 3)}),
            lambda i, c: pytest.fail("a letterhead"),
        )
        assert out.figures == [] and "[Figure" not in out.text and "Page 2 text." in out.text

    def test_a_describer_may_decline_or_call_it_decoration(self):
        doc = self._doc(
            "![a](a.png)\n\nBetween.\n\n![b](b.png)",
            {"a.png": png(640, 480), "b.png": png(640, 481)},
        )
        out = describe_figures(
            doc, lambda image, context: None if context["caption"] == "a" else {"decorative": True}
        )
        assert [i["caption"] for _, i in out.figures] == ["a"] and "[Figure: b]" not in out.text

    def test_a_describer_that_fails_stops_the_ingestion(self):
        def broken(image, context):
            raise RuntimeError("quota exceeded")

        with pytest.raises(
            ExtractionError, match="describing the figure a.png in q3.pdf failed: quota exceeded"
        ):
            describe_figures(
                self._doc("![a](a.png)", {"a.png": png(640, 480)}), broken, name="q3.pdf"
            )

    CHART = "Before the chart.\n\n[Figure: p1-chart1.png]\nWords in the picture: 820 540 Canada U.S.\n\nAfter the chart."

    def _chart(self, image):
        from vectrixdb.ingest import _marked_figures

        images = {"p1-chart1.png": image}
        return LoadedDocument(
            text=self.CHART, figures=_marked_figures(self.CHART, images), images=images
        )

    def test_a_charts_printed_words_give_way_to_its_description(self):
        doc = self._chart(png(640, 480))
        assert [info["src"] for _o, info in doc.figures] == ["p1-chart1.png"], (
            "the name is the figure line's, not the words under it"
        )
        seen = []

        def describer(image, context):
            seen.append(context)
            return {
                "description": "Bars for two segments.",
                "table": [["Segment", "Net income"], ["Canada", "820"]],
            }

        out = describe_figures(doc, describer)
        assert (
            out.text
            == "Before the chart.\n\n[Figure: p1-chart1.png]\nBars for two segments.\nSegment: Canada; Net income: 820\n\nAfter the chart."
        )
        assert seen[0]["before"] == "Before the chart." and seen[0]["after"] == "After the chart."

    def test_a_charts_rows_hold_only_what_the_chart_prints(self):
        from vectrixdb.ingest import _marked_figures

        text = (
            "Before the chart.\n\n[Figure: p1-chart1.png]\nWords in the picture: NET INCOME 8,000 7,000 6,000 5,000 4,000 "
            "3,000 2,000 1,000 0 2024 2025 Reported 10.0 14.4\n\nAfter the chart."
        )
        images = {"p1-chart1.png": png(640, 480)}
        doc = LoadedDocument(text=text, figures=_marked_figures(text, images), images=images)
        out = describe_figures(
            doc,
            lambda image, context: {
                "description": "Two bars, 2025 slightly higher.",
                "table": [
                    ["Series", "2024", "2025"],
                    ["Net income", "7,100", "7,000"],
                    ["Reported", "10.0", "14.4"],
                ],
            },
        )
        block = out.text[out.text.index("[Figure") :].split("\n\n")[0]
        assert (
            block
            == "[Figure: p1-chart1.png]\nTwo bars, 2025 slightly higher.\nSeries: Reported; 2024: 10.0; 2025: 14.4"
        ), (
            "7,100 is printed nowhere and 7,000 only on the scale: the bar's row goes, the printed values stay"
        )

    def test_a_chart_nobody_describes_keeps_what_it_printed(self):
        assert (
            describe_figures(self._chart(png(640, 480)), lambda image, context: None).text
            == self.CHART
        )

    def test_a_chart_taken_for_decoration_leaves_its_words_behind(self):
        out = describe_figures(
            self._chart(png(1200, 40)),
            lambda image, context: pytest.fail("decoration is not described"),
        )
        assert (
            out.text
            == "Before the chart.\n\nWords in the picture: 820 540 Canada U.S.\n\nAfter the chart."
        )

    def test_offsets_after_a_rewrite_still_point_at_the_same_words(self):
        text = "![a](a.png)\n\n# Second\n\nThe second page starts here."
        doc = markdown_document(text)
        doc = LoadedDocument(
            text=doc.text,
            headings=doc.headings,
            figures=doc.figures,
            pages=[(0, 1), (doc.text.index("The second"), 2)],
            images={"a.png": png(640, 480)},
        )
        out = describe_figures(doc, lambda i, c: "A long description. " * 10)
        assert out.text[out.pages[1][0] :].startswith("The second page")
        assert out.text[out.headings[0][0] :].startswith("# Second")


class TestThroughVectrix:
    def test_ids_links_context_and_the_kept_image(self, tmp_path):
        folder = tmp_path / "inbox"
        (folder / "charts").mkdir(parents=True)
        (folder / "charts" / "revenue.png").write_bytes(png(640, 480))
        (folder / "q3.md").write_text(REPORT, encoding="utf-8")
        seen = []
        db = open_db(
            tmp_path,
            keep_source=tmp_path / "kept",
            embed_fn=lambda texts: (seen.extend(texts), embed(texts))[1],
        )
        try:
            db.add_document(folder / "q3.md", doc_id="q3.md", chunk="markdown")
            stored = rows(db)
            (figure,) = [r for r in stored if r[2].get("figure")]
            paragraphs = [r for r in stored if not r[2].get("figure")]
            assert (
                figure[2]["figure_id"] == "q3.md#fig1"
                and figure[2]["figure_src"] == "charts/revenue.png"
            )
            assert (
                figure[2]["_vx_citation"]
                == "q3.md#4.2 Regional performance(Figure 3: Revenue by region, FY2025)"
            )
            mentioning = [r for r in paragraphs if "refers_to" in r[2]]
            assert [r[2]["refers_to"] for r in mentioning] == [["q3.md#fig1"]], (
                "Figure 31 is not Figure 3"
            )
            assert figure[2]["referenced_by"] == [mentioning[0][0]]
            sent = [t for t in seen if t.startswith("4.2 Regional performance: [Figure:")]
            assert len(sent) == 1 and sent[0].endswith(
                "Mentioned as: Revenue grew in every region, as shown in Figure 3."
            )
            assert figure[1].startswith("[Figure: Figure 3") and "Mentioned as" not in figure[1]

            from dataclasses import replace

            found = db.search("revenue grew in every region", limit=5)
            hits = replace(found, items=[r for r in found if r.id == mentioning[0][0]])
            assert len(hits) == 1 and not hits.figures()
            with_them = db.with_figures(hits)
            assert [bool(r.metadata.get("figure")) for r in with_them] == [False, True]
            assert with_them.figures(top=1)[0].metadata["figure_id"] == "q3.md#fig1"
            assert db.figure_bytes(with_them.figures()[0]) == png(640, 480)
            assert (tmp_path / "kept" / "q3.md.figures" / "revenue.png").is_file()
            assert db.with_figures(with_them).items == with_them.items, "not added twice"
        finally:
            db.close()

    def test_an_image_outside_the_documents_folder_is_not_followed(self, tmp_path):
        (tmp_path / "secret.png").write_bytes(png(640, 480))
        folder = tmp_path / "inbox"
        folder.mkdir()
        (folder / "a.md").write_text(
            "![x](../secret.png)\n\n![y](https://example.test/y.png)\n", encoding="utf-8"
        )
        assert load(folder / "a.md").images == {}

    def test_a_pdfs_images_are_figures_when_something_can_describe_them(
        self, tmp_path, monkeypatch
    ):
        pypdf = pytest.importorskip("pypdf")

        class Image:
            def __init__(self, name, data):
                self.name, self.data = name, data

        class Page:
            def __init__(self, text, images):
                self._text, self.images = text, images

            def extract_text(self):
                return self._text

        class Reader:
            def __init__(self, path):
                self.pages = [
                    Page(
                        "Revenue grew in every region this year.",
                        [Image("Im1.png", png(640, 480)), Image("logo.png", png(30, 30))],
                    ),
                    Page("Costs were flat across the period.", []),
                ]

        monkeypatch.setattr(pypdf, "PdfReader", Reader)
        report = tmp_path / "q3.pdf"
        report.write_bytes(b"%PDF")
        assert load(report).figures == [] and load(report).images == {}

        db = open_db(
            tmp_path, describe_figures=lambda image, context: "Bar chart of revenue by region."
        )
        try:
            db.add_document(report, doc_id="q3.pdf")
            figures = [r for r in rows(db) if r[2].get("figure")]
            assert len(figures) == 1, "the logo was decoration"
            text, meta = figures[0][1], figures[0][2]
            assert text == "[Figure: p1-fig1.png]\nBar chart of revenue by region."
            assert meta["figure_id"] == "q3.pdf#p1-fig1" and meta["page"] == 1
        finally:
            db.close()

    def test_describe_figures_is_checked(self, tmp_path):
        with pytest.raises(TypeError, match="describe_figures is callable"):
            open_db(tmp_path, describe_figures="gpt")


class TestHttpDescriber:
    def test_raw_body_and_the_context_in_a_header(self):
        sent = {}

        def transport(method, url, headers, body, timeout):
            sent.update(url=url, headers=headers, body=body)
            return 200, {"Content-Type": "text/plain"}, b" A bar chart. "

        describe = HttpDescriber(
            "http://127.0.0.1:9001/transcribe/image", body="raw", transport=transport
        )
        assert (
            describe(b"PNGDATA", {"caption": "Revenue", "name": "q3.pdf", "page": 4, "before": ""})
            == "A bar chart."
        )
        assert sent["body"] == b"PNGDATA" and sent["headers"]["X-Filename"] == "q3.pdf.png"
        from urllib.parse import unquote

        assert json.loads(unquote(sent["headers"]["X-Context"])) == {
            "caption": "Revenue",
            "name": "q3.pdf",
            "page": 4,
        }

    def test_multipart_carries_the_context_as_a_field_and_json_comes_back_whole(self):
        sent = {}

        def transport(method, url, headers, body, timeout):
            sent.update(headers=headers, body=body)
            return (
                200,
                {"Content-Type": "application/json"},
                json.dumps({"description": "A chart.", "decorative": False}).encode(),
            )

        reply = HttpDescriber("https://ai.example.test/describe", transport=transport)(
            b"PNGDATA", {"caption": "Revenue"}
        )
        assert reply == {"description": "A chart.", "decorative": False}
        assert (
            b'name="context"' in sent["body"]
            and b'{"caption": "Revenue"}' in sent["body"]
            and b"PNGDATA" in sent["body"]
        )
        assert b"\r\n\r\nPNGDATA\r\n--" in sent["body"]

    def test_a_refusal_is_an_extraction_error(self):
        describe = HttpDescriber(
            "https://ai.example.test/describe", transport=lambda *a: (429, {}, b"slow down")
        )
        with pytest.raises(ExtractionError, match="answered 429: slow down"):
            describe(b"x", {})
        with pytest.raises(ValueError, match="http or https"):
            HttpDescriber("ftp://x/y")
