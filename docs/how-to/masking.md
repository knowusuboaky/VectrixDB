# Masking

Masking hides identifiers in text: email addresses, phone numbers, card
numbers, national ids, keys and, with an engine that reads meaning, names and
addresses. It happens in two places.

| | On the way out | Before indexing |
| --- | --- | --- |
| Where | Every reply from a collection's routes to a person or a guest | The extraction service, as it reads a document |
| What | Emails, phone numbers and card numbers, by their shape | Any type below, found by the engine you choose, then by the patterns |
| Switch | None: always on with sign-in, never for an API key | `?mask=1` on the service, or `VECTRIXDB_EXTRACTOR_MASK` on the server |
| The index | Holds the text as written | Never holds the identifier |

The first lowers casual exposure in the dashboard and is described in
[Masking identifiers](sign-in.md#masking-identifiers). This page is mostly
about the second, the stronger one: the identifier is gone before a chunk is
cut, so no search, export or key can bring it back.

## What masked text looks like

Three types are masked where they stand, so a sentence still reads: an email
keeps its first letter and its domain, a phone number and a card their last
four digits. Every other type is replaced whole by its name in brackets.

```text
Before:  Ada Lovelace, SIN 046-454-286, phone 416-555-0199, ada@example.com,
         IBAN GB82 WEST 1234 5698 7654 32, key AKIAIOSFODNN7EXAMPLE

After:   Ada Lovelace, SIN [SIN], phone •••-•••-0199, a•••@example.com,
         IBAN [IBAN], key [API_KEY]
```

That is the patterns alone, with the default types. The name is still there:
names are not in the default types, and no pattern can find one. With
Presidio or Azure AI Language as the engine and `types=all`, a name is replaced
by `[NAME]` and an address by `[ADDRESS]`.

## The types

| Type | Masked as | Default | Found by the patterns |
| --- | --- | --- | --- |
| `email` | `a•••@example.com` | yes | yes |
| `phone` | `•••-•••-0199` | yes | yes |
| `credit_card` | `•••• •••• •••• 1111` | yes | yes, 13 to 19 digits that pass the Luhn check |
| `ssn` | `[SSN]` | yes | yes, the dashed US form |
| `sin` | `[SIN]` | yes | yes, three groups of three that pass the Luhn check |
| `iban` | `[IBAN]` | yes | yes, checked mod 97 |
| `bank_account` | `[BANK_ACCOUNT]` | yes | no |
| `api_key` | `[API_KEY]` | yes | yes, by known prefixes and `key = value` |
| `connection_string` | `[CONNECTION_STRING]` | yes | yes, the password inside one |
| `name` | `[NAME]` | | no |
| `address` | `[ADDRESS]` | | no |
| `passport` | `[PASSPORT]` | | no |
| `drivers_license` | `[DRIVERS_LICENSE]` | | no |
| `date_of_birth` | `[DATE_OF_BIRTH]` | | no |
| `date` | `[DATE]` | | no |
| `ip_address` | `[IP_ADDRESS]` | | yes |
| `internal_host` | `[INTERNAL_HOST]` | | yes, names under `.internal`, `.corp`, `.intranet`, `.lan`, `.local` |

Asking for nothing masks the default types: identifiers, and not people's
names. `all` masks every type in the table. A list, such as
`email,phone,name`, masks those; `national_id` and `organization` may also be
named, for the ids and organisations an engine reports.

Years and runs of figures are not phone numbers: a table's `2025 2024 2023`, a
page range and a chart's axis are left alone.

## The engines

| `VECTRIXDB_MASKING_ENGINE` | What reads the text | Needs |
| --- | --- | --- |
| `auto`, the default | Azure AI Language when `AZURE_LANGUAGE_ENDPOINT` is set, else Presidio when it is installed with a model for the language, else the patterns | |
| `regex` | The patterns alone: shapes, no model, no service | Nothing |
| `presidio` | Microsoft Presidio, in the server's own process | `pip install "vectrixdb[masking]"` and a spaCy model a language |
| `language` | Azure AI Language, one call a document | `AZURE_LANGUAGE_ENDPOINT`, and `AZURE_LANGUAGE_KEY` or the managed identity |
| `comprehend` | Amazon Comprehend, English and Spanish | `AWS_REGION` and the standard AWS credentials |

Whichever engine runs, the patterns run after it, so a shape the model missed
is still caught by its form. Comprehend is used only when named, never picked
by `auto`, so a region set for another AWS service does not start billing this
one. A named engine whose package or settings are missing stops the start,
rather than failing at the first file.

### Presidio

```bash
pip install "vectrixdb[masking]"
export VECTRIXDB_MASKING_LANGUAGES=en,fr
vectrixdb download-models --type masking
```

`download-models --type masking` fetches the spaCy model Presidio reads each
language in `VECTRIXDB_MASKING_LANGUAGES` with, the large one, since names are
the point:

| Language | Model |
| --- | --- |
| `en` | `en_core_web_lg` |
| `fr` | `fr_core_news_lg` |
| `es` | `es_core_news_lg` |
| `de` | `de_core_news_lg` |
| `it` | `it_core_news_lg` |
| `pt` | `pt_core_news_lg` |
| `nl` | `nl_core_news_lg` |

No service and no cost a call, and a few hundred megabytes of model in memory.
`auto` picks Presidio only for the languages whose model is already there; it
never downloads one.

### Azure AI Language

```bash
export VECTRIXDB_MASKING_ENGINE=language
export AZURE_LANGUAGE_ENDPOINT=https://<name>.cognitiveservices.azure.com
export AZURE_LANGUAGE_KEY_FILE=/run/secrets/language-key
```

Many languages, French included, and it detects the language when it is not
told. With `AZURE_LANGUAGE_KEY` empty, the managed identity is used, which
needs `azure-identity`, part of the `azure` extra.

### Settings

| Setting | What it does |
| --- | --- |
| `VECTRIXDB_MASKING_ENGINE` | `auto`, `regex`, `presidio`, `language` or `comprehend` |
| `VECTRIXDB_MASKING_LANGUAGES` | The languages the documents are in, comma separated. `en,fr` unless set. |
| `AZURE_LANGUAGE_ENDPOINT` | The Azure AI Language resource. Set, `auto` picks it. |
| `AZURE_LANGUAGE_KEY` | Its key, or `AZURE_LANGUAGE_KEY_FILE`. Empty: the managed identity. |
| `VECTRIXDB_EXTRACTOR_MASK` | On the server: ask the extraction service to mask as it reads. |

An engine asked about a language it does not cover finds nothing, the patterns
still run, and the reply says `"regex_only": true` rather than answering as if
it had read the text. `vectrixdb check` tests these settings before a start.

## Mask with the extraction service

The [extraction service](extraction-service.md) holds the engine. Ask it to
mask one text with `POST /mask`. Here the service runs the patterns alone:

```bash
curl -s -X POST https://extract.example.com/mask -H "api-key: $EXTRACT_KEY" \
  -H 'content-type: application/json' \
  -d '{"text": "Call Ada on 416-555-0199 or ada@example.com", "types": "all", "language": "en"}'
```

```json
{"text": "Call Ada on •••-•••-0199 or a•••@example.com",
 "found": [{"type": "phone", "start": 12, "end": 24, "engine": "regex", "score": 1.0},
           {"type": "email", "start": 28, "end": 43, "engine": "regex", "score": 1.0}],
 "counts": {"phone": 1, "email": 1},
 "score": 0.7, "engine": "regex", "language": "en", "regex_only": false}
```

| Field | What it says |
| --- | --- |
| `text` | The text, masked |
| `found` | Each identifier: its type, where it was, which engine found it and how sure it was |
| `counts` | How many of each type |
| `score` | The risk, 0 to 1: the heaviest type found, raised a tenth for each further find |
| `engine` | Which engine ran |
| `regex_only` | True when the engine does not cover the language, so only the patterns ran |

Every `/extract` and `/transcribe` route takes `?mask=1` too, with `types` and
`language` as `/mask` takes them, and answers with the document already
masked. The JSON carries a `masking` summary beside the text, and masks every
other text in the reply as well: a transcript's segments, headings, captions
and the title. The Markdown answer carries the summary in an `X-Masking`
header. `GET /health` says which engine the service runs and which languages
it covers.

## Mask everything a server indexes

A VectrixDB server that reads files through the extraction service can ask it
to mask every document on the way in:

```bash
export VECTRIXDB_EXTRACTOR_URL=https://extract.example.com
export VECTRIXDB_EXTRACTOR_KEY_FILE=/run/secrets/extract-key
export VECTRIXDB_EXTRACTOR_KEY_HEADER=api-key
export VECTRIXDB_EXTRACTOR_MASK=1
```

| `VECTRIXDB_EXTRACTOR_MASK` | Masks |
| --- | --- |
| empty | Nothing is asked |
| `1` | The default types |
| `all` | Every type |
| `email,phone,name` | The types named |

What was masked is kept with the document, in its front matter as `masking`:
the counts by type, the risk score, the engine, the language and
`regex_only`. Never the text, and never the offsets. The Markdown the server
keeps and the index it builds never held the identifiers. See
[Reading through it from another server](extraction-service.md#reading-through-it-from-another-server).

## What masking is not

It lowers exposure. It is not a guarantee:

- A thing no engine knows is left as written: an account number written with
  letters, an identifier from somebody else's format.
- The patterns never guess at names, addresses, passports or licences. Those
  need Presidio, Azure AI Language or Comprehend.
- Masking on the way out leaves the index as written, so a search for a phone
  number still finds the chunk that has it. Mask before indexing when the
  index itself must not hold it.
- Anyone with an API key reads the text as stored, so masking on the way out is
  only as good as the list of who holds keys. See
  [Keys and roles](keys-and-roles.md).

## See also

- [Masking identifiers](sign-in.md#masking-identifiers): masking on the way out.
- [Run an extraction service](extraction-service.md): the service that masks.
- [Settings](../reference/settings.md#masking): every masking setting.
