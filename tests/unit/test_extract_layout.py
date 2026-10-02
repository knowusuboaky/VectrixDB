"""What an OCR engine leaves to whoever calls it: order, blank pages, running lines.

RapidOcr sorted what the engine found by the top of each box. A page in two
columns came out ``L1 R1 L2 R2``, the columns interleaved line by line, and
the words of one line came out of order whenever a scan put their tops a pixel
apart. Both were text nobody wrote, chunked and embedded as if somebody had.
"""

from __future__ import annotations

import io

import pytest

from vectrixdb.extract.engines import RapidOcr, Textract
from vectrixdb.extract.layout import (
    paragraphs_of,
    drop_running_lines,
    looks_blank,
    mend_sentence_breaks,
    reading_order,
)
from vectrixdb.ingest import join_pages, markdown_document, normalise_tables, prepare_document
from vectrixdb.quality import extraction_quality


def box(x, y, w=240, h=20):
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def column(x, top, names, w=240):
    return [(box(x, top + 30 * n, w), name) for n, name in enumerate(names)]


# ------------------------------------------------------------- reading order


class TestReadingOrder:
    def test_a_page_in_two_columns_is_read_down_one_and_then_the_other(self):
        page = column(40, 100, ["L1", "L2", "L3", "L4"]) + column(
            340, 100, ["R1", "R2", "R3", "R4"]
        )
        assert reading_order(page) == ["L1", "L2", "L3", "L4", "R1", "R2", "R3", "R4"]

    def test_the_order_the_engine_gave_them_in_does_not_matter(self):
        page = column(40, 100, ["L1", "L2", "L3"]) + column(340, 100, ["R1", "R2", "R3"])
        assert reading_order(list(reversed(page))) == ["L1", "L2", "L3", "R1", "R2", "R3"]

    def test_words_on_one_line_a_pixel_apart_stay_in_order(self):
        line = [
            (box(40, 101, 80), "Payment"),
            (box(130, 100, 80), "is"),
            (box(220, 102, 80), "due"),
        ]
        assert reading_order(line) == ["Payment is due"]

    def test_three_columns(self):
        page = (
            column(20, 100, ["A1", "A2", "A3"], 180)
            + column(230, 100, ["B1", "B2", "B3"], 180)
            + column(440, 100, ["C1", "C2", "C3"], 180)
        )
        assert reading_order(page) == ["A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3"]

    def test_a_heading_across_both_columns_is_read_where_it_sits(self):
        page = (
            [(box(40, 40, 540), "Annual report")]
            + column(40, 100, ["L1", "L2", "L3"])
            + column(340, 100, ["R1", "R2", "R3"])
        )
        assert reading_order(page) == ["Annual report", "L1", "L2", "L3", "R1", "R2", "R3"]

    def test_columns_then_a_full_width_line_then_columns_again(self):
        page = (
            column(40, 100, ["L1", "L2", "L3"])
            + column(340, 100, ["R1", "R2", "R3"])
            + [(box(40, 200, 540), "Section two")]
            + column(40, 240, ["L4", "L5", "L6"])
            + column(340, 240, ["R4", "R5", "R6"])
        )
        assert reading_order(page) == [
            "L1",
            "L2",
            "L3",
            "R1",
            "R2",
            "R3",
            "Section two",
            "L4",
            "L5",
            "L6",
            "R4",
            "R5",
            "R6",
        ]

    def test_a_table_is_read_across_not_down(self):
        """Its columns are short cells, not prose: read down, a row would lose its value."""
        rows = [("Region", "Revenue"), ("EMEA", "1200"), ("APAC", "900"), ("LATAM", "400")]
        table = [(box(40, 100 + 30 * n, 60), a) for n, (a, _) in enumerate(rows)] + [
            (box(340, 100 + 30 * n, 50), b) for n, (_, b) in enumerate(rows)
        ]
        assert reading_order(table) == ["Region Revenue", "EMEA 1200", "APAC 900", "LATAM 400"]

    def test_one_column_is_top_to_bottom(self):
        assert reading_order(column(40, 100, ["one", "two", "three"], 500)) == [
            "one",
            "two",
            "three",
        ]

    def test_a_rectangle_is_taken_as_well_as_four_corners(self):
        assert reading_order(
            [((0.1, 0.5, 0.4, 0.55), "second"), ((0.1, 0.1, 0.4, 0.15), "first")]
        ) == ["first", "second"]

    def test_nothing_and_blanks(self):
        assert reading_order([]) == [] and reading_order([(box(1, 1), "  ")]) == []


class TestLinesAnEngineReadBecomeParagraphs:
    def test_full_lines_run_on_and_a_short_line_ends_the_paragraph(self):
        lines = [
            "The committee reviewed the self-insur-",
            "ance reserves and the long-term obliga-",
            "tions of the group. Management recom-",
            "mended no change to the re-evaluation",
            "policy, as the auditors agreed.",
            "A second paragraph follows the first and",
            "runs over two lines of print as well. It",
            "ends here.",
        ]
        assert paragraphs_of(lines) == (
            "The committee reviewed the self-insurance reserves and the long-term obligations"
            " of the group. Management recommended no change to the re-evaluation policy,"
            " as the auditors agreed.\n\n"
            "A second paragraph follows the first and runs over two lines of print as well."
            " It ends here."
        )

    def test_a_list_item_and_a_heading_start_blocks_of_their_own(self):
        lines = [
            "# Minutes of the meeting",
            "The board resolved the following items at",
            "its meeting, each carried unanimously:",
            "• Launch the product line in the third quar-",
            "ter of the year, subject to approval there.",
            "• Appoint a new chair of the audit committee.",
            "The meeting closed at noon after the vote.",
        ]
        assert paragraphs_of(lines).split("\n\n") == [
            "# Minutes of the meeting",
            "The board resolved the following items at its meeting, each carried unanimously:",
            "• Launch the product line in the third quarter of the year, subject to approval there.",
            "• Appoint a new chair of the audit committee.",
            "The meeting closed at noon after the vote.",
        ]

    def test_a_page_of_short_lines_is_left_a_line_a_line(self):
        lines = ["INVOICE 1042", "Total due: 310.00", "Due: 2025-01-01", "Thank you"]
        assert paragraphs_of(lines) == "INVOICE 1042\nTotal due: 310.00\nDue: 2025-01-01\nThank you"

    def test_a_scanned_page_reaches_the_reader_as_paragraphs(self):
        lines = [
            "The committee reviewed the reserves of the group and",
            "found them adequate for the year ahead, as before.",
            "Management recommended no change to the policy.",
        ]
        reader = RapidOcr(
            engine=lambda image: lines,
            rasterize=lambda pdf: [b"page"],
            page_text=lambda pdf: [""],
            skip_blank=False,
        )
        doc = reader(b"%PDF", "scan.pdf")
        assert doc.text == " ".join(lines) and doc.metadata["ocr_pages"] == [1]


class TestTheSpaceAnEngineDrops:
    """Seen on the real engine: "fees.Interest still accrues", wherever a sentence ends mid line."""

    def test_it_is_put_back_between_sentences(self):
        assert (
            mend_sentence_breaks("no late fees.Interest still accrues")
            == "no late fees. Interest still accrues"
        )
        assert (
            mend_sentence_breaks("Is it due?Yes, today!Pay now") == "Is it due? Yes, today! Pay now"
        )

    @pytest.mark.parametrize(
        "kept",
        [
            "e.g.This is kept",
            "the U.S.Army",
            "file v2.Final",
            "see www.Example.com",
            "3.5 percent",
            "Already fine. Next one.",
            "A.B. Smith",
        ],
    )
    def test_what_is_not_a_lost_space_is_left_alone(self, kept):
        assert mend_sentence_breaks(kept) == kept


class TestTextractIsPutInOrder:
    def block(self, text, left, top, page=1):
        return {
            "BlockType": "LINE",
            "Text": text,
            "Page": page,
            "Geometry": {"BoundingBox": {"Left": left, "Top": top, "Width": 0.4, "Height": 0.02}},
        }

    def test_two_columns_returned_a_line_from_each_in_turn(self):
        interleaved = []
        for n in range(3):
            interleaved += [
                self.block(f"L{n + 1}", 0.05, 0.1 + 0.04 * n),
                self.block(f"R{n + 1}", 0.55, 0.1 + 0.04 * n),
            ]
        doc = Textract._document(interleaved)
        assert doc.text.split() == ["L1", "L2", "L3", "R1", "R2", "R3"]
        assert doc.metadata["ocr_pages"] == [1]

    def test_blocks_with_no_box_keep_the_order_they_came_in(self):
        doc = Textract._document(
            [
                {"BlockType": "LINE", "Text": "first", "Page": 1},
                {"BlockType": "LINE", "Text": "second", "Page": 1},
            ]
        )
        assert doc.text.split() == ["first", "second"]


# ---------------------------------------------------------------- blank pages

try:
    from PIL import Image
except ImportError:  # only the blank-page tests need it; the rest of this file runs without
    Image = None
needs_pillow = pytest.mark.skipif(Image is None, reason="Pillow is not installed")


def png(draw=None, size=(600, 800), paper=255):
    from PIL import ImageDraw

    image = Image.new("L", size, paper)
    if draw:
        draw(ImageDraw.Draw(image))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def speckled(draw):
    import random

    rng = random.Random(3)
    for _ in range(400):
        draw.point((rng.randrange(600), rng.randrange(800)), fill=0)


@needs_pillow
class TestBlankPages:
    def test_white_paper_is_blank(self):
        assert looks_blank(png()) is True

    def test_a_scans_speckle_is_still_blank(self):
        assert looks_blank(png(speckled)) is True

    def test_off_white_paper_is_blank(self):
        assert looks_blank(png(paper=228)) is True

    def test_one_line_of_text_is_not(self):
        assert (
            looks_blank(
                png(
                    lambda d: [
                        d.rectangle((60, 100 + 0, 60 + 22 * n, 112), fill=0) for n in range(1, 22)
                    ]
                )
            )
            is False
        )

    def test_a_page_of_text_is_not(self):
        def page(d):
            for row in range(30):
                d.rectangle((60, 60 + 22 * row, 540, 70 + 22 * row), fill=0)

        assert looks_blank(png(page)) is False

    def test_white_on_black_with_nothing_on_it_is_blank_and_with_text_is_not(self):
        assert looks_blank(png(paper=0)) is True
        assert (
            looks_blank(png(lambda d: d.rectangle((60, 100, 540, 130), fill=255), paper=0)) is False
        )

    def test_what_is_not_an_image_is_read_not_skipped(self):
        assert looks_blank(b"page-2-png") is False and looks_blank(b"") is False

    def test_a_blank_scanned_page_never_reaches_the_engine(self):
        seen = []

        def engine(image):
            seen.append(image)
            return ["Scanned covenant schedule, read by the engine."]

        written = png(
            lambda d: [d.rectangle((60, 60 + 22 * r, 540, 70 + 22 * r), fill=0) for r in range(20)]
        )
        reader = RapidOcr(
            engine=engine,
            rasterize=lambda pdf: [written, png(), written],
            page_text=lambda pdf: ["", "", ""],
        )
        doc = reader(b"%PDF", "scan.pdf")
        assert seen == [written, written], (
            "the empty page cost nothing, and a vision model had nothing to make up"
        )
        assert (
            doc.metadata["ocr_pages"] == [1, 3]
            and doc.metadata["pages_blank"] == 1
            and doc.metadata["pages"] == 3
        )

    def test_it_can_be_turned_off(self):
        seen = []
        reader = RapidOcr(
            engine=lambda image: seen.append(image) or [],
            rasterize=lambda pdf: [png()],
            page_text=lambda pdf: [""],
            skip_blank=False,
        )
        reader(b"%PDF", "scan.pdf")
        assert len(seen) == 1

    def test_a_blank_image_file(self):
        doc = RapidOcr(engine=lambda image: ["made up"])(png(), "empty.png")
        assert doc.text == "" and doc.metadata["pages_blank"] == 1


# ------------------------------------------------------ running heads and feet


def report(n=6):
    return [
        f"Acme Holdings Annual Report\nParagraph {p} opens here and says something of its own.\n\nA second paragraph on page {p}.\nPage {p} of {n}"
        for p in range(1, n + 1)
    ]


def body(p):
    return f"Body text for page {p}, which says something different every time it is read."


class TestRunningLines:
    def test_the_title_and_the_page_number_go_and_the_text_stays(self):
        pages, dropped = drop_running_lines(report())
        assert dropped == ["Acme Holdings Annual Report", "Page 1 of 6"]
        assert (
            pages[2]
            == "Paragraph 3 opens here and says something of its own.\n\nA second paragraph on page 3."
        )

    def test_a_paragraph_break_inside_a_page_is_kept(self):
        assert "\n\n" in drop_running_lines(report())[0][0]

    def test_once_the_footer_is_gone_the_line_above_it_is_the_body(self):
        """Found while building this: the second look took "See note 4." for a footer because it differs only by a number."""
        pages = [
            f"Body text for page {p}, which says something different every time it is read.\nSee note {p}.\nPage {p} of 7"
            for p in range(1, 8)
        ]
        cleaned, dropped = drop_running_lines(pages)
        assert dropped == ["Page 1 of 7"] and cleaned[3].endswith("See note 4.")

    def test_a_header_of_two_lines_is_two_running_lines(self):
        pages = [
            f"Acme Holdings\nConfidential\nBody text for page {p}, which says something different every time it is read."
            for p in range(1, 7)
        ]
        cleaned, dropped = drop_running_lines(pages)
        assert dropped == ["Acme Holdings", "Confidential"] and cleaned[0].startswith("Body text")

    def test_a_short_document_is_left_alone(self):
        pages = report(3)
        assert drop_running_lines(pages) == (pages, [])

    def test_a_line_on_a_few_pages_only_is_not_running(self):
        pages = [
            ("Draft\n" if p < 3 else "")
            + f"Body text for page {p}, which says something different every time it is read."
            for p in range(1, 9)
        ]
        assert drop_running_lines(pages)[1] == []

    def test_sentences_that_differ_only_by_a_number_are_not_a_header(self):
        """Found while building this: numbers were taken out of every line, so a numbered list at the top of each page went."""
        pages = [
            f"Clause {p} sets out what the borrower must report to the lender each quarter.\nMore of page {p} follows on from that clause here."
            for p in range(1, 8)
        ]
        assert drop_running_lines(pages) == (pages, [])

    def test_a_long_running_footer_with_its_page_number_glued_on_goes(self):
        """Found in a bank's annual report: "...MANAGEMENT'S DISCUSSION AND ANALYSIS19" on every page, the number part of the word."""
        pages = [
            f"Body text for page {p}, which says something different every time it is read.\n"
            f"ACME HOLDINGS ANNUAL REPORT 2025 MANAGEMENT'S DISCUSSION AND ANALYSIS{p + 18}"
            for p in range(1, 8)
        ]
        cleaned, dropped = drop_running_lines(pages)
        assert dropped == [
            "ACME HOLDINGS ANNUAL REPORT 2025 MANAGEMENT'S DISCUSSION AND ANALYSIS19"
        ]
        assert all("ANALYSIS" not in page for page in cleaned)

    def test_a_page_number_glued_to_the_front_goes_too(self):
        pages = [
            f"{p}ACME HOLDINGS ANNUAL REPORT 2025 MANAGEMENT'S DISCUSSION AND ANALYSIS\n"
            f"Body text for page {p}, which says something different every time it is read."
            for p in range(1, 8)
        ]
        cleaned, dropped = drop_running_lines(pages)
        assert len(dropped) == 1 and cleaned[3].startswith("Body text for page 4")

    def test_a_body_line_ending_in_a_different_number_on_each_page_stays(self):
        pages = [
            f"Acme Report\nThe balance at the end of the quarter on this page was {p * 137}"
            for p in range(1, 8)
        ]
        cleaned, dropped = drop_running_lines(pages)
        assert dropped == ["Acme Report"] and cleaned[2].endswith("was 411")

    def test_a_header_the_reader_puts_last_on_some_pages_is_one_header(self):
        """Found in a bank's annual report: the header came first on some pages and last on others, so neither end had enough."""
        pages = [
            f"Acme Holdings Annual Report\n{body(p)}"
            if p % 2
            else f"{body(p)}\nAcme Holdings Annual Report"
            for p in range(1, 9)
        ]
        cleaned, dropped = drop_running_lines(pages)
        assert dropped == ["Acme Holdings Annual Report"] and cleaned == [
            body(p) for p in range(1, 9)
        ]

    def test_the_page_number_on_the_outside_edge_of_the_page(self):
        """A left-hand page prints it first and a right-hand page last, and pages taken out of a longer file skip numbers.

        With no numbering to follow, the line is still one line, and short:
        its page number and its "|" are not its words.
        """
        printed = [3, 8, 11, 14, 19, 22, 27, 30]
        pages = [
            (
                f"{n} | Acme Holdings Group Annual Report 2025"
                if n % 2 == 0
                else f"Acme Holdings Group Annual Report 2025 | {n}"
            )
            + f"\n{body(p)}"
            for p, n in enumerate(printed, start=1)
        ]
        cleaned, dropped = drop_running_lines(pages)
        assert dropped == ["Acme Holdings Group Annual Report 2025 | 3"] and cleaned == [
            body(p) for p in range(1, 9)
        ]

    def test_a_page_number_the_reader_ran_into_the_year(self):
        """Found in a bank's annual report: "ACME HOLDINGS ANNUAL REPORT 20253" is the year 2025 and page 3, the space between them lost."""
        pages = [
            (
                f"ACME HOLDINGS ANNUAL REPORT 2025{p}"
                if p % 2
                else f"{p}ACME HOLDINGS ANNUAL REPORT 2025"
            )
            + f"\n{body(p)}"
            for p in range(1, 13)
        ]
        cleaned, dropped = drop_running_lines(pages)
        assert dropped == ["ACME HOLDINGS ANNUAL REPORT 20251"] and cleaned == [
            body(p) for p in range(1, 13)
        ]

    def test_a_footer_that_names_the_part_of_the_report_it_is_in(self):
        """Found in a bank's annual report: the footer changes with the part, so in a batch of pages no one footer is on most of them."""
        parts = ["MANAGEMENT DISCUSSION"] * 5 + ["GLOSSARY"] * 2 + ["FINANCIAL STATEMENTS"] * 5
        pages = [
            f"{body(p)}\nACME HOLDINGS ANNUAL REPORT 2025 {part} {p + 40}"
            for p, part in enumerate(parts, start=1)
        ]
        cleaned, dropped = drop_running_lines(pages)
        assert dropped == [
            "ACME HOLDINGS ANNUAL REPORT 2025 MANAGEMENT DISCUSSION 41",
            "ACME HOLDINGS ANNUAL REPORT 2025 GLOSSARY 46",
            "ACME HOLDINGS ANNUAL REPORT 2025 FINANCIAL STATEMENTS 48",
        ]
        assert cleaned == [body(p) for p in range(1, 13)]

    @pytest.mark.parametrize(
        "footer",
        [
            "ACME HOLDINGS ANNUAL REPORT 2025 {part} {n}",
            "{part} ACME HOLDINGS ANNUAL REPORT 2025 {n}",
        ],
    )
    def test_the_footer_of_a_part_that_is_one_page_long(self, footer):
        """Seen on that page alone, it is known by its page's number and the words it shares with the other footers, first or last."""
        parts = ["FINANCIAL STATEMENTS"] * 7 + ["SHAREHOLDER INFORMATION"]
        pages = [
            f"{body(p)}\n" + footer.format(part=part, n=p + 40)
            for p, part in enumerate(parts, start=1)
        ]
        cleaned, dropped = drop_running_lines(pages)
        assert dropped[-1] == footer.format(part="SHAREHOLDER INFORMATION", n=48) and cleaned[
            7
        ] == body(8)

    def test_a_line_that_only_starts_like_the_footer_stays(self):
        """Three words in common and the page's number at the end make a sentence about the company, not its footer."""
        pages = [
            f"{body(p)}\nACME HOLDINGS ANNUAL REPORT 2025 FINANCIAL STATEMENTS {p + 40}"
            for p in range(1, 8)
        ]
        pages.append(f"{body(8)}\nAcme Holdings Annual revenue passed a record 48")
        assert (
            drop_running_lines(pages)[0][7]
            == f"{body(8)}\nAcme Holdings Annual revenue passed a record 48"
        )

    def test_a_footer_inside_the_page_goes_too(self):
        """Found in a bank's annual report: a reader keeps the order things were drawn in, and a footer drawn before a table lands above it."""
        pages = [
            f"{body(p)}\nACME HOLDINGS ANNUAL REPORT 2025 FINANCIAL STATEMENTS {p + 40}"
            for p in range(1, 8)
        ]
        pages.append(
            f"{body(8)}\nACME HOLDINGS ANNUAL REPORT 2025 FINANCIAL STATEMENTS 48\nRevenue 1,200 1,100"
        )
        assert drop_running_lines(pages)[0][7] == f"{body(8)}\nRevenue 1,200 1,100"

    def test_the_footer_s_words_with_another_page_s_number_stay(self):
        """A contents page lists the parts with the pages they start on, which are not its own."""
        pages = [f"Contents\nACME HOLDINGS ANNUAL REPORT 2025 FINANCIAL STATEMENTS 47\n{body(1)}"]
        pages += [
            f"{body(p)}\nACME HOLDINGS ANNUAL REPORT 2025 FINANCIAL STATEMENTS {p + 40}"
            for p in range(2, 9)
        ]
        cleaned, _ = drop_running_lines(pages)
        assert cleaned[0] == pages[0] and all(
            page == body(p) for p, page in enumerate(cleaned[1:], start=2)
        )

    def test_a_footer_read_onto_the_text_is_taken_off_it(self):
        """Found in a bank's annual report: "...lease-related payments.ACME HOLDINGS ... STATEMENTS122", one line to the reader."""
        pages = [
            f"{body(p)}\nACME HOLDINGS ANNUAL REPORT 2025 FINANCIAL STATEMENTS{p + 40}"
            for p in range(1, 7)
        ]
        pages.append(
            "Body text for page 7, which runs on to the lease payments.ACME HOLDINGS ANNUAL REPORT 2025 FINANCIAL STATEMENTS47"
        )
        pages.append(
            "Body text for page 8, which ends on a sum in millions ($3.2 billion). 48 ACME HOLDINGS ANNUAL REPORT 2025 FINANCIAL STATEMENTS"
        )
        cleaned, _ = drop_running_lines(pages)
        assert cleaned[6] == "Body text for page 7, which runs on to the lease payments."
        assert cleaned[7] == "Body text for page 8, which ends on a sum in millions ($3.2 billion)."

    @pytest.mark.parametrize(
        "text",
        [
            "Body text for page 8, which sends the reader to ACME HOLDINGS ANNUAL REPORT 2025 FINANCIAL STATEMENTS 12",
            "Body text for page 8, which could end on a total of 148 ACME HOLDINGS ANNUAL REPORT 2025 FINANCIAL STATEMENTS",
        ],
    )
    def test_the_footer_s_words_on_the_text_without_the_page_s_own_number_stay(self, text):
        """Another page's number, or one the page's number cannot be told apart from: 148 on page 48 is left as it is."""
        pages = [
            f"{body(p)}\nACME HOLDINGS ANNUAL REPORT 2025 FINANCIAL STATEMENTS{p + 40}"
            for p in range(1, 8)
        ] + [text]
        assert drop_running_lines(pages)[0][7] == text

    def test_whole_page_numbers_decide_the_numbering(self):
        """The 1 that "41" ends in fits page 1 as well, and a sum of 1,209 on page 9 would tip it: whole numbers are counted first."""
        pages = [
            f"{body(p)}\nACME HOLDINGS ANNUAL REPORT 2025 FINANCIAL STATEMENTS{p + 40}"
            for p in range(1, 8)
        ]
        pages.append(
            "Body text for page 8, which ends on a sum in millions ($3.2 billion). 48 ACME HOLDINGS ANNUAL REPORT 2025 FINANCIAL STATEMENTS"
        )
        pages.append("Body text for page 9, which has no footer and ends on revenue of 1,209")
        cleaned, _ = drop_running_lines(pages)
        assert (
            cleaned[7] == "Body text for page 8, which ends on a sum in millions ($3.2 billion)."
            and cleaned[8] == pages[8]
        )

    def test_a_number_inside_a_page_without_its_footer_stays(self):
        """Only a running line of three words or more is looked for inside a page: a table's 8 on page 8 is not its page number."""
        pages = [f"{body(p)}\n{p}" for p in range(1, 8)] + [f"Units sold by region\n8\n{body(8)}"]
        cleaned, _ = drop_running_lines(pages)
        assert cleaned[7] == pages[7] and cleaned[:7] == [body(p) for p in range(1, 8)]

    def test_a_page_has_one_printed_number(self):
        """Its number is in its footer, so "Step 3" at the top of page 3 is a coincidence and stays."""
        pages = [(f"Step {p}\n" if p in (3, 4) else "") + f"{body(p)}\n{p}" for p in range(1, 9)]
        cleaned, dropped = drop_running_lines(pages)
        assert (
            dropped == ["1"]
            and cleaned[2] == f"Step 3\n{body(3)}"
            and cleaned[3] == f"Step 4\n{body(4)}"
        )

    def test_a_page_number_in_a_long_header_and_a_long_footer(self):
        """One printed number a page, but a second line that carries it on every page is a running line too."""
        pages = [
            f"ACME HOLDINGS ANNUAL REPORT 2025 MANAGEMENT DISCUSSION {p + 40}\n{body(p)}\n"
            f"ACME HOLDINGS MATERIAL FOR ITS SHAREHOLDERS AND NOBODY ELSE {p + 40}"
            for p in range(1, 9)
        ]
        cleaned, dropped = drop_running_lines(pages)
        assert len(dropped) == 2 and cleaned == [body(p) for p in range(1, 9)]

    @pytest.mark.parametrize(
        "footer",
        [
            "ACME HOLDINGS ANNUAL REPORT {part} 2025{n}",
            "{n}2025 ACME HOLDINGS ANNUAL REPORT {part}",
        ],
    )
    def test_a_footer_with_its_page_number_run_into_the_year(self, footer):
        """No whole number to go by: "2025" and page 1 read as "20251", page 10 as "202510", before or after it."""
        parts = ["MANAGEMENT DISCUSSION"] * 6 + ["FINANCIAL STATEMENTS"] * 6
        pages = [
            f"{body(p)}\n" + footer.format(part=part, n=p) for p, part in enumerate(parts, start=1)
        ]
        cleaned, dropped = drop_running_lines(pages)
        assert len(dropped) == 2 and cleaned == [body(p) for p in range(1, 13)]

    def test_a_long_first_line_that_repeats_is_a_paragraph_not_a_header(self):
        long = (
            "This agreement is made between the parties named below and is subject to the terms set out in the schedules attached. "
            * 2
        )
        pages = [
            f"{long}\nThe body of page {p} follows the recital and is different on every page of the agreement."
            for p in range(1, 7)
        ]
        assert drop_running_lines(pages)[1] == []

    def test_blank_pages_are_not_counted_against_a_header(self):
        pages = report()
        pages.insert(2, "")
        assert drop_running_lines(pages)[1] == ["Acme Holdings Annual Report", "Page 1 of 6"]

    def test_a_sentence_that_runs_over_a_page_break_is_joined_once_the_footer_is_gone(self):
        """The reason this matters to chunks: the footer stood between the two halves."""
        pages = [
            f"Acme Report\nText on page {p} that ends mid sentence and carries on to\nPage {p}"
            for p in range(1, 6)
        ]
        pages = [
            p.replace("Text on", "the next page. Text on") if n else p for n, p in enumerate(pages)
        ]
        joined_with = join_pages(pages)[0]
        joined_without = join_pages(drop_running_lines(pages)[0])[0]
        assert (
            "carries on to the next page." in joined_without
            and "carries on to the next page." not in joined_with
        )

    def test_the_pdf_reader_says_what_it_took_out(self):
        texts = report()
        reader = RapidOcr(engine=lambda image: [], page_text=lambda pdf: texts)
        doc = reader(b"%PDF", "report.pdf")
        assert doc.metadata["running_lines"] == ["Acme Holdings Annual Report", "Page 1 of 6"]
        assert (
            "Acme Holdings Annual Report" not in doc.text and "Paragraph 4 opens here" in doc.text
        )

    def test_it_can_be_turned_off(self):
        reader = RapidOcr(
            engine=lambda image: [], page_text=lambda pdf: report(), drop_running=False
        )
        doc = reader(b"%PDF", "report.pdf")
        assert (
            "running_lines" not in doc.metadata
            and doc.text.count("Acme Holdings Annual Report") == 6
        )


# ----------------------------------------------------------------- html tables

HTML = "Revenue by region.\n\n<table><tr><th>Region</th><th>Revenue</th></tr><tr><td>EMEA</td><td>1,200</td></tr><tr><td>APAC</td><td><b>900</b></td></tr></table>\n\nMore prose."


class TestHtmlTables:
    def test_a_table_a_vision_model_wrote_as_html_becomes_rows(self):
        text = normalise_tables(HTML)
        assert (
            "<t" not in text
            and "Region: EMEA; Revenue: 1,200" in text
            and "Region: APAC; Revenue: 900" in text
        )
        assert text.startswith("Revenue by region.") and text.endswith("More prose.")

    def test_it_is_the_rendering_a_pipe_table_gets(self):
        pipes = "| Region | Revenue |\n| --- | --- |\n| EMEA | 1,200 |\n| APAC | 900 |\n"
        rows = [line for line in normalise_tables(HTML).splitlines() if line.startswith("Region:")]
        assert rows == [
            line for line in normalise_tables(pipes).splitlines() if line.startswith("Region:")
        ]

    def test_offsets_after_the_table_move_with_it(self):
        where = HTML.index("More prose.")
        doc = markdown_document(HTML, pages=[(0, 1), (where, 2)])
        assert doc.text[doc.pages[1][0] :].startswith("More prose.")

    def test_a_table_inside_a_code_fence_is_left_alone(self):
        fenced = "```html\n<table><tr><th>a</th></tr><tr><td>1</td></tr></table>\n```\n"
        assert normalise_tables(fenced) == fenced

    def test_a_table_of_one_row_and_broken_markup_are_left_alone(self):
        for markup in ("<table><tr><td>only</td></tr></table>", "<table><tr><td>never closed"):
            assert normalise_tables(markup) == markup


# ------------------------------------------------------------------ repetition

PROSE = (
    "Customers in a declared disaster area can defer loan payments for up to ninety days with no late fees. "
    "Interest still accrues during the deferral, and an adviser calls within a day to confirm the new schedule."
)


class TestTextThatLoops:
    def test_the_same_word_a_hundred_times_used_to_score_a_perfect_one(self):
        scored = extraction_quality("the " * 100)
        assert scored.signals["varied"] == 0.0 and scored.score == 0.0 and not scored.usable

    def test_a_sentence_over_and_over_which_is_how_a_vision_model_fails(self):
        scored = extraction_quality("This page intentionally left blank. " * 40)
        assert scored.signals["varied"] < 0.2 and not scored.usable

    def test_ordinary_prose_is_exactly_where_it_was(self):
        scored = extraction_quality(PROSE)
        assert scored.signals["varied"] == 1.0 and scored.usable
        weights = {
            "plain_characters": 0.2,
            "whole_words": 0.4,
            "pronounceable": 0.05,
            "known_words": 0.25,
            "word_length": 0.1,
        }
        assert scored.score == round(sum(scored.signals[k] * w for k, w in weights.items()), 4), (
            "the line it was calibrated on has not moved"
        )

    def test_a_sentence_said_three_times_is_somebodys_real_text(self):
        """A refrain, a repeated disclaimer. Found while building this: the first cut refused it."""
        assert extraction_quality(PROSE + " " + PROSE + " " + PROSE).usable

    def test_a_table_with_a_row_of_the_same_answer_is_not_a_loop(self):
        """Found by measuring: the labelled set's clean tables, "yes yes yes yes" and a rule of dashes, were scaled down."""
        table = "May viewer operator admin --- --- --- --- See collections yes yes yes yes Open a chunk no yes yes yes Delete a collection no no no yes"
        assert extraction_quality(table).signals["varied"] == 1.0

    def test_emphasis_is_not_a_loop(self):
        assert (
            extraction_quality(
                "It was very very very good and everybody in the room said so at once."
            ).signals["varied"]
            == 1.0
        )

    def test_rows_of_a_table_are_not_a_loop(self):
        rows = "\n".join(
            f"Region: R{n}; Revenue: {1000 + 37 * n}; Quarter: Q{n % 4 + 1}; Owner: person {n}"
            for n in range(40)
        )
        assert extraction_quality(rows).signals["varied"] == 1.0

    def test_a_short_text_is_never_judged_a_loop_by_its_windows(self):
        assert extraction_quality("yes yes no no").signals["varied"] == 1.0


# --------------------------------------------------- which chunks are OCR's


class TestAChunkKnowsWhetherItsPageWasRead:
    def document(self):
        typed = (
            "The first page has a real text layer and says a good deal about the covenant schedule and its terms. "
            * 3
        )
        scanned = (
            "The second page was a scan and the engine read it, so what it says deserves a second look later on. "
            * 3
        )
        reader = RapidOcr(
            engine=lambda image: [scanned],
            rasterize=lambda pdf: [b"1", b"2"],
            page_text=lambda pdf: [typed, ""],
            skip_blank=False,
        )
        return reader(b"%PDF", "mixed.pdf")

    def test_only_the_scanned_pages_chunks_say_ocr(self):
        prepared = prepare_document(
            self.document(), "doc-1", chunk="recursive", chunk_size=200, overlap=0
        )
        typed_only = {
            m["ocr"] for m in prepared.metadata if m["page"] == 1 and m.get("page_end", 1) == 1
        }
        touching_the_scan = {
            m["ocr"] for m in prepared.metadata if 2 in (m["page"], m.get("page_end"))
        }
        assert typed_only == {False} and touching_the_scan == {True}, (
            "a chunk that runs onto the scanned page holds OCR's text too"
        )

    def test_what_is_about_the_document_is_not_copied_onto_every_chunk(self):
        prepared = prepare_document(
            self.document(), "doc-1", chunk="recursive", chunk_size=200, overlap=0
        )
        assert all("ocr_pages" not in m and "running_lines" not in m for m in prepared.metadata)
