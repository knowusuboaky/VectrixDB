"""Azure AI Translator. The service is a fake; its replies are shaped like version 3's own."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from vectrixdb.exceptions import TranslationError
from vectrixdb.translate import AzureTranslator


class Service:
    """Records every request and answers the way Translator v3 does."""

    def __init__(self, answers=None):
        self.asked = []
        self.answers = list(answers or [])

    def __call__(self, method, url, headers, body, timeout):
        parts = urlsplit(url)
        self.asked.append(
            {
                "method": method,
                "path": parts.path,
                "query": parse_qs(parts.query),
                "headers": dict(headers),
                "texts": [item["Text"] for item in json.loads(body)] if body else [],
            }
        )
        if self.answers:
            status, payload = self.answers.pop(0)
            return status, {}, json.dumps(payload).encode()
        return self.echo(parts, body)

    @staticmethod
    def echo(parts, body):
        """A translation of each text into each language asked for, marked so order can be checked."""
        query = parse_qs(parts.query)
        texts = [item["Text"] for item in json.loads(body)]
        if parts.path.endswith("/translate"):
            reply = []
            for text in texts:
                one = {"translations": [{"text": f"[{to}] {text}", "to": to} for to in query["to"]]}
                if "from" not in query:
                    one["detectedLanguage"] = {"language": "fr", "score": 0.98}
                reply.append(one)
        else:
            reply = [
                {"language": "de", "score": 1.0, "isTranslationSupported": True} for _ in texts
            ]
        return 200, {}, json.dumps(reply).encode()


def translator(service=None, **options):
    return AzureTranslator("a-secret-key", transport=service or Service(), **options)


class TestTranslate:
    def test_one_text_into_one_language(self):
        said = translator().translate("Bonjour tout le monde", to="en")
        assert said == [
            {
                "translations": {"en": "[en] Bonjour tout le monde"},
                "detected": {"language": "fr", "score": 0.98},
            }
        ]

    def test_into_several_languages_at_once(self):
        said = translator().translate("Bonjour", to=["en", "es"])
        assert said[0]["translations"] == {"en": "[en] Bonjour", "es": "[es] Bonjour"}

    def test_a_named_source_is_sent_and_nothing_is_detected(self):
        service = Service()
        said = translator(service).translate("Bonjour", to="en", source="fr")
        assert service.asked[0]["query"]["from"] == ["fr"]
        assert "detected" not in said[0], "nothing was worked out, so nothing is claimed"

    def test_what_comes_back_is_in_the_order_it_went(self):
        said = translator().translate(["one", "two", "three"], to="fr")
        assert [s["translations"]["fr"] for s in said] == ["[fr] one", "[fr] two", "[fr] three"]

    def test_no_language_to_translate_to_is_refused(self):
        with pytest.raises(ValueError, match="to is a language code"):
            translator().translate("Bonjour", to=[])


class TestDetect:
    def test_the_language_and_how_sure(self):
        assert translator().detect("Guten Tag") == [
            {"language": "de", "score": 1.0, "translatable": True}
        ]

    def test_alternatives_are_kept_when_the_service_gives_them(self):
        service = Service(
            [
                (
                    200,
                    [
                        {
                            "language": "no",
                            "score": 0.6,
                            "isTranslationSupported": True,
                            "alternatives": [{"language": "da", "score": 0.4}],
                        }
                    ],
                )
            ]
        )
        said = translator(service).detect("Hei")
        assert said[0]["alternatives"] == [{"language": "da", "score": 0.4}], (
            "a short text is easy to mistake"
        )


class TestLanguages:
    def test_every_language_by_its_code(self):
        service = Service(
            [
                (
                    200,
                    {
                        "translation": {
                            "fr": {"name": "French", "nativeName": "Français", "dir": "ltr"}
                        }
                    },
                )
            ]
        )
        assert translator(service).languages() == {
            "fr": {"name": "French", "native": "Français", "direction": "ltr"}
        }

    def test_it_needs_no_key(self):
        service = Service(
            [
                (
                    200,
                    {
                        "translation": {
                            "ar": {"name": "Arabic", "nativeName": "العربية", "dir": "rtl"}
                        }
                    },
                )
            ]
        )
        said = AzureTranslator(transport=service).languages()
        assert said["ar"]["direction"] == "rtl"
        assert "Ocp-Apim-Subscription-Key" not in service.asked[0]["headers"]


class TestTheRequest:
    def test_the_key_goes_in_a_header_and_never_in_what_is_printed(self):
        service = Service()
        made = translator(service)
        made.detect("x")
        assert service.asked[0]["headers"]["Ocp-Apim-Subscription-Key"] == "a-secret-key"
        assert "a-secret-key" not in repr(made)

    def test_a_region_is_sent_when_there_is_one_and_not_otherwise(self):
        """A regional resource refuses a request without it; a global one does not want it."""
        with_region, without = Service(), Service()
        translator(with_region, region="canadacentral").detect("x")
        translator(without).detect("x")
        assert with_region.asked[0]["headers"]["Ocp-Apim-Subscription-Region"] == "canadacentral"
        assert "Ocp-Apim-Subscription-Region" not in without.asked[0]["headers"]

    def test_version_3_is_asked_for(self):
        service = Service()
        translator(service).detect("x")
        assert service.asked[0]["query"]["api-version"] == ["3.0"]


class TestTheServicesLimits:
    def test_more_than_a_thousand_texts_is_several_requests_in_order(self):
        service = Service()
        texts = [f"t{i}" for i in range(2500)]
        said = translator(service).translate(texts, to="fr")
        assert [len(a["texts"]) for a in service.asked] == [1000, 1000, 500]
        assert len(said) == 2500 and said[-1]["translations"]["fr"] == "[fr] t2499"

    def test_fifty_thousand_characters_a_request(self):
        service = Service()
        translator(service).translate(["a" * 30_000, "b" * 30_000], to="fr")
        assert len(service.asked) == 2, "together they are over the limit, so they go separately"

    def test_the_characters_are_counted_once_per_target_language(self):
        """The service counts the 50,000 against every target: two languages leave 25,000 a request."""
        service = Service()
        translator(service).translate(["a" * 20_000, "b" * 20_000], to=["fr", "de"])
        assert len(service.asked) == 2, (
            "40,000 characters twice over is 80,000, so they go separately"
        )
        service = Service()
        translator(service).translate(["a" * 20_000, "b" * 20_000], to="fr")
        assert len(service.asked) == 1, "one language: 40,000 fits"
        with pytest.raises(ValueError, match="16,666 at most into 3 languages"):
            translator().translate("x" * 16_667, to=["fr", "de", "it"])

    def test_one_text_over_the_limit_is_refused_by_name_and_not_cut(self):
        with pytest.raises(ValueError, match="50,000 at most"):
            translator().translate("x" * 50_001, to="fr")

    def test_nothing_to_translate_is_said(self):
        with pytest.raises(ValueError, match="nothing to translate"):
            translator().translate([], to="fr")

    def test_something_that_is_not_text_is_refused(self):
        with pytest.raises(TypeError, match="each text is a string"):
            translator().detect(["fine", 42])


class TestWhenTheServiceRefuses:
    def test_a_wrong_key_says_what_is_usually_wrong(self):
        service = Service(
            [(401, {"error": {"code": 401000, "message": "credentials are missing or invalid"}})]
        )
        with pytest.raises(TranslationError, match="no region was given") as caught:
            translator(service).detect("x")
        assert caught.value.status == 401 and caught.value.route == "detect"

    def test_a_throttle_is_waited_out_once(self, monkeypatch):
        waited = []
        monkeypatch.setattr("vectrixdb.translate.time.sleep", waited.append)
        service = Service(
            [(429, {"error": {"message": "slow down"}}), (200, [{"language": "de", "score": 1.0}])]
        )
        assert translator(service).detect("x")[0]["language"] == "de"
        assert len(service.asked) == 2 and waited, "it waited, then asked again"

    def test_the_wait_is_never_long(self, monkeypatch):
        waited = []
        monkeypatch.setattr("vectrixdb.translate.time.sleep", waited.append)

        class Slow(Service):
            def __call__(self, method, url, headers, body, timeout):
                super().__call__(method, url, headers, body, timeout)
                return 429, {"Retry-After": "600"}, b'{"error":{"message":"slow down"}}'

        with pytest.raises(TranslationError, match="429"):
            translator(Slow()).detect("x")
        assert waited == [AzureTranslator.MOST_WAIT], (
            "ten minutes asked for, ten seconds given, then told"
        )

    def test_a_service_that_cannot_be_reached(self):
        def down(*args):
            raise OSError("no route to host")

        with pytest.raises(TranslationError, match="could not be reached"):
            translator(down).detect("x")

    def test_a_reply_with_the_wrong_number_of_answers(self):
        service = Service([(200, [{"language": "de"}])])
        with pytest.raises(TranslationError, match="1 for 2 texts"):
            translator(service).detect(["a", "b"])

    def test_translating_without_a_key_names_the_setting(self):
        with pytest.raises(TranslationError, match="AZURE_TRANSLATOR_KEY"):
            AzureTranslator(transport=Service()).translate("x", to="fr")


class TestFromTheEnvironment:
    def test_no_key_is_none(self):
        assert AzureTranslator.from_environment({}) is None

    def test_key_region_and_endpoint(self):
        made = AzureTranslator.from_environment(
            {
                "AZURE_TRANSLATOR_KEY": "k",
                "AZURE_TRANSLATOR_REGION": "eastus",
                "AZURE_TRANSLATOR_ENDPOINT": "https://my.cognitiveservices.azure.com/translator/text/v3.0/",
            }
        )
        assert made.region == "eastus"
        assert made.endpoint == "https://my.cognitiveservices.azure.com/translator/text/v3.0"

    def test_the_global_endpoint_by_default(self):
        assert (
            AzureTranslator.from_environment({"AZURE_TRANSLATOR_KEY": "k"}).endpoint
            == AzureTranslator.ENDPOINT
        )
