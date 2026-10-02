"""A PDF read by Document Intelligence's layout model, tables headed the way reports head them, and the figure list kept.

The layout model gives a PDF's tables as tables, the headings it sees, the
number printed on each page and its running headers and footers marked as
such, and a picture of every figure, a chart drawn in the PDF included. It
is read so that the running lines go, the printed numbers stay, a table is
rows under its joined headings and a figure is a figure line a describer can
say more about. The figure list, where each figure is and who described it,
now reaches the kept Markdown whichever way the document came. Every service
here is a fake; nothing is called.
"""

from __future__ import annotations

import io
import logging

import pytest

from vectrixdb.extract.engines import AzureDocumentIntelligence
from vectrixdb.ingest import LoadedDocument, describe_figures, load, markdown_document, normalise_tables

CONTENT = """<!-- PageHeader="ANNUAL REPORT 2025" -->

# Financial highlights

Revenue grew in every region.

<table>
<tr><th rowspan="2">Region</th><th colspan="2">Revenue</th></tr>
<tr><th>2025</th><th>2024</th></tr>
<tr><td>EMEA</td><td>1,200</td><td>1,100</td></tr>
<tr><td>APAC</td><td>950</td><td>900</td></tr>
</table>

<!-- PageFooter="Unaudited" -->
<!-- PageNumber="Page 12" -->
<!-- PageBreak -->

<!-- PageHeader="ANNUAL REPORT 2025" -->

<figure>
<figcaption>Figure 3: Revenue by segment</figcaption>

37%
Canadian Personal &amp; Commercial Banking
26%

</figure>

Costs were flat.

<!-- PageNumber="13" -->
"""
SECOND_PAGE = CONTENT.index("<!-- PageHeader", 10)
CROP = b"\x89PNG" + bytes(range(256)) * 30


class Poller:
    def __init__(self, result):
        self._result = result
        self.details = {"operation_id": "op-7"}

    def result(self):
        return self._result


class Client:
    """Document Intelligence as the SDK shows it: an analysis to wait for, and each figure's picture after it."""

    def __init__(self, content=CONTENT, pages=((0, 1), (SECOND_PAGE, 2)), figures=None, crops=None, old_sdk=False):
        self.result = {
            "content": content,
            "model_id": "prebuilt-layout",
            "pages": [{"page_number": n, "spans": [{"offset": o, "length": 1}]} for o, n in pages],
            "figures": figures if figures is not None else [{"id": "2.1", "spans": [{"offset": content.index("<figure>")}]}] if "<figure>" in content else [],
        }
        self.crops = crops if crops is not None else {"2.1": CROP}
        self.old_sdk = old_sdk
        self.asked = []
        self.pictures = []

    def begin_analyze_document(self, model, body, **options):
        if self.old_sdk and "output" in options:
            raise TypeError("begin_analyze_document() got an unexpected keyword argument 'output'")
        self.asked.append((model, options))
        return Poller(self.result)

    def get_analyze_result_figure(self, model_id, result_id, figure_id):
        self.pictures.append((model_id, result_id, figure_id))
        return iter([self.crops[figure_id]])


def read(client=None, crops=False):
    return AzureDocumentIntelligence(client or Client(), crops=crops)(b"%PDF", "report.pdf")


# ================================================================ the layout model's Markdown ===


class TestWhatTheLayoutModelGives:
    def test_the_page_furniture_goes_and_the_printed_numbers_stay(self):
        doc = read()
        for furniture in ("ANNUAL REPORT 2025", "Unaudited", "PageNumber", "PageBreak", "<!--"):
            assert furniture not in doc.text, furniture
        assert "\n\n\n" not in doc.text, "and the blank lines it left are one"
        assert doc.page_labels == {1: "12", 2: "13"}, "Page 12 is printed as 12"
        assert doc.page_label(doc.page_at(doc.text.index("Costs were flat"))) == "13"
        assert doc.page_at(doc.text.index("Region: APAC")) == 1 and doc.page_at(doc.text.index("[Figure")) == 2

    def test_a_table_under_two_rows_of_headings_is_rows_under_their_joined_names(self):
        assert (
            "Region: EMEA; Revenue 2025: 1,200; Revenue 2024: 1,100\nRegion: APAC; Revenue 2025: 950; Revenue 2024: 900"
            in read().text
        )

    def test_a_figure_is_a_figure_line_with_the_words_read_in_it(self):
        doc = read()
        assert "[Figure: Figure 3: Revenue by segment]\nThe words in it read: 37%; Canadian Personal & Commercial Banking; 26%." in doc.text
        (at, info), = doc.figures
        assert doc.text[at:].startswith("[Figure: Figure 3")
        assert info == {"caption": "Figure 3: Revenue by segment", "src": "figure-2.1.png", "described": False}
        assert doc.headings == [(0, "Financial highlights", 1)]
        assert doc.text.startswith("# Financial highlights\n\nRevenue grew") and doc.text.endswith("Costs were flat.")

    def test_with_crops_each_figure_is_cut_out_for_a_describer_to_say_more(self):
        client = Client()
        doc = read(client, crops=True)
        assert client.asked[0][1]["output"] == ["figures"]
        assert client.pictures == [("prebuilt-layout", "op-7", "2.1")] and doc.images == {"figure-2.1.png": CROP}
        seen = []
        described = describe_figures(
            doc, lambda data, context: seen.append((data, context["caption"])) or {"description": "A donut chart: 37% and 26%.", "by": "gpt-4o"},
            skip_decorative=False,
        )
        assert seen == [(CROP, "Figure 3: Revenue by segment")]
        assert "[Figure: Figure 3: Revenue by segment]\nA donut chart: 37% and 26%." in described.text
        assert "The words in it read" not in described.text, "the describer's words replace the model's"

    def test_nothing_to_describe_it_with_the_words_stay(self):
        doc = read(crops=True)
        assert "The words in it read: 37%" in describe_figures(doc, lambda data, context: None).text

    def test_an_sdk_that_cannot_be_asked_for_figures_still_reads_the_document(self):
        client = Client(old_sdk=True)
        doc = read(client, crops=True)
        assert "output" not in client.asked[0][1] and client.pictures == [] and doc.images == {}
        assert "The words in it read: 37%" in doc.text

    def test_figures_are_matched_by_where_they_are_not_by_their_order(self):
        content = "Intro.\n\n<figure>\n\nFirst chart\n\n</figure>\n\nMiddle.\n\n<figure>\n\nSecond chart\n\n</figure>\n"
        first, second = content.index("<figure>"), content.index("<figure>", 20)
        client = Client(
            content=content, pages=((0, 1),),
            figures=[{"id": "1.2", "spans": [{"offset": second}]}, {"id": "1.1", "spans": [{"offset": first}]}],
            crops={"1.1": b"first", "1.2": b"second"},
        )
        doc = read(client, crops=True)
        by_src = {info["src"]: doc.text[at:].split("\n", 2)[1] for at, info in doc.figures}
        assert by_src == {"figure-1.1.png": "The words in it read: First chart.", "figure-1.2.png": "The words in it read: Second chart."}
        assert doc.images == {"figure-1.1.png": b"first", "figure-1.2.png": b"second"}

    def test_a_reply_with_none_of_it_reads_as_before(self):
        doc = read(Client(content="Plain words on a page.\n\nMore words.", pages=((0, 1),)))
        assert doc.text == "Plain words on a page.\n\nMore words."
        assert doc.page_labels == {} and doc.figures == [] and doc.metadata == {"ocr": True, "pages": 1}


# ================================================================ tables anywhere ===


class TestAReportsTableHeadings:
    TABLE = (
        "<table><tr><th rowspan=\"2\">Region</th><th colspan=\"2\">Revenue</th><th rowspan=\"2\">Note</th></tr>"
        "<tr><th>2025</th><th>2024</th></tr>"
        "<tr><td rowspan=\"2\">Canada</td><td>1,200</td><td>1,100</td><td>Audited</td></tr>"
        "<tr><td>1,250</td><td>1,150</td><td colspan=\"1\">Restated</td></tr></table>"
    )

    def test_headings_over_headings_are_joined_and_a_merged_value_is_carried_down(self):
        assert normalise_tables(self.TABLE) == (
            "Region: Canada; Revenue 2025: 1,200; Revenue 2024: 1,100; Note: Audited\n"
            "Region: Canada; Revenue 2025: 1,250; Revenue 2024: 1,150; Note: Restated"
        )

    def test_a_value_across_columns_is_written_once(self):
        table = "<table><tr><th>Region</th><th>Q1</th><th>Q2</th></tr><tr><td>EMEA</td><td colspan=\"2\">not reported</td></tr></table>"
        assert normalise_tables(table) == "Region: EMEA; Q1: not reported"

    def test_a_web_page_reads_its_tables_the_same_way(self, tmp_path):
        (tmp_path / "page.html").write_text(f"<h1>Results</h1>{self.TABLE}", encoding="utf-8")
        assert "Region: Canada; Revenue 2025: 1,250; Revenue 2024: 1,150; Note: Restated" in load(tmp_path / "page.html").text


# ================================================================ the extraction app ===


class Reader:
    label = "rapidocr"

    def __call__(self, data, name):
        return LoadedDocument(text="words")


def blank_pdf(title):
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": title})
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class TestTheExtractionAppReadsPdfsWithTheLayoutModelWhenAsked:
    VISION = {"AZURE_VISION_ENDPOINT": "https://v.cognitiveservices.azure.com", "AZURE_VISION_KEY": "k"}

    def service(self, env, image=None):
        from vectrixdb.api.extraction import ExtractionService

        return ExtractionService.from_environment(env, image=image or AzureDocumentIntelligence(Client(), model="prebuilt-read"), jobs=None)

    def test_it_is_off_unless_asked_for(self):
        assert self.service({}).pdf_layout is None

    def test_asked_for_it_the_layout_model_reads_every_pdf(self):
        layout = self.service({"VECTRIXDB_EXTRACT_PDF": "layout"}).pdf_layout
        assert (layout.model, layout.crops) == ("prebuilt-layout", False), "no describer, so nothing is cut out"
        assert self.service({"VECTRIXDB_EXTRACT_PDF": "layout", **self.VISION}).pdf_layout.crops is True

    def test_asked_for_it_without_document_intelligence_it_says_so_and_reads_the_text(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert self.service({"VECTRIXDB_EXTRACT_PDF": "layout"}, image=Reader()).pdf_layout is None
        assert "VECTRIXDB_EXTRACT_PDF=layout needs AZURE_DOCINTEL_ENDPOINT" in caplog.text

    def test_a_pdf_comes_back_whole_its_title_its_numbers_its_chart_described(self, monkeypatch):
        from fastapi.testclient import TestClient

        from vectrixdb.api.extraction import ExtractionService, create_extraction_app
        from vectrixdb.extract import coerce

        monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
        service = ExtractionService(
            pdf_layout=AzureDocumentIntelligence(Client(), crops=True),
            describer=lambda data, context: {"description": "A donut chart of revenue by segment: 37% and 26%.", "by": "gpt-4o"},
        )
        app = TestClient(create_extraction_app(service, allow_open=True))
        reply = app.post("/extract/pdf", content=blank_pdf("2025 Annual Report"), headers={"X-Filename": "report.pdf", "Accept": "application/json"}).json()
        assert reply["metadata"]["title"] == "2025 Annual Report"
        assert reply["page_labels"] == {"1": "12", "2": "13"}
        doc = coerce(reply, "report.pdf")
        assert "A donut chart of revenue by segment" in doc.text and "ANNUAL REPORT 2025" not in doc.text
        (at, info), = doc.figures
        assert doc.text[at:].startswith("[Figure: Figure 3") and info["described_by"] == "gpt-4o"


# ================================================================ the figure list, end to end ===


class TestTheFigureListIsKept:
    def test_it_survives_the_reply(self):
        from vectrixdb.api.extraction import _as_json
        from vectrixdb.extract import coerce

        doc = read()
        again = coerce(_as_json(doc), "report.pdf")
        assert again.figures == doc.figures and again.text == doc.text

    def test_a_figure_moves_with_the_text_when_a_table_before_it_is_rewritten(self):
        text = "| Region | Revenue |\n|---|---|\n| EMEA | 1200 |\n\n[Figure: Revenue by region]\nA bar chart."
        doc = markdown_document(text, figures=[(text.index("[Figure"), {"caption": "Revenue by region", "src": "p1-fig1.png"})])
        (at, info), = doc.figures
        assert doc.text[at:].startswith("[Figure: Revenue by region]") and info["src"] == "p1-fig1.png"

    def test_pieces_keep_their_figures_and_their_printed_numbers(self):
        from vectrixdb.extract.batches import Batched

        def piece(first):
            text = f"Page {first + 1} words.\n\n[Figure: chart {first + 1}]"
            return LoadedDocument(
                text=text, pages=[(0, 1)], figures=[(text.index("[Figure"), {"caption": f"chart {first + 1}", "src": "figure-1.1.png"})],
                # The cover is printed C1; the next page is printed 2, its own place.
                page_labels={1: "C1"} if first == 0 else {1: str(first + 1)},
            )

        joined = Batched(lambda data, name: None)._join_pages("report.pdf", 2, [(0, piece(0)), (1, piece(1))])
        assert [joined.text[at:].split("\n")[0] for at, _info in joined.figures] == ["[Figure: chart 1]", "[Figure: chart 2]"]
        assert joined.page_labels == {1: "C1"}, "a printed number that is its page's own place says nothing and is not kept"
