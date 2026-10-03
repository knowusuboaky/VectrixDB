"""Pictures described in detail when a model that can see is there, and as well as possible when it is not.

A chat model that can see writes the detailed description; Azure Vision a
caption, the parts and the words; OCR the words alone. ``Fallback`` asks
them in that order for each picture, so one refusal or one throttled call
costs that picture a plainer description and never the document. Who wrote
each description is recorded, so the plainer ones can be found and done
again. Every service here is a fake; nothing is called.
"""

from __future__ import annotations

import base64
import io
import json
import logging

import pytest

from vectrixdb.exceptions import DependencyError
from vectrixdb.extract.describers import (
    INSTRUCTION,
    ChatDescriber,
    Fallback,
    WordsOnly,
    _picture,
    describer_from_environment,
)
from vectrixdb.ingest import LoadedDocument, describe_figures, prepare_document

AZURE = "https://o.openai.azure.com/"
CONTEXT = {
    "caption": "",
    "name": "td-annual-report-2025.pdf",
    "src": "p4-fig1.jp2",
    "page": 4,
    "page_label": "2",
    "heading": "Group President and CEO's Message",
    "before": "Dear shareholders, I am very pleased to report on our progress in 2025.",
    "after": "Financial performance and shareholder value",
}
DETAILED = {
    "kind": "photograph",
    "caption": "Portrait of a smiling man in a navy suit in front of a green TD sign.",
    "description": "A colour photograph, landscape, taken outdoors.\nA man in a dark navy suit, a white shirt and a green\n tie stands at the left, smiling.",
    "words": ["TD", "TD", " "],
    "table": None,
    "decorative": False,
}


def png(width=60, height=40, noisy=False):
    """A PNG. A plain one is blank to describe_figures, which drops it unasked, so a figure's is noise."""
    Image = pytest.importorskip("PIL.Image")
    import os

    buffer = io.BytesIO()
    picture = (
        Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
        if noisy
        else Image.new("RGB", (width, height), (20, 120, 60))
    )
    picture.save(buffer, "PNG")
    return buffer.getvalue()


def completion(content, status=200, headers=None):
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
    return status, headers or {}, json.dumps(body).encode()


class Service:
    """A chat completions route: answers in turn, and every request kept."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, method, url, headers, body, timeout):
        self.requests.append(
            {"method": method, "url": url, "headers": dict(headers), "body": json.loads(body)}
        )
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def refused(message, status=400, headers=None):
    return status, headers or {}, json.dumps({"error": {"message": message}}).encode()


def asking(service, **options):
    return ChatDescriber.azure_openai(
        AZURE, "gpt-4o", key="the-key", transport=service, max_wait=0, **options
    )


# ================================================================ the request ===


class TestWhatTheModelIsSent:
    def test_an_azure_openai_deployment(self):
        service = Service(completion(DETAILED))
        asking(service)(png(), CONTEXT)
        sent = service.requests[0]
        assert (
            sent["url"]
            == "https://o.openai.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=2024-10-21"
        )
        assert sent["headers"]["api-key"] == "the-key" and "Authorization" not in sent["headers"]
        body = sent["body"]
        assert "model" not in body, "the deployment is the model"
        assert (body["temperature"], body["response_format"], body["max_tokens"]) == (
            0,
            {"type": "json_object"},
            1500,
        )
        system, user = body["messages"]
        assert system["role"] == "system" and "Never say who a person is" in system["content"]
        text, image = user["content"]
        assert (
            image["image_url"]["url"].startswith("data:image/png;base64,")
            and image["image_url"]["detail"] == "high"
        )
        for said in (
            "File: td-annual-report-2025.pdf",
            "Page: 4, printed as 2",
            "Section: Group President",
            "Caption in the document: none",
            "Dear shareholders",
        ):
            assert said in text["text"], said

    def test_any_openai_style_route(self):
        service = Service(completion(DETAILED))
        ChatDescriber(
            "http://localhost:11434/v1/chat/completions", model="llava", key="k", transport=service
        )(png(), CONTEXT)
        sent = service.requests[0]
        assert sent["headers"]["Authorization"] == "Bearer k" and sent["body"]["model"] == "llava"

    def test_a_managed_identity_sends_a_fresh_token(self):
        tokens = iter(["first", "second"])
        service = Service(completion(DETAILED), completion(DETAILED))
        describer = ChatDescriber.azure_openai(
            AZURE, "gpt-4o", token=lambda: next(tokens), transport=service
        )
        describer(png(), CONTEXT)
        describer(png(), CONTEXT)
        assert [r["headers"]["Authorization"] for r in service.requests] == [
            "Bearer first",
            "Bearer second",
        ]
        assert all("api-key" not in r["headers"] for r in service.requests)

    def test_the_key_is_not_in_its_repr(self):
        assert "the-key" not in repr(asking(Service()))

    def test_the_language_can_be_named(self):
        service = Service(completion(DETAILED))
        asking(service, language="French")(png(), CONTEXT)
        assert service.requests[0]["body"]["messages"][0]["content"].endswith("Write in French.")

    def test_the_instruction_asks_for_the_whole_checklist(self):
        for asked in (
            '"words"',
            '"table"',
            '"decorative"',
            "every person",
            "for a chart",
            "exactly as written",
        ):
            assert asked in INSTRUCTION, asked


# ================================================================ the answer ===


class TestWhatComesBack:
    def test_a_detailed_description_with_the_words_in_the_picture(self):
        said = asking(Service(completion(DETAILED)))(png(), CONTEXT)
        assert said["description"] == (
            "A colour photograph, landscape, taken outdoors. A man in a dark navy suit, a white shirt and a green tie "
            "stands at the left, smiling.\nWords in the picture: TD."
        )
        assert (
            said["caption"]
            == "Portrait of a smiling man in a navy suit in front of a green TD sign"
        )
        assert said["by"] == "gpt-4o" and "table" not in said

    def test_the_documents_own_caption_is_kept(self):
        said = asking(Service(completion(DETAILED)))(
            png(), {**CONTEXT, "caption": "Figure 3: Our leadership"}
        )
        assert "caption" not in said

    def test_a_chart_comes_back_with_its_values_as_rows(self):
        chart = {
            "kind": "chart",
            "caption": "Donut chart of revenue by business segment",
            "description": "A donut chart in four segments.",
            "words": ["37%", "26%"],
            "table": [
                ["Segment", "Share of revenue"],
                ["Canadian Personal & Commercial Banking", "37%"],
                ["Wealth Management & Insurance", 26],
                ["", None],
            ],
        }
        said = asking(Service(completion(chart)))(png(), CONTEXT)
        assert said["table"] == [
            ["Segment", "Share of revenue"],
            ["Canadian Personal & Commercial Banking", "37%"],
            ["Wealth Management & Insurance", "26"],
        ]
        rows = asking(
            Service(
                completion({**chart, "table": [["a", "b"], *[[str(i), "x"] for i in range(100)]]})
            ),
            max_rows=5,
        )(png(), CONTEXT)["table"]
        assert len(rows) == 6, "the header and five rows"

    def test_a_chart_row_with_no_value_is_left_out_and_a_tables_is_kept(self):
        chart = {
            "kind": "chart",
            "caption": "Dividend history bar chart",
            "description": "Bars rising from 2016 to 2025, only the first and last labelled.",
            "table": [
                ["Year", "Dividend"],
                ["2016", "$2.16"],
                ["2017", ""],
                ["2018", None],
                ["2025", "$4.20"],
            ],
        }
        said = asking(Service(completion(chart)))(png(), CONTEXT)
        assert said["table"] == [["Year", "Dividend"], ["2016", "$2.16"], ["2025", "$4.20"]], (
            "a year with no value printed says nothing"
        )
        nothing = {**chart, "table": [["Year", "Dividend"], ["2017", ""], ["2018", ""]]}
        assert "table" not in asking(Service(completion(nothing)))(png(), CONTEXT)
        table = {
            **chart,
            "kind": "table",
            "table": [["Item", "2025"], ["Current assets", ""], ["Cash", "120"]],
        }
        kept = asking(Service(completion(table)))(png(), CONTEXT)["table"]
        assert kept == [["Item", "2025"], ["Current assets", ""], ["Cash", "120"]], (
            "in a table, a label alone heads the rows under it"
        )

    def test_a_chart_value_the_model_says_it_guessed_is_no_value(self):
        chart = {
            "kind": "chart",
            "description": "Two bars for each segment, 2025 higher.",
            "table": [
                ["Series", "2024", "2025"],
                ["Net income", "about 7,100", "approximately 7,200"],
                ["Deposits", "310", "~450"],
            ],
        }
        said = asking(Service(completion(chart)))(png(), CONTEXT)
        assert said["table"] == [["Series", "2024", "2025"], ["Deposits", "310", "~450"]], (
            "a sign may be printed; a word says it was guessed"
        )

    def test_empty_chart_rows_do_not_crowd_out_the_real_ones(self):
        rows = (
            [["Year", "Value"]]
            + [[str(2000 + i), ""] for i in range(50)]
            + [["2050", "7"], ["2051", "9"]]
        )
        said = asking(
            Service(completion({"kind": "chart", "description": "A long chart.", "table": rows})),
            max_rows=5,
        )(png(), CONTEXT)
        assert said["table"] == [["Year", "Value"], ["2050", "7"], ["2051", "9"]]

    def test_a_decorative_picture_is_said_to_be(self):
        assert asking(Service(completion({"decorative": True, "caption": "a line"})))(
            png(), CONTEXT
        ) == {"decorative": True, "by": "gpt-4o"}

    def test_prose_in_place_of_json_is_still_a_description(self):
        said = asking(Service(completion("A bar chart of revenue\nby region.")))(png(), CONTEXT)
        assert said == {"description": "A bar chart of revenue by region.", "by": "gpt-4o"}

    def test_json_in_a_fence_is_read(self):
        said = asking(
            Service(completion("Here it is:\n```json\n" + json.dumps(DETAILED) + "\n```"))
        )(png(), CONTEXT)
        assert said["caption"].startswith("Portrait")

    def test_a_long_one_is_cut_after_a_sentence(self):
        long = {**DETAILED, "description": "The sky is blue. " * 200, "words": ["TD"]}
        said = asking(Service(completion(long)), max_chars=300)(png(), CONTEXT)
        assert len(said["description"]) <= 300
        assert said["description"].endswith("Words in the picture: TD."), (
            "the words survive the cut"
        )
        assert said["description"].split("\n")[0].endswith("blue.")

    def test_a_refusal_is_no_description(self):
        """Not even when the apology comes back as content: written under a figure it would be searchable, and wrong."""
        for content in (None, "I'm sorry, I can't help with that."):
            refusal = (
                200,
                {},
                json.dumps(
                    {
                        "choices": [
                            {"message": {"content": content, "refusal": "I cannot help with that."}}
                        ]
                    }
                ).encode(),
            )
            assert asking(Service(refusal))(png(), CONTEXT) is None

    def test_an_answer_with_nothing_in_it_is_no_description(self):
        assert (
            asking(Service(completion({"kind": "other", "description": "", "words": []})))(
                png(), CONTEXT
            )
            is None
        )


# ================================================================ when the service says no ===


class TestWhenTheServiceSaysNo:
    def test_throttled_it_waits_as_told_and_asks_again(self):
        service = Service(refused("Rate limit", 429, {"Retry-After": "7"}), completion(DETAILED))
        assert asking(service)(png(), CONTEXT)["by"] == "gpt-4o"
        assert len(service.requests) == 2

    def test_how_long_it_waits(self):
        describer = ChatDescriber("https://x.example/v1/chat/completions", max_wait=20)
        assert describer._wait({"Retry-After": "7"}, 1) == 7
        assert describer._wait({"retry-after-ms": "1500"}, 1) == 1.5
        assert [describer._wait({}, attempt) for attempt in (1, 2, 3, 9)] == [1, 2, 4, 20]
        assert describer._wait({"Retry-After": "600"}, 1) == 20

    def test_still_down_after_its_tries_it_gives_up_without_raising(self, caplog):
        service = Service(refused("busy", 503), refused("busy", 503), refused("busy", 503))
        with caplog.at_level(logging.WARNING):
            assert asking(service)(png(), CONTEXT) is None
        assert (
            len(service.requests) == 3 and "gpt-4o did not describe a picture: 503" in caplog.text
        )

    def test_no_answer_at_all_is_waited_for_too(self):
        service = Service(ConnectionError("reset"), completion(DETAILED))
        assert asking(service)(png(), CONTEXT) is not None and len(service.requests) == 2

    def test_a_model_that_wants_max_completion_tokens_gets_it_from_then_on(self):
        said = "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead."
        service = Service(refused(said), completion(DETAILED), completion(DETAILED))
        describer = asking(service)
        describer(png(), CONTEXT)
        describer(png(), CONTEXT)
        assert "max_tokens" in service.requests[0]["body"]
        assert [
            ("max_tokens" in r["body"], r["body"].get("max_completion_tokens"))
            for r in service.requests[1:]
        ] == [(False, 1500), (False, 1500)]

    def test_calls_made_side_by_side_are_all_asked_again_with_the_setting_put_right(self):
        """Four pages read at once all go out with max_tokens; the first refusal back fixes it for all four, not one."""
        import threading
        from concurrent.futures import ThreadPoolExecutor

        said = "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead."
        together = threading.Barrier(4)
        answered = []

        def transport(method, url, headers, body, timeout):
            sent = json.loads(body)
            if "max_tokens" in sent:
                together.wait(timeout=10)  # every call is on its way before any refusal is back
                return refused(said)
            answered.append(sent["max_completion_tokens"])
            return completion(DETAILED)

        describer = asking(transport)
        with ThreadPoolExecutor(4) as pool:
            said_back = list(pool.map(lambda _n: describer(png(), CONTEXT), range(4)))
        assert all(said_back) and answered == [1500] * 4

    def test_a_model_that_takes_no_temperature_is_asked_without_one(self):
        said = "Unsupported value: 'temperature' does not support 0 with this model. Only the default (1) value is supported."
        service = Service(refused(said), completion(DETAILED))
        assert asking(service)(png(), CONTEXT) is not None
        assert "temperature" not in service.requests[1]["body"]

    def test_a_refusal_is_remembered_by_the_next_route_to_the_same_model(self):
        said = "Unsupported value: 'temperature' does not support 0 with this model. Only the default (1) value is supported."
        first = Service(refused(said), completion(DETAILED))
        assert asking(first)(png(), CONTEXT) is not None
        second = Service(completion(DETAILED))
        assert asking(second)(png(), CONTEXT) is not None
        assert len(second.requests) == 1 and "temperature" not in second.requests[0]["body"]

    def test_a_busy_service_is_waited_for_as_long_as_it_says_up_to_a_minute(self):
        from vectrixdb._chat import ChatRoute

        route = ChatRoute("https://example.test/v1/chat/completions")
        assert route._wait({"Retry-After": "45"}, 1) == 45.0
        assert route._wait({"retry-after": "600"}, 1) == 60.0

    def test_a_server_that_takes_no_response_format_is_asked_without_one(self):
        service = Service(
            refused("response_format is not supported by this server"),
            completion(json.dumps(DETAILED)),
        )
        assert asking(service)(png(), CONTEXT)["caption"].startswith("Portrait")
        assert "response_format" not in service.requests[1]["body"]

    def test_a_filtered_picture_is_not_asked_about_again(self):
        service = Service(
            refused(
                "The response was filtered due to the prompt triggering the content management policy."
            )
        )
        assert asking(service)(png(), CONTEXT) is None and len(service.requests) == 1

    def test_a_host_with_no_identity_to_give_is_no_answer_not_an_error(self):
        def no_identity():
            raise RuntimeError("DefaultAzureCredential failed to retrieve a token")

        service = Service()
        describer = ChatDescriber.azure_openai(
            AZURE, "gpt-4o", token=no_identity, transport=service, max_wait=0
        )
        assert describer(png(), CONTEXT) is None and service.requests == []


# ================================================================ the picture sent ===


class TestThePictureSent:
    def test_one_a_model_reads_goes_as_it_came(self):
        picture = png()
        assert _picture(picture, 2048) == (picture, "image/png")

    def test_a_large_one_is_made_smaller(self):
        Image = pytest.importorskip("PIL.Image")
        data, mime = _picture(png(3000, 1000), 2048)
        assert mime == "image/png" or mime == "image/jpeg"
        assert max(Image.open(io.BytesIO(data)).size) == 2048

    def test_jpeg_2000_is_turned_into_one_a_model_reads(self):
        Image = pytest.importorskip("PIL.Image")
        features = pytest.importorskip("PIL.features")
        if not features.check("jpg_2000"):
            pytest.skip("this Pillow writes no JPEG 2000")
        buffer = io.BytesIO()
        Image.new("RGB", (40, 30), (10, 200, 90)).save(buffer, "JPEG2000")
        data, mime = _picture(buffer.getvalue(), 2048)
        assert mime == "image/jpeg" and Image.open(io.BytesIO(data)).format == "JPEG"

    def test_a_printers_cmyk_jpeg_is_turned_into_rgb(self):
        Image = pytest.importorskip("PIL.Image")
        buffer = io.BytesIO()
        Image.new("CMYK", (40, 30), (0, 255, 255, 0)).save(buffer, "JPEG")
        data, mime = _picture(buffer.getvalue(), 2048)
        assert mime == "image/jpeg" and Image.open(io.BytesIO(data)).mode == "RGB"

    def test_what_is_not_a_picture_is_not_sent(self):
        service = Service()
        assert asking(service)(b"not a picture", CONTEXT) is None and service.requests == []

    def test_the_bytes_sent_are_the_picture(self):
        picture = png()
        service = Service(completion(DETAILED))
        asking(service)(picture, CONTEXT)
        url = service.requests[0]["body"]["messages"][1]["content"][1]["image_url"]["url"]
        assert base64.b64decode(url.split(",", 1)[1]) == picture


# ================================================================ the words alone ===


class Reader:
    label = "azure-document-intelligence"

    def __init__(self, text="Revenue\nQ1  2025", fail=None):
        self.text, self.fail, self.names = text, fail, []

    def __call__(self, data, name):
        self.names.append(name)
        if self.fail:
            raise self.fail
        return LoadedDocument(text=self.text)


class TestTheWordsAlone:
    def test_the_words_are_the_description(self):
        said = WordsOnly(Reader())(png(), CONTEXT)
        assert said == {
            "description": "The words in it read: Revenue; Q1 2025.",
            "by": "ocr:azure-document-intelligence",
        }

    def test_a_picture_with_no_words_gets_no_description(self):
        assert WordsOnly(Reader(text="  \n"))(png(), CONTEXT) is None

    def test_the_reader_is_given_the_pictures_own_name(self):
        reader = Reader()
        WordsOnly(reader)(png(), CONTEXT)
        assert reader.names == ["p4-fig1.png"], "not the document's name, which would read as a PDF"

    def test_a_reader_that_is_not_installed_is_not_asked_again(self):
        reader = Reader(fail=DependencyError("rapidocr-onnxruntime", "ocr"))
        words = WordsOnly(reader)
        assert words(png(), CONTEXT) is None and words(png(), CONTEXT) is None
        assert len(reader.names) == 1 and not words.available


# ================================================================ the chain ===


class Says:
    def __init__(self, label, answer=None, fail=None):
        self.label, self.answer, self.fail, self.asked = label, answer, fail, 0

    def __call__(self, image, context):
        self.asked += 1
        if self.fail:
            raise self.fail
        return self.answer


class TestTheChain:
    def test_the_first_with_an_answer_describes_it(self):
        model, vision = (
            Says("gpt-4o", {"description": "Detailed."}),
            Says("azure-image-analysis", {"description": "Plain."}),
        )
        assert Fallback(model, vision)(png(), CONTEXT) == {
            "description": "Detailed.",
            "by": "gpt-4o",
        }
        assert vision.asked == 0

    def test_one_with_nothing_to_say_hands_it_on(self):
        said = Fallback(Says("gpt-4o", None), Says("azure-image-analysis", {"caption": "a man"}))(
            png(), CONTEXT
        )
        assert said == {"caption": "a man", "by": "azure-image-analysis"}

    def test_one_that_fails_hands_it_on_and_the_log_says_so(self, caplog):
        with caplog.at_level(logging.WARNING):
            said = Fallback(
                Says("gpt-4o", fail=RuntimeError("quota")), Says("azure-image-analysis", "Plain.")
            )(png(), CONTEXT)
        assert said == {"description": "Plain.", "by": "azure-image-analysis"}
        assert "gpt-4o could not describe p4-fig1.jp2: quota" in caplog.text

    def test_nobody_with_an_answer_is_none(self):
        assert Fallback(Says("a", None), Says("b", "  "))(png(), CONTEXT) is None

    def test_a_decorative_answer_is_an_answer(self):
        assert Fallback(
            Says("gpt-4o", {"decorative": True}), Says("azure-image-analysis", "Plain.")
        )(png(), CONTEXT)["decorative"]

    def test_the_ones_that_see(self):
        chain = Fallback(Says("gpt-4o"), None, Says("azure-image-analysis"), WordsOnly(Reader()))
        assert chain.labels == ["gpt-4o", "azure-image-analysis", "ocr:azure-document-intelligence"]
        assert chain.seeing.labels == ["gpt-4o", "azure-image-analysis"]
        assert Fallback(WordsOnly(Reader())).seeing is None


class TestBuiltFromTheSettings:
    VISION = {
        "AZURE_VISION_ENDPOINT": "https://v.cognitiveservices.azure.com",
        "AZURE_VISION_KEY": "k",
    }
    MODEL = {
        "AZURE_OPENAI_ENDPOINT": "https://o.openai.azure.com",
        "AZURE_OPENAI_KEY": "k",
        "AZURE_OPENAI_VISION_DEPLOYMENT": "gpt-4o",
    }

    def test_best_first(self):
        chain = describer_from_environment({**self.VISION, **self.MODEL}, reader=Reader())
        assert chain.labels == ["gpt-4o", "azure-image-analysis", "ocr:azure-document-intelligence"]

    def test_vision_and_the_words_as_today(self):
        assert describer_from_environment(self.VISION, reader=Reader()).labels == [
            "azure-image-analysis",
            "ocr:azure-document-intelligence",
        ]

    def test_nothing_that_sees_is_none_and_no_picture_is_opened(self):
        assert describer_from_environment({}, reader=Reader()) is None

    def test_any_openai_style_route(self):
        made = ChatDescriber.from_environment(
            {
                "VECTRIXDB_DESCRIBER_URL": "http://localhost:8000/v1/chat/completions",
                "VECTRIXDB_DESCRIBER_MODEL": "qwen2-vl",
                "VECTRIXDB_DESCRIBER_KEY": "k",
            }
        )
        assert (
            made.label == "qwen2-vl"
            and made.model == "qwen2-vl"
            and made._auth() == {"Authorization": "Bearer k"}
        )

    def test_a_deployment_with_its_own_api_version(self):
        made = ChatDescriber.from_environment(
            {**self.MODEL, "AZURE_OPENAI_API_VERSION": "2025-04-01-preview"}
        )
        assert made.url.endswith("?api-version=2025-04-01-preview")


# ================================================================ in the document ===


def with_a_picture(caption="p4-fig1.jp2", src="p4-fig1.jp2"):
    text = f"Dear shareholders.\n\n[Figure: {caption}]\n\nFinancial performance."
    doc = LoadedDocument(
        text=text,
        pages=[(0, 4)],
        metadata={"filename": "report.pdf"},
        page_labels={4: "2"},
        figures=[(text.index("[Figure"), {"caption": caption, "src": src, "described": False})],
    )
    doc.images = {src: png(120, 90, noisy=True)}
    return doc


class TestInTheDocument:
    def test_a_file_name_is_no_caption_and_the_describers_takes_its_place(self):
        seen = []
        described = describe_figures(
            with_a_picture(),
            lambda data, context: (
                seen.append(dict(context))
                or {
                    "caption": "Portrait of a smiling man",
                    "description": "A photograph.",
                    "by": "gpt-4o",
                }
            ),
        )
        assert seen[0]["caption"] == "" and seen[0]["src"] == "p4-fig1.jp2"
        assert (seen[0]["page"], seen[0]["page_label"]) == (4, "2")
        assert "[Figure: Portrait of a smiling man]\nA photograph." in described.text

    def test_a_documents_own_caption_is_given_and_kept(self):
        seen = []
        described = describe_figures(
            with_a_picture(caption="Figure 3: Our leadership"),
            lambda data, context: seen.append(dict(context)) or {"description": "A photograph."},
        )
        assert (
            seen[0]["caption"] == "Figure 3: Our leadership"
            and "[Figure: Figure 3: Our leadership]" in described.text
        )

    def test_who_described_it_is_recorded(self):
        described = describe_figures(
            with_a_picture(),
            Fallback(Says("gpt-4o", None), Says("azure-image-analysis", {"description": "Plain."})),
        )
        assert described.figures[0][1]["described_by"] == "azure-image-analysis"
        assert described.metadata["figures_described_by"] == {"azure-image-analysis": 1}

    def test_one_nobody_could_describe_is_counted_too(self):
        described = describe_figures(with_a_picture(), Fallback(Says("gpt-4o", None)))
        assert described.metadata["figures_described_by"] == {"undescribed": 1}
        assert "[Figure: p4-fig1.jp2]" in described.text

    def test_the_count_is_about_the_document_and_not_on_every_chunk(self):
        described = describe_figures(
            with_a_picture(), Says("gpt-4o", {"description": "A photograph."})
        )
        prepared = prepare_document(
            described, "report.pdf", chunk="recursive", chunk_size=1000, overlap=0
        )
        assert all("figures_described_by" not in m for m in prepared.metadata)

    def test_pieces_of_a_long_file_add_their_counts_up(self):
        from vectrixdb.extract.batches import _merge

        whole = {}
        _merge(whole, {"figures_described_by": {"gpt-4o": 2}}, 0)
        _merge(whole, {"figures_described_by": {"gpt-4o": 1, "azure-image-analysis": 1}}, 20)
        assert whole["figures_described_by"] == {"gpt-4o": 3, "azure-image-analysis": 1}


# ================================================================ the extraction app ===


class TestTheExtractionApp:
    MODEL = TestBuiltFromTheSettings.MODEL
    VISION = TestBuiltFromTheSettings.VISION

    def test_its_settings_build_the_chain_behind_its_own_reader(self):
        from vectrixdb.api.extraction import ExtractionService

        reader = Reader()
        service = ExtractionService.from_environment(
            {**self.VISION, **self.MODEL}, image=reader, jobs=None
        )
        assert service.describer.labels == [
            "gpt-4o",
            "azure-image-analysis",
            "ocr:azure-document-intelligence",
        ]
        assert service.describer.describers[-1].reader is reader

    def test_a_describer_it_is_given_is_the_one_it_uses(self):
        from vectrixdb.api.extraction import ExtractionService

        mine = Says("mine", "Mine.")
        assert (
            ExtractionService.from_environment(
                {**self.VISION}, image=Reader(), describer=mine, jobs=None
            ).describer
            is mine
        )

    def test_an_image_file_is_not_read_for_its_words_twice(self):
        from vectrixdb.api.extraction import ExtractionService

        reader = Reader(text="Total assets 1200")
        service = ExtractionService(
            image=reader, describer=Fallback(Says("gpt-4o", None), WordsOnly(reader))
        )
        doc = service.read_image(png(), "scan.png")
        assert reader.names == ["scan.png"], "the words-only last resort was not asked"
        assert doc.text == "Total assets 1200"

    def test_a_documents_picture_comes_back_described_in_detail(self, monkeypatch):
        pytest.importorskip("docx")
        from fastapi.testclient import TestClient

        from vectrixdb.api.extraction import ExtractionService, create_extraction_app

        monkeypatch.delenv("VECTRIXDB_API_KEY", raising=False)
        document = __import__("docx").Document()
        document.add_paragraph("Revenue grew in every region.")
        document.add_picture(io.BytesIO(png(120, 90, noisy=True)))
        document.add_paragraph("Unaudited.")
        buffer = io.BytesIO()
        document.save(buffer)
        service = Service(completion(DETAILED))
        app = TestClient(
            create_extraction_app(
                ExtractionService(describer=Fallback(asking(service))), allow_open=True
            )
        )
        body = app.post(
            "/extract/docx",
            content=buffer.getvalue(),
            headers={"X-Filename": "report.docx", "Accept": "application/json"},
        ).json()
        assert (
            "[Figure: Portrait of a smiling man in a navy suit in front of a green TD sign]"
            in body["text"]
        )
        assert "Words in the picture: TD." in body["text"]
        assert body["metadata"]["figures_described_by"] == {"gpt-4o": 1}
        assert "images" not in body, "descriptions travel, pictures do not"
        assert [info["described_by"] for _at, info in body["figures"]] == ["gpt-4o"], (
            "and who wrote each"
        )
