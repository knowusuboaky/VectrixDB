"""Azure AI Vision as a figure describer. The service is a fake throughout.

What matters here is not that it calls an endpoint. It is that what comes
back is put through the same treatment a scanned page gets: read in the order
a person reads it, mended where a space was lost, and handed to the library
as the parts it knows what to do with, rather than as one string the caller
already flattened.
"""

from __future__ import annotations

import pytest

from vectrixdb.extract.engines import AzureImageAnalysis
from vectrixdb.extract.layout import reading_order
from vectrixdb.ingest import LoadedDocument, describe_figures


def box(left, top, right, bottom):
    """A polygon the way Image Analysis writes one: four corners, each a map."""
    return [{"x": left, "y": top}, {"x": right, "y": top}, {"x": right, "y": bottom}, {"x": left, "y": bottom}]


def reply(caption=None, dense=(), lines=()):
    answer = {"readResult": {"blocks": [{"lines": [{"text": t, "boundingPolygon": b} for b, t in lines]}]}}
    if caption is not None:
        answer["captionResult"] = {"text": caption[0], "confidence": caption[1]}
    if dense:
        answer["denseCaptionsResult"] = {"values": [{"text": t, "confidence": c} for t, c in dense]}
    return answer


def describer(answer, **options):
    return AzureImageAnalysis("https://v.cognitiveservices.azure.com", "a-secret-key", transport=lambda url, image: answer, **options)


class TestACornerCanBeAMap:
    """Azure writes a corner as {"x": .., "y": ..}; RapidOCR writes a pair."""

    def test_reading_order_takes_both_spellings(self):
        as_maps = [(box(0, 0, 60, 10), "Revenue by quarter"), (box(0, 40, 25, 50), "Q1")]
        as_pairs = [([(0, 0), (60, 0), (60, 10), (0, 10)], "Revenue by quarter"), ([(0, 40), (25, 40), (25, 50), (0, 50)], "Q1")]
        assert reading_order(as_maps) == reading_order(as_pairs) == ["Revenue by quarter", "Q1"]

    def test_a_plain_rectangle_still_works(self):
        assert reading_order([((0, 0, 60, 10), "Revenue by quarter")]) == ["Revenue by quarter"]


class TestTheWordsAreReadInOrder:
    def test_what_the_service_emitted_last_can_be_read_first(self):
        """The whole reason for the boxes: emission order is not reading order."""
        asking = describer(
            reply(lines=[(box(80, 40, 95, 50), "Q4"), (box(0, 0, 60, 10), "Revenue by quarter"), (box(10, 40, 25, 50), "Q1")])
        )
        said = asking(b"PNG", {})["description"]
        assert said == "The words in it read: Revenue by quarter; Q1 Q4."

    def test_a_line_that_lost_its_space_is_mended(self):
        asking = describer(reply(lines=[(box(0, 0, 80, 10), "fees.Interest is charged monthly")]))
        assert "fees. Interest" in asking(b"PNG", {})["description"]

    def test_a_line_with_no_box_is_kept_where_it_came(self):
        answer = {"readResult": {"blocks": [{"lines": [{"text": "first"}, {"text": "second"}]}]}}
        assert describer(answer)(b"PNG", {})["description"] == "The words in it read: first; second."

    def test_a_wall_of_words_is_cut_to_what_a_chunk_can_hold(self):
        lines = [(box(0, i * 10, 50, i * 10 + 8), f"line {i}") for i in range(60)]
        said = describer(reply(lines=lines), most=5)(b"PNG", {})["description"]
        assert said.count(";") == 4, "five lines, four separators"


class TestWhatItSaysThePictureIs:
    def test_the_caption_leads_and_the_dense_ones_fill_it_in(self):
        asking = describer(
            reply(
                caption=("a bar chart of revenue", 0.7),
                dense=[("a bar chart of revenue", 0.7), ("a blue bar taller than the rest", 0.55)],
                lines=[(box(0, 0, 60, 10), "Revenue by quarter")],
            )
        )
        said = asking(b"PNG", {})["description"]
        assert said == (
            "A bar chart of revenue. In it: a blue bar taller than the rest. The words in it read: Revenue by quarter."
        )

    def test_the_whole_is_not_repeated_as_one_of_its_parts(self):
        asking = describer(reply(caption=("a bar chart", 0.8), dense=[("A Bar Chart", 0.8)]))
        assert "In it:" not in asking(b"PNG", {})["description"]

    def test_a_guess_is_not_written_down(self):
        """A low-confidence caption is searchable and wrong, which is worse than absent."""
        asking = describer(reply(caption=("two people on a beach", 0.05), lines=[(box(0, 0, 60, 10), "Total assets")]))
        assert asking(b"PNG", {})["description"] == "The words in it read: Total assets."

    def test_nothing_seen_and_nothing_written_is_decoration(self):
        assert describer(reply())(b"PNG", {}) == {"decorative": True}


class TestTheCaption:
    def test_a_figure_with_no_caption_is_given_one(self):
        asking = describer(reply(caption=("a bar chart of revenue", 0.7)))
        assert asking(b"PNG", {"caption": ""})["caption"] == "a bar chart of revenue"

    def test_a_figure_that_has_a_caption_keeps_it(self):
        asking = describer(reply(caption=("a bar chart of revenue", 0.7)))
        assert "caption" not in asking(b"PNG", {"caption": "Figure 12. Revenue by quarter"})


class TestWhenTheServiceWillNotPlay:
    def test_a_region_that_cannot_caption_asks_again_for_what_it_can_have(self):
        import urllib.error

        asked = []

        class Refusal(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("u", 400, "Bad Request", {}, None)

            def read(self):
                return b'{"error":{"message":"caption is not supported in this region"}}'

        asking = AzureImageAnalysis("https://v.cognitiveservices.azure.com", "k")

        def transport(url, image):
            asked.append(url)
            if "caption" in url:
                raise Refusal()
            return reply(lines=[(box(0, 0, 60, 10), "Total assets")])

        asking._transport = transport
        said = asking(b"PNG", {})
        assert said["description"] == "The words in it read: Total assets."
        assert len(asked) == 2 and "caption" not in asked[1]
        assert asking.captions is False, "it does not ask again for the next figure either"

    def test_a_service_that_will_not_answer_leaves_the_figure_alone(self):
        import urllib.error

        def refuse(url, image):
            raise urllib.error.URLError("no route to host")

        asking = AzureImageAnalysis("https://v.cognitiveservices.azure.com", "k", transport=refuse)
        assert asking(b"PNG", {"caption": "Figure 1"}) is None, "one refusal is not worth failing a document for"

    def test_the_key_is_not_in_what_gets_printed(self):
        assert "a-secret-key" not in repr(describer(reply()))

    def test_an_endpoint_that_is_not_one_is_refused_at_the_door(self):
        with pytest.raises(ValueError, match="endpoint is the Vision resource"):
            AzureImageAnalysis("", "k")


class TestItIsBuiltFromTheTwoSettings:
    def test_both_or_nothing(self):
        assert AzureImageAnalysis.from_environment({}) is None
        assert AzureImageAnalysis.from_environment({"AZURE_VISION_ENDPOINT": "https://v.cognitiveservices.azure.com"}) is None
        assert AzureImageAnalysis.from_environment({"AZURE_VISION_KEY": "k"}) is None
        made = AzureImageAnalysis.from_environment(
            {"AZURE_VISION_ENDPOINT": "https://v.cognitiveservices.azure.com", "AZURE_VISION_KEY": "k"}
        )
        assert isinstance(made, AzureImageAnalysis)

    def test_none_is_a_working_deployment_without_one(self):
        """So a host passes it straight to describe_figures without deciding first."""
        doc = LoadedDocument(text="[Figure: a.png]", figures=[(0, {"src": "a.png"})], images={"a.png": b"PNG"})
        # skip_decorative off, or the triage drops the figure and the document
        # changes for a reason that has nothing to do with the describer.
        assert describe_figures(doc, AzureImageAnalysis.from_environment({}), skip_decorative=False) is doc


class TestThroughTheLibraryThatCallsIt:
    def test_what_it_returns_is_what_describe_figures_writes(self):
        doc = LoadedDocument(
            text="[Figure: p1-fig1.png]",
            figures=[(0, {"src": "p1-fig1.png", "caption": ""})],
            images={"p1-fig1.png": b"\x89PNG\r\n\x1a\n" + bytes(4000)},
        )
        asking = describer(reply(caption=("a bar chart of revenue", 0.7), lines=[(box(0, 0, 60, 10), "Revenue by quarter")]))
        after = describe_figures(doc, asking, name="q3.pdf", skip_decorative=False)
        assert after.text == (
            "[Figure: a bar chart of revenue]\nA bar chart of revenue. The words in it read: Revenue by quarter."
        )
        assert asking.asked == 1

    def test_decorative_takes_the_figure_out_of_the_text(self):
        doc = LoadedDocument(
            text="before\n\n[Figure: p1-fig1.png]\n\nafter",
            figures=[(8, {"src": "p1-fig1.png"})],
            images={"p1-fig1.png": b"\x89PNG\r\n\x1a\n" + bytes(4000)},
        )
        after = describe_figures(doc, describer(reply()), skip_decorative=False)
        assert "[Figure:" not in after.text and after.images == {}
