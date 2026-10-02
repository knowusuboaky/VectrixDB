"""Masking before a document is indexed: the engines behind one call, and the patterns that always run after them.

What is held to. The text is folded first, so a card number in Cyrillic digits
or split by a zero-width space is still a card number. The patterns know the
shapes a model need not: social numbers, IBANs, IP addresses, keys and the
password inside a connection string. Presidio, Azure AI Language and Amazon
Comprehend each answer in the same shape, mapped to the library's type names,
and each is a fake here: no model, no network. An engine asked about a
language it does not cover finds nothing and says so. The deployment's engine
comes from the settings, a bad one is an error at start, and the health reply
says which engine loaded and which languages it covers.
"""

from __future__ import annotations

import pytest

from vectrixdb.exceptions import ConfigurationError, DependencyError
from vectrixdb.masking import (
    DEFAULT_TYPES,
    TYPES,
    ComprehendEngine,
    Found,
    LanguageEngine,
    PresidioEngine,
    RegexEngine,
    describe_engine,
    download_models,
    engine_from_env,
    languages_from_env,
    mask,
    mask_text,
    normalize,
    types_of,
)
from vectrixdb.masking.engines import pieces


# ---------------------------------------------------------------- folding ---


class TestFolding:
    def test_what_looks_like_an_identifier_is_one_after_folding(self):
        assert normalize("４１１１ １１１１ １１１１ １１１１") == "4111 1111 1111 1111", (
            "fullwidth digits"
        )
        assert normalize("ada​@example.com") == "ada@example.com", (
            "a zero-width space inside an address"
        )
        assert normalize("аdа@example.com") == "ada@example.com", "Cyrillic letters that look Latin"
        assert normalize("416‑555‑0199") == "416-555-0199", "a non-breaking hyphen"

    def test_ordinary_text_is_left_as_it_is(self):
        assert normalize("Net income rose 12% in Q3.") == "Net income rose 12% in Q3."
        assert normalize("") == ""

    def test_the_folded_text_is_what_is_masked(self):
        done = mask(
            "card ４１１１ １１１１ １１１１ １１１１", types="credit_card", engine=RegexEngine()
        )
        assert done.text == "card •••• •••• •••• 1111" and done.found[0].type == "credit_card"


# --------------------------------------------------------------- patterns ---


class TestThePatterns:
    @pytest.mark.parametrize(
        "text, kind, shown",
        [
            ("SSN 078-05-1120 on file", "ssn", "SSN [SSN] on file"),
            ("SIN 046 454 286", "sin", "SIN [SIN]"),
            ("IBAN GB82 WEST 1234 5698 7654 32", "iban", "IBAN [IBAN]"),
            ("from 10.0.0.12 today", "ip_address", "from [IP_ADDRESS] today"),
            ("key AKIAIOSFODNN7EXAMPLE", "api_key", "key [API_KEY]"),
            # Written in two halves, so a secret scanner reading this file finds no token in it.
            ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "api_key", "token [API_KEY]"),
            ("api_key = s3cr3t-value-here", "api_key", "api_key = [API_KEY]"),
            (
                "AccountName=vx;AccountKey=abcdefghijklmnop123456==;EndpointSuffix=core.windows.net",
                "connection_string",
                "AccountName=vx;AccountKey=[CONNECTION_STRING];EndpointSuffix=core.windows.net",
            ),
            (
                "postgresql://vx:hunter2hunter2@db.example.test/access",
                "connection_string",
                "postgresql://vx:[CONNECTION_STRING]@db.example.test/access",
            ),
            (
                "see wiki.corp for the runbook",
                "internal_host",
                "see [INTERNAL_HOST] for the runbook",
            ),
        ],
    )
    def test_each_shape_is_found_and_replaced_by_its_type(self, text, kind, shown):
        done = mask(text, types="all", engine=RegexEngine())
        assert [f.type for f in done.found] == [kind] and done.text == shown

    @pytest.mark.parametrize(
        "text, kind",
        [
            ("SIN 123 456 789", "sin"),
            ("IBAN GB00 WEST 0000 0000 0000 00", "iban"),
            ("from 999.1.1.1", "ip_address"),
            ("version 1.2.3.4.5", "ip_address"),
            ("order 078-05-11200", "ssn"),
        ],
    )
    def test_a_shape_that_fails_its_check_is_not_one(self, text, kind):
        assert kind not in [f.type for f in mask(text, types="all", engine=RegexEngine()).found]

    def test_the_three_shapes_on_the_way_out_are_unchanged(self):
        assert (
            mask_text("Call +1 416 555 0199 or ada@example.com, card 4111 1111 1111 1111.")
            == "Call +• ••• ••• 0199 or a•••@example.com, card •••• •••• •••• 1111."
        )
        assert (
            mask_text("key AKIAIOSFODNN7EXAMPLE on wiki.corp")
            == "key AKIAIOSFODNN7EXAMPLE on wiki.corp"
        ), "on the way out only the three shapes, as documented"

    def test_the_default_types_are_identifiers_not_names(self):
        assert (
            "name" not in DEFAULT_TYPES
            and "address" not in DEFAULT_TYPES
            and "ssn" in DEFAULT_TYPES
            and "api_key" in DEFAULT_TYPES
        )
        assert (
            types_of(None) == DEFAULT_TYPES
            and types_of(True) == DEFAULT_TYPES
            and types_of(False) == ()
        )
        assert (
            types_of("all") == TYPES
            and types_of("email, phone") == ("email", "phone")
            and types_of(["SSN", "ssn"]) == ("ssn",)
        )
        with pytest.raises(ConfigurationError, match="not a type"):
            types_of("hairstyle")

    def test_the_score_is_the_heaviest_thing_found_nudged_by_how_many(self):
        assert mask("ada@example.com", types="all", engine=RegexEngine()).score == 0.6
        assert (
            mask("SSN 078-05-1120 and ada@example.com", types="all", engine=RegexEngine()).score
            == 1.0
        )
        assert mask("nothing here", types="all", engine=RegexEngine()).score == 0.0

    def test_only_the_types_asked_for_are_masked(self):
        done = mask(
            "ada@example.com, card 4111 1111 1111 1111", types="email", engine=RegexEngine()
        )
        assert done.text == "a•••@example.com, card 4111 1111 1111 1111"


# ---------------------------------------------------------------- engines ---


class FakePresidio:
    """What presidio_analyzer's AnalyzerEngine answers, without the model."""

    def __init__(self):
        self.calls = []

    def analyze(self, text, language, entities=None):
        self.calls.append((language, entities))
        found = []
        at = text.find("Ada Mensah")
        if at >= 0:
            found.append(
                type(
                    "R", (), {"entity_type": "PERSON", "start": at, "end": at + 10, "score": 0.85}
                )()
            )
        at = text.find("12 Rue Sainte-Catherine")
        if at >= 0:
            found.append(
                type(
                    "R", (), {"entity_type": "LOCATION", "start": at, "end": at + 23, "score": 0.7}
                )()
            )
        return found


class TestOverlappingSpans:
    def test_spans_that_overlap_are_masked_as_one(self):
        """An engine's person span ran past a pattern's email span. apply() kept the earliest and
        dropped the other whole, so the tail of the later one stayed in the clear."""
        from vectrixdb.masking.patterns import apply

        text = "contact ada@corp.test today"
        #       0123456789012345678901234567
        found = [
            Found("email", 8, 21, engine="regex"),
            Found("person", 12, 27, engine="presidio", score=0.6),
        ]
        assert apply(text, found) == "contact a•••@corp.test", (
            "masked to the end of the later span, under the earlier type"
        )
        assert (
            apply(text, [Found("person", 0, 12, engine="presidio"), Found("email", 8, 21)])
            == "[PERSON] today"
        )
        assert (
            apply(text, [Found("person", 0, 12), Found("email", 8, 21), Found("other", 14, 16)])
            == "[PERSON] today"
        )


class TestPresidio:
    def test_names_and_addresses_come_from_the_model_and_the_patterns_still_run(self):
        fake = FakePresidio()
        engine = PresidioEngine(analyzer=fake, languages=("en", "fr"))
        done = mask(
            "Ada Mensah, ada@example.com, 12 Rue Sainte-Catherine",
            types="all",
            engine=engine,
            language="fr-CA",
        )
        assert done.text == "[NAME], a•••@example.com, [ADDRESS]"
        assert [(f.type, f.engine) for f in done.found] == [
            ("name", "presidio"),
            ("email", "regex"),
            ("address", "presidio"),
        ]
        assert fake.calls[0][0] == "fr" and "PERSON" in fake.calls[0][1], (
            "asked in the document's language, for the types wanted"
        )
        assert done.engine == "presidio" and not done.regex_only and done.score == 0.8

    def test_a_language_it_has_no_model_for_is_the_patterns_alone_and_says_so(self):
        engine = PresidioEngine(analyzer=FakePresidio(), languages=("en",))
        done = mask("Ada Mensah, ada@example.com", types="all", engine=engine, language="de")
        assert done.text == "Ada Mensah, a•••@example.com" and done.regex_only is True

    def test_without_the_package_the_first_use_says_what_to_install(self):
        engine = PresidioEngine(languages=("en",))
        try:
            import presidio_analyzer  # noqa: F401
        except ImportError:
            with pytest.raises(DependencyError, match="vectrixdb\\[masking\\]"):
                engine.find("Ada", ["name"], "en")

    def test_the_models_are_fetched_one_a_language(self):
        fetched = []
        assert download_models(["en", "fr-CA"], downloader=fetched.append) == [
            "en_core_web_lg",
            "fr_core_news_lg",
        ]
        assert fetched == ["en_core_web_lg", "fr_core_news_lg"]


class TestAzureLanguage:
    def transport(self, seen):
        def post(url, headers, body):
            seen.append((url, headers, body))
            text = body["analysisInput"]["documents"][0]["text"]
            entities = []
            at = text.find("Ada Mensah")
            if at >= 0:
                entities.append(
                    {"category": "Person", "offset": at, "length": 10, "confidenceScore": 0.9}
                )
            at = text.find("046 454 286")
            if at >= 0:
                entities.append(
                    {
                        "category": "CASocialInsuranceNumber",
                        "offset": at,
                        "length": 11,
                        "confidenceScore": 0.8,
                    }
                )
            return {"results": {"documents": [{"id": "1", "entities": entities}]}}

        return post

    def test_one_call_a_document_in_its_language_with_the_key_in_the_header(self):
        seen = []
        engine = LanguageEngine(
            "https://vx.cognitiveservices.azure.com", "k3y", transport=self.transport(seen)
        )
        done = mask("Ada Mensah, SIN 046 454 286", types="all", engine=engine, language="fr")
        assert done.text == "[NAME], SIN [SIN]"
        url, headers, body = seen[0]
        assert (
            url
            == "https://vx.cognitiveservices.azure.com/language/:analyze-text?api-version=2023-04-01"
        )
        assert (
            headers["Ocp-Apim-Subscription-Key"] == "k3y" and body["kind"] == "PiiEntityRecognition"
        )
        assert (
            body["analysisInput"]["documents"][0]["language"] == "fr"
            and body["parameters"]["stringIndexType"] == "UnicodeCodePoint"
        )
        assert [(f.type, f.engine) for f in done.found] == [
            ("name", "language"),
            ("sin", "language"),
        ], "the service saw it first; the pattern found the same and was folded into it"

    def test_a_long_document_goes_in_pieces_and_the_offsets_are_put_back(self):
        seen = []
        engine = LanguageEngine(
            "https://vx.cognitiveservices.azure.com", "k3y", transport=self.transport(seen)
        )
        engine.LIMIT = 40
        text = ("line of nothing much here\n" * 3) + "then Ada Mensah signed"
        done = mask(text, types="all", engine=engine)
        assert (
            len(seen) >= 2
            and "[NAME]" in done.text
            and done.found[0].start == text.index("Ada Mensah")
        )

    def test_without_a_language_the_service_is_left_to_detect_it(self):
        seen = []
        engine = LanguageEngine(
            "https://vx.cognitiveservices.azure.com", "k3y", transport=self.transport(seen)
        )
        mask("nothing", types="all", engine=engine)
        assert "language" not in seen[0][2]["analysisInput"]["documents"][0]

    def test_the_endpoint_has_to_be_the_resources_address(self):
        with pytest.raises(ConfigurationError, match="AZURE_LANGUAGE_ENDPOINT"):
            LanguageEngine("vx.cognitiveservices.azure.com")


class TestComprehend:
    class Client:
        def __init__(self):
            self.calls = []

        def detect_pii_entities(self, Text, LanguageCode):
            self.calls.append((Text, LanguageCode))
            at = Text.find("Ada Mensah")
            return {
                "Entities": [
                    {"Type": "NAME", "BeginOffset": at, "EndOffset": at + 10, "Score": 0.99}
                ]
                if at >= 0
                else []
            }

    def test_one_call_a_document_through_the_client_given(self):
        client = self.Client()
        done = mask(
            "Ada Mensah, ada@example.com",
            types="all",
            engine=ComprehendEngine(client=client),
            language="en",
        )
        assert done.text == "[NAME], a•••@example.com" and client.calls == [
            ("Ada Mensah, ada@example.com", "en")
        ]
        assert [f.engine for f in done.found] == ["comprehend", "regex"]

    def test_french_is_the_patterns_alone_and_says_so(self):
        client = self.Client()
        done = mask(
            "Ada Mensah, ada@example.com",
            types="all",
            engine=ComprehendEngine(client=client),
            language="fr",
        )
        assert (
            done.regex_only is True
            and client.calls == []
            and done.text == "Ada Mensah, a•••@example.com"
        )

    def test_a_region_is_needed_before_a_client_is_made(self):
        engine = ComprehendEngine(region=None)
        engine.region = None
        try:
            import boto3  # noqa: F401
        except ImportError:
            pytest.skip("boto3 is not here, which is its own message")
        with pytest.raises(ConfigurationError, match="AWS_REGION"):
            engine.find("Ada", ["name"], "en")


def test_pieces_cut_at_line_ends_and_carry_their_offsets():
    text = "aaaa\nbbbb\ncccc dddd"
    got = pieces(text, 6)
    assert "".join(piece for _, piece in got) == text
    assert all(text[at : at + len(piece)] == piece for at, piece in got) and all(
        len(piece) <= 6 for _, piece in got
    )
    assert pieces("short", 100) == [(0, "short")]


# --------------------------------------------------------------- settings ---


class TestTheDeploymentsEngine:
    def test_auto_picks_language_when_its_endpoint_is_set(self):
        engine = engine_from_env(
            {
                "AZURE_LANGUAGE_ENDPOINT": "https://vx.cognitiveservices.azure.com",
                "AZURE_LANGUAGE_KEY": "k",
            }
        )
        assert engine.name == "language" and engine.key == "k" and engine.languages == ("en", "fr")

    def test_auto_without_a_cloud_is_presidio_when_installed_else_the_patterns(self):
        engine = engine_from_env({})
        try:
            import presidio_analyzer  # noqa: F401

            assert engine.name == "presidio"
        except ImportError:
            assert engine.name == "regex"

    def test_a_named_engine_is_built_or_refused_at_start(self):
        assert engine_from_env({"VECTRIXDB_MASKING_ENGINE": "regex"}).name == "regex"
        with pytest.raises(ConfigurationError, match="AZURE_LANGUAGE_ENDPOINT"):
            engine_from_env({"VECTRIXDB_MASKING_ENGINE": "language"})
        with pytest.raises(
            ConfigurationError, match="auto, regex, presidio, language or comprehend"
        ):
            engine_from_env({"VECTRIXDB_MASKING_ENGINE": "ldap"})
        comprehend = engine_from_env(
            {"VECTRIXDB_MASKING_ENGINE": "comprehend", "AWS_REGION": "ca-central-1"}
        )
        assert comprehend.name == "comprehend" and comprehend.region == "ca-central-1"

    def test_comprehend_is_never_picked_by_auto(self):
        assert (
            engine_from_env({"AWS_REGION": "us-east-1", "VECTRIXDB_MASKING_ENGINE": "regex"}).name
            == "regex"
        )
        assert engine_from_env({"AWS_REGION": "us-east-1"}).name != "comprehend", (
            "a region set for Bedrock must not start billing Comprehend"
        )

    def test_the_key_can_come_from_a_file(self, tmp_path):
        (tmp_path / "key").write_text("fr0m-file\n", encoding="utf-8")
        engine = engine_from_env(
            {
                "AZURE_LANGUAGE_ENDPOINT": "https://vx.cognitiveservices.azure.com",
                "AZURE_LANGUAGE_KEY_FILE": str(tmp_path / "key"),
            }
        )
        assert engine.key == "fr0m-file"

    def test_the_languages_are_read_once_and_default_to_english_and_french(self):
        assert languages_from_env({}) == ("en", "fr")
        assert languages_from_env({"VECTRIXDB_MASKING_LANGUAGES": "fr-CA, en, fr"}) == ("fr", "en")

    def test_the_health_reply_says_which_engine_and_which_languages(self):
        said = describe_engine(ComprehendEngine(client=object()), ["en", "fr"])
        assert (
            said["engine"] == "comprehend"
            and said["languages"] == {"en": True, "fr": False}
            and said["patterns_last"] is True
        )
        assert describe_engine(RegexEngine(), ["fr"])["languages"] == {"fr": True}

    def test_the_check_command_reads_the_masking_settings(self, tmp_path):
        from vectrixdb.check import run

        def masking(env):
            return [
                (f.level, f.text)
                for f in run(str(tmp_path), {"VECTRIXDB_OFFLINE": "1", **env})
                if f.area == "Masking"
            ]

        assert any(
            level == "ok"
            and "Azure AI Language at https://vx.cognitiveservices.azure.com, with its key" in text
            for level, text in masking(
                {
                    "AZURE_LANGUAGE_ENDPOINT": "https://vx.cognitiveservices.azure.com",
                    "AZURE_LANGUAGE_KEY": "k",
                }
            )
        )
        assert any(
            level == "error" and "AZURE_LANGUAGE_ENDPOINT" in text
            for level, text in masking({"VECTRIXDB_MASKING_ENGINE": "language"})
        )
        assert any(
            level == "error" and "AWS_REGION" in text
            for level, text in masking({"VECTRIXDB_MASKING_ENGINE": "comprehend"})
        )
        assert any(
            level == "ok" and "patterns alone" in text
            for level, text in masking({"VECTRIXDB_MASKING_ENGINE": "regex"})
        )
        assert any(
            level == "error" and "can be auto" in text
            for level, text in masking({"VECTRIXDB_MASKING_ENGINE": "ldap"})
        )


def test_found_and_masked_serialise_for_a_reply():
    done = mask("ada@example.com and 10.0.0.1", types="all", engine=RegexEngine(), language="en")
    said = done.to_dict()
    assert (
        said["counts"] == {"email": 1, "ip_address": 1}
        and said["engine"] == "regex"
        and said["language"] == "en"
    )
    assert said["found"][0] == Found("email", 0, 15, "regex", 1.0).to_dict()
