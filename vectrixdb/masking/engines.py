"""The engines that read meaning, not shape: names, addresses and the ids no pattern can trust.

Three of them, each answering in the same shape as the pattern engine, a list
of :class:`~vectrixdb.masking.patterns.Found`, so what is done with what they
find is the same whichever ran:

- :class:`PresidioEngine`: Microsoft Presidio, in this process. Optional,
  ``pip install 'vectrixdb[masking]'``, plus a spaCy model a language,
  ``vectrixdb download-models --type masking``. No service, no per-call cost,
  a few hundred megabytes of model in memory.
- :class:`LanguageEngine`: Azure AI Language, one call a document to its PII
  task. ``AZURE_LANGUAGE_ENDPOINT`` and ``AZURE_LANGUAGE_KEY``, named the way
  the library's other Azure settings are. Many languages, French included.
- :class:`ComprehendEngine`: Amazon Comprehend, one call a document through
  boto3 with the standard credential chain and ``AWS_REGION``. English and
  Spanish, as the service documents.

Each says which languages it covers; asked about one it does not, it finds
nothing and the caller is told, rather than an answer nobody should trust.
The pattern engine runs after every one of these, so a shape the model missed
is still caught by its form.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..exceptions import ConfigurationError, DependencyError
from .patterns import Found

__all__ = ["ComprehendEngine", "LanguageEngine", "PresidioEngine", "SPACY_MODELS", "pieces"]


# ============================================================================
# SETTINGS: the log, the spaCy models, and the types each service reports
# ============================================================================
#
# One spaCy model a language for Presidio, and the entity types each service
# reports mapped to the library's own.

log = logging.getLogger("vectrixdb.masking")

#: The spaCy model Presidio reads each language with, the large one, since names are the point.
SPACY_MODELS: Dict[str, str] = {
    "en": "en_core_web_lg", "fr": "fr_core_news_lg", "es": "es_core_news_lg", "de": "de_core_news_lg",
    "it": "it_core_news_lg", "pt": "pt_core_news_lg", "nl": "nl_core_news_lg",
}

#: Presidio's entity names, in the library's words. Anything not here is reported under its own name, lower case.
#: A date an engine finds is a "date": "today" and "March 3rd" are not anybody's
#: birthday, and calling every date one masked a meeting's date as a person's.
#: A date of birth is what the patterns find beside the words that say so.
PRESIDIO_TYPES: Dict[str, str] = {
    "PERSON": "name", "EMAIL_ADDRESS": "email", "PHONE_NUMBER": "phone", "CREDIT_CARD": "credit_card", "US_SSN": "ssn",
    "CA_SIN": "sin", "IBAN_CODE": "iban", "US_BANK_NUMBER": "bank_account", "US_PASSPORT": "passport",
    "US_DRIVER_LICENSE": "drivers_license", "IP_ADDRESS": "ip_address", "LOCATION": "address", "DATE_TIME": "date",
    "UK_NHS": "national_id", "UK_NINO": "national_id", "AU_TFN": "national_id", "AU_MEDICARE": "national_id",
    "SG_NRIC_FIN": "national_id", "IN_AADHAAR": "national_id", "IN_PAN": "national_id", "US_ITIN": "national_id",
    "CRYPTO": "bank_account",
}
#: Azure AI Language's categories, in the library's words.
LANGUAGE_TYPES: Dict[str, str] = {
    "Person": "name", "Email": "email", "PhoneNumber": "phone", "CreditCardNumber": "credit_card",
    "USSocialSecurityNumber": "ssn", "CASocialInsuranceNumber": "sin", "InternationalBankingAccountNumber": "iban",
    "ABARoutingNumber": "bank_account", "SWIFTCode": "bank_account", "IPAddress": "ip_address", "Address": "address",
    "DateTime": "date", "Date": "date", "USUKPassportNumber": "passport", "CAPassportNumber": "passport",
    "USDriversLicenseNumber": "drivers_license", "CADriversLicenseNumber": "drivers_license", "AzureDocumentDBAuthKey": "api_key",
    "AzureStorageAccountKey": "api_key", "AzureSAS": "api_key", "AzureIoTConnectionString": "connection_string",
    "SQLServerConnectionString": "connection_string", "Organization": "organization",
}
#: Amazon Comprehend's entity types, in the library's words.
COMPREHEND_TYPES: Dict[str, str] = {
    "NAME": "name", "EMAIL": "email", "PHONE": "phone", "CREDIT_DEBIT_NUMBER": "credit_card", "SSN": "ssn",
    "BANK_ACCOUNT_NUMBER": "bank_account", "BANK_ROUTING": "bank_account", "INTERNATIONAL_BANK_ACCOUNT_NUMBER": "iban",
    "SWIFT_CODE": "bank_account", "PASSPORT_NUMBER": "passport", "DRIVER_ID": "drivers_license", "IP_ADDRESS": "ip_address",
    "ADDRESS": "address", "DATE_TIME": "date", "AWS_ACCESS_KEY": "api_key", "AWS_SECRET_KEY": "api_key",
    "PASSWORD": "api_key", "USERNAME": "name", "CA_SOCIAL_INSURANCE_NUMBER": "sin", "CA_HEALTH_NUMBER": "national_id",
    "UK_NATIONAL_INSURANCE_NUMBER": "national_id", "UK_NATIONAL_HEALTH_SERVICE_NUMBER": "national_id",
    "IN_AADHAAR": "national_id", "IN_PERMANENT_ACCOUNT_NUMBER": "national_id", "CREDIT_DEBIT_CVV": "credit_card",
    "CREDIT_DEBIT_EXPIRY": "credit_card", "PIN": "api_key", "MAC_ADDRESS": "ip_address", "URL": "internal_host",
    "LICENSE_PLATE": "drivers_license", "VEHICLE_IDENTIFICATION_NUMBER": "drivers_license", "AGE": "date_of_birth",
}


# ============================================================================
# PIECES
# ============================================================================
#
# INPUT   a text and a limit
# OUTPUT  the text in pieces of at most the limit, cut at line ends where it
#         can be, each with its offset
#
# Azure AI Language takes 5,000 characters a document, so a long one goes in
# pieces and the offsets are put back.


def pieces(text: str, limit: int) -> List[Tuple[int, str]]:
    """``text`` in pieces of at most ``limit`` characters, cut at line ends where it can be, each with its offset."""
    if len(text) <= limit:
        return [(0, text)]
    out, at = [], 0
    while at < len(text):
        end = min(at + limit, len(text))
        if end < len(text):
            cut = text.rfind("\n", at, end)
            if cut <= at:
                cut = text.rfind(" ", at, end)
            if cut > at:
                end = cut + 1
        out.append((at, text[at:end]))
        at = end
    return out


def _language_code(language: Optional[str]) -> str:
    return (language or "en").split("-")[0].lower()


# ============================================================================
# THE THREE ENGINES
# ============================================================================
#
# INPUT   a text and its language
# OUTPUT  Presidio in this process, one spaCy model a language; Azure AI
#         Language's PII task, one call a document; Amazon Comprehend's PII
#         detection through boto3; each answering in the same shape as the
#         pattern engine, a list of finds
#
# An engine asked about a language it does not cover says so rather than
# answering as if it had.


class PresidioEngine:
    """Microsoft Presidio, in this process, one spaCy model a language."""

    name = "presidio"

    def __init__(self, analyzer: Any = None, languages: Sequence[str] = ("en",)) -> None:
        self.languages: Tuple[str, ...] = tuple(_language_code(lang) for lang in languages) or ("en",)
        self._analyzer = analyzer

    def covers(self, language: Optional[str]) -> bool:
        return _language_code(language) in self.languages

    def _engine(self) -> Any:
        if self._analyzer is None:
            try:
                from presidio_analyzer import AnalyzerEngine
                from presidio_analyzer.nlp_engine import NlpEngineProvider
            except ImportError as exc:
                raise DependencyError("Presidio is not installed: pip install 'vectrixdb[masking]', then vectrixdb download-models --type masking") from exc
            models = [{"lang_code": lang, "model_name": SPACY_MODELS.get(lang, f"{lang}_core_news_lg")} for lang in self.languages]
            provider = NlpEngineProvider(nlp_configuration={"nlp_engine_name": "spacy", "models": models})
            self._analyzer = AnalyzerEngine(nlp_engine=provider.create_engine(), supported_languages=list(self.languages))
        return self._analyzer

    def find(self, text: str, types: Iterable[str], language: Optional[str] = None) -> List[Found]:
        lang = _language_code(language)
        if not text or lang not in self.languages:
            return []
        wanted = set(types)
        entities = [presidio for presidio, ours in PRESIDIO_TYPES.items() if ours in wanted] or None
        results = self._engine().analyze(text=text, language=lang, entities=entities)
        out = []
        for result in results:
            kind = PRESIDIO_TYPES.get(str(result.entity_type), str(result.entity_type).lower())
            if kind in wanted:
                out.append(Found(kind, int(result.start), int(result.end), self.name, float(getattr(result, "score", 1.0))))
        return out


class LanguageEngine:
    """Azure AI Language's PII task, one call a document, in the language the document is in."""

    name = "language"
    #: What one call may hold, under the service's limit of 5,120.
    LIMIT = 5000
    API_VERSION = "2023-04-01"

    def __init__(self, endpoint: str, key: Optional[str] = None, *, transport: Optional[Callable[[str, Dict[str, str], Dict[str, Any]], Dict[str, Any]]] = None, languages: Sequence[str] = ("en", "fr")) -> None:
        if not endpoint or not endpoint.startswith("https://"):
            raise ConfigurationError("AZURE_LANGUAGE_ENDPOINT is the Language resource's address, https://<name>.cognitiveservices.azure.com")
        self.endpoint = endpoint.rstrip("/")
        self.key = key or None
        self.languages: Tuple[str, ...] = tuple(_language_code(lang) for lang in languages)
        self._transport = transport or self._post
        self._token: Optional[str] = None

    def covers(self, language: Optional[str]) -> bool:
        return True  # the service reads what it is told the language is, and detects it when told nothing

    def _headers(self) -> Dict[str, str]:
        if self.key:
            return {"Ocp-Apim-Subscription-Key": self.key, "Content-Type": "application/json"}
        if self._token is None:
            try:
                from azure.identity import DefaultAzureCredential
            except ImportError as exc:
                raise DependencyError("AZURE_LANGUAGE_KEY is empty, so the managed identity is used, which needs azure-identity: pip install 'vectrixdb[azure]'") from exc
            self._token = DefaultAzureCredential().get_token("https://cognitiveservices.azure.com/.default").token
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    @staticmethod
    def _post(url: str, headers: Dict[str, str], body: Dict[str, Any]) -> Dict[str, Any]:
        request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as reply:  # noqa: S310 - the endpoint is the deployment's own setting
                return json.loads(reply.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ConfigurationError(f"Azure AI Language answered {exc.code} to the PII task: {exc.read().decode('utf-8', 'replace')[:300]}") from exc

    def find(self, text: str, types: Iterable[str], language: Optional[str] = None) -> List[Found]:
        if not text:
            return []
        wanted = set(types)
        url = f"{self.endpoint}/language/:analyze-text?api-version={self.API_VERSION}"
        out: List[Found] = []
        for base, piece in pieces(text, self.LIMIT):
            document: Dict[str, Any] = {"id": "1", "text": piece}
            if language:
                document["language"] = _language_code(language)
            body = {
                "kind": "PiiEntityRecognition",
                "analysisInput": {"documents": [document]},
                "parameters": {"modelVersion": "latest", "stringIndexType": "UnicodeCodePoint"},
            }
            answer = self._transport(url, self._headers(), body)
            for doc in (answer.get("results") or {}).get("documents") or []:
                for entity in doc.get("entities") or []:
                    kind = LANGUAGE_TYPES.get(str(entity.get("category")), str(entity.get("category", "")).lower())
                    if kind in wanted:
                        start = base + int(entity.get("offset", 0))
                        out.append(Found(kind, start, start + int(entity.get("length", 0)), self.name, float(entity.get("confidenceScore", 1.0))))
        return out


class ComprehendEngine:
    """Amazon Comprehend's PII detection, one call a document, through boto3."""

    name = "comprehend"
    #: What one call may hold, well under the service's 100 KB.
    LIMIT = 20000

    def __init__(self, client: Any = None, region: Optional[str] = None, languages: Sequence[str] = ("en", "es")) -> None:
        self._client = client
        self.region = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        self.languages: Tuple[str, ...] = tuple(_language_code(lang) for lang in languages)

    def covers(self, language: Optional[str]) -> bool:
        return _language_code(language) in self.languages

    def _boto(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:
                raise DependencyError("Amazon Comprehend needs boto3: pip install boto3") from exc
            if not self.region:
                raise ConfigurationError("Amazon Comprehend needs AWS_REGION, the way the Bedrock adapter does")
            self._client = boto3.client("comprehend", region_name=self.region)
        return self._client

    def find(self, text: str, types: Iterable[str], language: Optional[str] = None) -> List[Found]:
        lang = _language_code(language)
        if not text or lang not in self.languages:
            return []
        wanted = set(types)
        out: List[Found] = []
        for base, piece in pieces(text, self.LIMIT):
            answer = self._boto().detect_pii_entities(Text=piece, LanguageCode=lang)
            for entity in answer.get("Entities") or []:
                kind = COMPREHEND_TYPES.get(str(entity.get("Type")), str(entity.get("Type", "")).lower())
                if kind in wanted:
                    out.append(Found(kind, base + int(entity["BeginOffset"]), base + int(entity["EndOffset"]), self.name, float(entity.get("Score", 1.0))))
        return out


# ============================================================================
# THE MODELS, FETCHED
# ============================================================================
#
# INPUT   the languages
# OUTPUT  the spaCy model Presidio reads each with, fetched, and the names
#         fetched
#
# What vectrixdb download-models --type masking runs.


def download_models(languages: Iterable[str], downloader: Optional[Callable[[str], Any]] = None) -> List[str]:
    """Fetch the spaCy model Presidio reads each language with. Returns the model names fetched."""
    if downloader is None:
        try:
            from spacy.cli import download as downloader  # type: ignore[no-redef]
        except ImportError as exc:
            raise DependencyError("spaCy is not installed: pip install 'vectrixdb[masking]'") from exc
    got = []
    for lang in languages:
        model = SPACY_MODELS.get(_language_code(lang), f"{_language_code(lang)}_core_news_lg")
        assert downloader is not None
        assert downloader is not None
        downloader(model)
        got.append(model)
    return got
