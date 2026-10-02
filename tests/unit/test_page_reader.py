"""A PDF's hard pages read by sight: which pages, what the model is asked, and how its reading is held to the page.

The model is a fake answering the shape a chat completions route answers,
and the pages are drawn with PyMuPDF. What is tested is everything around
the model: which pages the rules hand over, what goes in the request, that a
number the page does not print never reaches the text, and that a reading
which fails in any way leaves the page as the rules read it.
"""

from __future__ import annotations

import json

import pytest

from vectrixdb.extract.page_reader import INSTRUCTION, PageReader, held_to_the_page, numbers_in
from vectrixdb.extract.pdf_text import Line, Run, _hard

LAYER = (
    "Snapshot\n169-year\nContinuous Dividend History\n4.6%\n2025 Dividend Yield\n25.9%\nTotal Shareholder Return\n"
    "Balance Sheet and Capital Position\n$2.1 trillion\nAssets\n$1.3 trillion\nDeposits\n14.7%\nCET1 Ratio"
)
READING = (
    "# 2025 Snapshot\n\n- 169-year continuous dividend history\n- 4.6% 2025 dividend yield\n- 25.9% total shareholder return\n\n"
    "## Balance Sheet and Capital Position\n\n| Measure | Amount |\n|---|---|\n| Assets | $2.1 trillion |\n| Deposits | $1.3 trillion |\n"
    "| CET1 Ratio | 14.7% |"
)


# ================================================================ the numbers ===


class TestNumbers:
    def test_a_number_is_its_value(self):
        assert numbers_in("Revenue of $1,200.50 rose 4.6% to (35) in 2025, and 10.0 is 10") == [
            1200.5,
            4.6,
            35.0,
            2025.0,
            10.0,
            10.0,
        ]

    def test_a_digit_inside_a_word_and_a_footnote_mark_are_not_numbers(self):
        assert numbers_in("CET1 in Q3, the dividend yield[^3] and the CAGR⁴") == []


TABLE_LAYER = (
    "Measure 2025 2024\nDeposits $1.3 trillion $1.2 trillion\nAssets $2.1 trillion $2.0 trillion\n"
    "Loans $0.9 trillion $0.8 trillion\nCapital 14.7% 13.1%\nRevenue $20,686 $19,790"
)
TABLE = (
    "| Measure | 2025 | 2024 |\n|---|---|---|\n| Deposits | $1.3 trillion | $1.2 trillion |\n| Assets | $2.1 trillion | $2.0 trillion |\n"
    "| Loans | $0.9 trillion | $0.8 trillion |\n| Capital | 14.7% | 13.1% |\n| Revenue | $20,686 | $19,790 |"
)


class TestHeldToThePage:
    def test_a_faithful_reading_is_taken_as_it_is(self):
        text, counts = held_to_the_page(READING, LAYER)
        assert text == READING and counts == {
            "numbers_checked": 8,
            "numbers_dropped": 0,
            "guesses_dropped": 0,
        }

    def test_a_sentence_with_a_number_the_page_does_not_print_goes(self):
        reading = (
            READING
            + "\n\nThe yield rose from 4.1% the year before. The bank has paid a dividend every year."
        )
        text, counts = held_to_the_page(
            reading, LAYER + "\nThe bank has paid a dividend every year."
        )
        assert "4.1%" not in text and text.endswith("The bank has paid a dividend every year.")
        assert counts["numbers_dropped"] == 1

    def test_in_a_table_the_cell_goes_and_the_row_stays_while_it_has_a_value(self):
        text, counts = held_to_the_page(
            TABLE.replace("$1.2 trillion |", "$1.5 trillion |"), TABLE_LAYER
        )
        assert (
            "| Deposits | $1.3 trillion |  |" in text
            and "1.5" not in text
            and counts["numbers_dropped"] == 1
        )

    def test_in_a_row_of_values_the_value_goes(self):
        text, _counts = held_to_the_page(
            TABLE + "\n\nMeasure: Deposits; 2025: $1.3 trillion; 2026: $1.4 trillion", TABLE_LAYER
        )
        assert text.endswith("Measure: Deposits; 2025: $1.3 trillion")

    def test_a_row_left_with_its_label_alone_goes(self):
        text, _counts = held_to_the_page(
            TABLE + "\n\nMeasure: Loans; 2025 target: $0.7 trillion", TABLE_LAYER
        )
        assert text == TABLE

    def test_a_reading_that_invents_too_much_is_not_taken(self):
        reading = READING + "\n\nNet income was 20.4 billion, up 11% on 18.4 billion."
        assert held_to_the_page(reading, LAYER) is None

    def test_a_reading_that_leaves_out_the_page_is_not_taken(self):
        assert held_to_the_page("# 2025 Snapshot\n\n- 4.6% 2025 dividend yield", LAYER) is None

    def test_a_value_that_is_only_a_tick_on_a_scale_was_read_off_a_bar_and_goes(self):
        layer = TABLE_LAYER + "\nNet income 8,000 7,000 6,000 5,000 4,000 3,000 2,000 1,000 0"
        reading = (
            TABLE
            + "\n\nNet income; 2025: 7,000\n\n| Segment | Net income |\n|---|---|\n| Canada | 6,000 |"
        )
        text, counts = held_to_the_page(
            reading, layer, ticks=[0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000]
        )
        assert "7,000" not in text and "6,000" not in text and text.startswith(TABLE)
        assert (counts["numbers_dropped"], counts["guesses_dropped"]) == (0, 2), (
            "read off the scale: guesses, not inventions"
        )
        prose, _counts = held_to_the_page(
            TABLE + "\n\nThe scale runs from 0 to 8,000.", layer, ticks=[0, 8000]
        )
        assert prose.endswith("The scale runs from 0 to 8,000."), "a sentence may name the scale"

    def test_guesses_go_without_costing_the_reading_and_inventions_still_do(self):
        guesses = (
            "\n\nMeasure: Net income; 2024: about 7,100; 2025: about 7,200\nMeasure: Deposits; 2025: approximately 450"
            "\nThe revenue bars reach roughly 19,500 and ~20,500."
        )
        text, counts = held_to_the_page(TABLE + guesses, TABLE_LAYER)
        assert text == TABLE and (counts["numbers_dropped"], counts["guesses_dropped"]) == (0, 5), (
            "five guesses, and the reading is still taken"
        )
        assert (
            held_to_the_page(
                TABLE + "\n\nMeasure: Net income; 2024: 7,100; 2025: 7,200; 2026: 7,900",
                TABLE_LAYER,
            )
            is None
        ), "the same numbers given as fact"

    def test_a_scale_in_a_charts_printed_words_is_its_ticks_in_a_row_through_zero(self):
        from vectrixdb.extract.page_reader import scale_in

        words = "NET INCOME 8,000 7,000 6,000 5,000 4,000 3,000 2,000 1,000 0 2024 2025 $50 40 30 20 10 0 (10) (20) (30)"
        assert scale_in(words) == {
            8000.0,
            7000.0,
            6000.0,
            5000.0,
            4000.0,
            3000.0,
            2000.0,
            1000.0,
            0.0,
            50.0,
            40.0,
            30.0,
            20.0,
            10.0,
        }
        assert scale_in("Net income 10.0 14.4 8.3 13.8 20.0 14.5 2023 2024 2025") == set(), (
            "values printed on the bars are no scale"
        )
        assert 7000.0 not in scale_in(words + " Net income 7,000"), (
            "a tick printed again as a value is a value too"
        )

    def test_a_list_the_reading_numbered_itself_is_not_a_number_on_the_page(self):
        reading = READING.replace("- 169-year", "1. 169-year").replace("- 4.6%", "2. 4.6%")
        text, counts = held_to_the_page(reading, LAYER)
        assert counts["numbers_dropped"] == 0 and "- 169-year continuous dividend history" in text


# ================================================================= the reader ===


def completion(content, status=200):
    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content if isinstance(content, str) else json.dumps(content),
                }
            }
        ]
    }
    return status, {}, json.dumps(body).encode()


class Service:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, method, url, headers, body, timeout):
        self.requests.append(json.loads(body))
        return self.answers.pop(0)


def png():
    Image = pytest.importorskip("PIL.Image")
    import io

    buffer = io.BytesIO()
    Image.new("RGB", (80, 100), (240, 240, 240)).save(buffer, "PNG")
    return buffer.getvalue()


def reader(service):
    return PageReader.azure_openai(
        "https://o.openai.azure.com", "gpt-5.4-mini", key="k", transport=service, max_wait=0
    )


class TestTheReader:
    def test_it_is_shown_the_page_and_given_its_words(self):
        service = Service(completion({"markdown": READING}))
        assert reader(service)(png(), LAYER, {"name": "report.pdf", "page": 8}) == READING
        system, user = service.requests[0]["messages"]
        assert (
            system["content"] == INSTRUCTION
            and "Never write a number that is not in the text layer" in INSTRUCTION
        )
        text, image = user["content"]
        assert (
            "File: report.pdf" in text["text"]
            and "Page: 8" in text["text"]
            and "4.6%\n2025 Dividend Yield" in text["text"]
        )
        assert (
            image["image_url"]["url"].startswith("data:image/png;base64,")
            and service.requests[0]["max_tokens"] == 4000
        )

    def test_an_answer_in_prose_is_still_a_reading_and_a_refusal_is_none(self):
        assert (
            reader(Service(completion("# Page\n\nThe words.")))(png(), LAYER, {"page": 1})
            == "# Page\n\nThe words."
        )
        assert reader(Service(completion("x", status=400)))(png(), LAYER, {"page": 1}) is None

    def test_a_deployment_for_pages_alone_is_taken_over_the_describers(self):
        env = {
            "AZURE_OPENAI_ENDPOINT": "https://o.openai.azure.com",
            "AZURE_OPENAI_KEY": "k",
            "AZURE_OPENAI_VISION_DEPLOYMENT": "gpt-5.4-mini",
        }
        assert PageReader.from_environment(env).label == "gpt-5.4-mini"
        assert (
            PageReader.from_environment({**env, "AZURE_OPENAI_PAGE_DEPLOYMENT": "gpt-5.4"}).label
            == "gpt-5.4"
        )
        assert PageReader.from_environment({}) is None


# ============================================================ the hard pages ===


def line(text, top, left=72.0, size=10.0, width=None):
    right = left + (width if width is not None else 5.5 * len(text))
    return Line([Run(left, top, right, top + size, text, size, False)])


class TestWhichPagesAreHard:
    def test_a_chart_makes_a_page_hard(self):
        assert _hard([line("Revenue grew.", 100)], [], 792, charted=True) == "chart"

    def test_big_figures_set_apart_from_their_labels_are_tiles(self):
        lines = [line("The year in brief, as the board reported it to shareholders.", 90)]
        lines += [
            line("4.6%", 150, size=28),
            line("2025 Dividend Yield", 185),
            line("25.9%", 230, size=28),
            line("Total Shareholder Return", 265),
        ]
        assert _hard(lines, [], 792, charted=False) == "tiles"

    def test_a_reading_that_starts_at_the_foot_or_climbs_the_page_twice_is_out_of_order(self):
        assert (
            _hard(
                [line("144 ANNUAL REPORT 2025", 761), line("Contents", 60), line("Notes", 120)],
                [],
                792,
                charted=False,
            )
            == "order"
        )
        climbs = [line("B", 500), line("A", 100), line("D", 650), line("C", 300)]
        assert _hard(climbs, [], 792, charted=False) == "order"
        assert (
            _hard(
                [
                    line("Left column ends.", 700, left=72),
                    line("Right column starts.", 80, left=320),
                ],
                [],
                792,
                charted=False,
            )
            is None
        )

    def test_letters_one_over_the_other_are_text_on_its_side(self):
        runs = [
            Run(x, 400 + 9 * i, x + 4, 408 + 9 * i, ch, 8.0, False)
            for x in (100, 140)
            for i, ch in enumerate("11/1/24")
        ]
        assert (
            _hard([line("A chart of the year's trading revenue.", 100)], runs, 792, charted=False)
            == "rotated"
        )

    def test_a_scale_is_figures_at_even_steps_going_up_from_zero_by_a_round_step(self):
        from vectrixdb.extract.pdf_text import _scale_ticks

        axis = [
            Run(60, 400 - 30 * i, 90, 408 - 30 * i, f"{1000 * i:,}" if i else "0", 8.0, False)
            for i in range(9)
        ]
        assert _scale_ticks(axis) == {float(1000 * i) for i in range(9)}
        table = [
            Run(300, 100 + 14 * i, 340, 108 + 14 * i, f"{v:,}", 8.0, False)
            for i, v in enumerate((14500, 6186, 20686, 13828, 5962))
        ]
        assert _scale_ticks(table) == set(), "a table's column of figures is no scale"
        years = [
            Run(100 + 50 * i, 420, 125 + 50 * i, 428, str(2019 + i), 8.0, False) for i in range(6)
        ]
        assert _scale_ticks(years) == set(), (
            "years along an axis are labels, and start from no zero"
        )
        labelled = axis + [Run(200, 150, 225, 158, "7,000", 8.0, False)]
        assert 7000.0 not in _scale_ticks(labelled), (
            "a figure also printed as a bar's value is not only a tick"
        )

    def test_prose_and_a_table_are_not(self):
        lines = [
            line("The bank grew its deposits in every region.", 100),
            line("Deposits 1,200 1,100", 140),
            line("Loans 900 850", 160),
        ]
        assert _hard(lines, [], 792, charted=False) is None


# ============================================================= through load() ===


BRIEF = "The year in brief, as the board reported it to its shareholders."
SEEN = f"{BRIEF}\n\n- 4.6% 2025 dividend yield\n- 25.9% total shareholder return"


def report(tmp_path, chart=False):
    """Two pages: big figures set apart from their labels, then plain prose."""
    fitz = pytest.importorskip("fitz")
    pytest.importorskip("pypdfium2")
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 90), BRIEF, fontsize=10)
    page.insert_text((72, 160), "4.6%", fontsize=28)
    page.insert_text((72, 185), "2025 Dividend Yield", fontsize=10)
    page.insert_text((72, 240), "25.9%", fontsize=28)
    page.insert_text((72, 265), "Total Shareholder Return", fontsize=10)
    if chart:
        for i, value in enumerate((300, 240, 180, 120)):
            x = 250 + i * 70
            page.draw_rect(fitz.Rect(x, 500 - value / 2, x + 40, 500), color=None, fill=(0, 0.5, 0))
            page.insert_text((x + 8, 495 - value / 2), f"{value}", fontsize=9)
    page = doc.new_page(width=612, height=792)
    for i in range(5):
        page.insert_text(
            (72, 100 + 14 * i),
            f"Sentence {i} is about the bank's deposits and loans in the year.",
            fontsize=10,
        )
    path = tmp_path / "report.pdf"
    doc.save(path)
    doc.close()
    return path


class TestThroughTheLoader:
    def test_the_unsure_page_is_read_by_sight_and_the_rest_by_the_rules(self, tmp_path):
        from vectrixdb.ingest import load

        asked = []

        def sight(png, layer, context):
            asked.append(context["page"])
            assert (
                png.startswith(b"\x89PNG") and "4.6%" in layer and context["name"] == "report.pdf"
            )
            return SEEN

        doc = load(report(tmp_path), page_reader=sight)
        assert asked == [1], "the prose page is read by the rules, and costs nothing"
        assert "- 4.6% 2025 dividend yield" in doc.text and "Sentence 3 is about" in doc.text
        assert (
            doc.metadata["pages_read_by_sight"] == [1] and doc.page_at(doc.text.index("4.6%")) == 1
        )
        assert doc.page_at(doc.text.index("Sentence 3")) == 2

    def test_a_reader_that_fails_leaves_the_page_as_the_rules_read_it(self, tmp_path):
        from vectrixdb.ingest import load

        def down(png, layer, context):
            raise RuntimeError("the service is down")

        doc = load(report(tmp_path), page_reader=down)
        assert "4.6%" in doc.text and doc.metadata["pages_read_by_sight"] == []
        assert doc.metadata["pages_kept_by_rules"] == {"1": "no reading"}

    def test_a_reading_that_invents_numbers_is_not_taken(self, tmp_path):
        from vectrixdb.ingest import load

        invented = f"{BRIEF}\n\n- 4.6% 2025 dividend yield, up from 4.1%\n- 25.9% total shareholder return against 18.2% and 12.7%"
        doc = load(report(tmp_path), page_reader=lambda png, layer, context: invented)
        assert doc.metadata["pages_kept_by_rules"] == {
            "1": "numbers or words the page does not hold"
        }
        assert "18.2%" not in doc.text and "4.1%" not in doc.text

    def test_every_page_when_asked(self, tmp_path):
        from vectrixdb.ingest import load

        asked = []
        load(
            report(tmp_path),
            page_reader=lambda png, layer, context: asked.append(context["page"]),
            every_page=True,
        )
        assert sorted(asked) == [1, 2]

    def test_a_page_read_by_sight_takes_its_pictures_with_it(self, tmp_path):
        from vectrixdb.ingest import load

        path = report(tmp_path, chart=True)
        assert "p1-chart1.png" in load(path, images=True).images, (
            "drawn as a picture when the rules read the page"
        )
        doc = load(
            path,
            images=True,
            page_reader=lambda png, layer, context: (
                SEEN + "\n\n[Figure: Four bars falling]\nValue: 300"
            ),
        )
        assert "p1-chart1.png" not in doc.images and "[Figure: Four bars falling]" in doc.text
