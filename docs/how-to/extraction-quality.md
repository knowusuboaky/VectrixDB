# Extraction quality and blocked pages

Two kinds of bad text reach an index without an error. A scan that OCR read badly gives chunks with every field filled in and nonsense in the text. A site's firewall answers a fetch with "Just a moment..." in place of the page, and that page reads like a short document. Both pass every schema check, and both show up weeks later as a ranking problem nobody can explain.

VectrixDB stops each at the write. Every chunk is scored for extraction quality, and a document that reads as a failed extraction is warned about or refused. A bot check is recognised and refused before anything reads it.

## The score

`extraction_quality(text)` scores a text from 0 to 1, higher being cleaner, and says whether it is usable at a threshold:

```python
from vectrixdb import extraction_quality
from vectrixdb.quality import degrade

report = ("The facility covenant requires a fixed charge coverage ratio of not less than 1.15x, "
          "tested quarterly against the borrower's accounts and reported to the agent within "
          "thirty days of each quarter end.")

clean = extraction_quality(report)
clean.score, clean.usable                 # (0.9625, True)

garbled = extraction_quality(degrade(report, 0.4))    # a simulated bad OCR pass
garbled.score, garbled.usable             # (0.5072, False)
garbled.signals["whole_words"]            # 0.1071: broken words
```

`degrade(text, level, seed=0)` puts a text through a simulation of what OCR gets wrong: shape-alike glyphs, `rn` read as `m`, lost and extra spaces, stray symbols. `level` is the share of characters touched. It is how the threshold was measured, and it is a quick way to see the score move.

The result is an `ExtractionQuality`: `score`, `threshold`, `usable`, `tokens`, the words it counted, `signals`, and `to_dict()` for JSON.

## The signals

Five signals that a bad OCR pass moves and clean prose does not are weighed into the score. A sixth scales it.

| Signal | What it measures | What fails it | Weight |
| --- | --- | --- | --- |
| `plain_characters` | the share of characters that are letters, digits, spaces or ordinary punctuation | glyph noise | 0.2 |
| `whole_words` | the share of tokens that are whole words, less lone letters and letters mixed with digits | broken words, `c1ient` | 0.4 |
| `pronounceable` | the share of words with a vowel ratio a word can have | runs of consonants | 0.05 |
| `known_words` | the hit rate on a short list of common English words, scaled so ordinary prose reaches 1 | nonsense, and text that is not English | 0.25 |
| `word_length` | how far the mean word length is from the 3 to 7 characters prose has | words joined, or shredded | 0.1 |
| `varied` | 1 for text that goes somewhere, falling towards 0 for text that loops | the same word over and over, or the same sentence | multiplies the score |

`varied` is a multiplier because text that loops is made of good words, so it scores well on the other five. OCR loops on noise, the same word a hundred times, and a vision model loops on a page it cannot read, the same sentence over and over. A run of up to eight identical words is allowed, since a table has rows of `yes yes yes yes`, and a sentence said three times is let through as a refrain or a repeated disclaimer.

Each signal is from 0 to 1, so a low score can be read: the lowest signals say what went wrong. The server's quality route and the dashboard's Quality tab put them in words.

Three kinds of text are judged with care:

- **Tables.** The rows the readers write for a table, `Region: EMEA; Revenue: 1200`, Markdown pipe rows, `[Figure: ...]` lines and the `Words in the picture:` line under a figure are set aside, and the prose around them is judged. A text that is nearly all rows has too little prose to judge and is usable.
- **Short text.** Fewer than five words is not enough to judge, and scores at least the threshold. A two-word heading is not an OCR failure.
- **Punctuation at a word's edges.** `ratio,` and `**Docs:**` count as words.

It is not a language model, a dictionary or a judgement about meaning. A clean page about nothing scores well.

## The threshold

`vectrixdb.quality.DEFAULT_THRESHOLD` is 0.78. It was measured, and the numbers are in its docstring:

- **The labelled set.** `scripts/quality_eval.py` takes prose paragraphs from this documentation and the same paragraphs put through `degrade()` at 5, 10, 20 and 40 percent. Five percent noise is a page a person can still read and counts as clean. Twenty percent and above destroys retrieval and counts as bad. Ten percent is the ambiguous band and is left out of the calibration.
- **The first run**, over 524 samples: precision 0.948, recall 0.988, balanced accuracy 0.926, AUC 0.994 at 0.78. Precision is how often "usable" is right; recall is how much clean text is kept.
- **The latest run**, on 2026-10-02, with punctuation stripped from the edges of words: precision 0.933, recall 0.998, balanced accuracy 0.909, AUC 0.998 at 0.78.

The best threshold for that set is now 0.87, and it was left at 0.78 on purpose. Twenty clean documents of shapes the docs prose is not were scored too: an email, legal prose, a memo, a FAQ, a recipe, a changelog, meeting minutes, a code-heavy README, a CSV, and prose in German, Spanish and French. Terse clean Markdown with headings and bullets scores 0.81 to 0.82, so any threshold above 0.78 refuses some of it. The shapes that score under the line are the known limit:

| Clean text that scores low | Score |
| --- | ---: |
| a slide outline of two-to-five-word lines | 0.766 |
| a changelog | 0.765 |
| meeting minutes | 0.741 |
| German prose | 0.741 |
| a six-line bullet checklist | 0.721 |
| Spanish prose | 0.718 |
| French prose | 0.661 |
| a code-heavy README | 0.641 |
| a bare CSV | 0.537 |

So the threshold suits English prose. For anything else, measure your own.

## Calibrate for your documents

`calibrate()` takes `(text, is_clean)` pairs, your own pages labelled by a person, and finds the threshold that best separates them:

```python
from vectrixdb.quality import calibrate

labelled = [
    (report, True),
    (degrade(report, 0.05), True),
    (degrade(report, 0.4), False),
    (degrade(report, 0.3, seed=1), False),
]
calibration = calibrate(labelled)
calibration.threshold, calibration.balanced_accuracy, calibration.auc    # (0.62, 1.0, 1.0)
```

Without `threshold=`, it picks the one with the best balanced accuracy over the set, which is how the default was chosen. With `threshold=`, it reports how that one does. The result is a `Calibration`: `threshold`, `samples`, `positives`, `precision`, `recall`, `balanced_accuracy`, `auc`, `clean_mean`, `degraded_mean`, and `to_dict()`.

Four samples are an example, not a calibration. Label fifty or a hundred of your own, the awkward ones included, and pass the threshold you get to `add_document()`.

## At the write

`add_document()` scores every chunk and stamps the score on it as `_vx_quality`. The document's score is the mean of its chunks'. A document under the threshold reads as a failed extraction, and `on_low_quality` says what happens:

| `on_low_quality` | What happens to a document under the threshold |
| --- | --- |
| `"warn"`, the default | it is written, and `ExtractionQualityWarning` names it, its score and the threshold |
| `"reject"` | it is refused with `ExtractionQualityError`, before anything is written |
| `"allow"` | it is written, and nothing is said |

`quality_threshold` sets the line for one call; left out, it is `DEFAULT_THRESHOLD`.

```python
import warnings

from vectrixdb import ExtractionQualityError, ExtractionQualityWarning, Vectrix

db = Vectrix("scans")
db.add_document(report, doc_id="covenant")                      # clean: written, nothing said

with warnings.catch_warnings(record=True) as said:
    warnings.simplefilter("always")
    db.add_document(degrade(report, 0.4), doc_id="scan-17")     # written, with a warning
said[0].category is ExtractionQualityWarning                    # True

try:
    db.add_document(degrade(report, 0.4), doc_id="scan-18", on_low_quality="reject")
except ExtractionQualityError as exc:
    exc.doc_id, round(exc.score, 2), exc.threshold              # ('scan-18', 0.51, 0.78)

db.add_document(report, doc_id="minutes", quality_threshold=calibration.threshold)
```

`"warn"` is the default because the detector is measured on English prose. A deployment ingesting something else should see the scores on its own documents before it lets them refuse anything. Once you have, `"reject"` is the right setting: on a queue, the exception is what asks for a retry and, in the end, the dead letter.

`ExtractionQualityError` is a `ValueError` and a `VectrixError`, and carries `doc_id`, `score` and `threshold`. `ExtractionQualityWarning` is a `UserWarning`. The gate is on the document, and one garbled page in a clean report does not refuse the report; that page's chunks still carry their own low `_vx_quality`, so they can be found.

## After the write

Every chunk keeps its `_vx_quality`, so a collection's bad text can be found later:

- **A filter.** `db.search(question, filter={"_vx_quality": {"$lt": 0.78}})` searches only the chunks under the line.
- **The server.** `GET /api/v1/collections/{name}/quality` answers twenty bins of scores, the count under the line, and the lowest chunks with what each fails, in words: "symbols and stray glyphs", "broken words, or letters mixed with digits", "the same words over and over", "few ordinary words: a table, codes, or another language". `threshold`, `worst` and `offset` are query parameters.
- **The dashboard.** A collection's Quality tab lists the chunks under the line with those words, and its Builds tab marks a build whose chunks read badly. See [Use the dashboard](dashboard.md#collections).
- **The documents route.** `POST /api/v1/collections/{name}/documents` takes `on_low_quality` as a query parameter, and its reply carries `quality`, `low_quality` and `quality_threshold`. Over REST the line is always `DEFAULT_THRESHOLD`; a `"reject"` answers `422`.

A scan with a few typed pages in it marks which chunks OCR read: each chunk's `ocr` is true only when its own page was read by OCR. To compare readers on your own files by these scores, see [Choosing between readers](extract-keep-index.md#choosing-between-readers).

## Blocked pages

A bot check is a page a site's firewall sends in place of the one asked for: a challenge, a CAPTCHA, an access denied page with a reference number. Read as a document, it would be indexed and cited as though it were the page, and "Just a moment..." would turn up in answers. So every fetch VectrixDB makes looks at what came back before anything reads it.

It is recognised by its status, its headers and the start of its body, and only when it is sure:

| Answer | Recognised by |
| --- | --- |
| Cloudflare challenge | the `cf-mitigated: challenge` header; or a "Just a moment..." title, its challenge scripts, or its wording, "Checking your browser before accessing", "Verify you are human" |
| Cloudflare block page | "Attention Required! \| Cloudflare", or "Sorry, you have been blocked" |
| AWS WAF | the `x-amzn-waf-action` header saying `captcha` or `challenge`, or its challenge script |
| DataDome | its CAPTCHA script, on a 401, 403, 405 or 429 or with DataDome's headers |
| HUMAN (PerimeterX) | its CAPTCHA, with "Press & Hold" or "Access to this page has been denied" |
| Imperva (Incapsula) | an incident id, or "Pardon Our Interruption" |
| Akamai | an "Access Denied" title with "You don't have permission to access" and a reference number |
| a JavaScript wall | a page whose only words ask for JavaScript and cookies |

Whatever the status: a challenge comes back as `200` as often as `403`, `429` or `503`. Apart from the two headers, a page with more to read than a bot check ever says, 1,500 visible characters or a body over 256 KB, is a page, so an article about Cloudflare that quotes "Just a moment..." is not refused.

What a refusal looks like depends on who fetched:

| Who fetched | What happens |
| --- | --- |
| `load_url(address)` | `ExtractionError`, naming the site, the status and what it sent; `exc.status` is the status |
| `db.sources.refresh()` | the page or the entry fails, with the same words in the report, and the refresh goes on; with `articles=True`, the feed's own text is indexed instead, with a note |
| an [extraction service](extraction-service.md#the-addresses-it-may-fetch)'s address routes | `502`, naming the site and what it sent |

The message never repeats the whole address, so a signed link's signature or token is not in it:

```python
from vectrixdb import load_url
from vectrixdb.exceptions import ExtractionError


def a_challenge(method, url, headers, body, timeout):
    """Stands in for the network: a site answering with Cloudflare's challenge."""
    page = b"<html><head><title>Just a moment...</title></head><body>Checking your browser before accessing the site.</body></html>"
    return 403, {"Server": "cloudflare", "CF-Ray": "8a1b2c3d"}, page


try:
    load_url("https://www.example.com/rates", transport=a_challenge)
except ExtractionError as exc:
    print(exc)
# www.example.com answered 403 with a Cloudflare challenge page, not the page. VectrixDB does not
# get past bot checks: ask the site for a feed or an API, or, if the site is yours, let VectrixDB's
# requests through its firewall.
```

VectrixDB does not get past a bot check, does not solve a CAPTCHA, and does not pretend to be a browser. Every request says who it is, `VectrixDB/<version>`, so a site's owner can allow it or not. For a site that sends one, ask for a feed or an API, or, if the site is yours, let VectrixDB's requests through its firewall.

A `401` or `403` that is not a bot check is the site saying no: a sign-in wanted, or requests that are not a browser's turned away. The error says that in words. A `429` or `503` is a site asking for time, and says how long when the site said; a source refresh waits that long before asking again. See [What is refused, and why](sources.md#what-is-refused-and-why) for robots.txt, private addresses and the rest of what a fetch checks.
