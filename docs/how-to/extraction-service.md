# Run an extraction service

Files, addresses and videos go in, and text comes out: a PDF's words, a recording's timed transcript, a YouTube video's, a translation. Nothing is indexed. It is the job a document-reading server usually does, served by the library, so the host you write is two lines and everything else is tested once for everyone.

It answers at the paths such a server is already called at, `/extract/pdf`, `/transcribe/youtube`, `/translate/text` and the rest, so a caller of one needs no change to call this one. A VectrixDB server can read through it too, which is the usual reason to run one: see [Reading through it from another server](#reading-through-it-from-another-server) below, and [Extract, keep, index](extract-keep-index.md) for who reads which file type when there is no service.

## What it serves

| Route | Takes | Answers |
| --- | --- | --- |
| `GET /health` | nothing, and no key | `{"status": "healthy", "masking": {"engine": ...}}` |
| `POST /mask` | `{"text": ..., "types": "all" or a list, "language": "fr"}` | the text with its identifiers masked, what was found with offsets, a risk score, and which engine ran |
| `POST /extract/pdf`, `docx`, `doc`, `pptx`, `xlsx`, `csv`, `txt`, `md`, `html` | the file as the body | its text, with what each picture in it shows |
| `POST /transcribe/image` | the picture as the body | what it shows, a chart's values as rows, and the words in it |
| `POST /transcribe/audio`, `video` | the file as the body, `?language=en-US` | a timed transcript, who said what, and for a video what its screen shows |
| `POST /transcribe/webpage` | `{"url": ...}` | the page's text |
| `POST /transcribe/image_url`, `audio_url`, `video_url` | `{"url": ..., "language": "en-US"}` | as for the file routes |
| `POST /transcribe/youtube` | `{"url": ..., "language": "en-US"}` | the video's timed transcript |
| `POST /transcribe/youtube_save` | `{"url": ..., "output_dir": "output", "auto_delete": true}` | `202` and a job |
| `POST /transcribe/auto` | `{"url": ...}` | a YouTube video, or whatever the address turns out to be |
| `POST /translate/text` | `{"text": ..., "to": "fr", "from_lang": null}` | the translations |
| `POST /translate/detect` | `{"text": ...}` | the language of each |
| `GET /translate/languages` | nothing | every language, by code |
| `GET /jobs/{job}` | nothing | how a job is doing, and what it made |

Every `/extract` and `/transcribe` route also takes `?mask=1`, with `types`
and `language` as `/mask` takes them, and answers with the text already
masked: the JSON carries a `masking` summary beside it, the Markdown an
`X-Masking` header. The engine is the deployment's, `VECTRIXDB_MASKING_ENGINE`:
the patterns alone, Presidio in the process, Azure AI Language with
`AZURE_LANGUAGE_ENDPOINT` and `AZURE_LANGUAGE_KEY`, or Amazon Comprehend with
`AWS_REGION`. Whatever ran, the patterns run after it, so a shape the model
missed is still caught by its form; and an engine asked about a language it
does not cover says so in `regex_only` rather than answering as if it had.

Every route answers Markdown, the way such a server does, or JSON when the request's `Accept` header puts `application/json` first: `text`, `pages`, `headings`, `metadata` and, for a recording, `segments`, each phrase with when it was said. A transcript in Markdown is written for a person:

```text
# YouTube: Quarterly results, explained

- URL: https://www.youtube.com/watch?v=q3Results01
- Channel: Northwind Bank
- Duration: 3:32
- Language: en-US

---

## Full Text

Welcome to the quarterly results. Revenue grew in every region.

## Segments

**[0:00 → 0:04]** Welcome to the quarterly results.

**[0:04 → 0:09]** Revenue grew in every region.
```

## As a FastAPI app

```python
# main.py
from vectrixdb.api import create_extraction_app

app = create_extraction_app()
```

```ini
# .env
VECTRIXDB_API_KEY=...                                  # it will not start without a key or sign-in
AZURE_DOCINTEL_ENDPOINT=...   AZURE_DOCINTEL_KEY=...   # pictures, and PDF pages with no text layer
AZURE_SPEECH_ENDPOINT=...     AZURE_SPEECH_KEY=...     # recordings, videos, YouTube
AZURE_VISION_ENDPOINT=...     AZURE_VISION_KEY=...     # what a picture shows, and a PDF's figures
AZURE_OPENAI_ENDPOINT=...     AZURE_OPENAI_VISION_DEPLOYMENT=gpt-4o   # pictures described in detail, ahead of Vision
AZURE_OPENAI_KEY=...                                   # or none, and the managed identity is used
AZURE_TRANSLATOR_KEY=...      AZURE_TRANSLATOR_REGION=eastus
VECTRIXDB_EXTRACT_URL_HOSTS=www.northwind.example,*.northwind.example   # the only hosts an address may be on
```

```bash
pip install "vectrixdb[api,documents,ocr-azure,youtube]"
uvicorn main:app --host 0.0.0.0 --port 8000
```

Every service is optional, and a route that needs one that is missing says which setting it wants rather than failing somewhere inside. Without Azure, pictures are read by RapidOCR and recordings by Whisper on the machine, which need the `ocr` and `asr` extras. To choose the services in code rather than the environment, pass them; anything not passed still comes from the environment:

```python
from vectrixdb import AzureTranslator
from vectrixdb.api import create_extraction_app
from vectrixdb.extract.engines import AzureSpeech

app = create_extraction_app(
    audio=AzureSpeech(SPEECH_ENDPOINT, SPEECH_KEY, locales=["en-US"]),
    translator=AzureTranslator(TRANSLATOR_KEY, region="eastus"),
    url_hosts=["www.northwind.example"],
)
```

## As an Azure function app

```text
function_app/
├── function_app.py
├── requirements.txt
├── host.json
└── local.settings.json
```

```python
# function_app.py
import azure.functions as func

from vectrixdb.api import create_extraction_app, run_extraction_job

app = func.AsgiFunctionApp(app=create_extraction_app(), http_auth_level=func.AuthLevel.ANONYMOUS)


@app.queue_trigger(arg_name="message", queue_name="extract-jobs", connection="AzureWebJobsStorage")
def extract_job(message: func.QueueMessage) -> None:
    run_extraction_job(message.get_body())
```

```text
# requirements.txt
azure-functions
vectrixdb[api,documents,ocr-azure,youtube,jobs-azure]
```

```json
{
  "version": "2.0",
  "functionTimeout": "01:00:00",
  "extensions": {
    "http": { "routePrefix": "" },
    "queues": { "batchSize": 1, "maxDequeueCount": 5, "messageEncoding": "none" }
  }
}
```

The settings are the ones above, plus `VECTRIXDB_EXTRACT_JOBS=azure`, which keeps jobs in the function app's own storage account. Three of those lines are there on purpose:

- **`http_auth_level=ANONYMOUS`**, because the door is the library's. A function key is one secret with no scope and no expiry; the library's keys are scoped, expire, and share one rate limit. See [who can call it](#who-can-call-it).
- **`"routePrefix": ""`**, or every route answers under `/api`, and a caller of `/extract/pdf` finds nothing.
- **`"messageEncoding": "none"`**, because the job message is written as plain JSON.

## Choose the path it answers at

The routes above are at the root of the host. A deployment that puts its services under a path of its own says so once, and every route, `/health` and the documentation move under it:

```ini
VECTRIXDB_EXTRACT_PREFIX=/acme            # /acme/extract/pdf, /acme/health, /acme/docs
```

or `create_extraction_app(prefix="/acme")`. A company's own name and `/api` are both common choices. Once a prefix is chosen the root is not answered, so there is one address for each route and not two.

### Behind a gateway

A gateway usually publishes each endpoint under a path of its own, chosen by the team that runs it, and the host it gives callers may carry a path too. The host is never a setting. The endpoint paths go in one list, as the team hands them over:

```ini
VECTRIXDB_EXTRACT_PREFIX=/acme
VECTRIXDB_EXTRACT_GATEWAY_PATHS=extract/pdf=/files/pdf, transcribe/youtube=/media/youtube, transcribe/youtube_save=/media/save, jobs=/media/jobs
```

or `create_extraction_app(prefix="/acme", gateway_paths={"extract/pdf": "/files/pdf", ...})`. A route is named as it is served with no prefix, and the job route is `jobs`. A name that is not a route here is refused when the app starts, so a typing mistake is found then and not by a caller.

Each listed route answers with its gateway path in front or without it, so the gateway may pass the path on or take it off. A path opens only its own route, and a route that is not listed answers under the prefix alone. With the settings above:

| Callers use | The app is given |
| --- | --- |
| `http://localhost:8000/acme/extract/pdf` | `/acme/extract/pdf`, with nothing in front |
| `https://extract.example.com/acme/extract/pdf` | `/acme/extract/pdf`, with nothing in front |
| `https://gateway.example.com/files/pdf/acme/extract/pdf` | `/files/pdf/acme/extract/pdf`, or `/acme/extract/pdf` if the gateway takes its path off |
| `https://gateway.example.com/team/files/pdf/acme/extract/pdf` | the same: `/team` is the host's own, and stays at the gateway |
| `https://gateway.example.com/media/youtube/acme/transcribe/youtube` | `/media/youtube/acme/transcribe/youtube`, or `/acme/transcribe/youtube` |
| `https://gateway.example.com/acme/translate/languages` | `/acme/translate/languages`: the route has no gateway path |

The job address `youtube_save` hands back starts with the job route's gateway path. A caller puts the host it was given in front, as it does for every other route, and the app answers the same address directly:

```text
{"job": "j_7f3a9c...", "status": "queued", "check": "/media/jobs/acme/jobs/j_7f3a9c..."}
https://gateway.example.com/team/media/jobs/acme/jobs/j_7f3a9c...
```

A gateway takes its description of the service from `/acme/openapi.json`, which needs no key and lists each route once, under the prefix. The documentation page is for callers who reach the app directly.

A path with a slash too many, `/acme/extract/pdf/`, is not found. It is not redirected, because a redirect names the host the app sees, and behind a gateway that would send the caller around it.

`VECTRIXDB_ROOT_PATH` is for a different front: a proxy that serves the whole app under one path, the same for every route, and forwards that path or takes it off. The path is put back into the addresses the service hands out, the documentation's and a job's `check`. A path in `VECTRIXDB_PUBLIC_URL` is read the same way, so behind a gateway give it the host alone, or leave it out.

## Calling it

```bash
# a document, answered at once
curl -X POST https://<app>/extract/pdf -H "api-key: $KEY" -H "X-Filename: q3.pdf" --data-binary @q3.pdf

# a recording, in French, answered at once
curl -X POST "https://<app>/transcribe/audio?language=fr-FR" -H "api-key: $KEY" --data-binary @call.wav

# a video
curl -X POST https://<app>/transcribe/youtube -H "api-key: $KEY" -H "Content-Type: application/json" \
     -d '{"url": "https://youtu.be/q3Results01", "language": "en-US"}'

# a video saved: a job at once, the transcript when it is done
curl -X POST https://<app>/transcribe/youtube_save -H "api-key: $KEY" -H "Content-Type: application/json" \
     -d '{"url": "https://youtu.be/q3Results01"}'
#   {"job": "j_7f3a9c...", "status": "queued", "check": "/jobs/j_7f3a9c..."}
curl https://<app>/jobs/j_7f3a9c... -H "api-key: $KEY"

# a translation
curl -X POST https://<app>/translate/text -H "api-key: $KEY" -H "Content-Type: application/json" \
     -d '{"text": ["Bonjour tout le monde"], "to": "en"}'
```

The key goes in `api-key`, or as `Authorization: Bearer <key>`.

## Reading through it from another server

A VectrixDB server reads files through the service with three settings, and keeps what the service knew: page breaks for citations, headings, what each picture shows, and every phrase of a recording with its time.

```ini
VECTRIXDB_EXTRACTOR_URL=https://<extraction app>.azurewebsites.net
VECTRIXDB_EXTRACTOR_KEY=<a key the extraction service accepts>
VECTRIXDB_EXTRACTOR_KEY_HEADER=api-key
```

The third is needed because the reader sends its key as `x-api-key` by default, to suit other servers, and this one reads `api-key`.

A fourth asks the service to mask identifiers as it reads, so the Markdown
the server keeps and the index it builds never held them:

```ini
VECTRIXDB_EXTRACTOR_MASK=1
```

`1` masks the identifiers, `all` everything the service's engine knows, and
a comma-separated list names the types. What was masked comes back with the
document and is kept in its front matter as `masking`: the counts by type, a
risk score and the engine that ran, never the text.

### Which route reads which file

The file's suffix decides, and the service keeps the table: `extraction_routes()`, every suffix it reads and the one route that reads each, 31 of them.

| Suffix | Route |
| --- | --- |
| `.pdf` `.docx` `.doc` `.pptx` `.xlsx` `.csv` `.txt` `.md` `.html` | `/extract/<the kind>` |
| `.xlsm`, `.markdown`, `.htm` | `/extract/xlsx`, `/extract/md`, `/extract/html` |
| `.png` `.jpg` `.jpeg` `.tif` `.tiff` `.bmp` `.webp` | `/transcribe/image` |
| `.wav` `.mp3` `.m4a` `.flac` `.ogg` `.opus` `.aac` | `/transcribe/audio` |
| `.mp4` `.mov` `.webm` `.mkv` `.avi` | `/transcribe/video` |

Hand it to the reader, and every file goes to the route its suffix names; a suffix the service does not read is not sent at all. Behind a gateway, give it the service's own prefix and gateway paths, `extraction_routes("/acme", "extract/pdf=/files/pdf")`, and each path is the one the gateway publishes, which the service answers directly as well:

```python
from vectrixdb.api import extraction_routes
from vectrixdb.extract import HttpExtractor

reader = HttpExtractor.from_environment(routes=extraction_routes())  # None until VECTRIXDB_EXTRACTOR_URL is set
```

A server's own documents route, told the address and nothing else, sends only `.pdf .docx .doc .png .jpg .jpeg .wav .mp3 .m4a .mp4`, the routes an extraction server of this kind usually has. To send it everything this one reads, set `VECTRIXDB_EXTRACTOR_ROUTES` to the table, printed with `python -c "import json; from vectrixdb.api import extraction_routes; print(json.dumps(extraction_routes()))"`.

### Long files

Azure ends every HTTP request at 230 seconds, and the service takes at most 100 MB a file, so an hour of sound or three hundred scanned pages does not fit in one call. Wrap the reader, and a long file is cut into pieces that each do, read as ordinary files, and put back together:

```python
from vectrixdb.extract.batches import batched

reader = batched(HttpExtractor.from_environment(routes=extraction_routes()))  # 10 minutes, 20 pages, 4 at once
```

| File | Pieces | Put back with |
| --- | --- | --- |
| sound | about ten minutes each, cut at the longest pause near each mark | each phrase's time moved on by where its piece starts, so the minutes are still the pages |
| video | its sound, taken out with ffmpeg, then the same | the same |
| PDF | twenty pages each, halved again while one is too big to send | page numbers moved on by the pages before |
| anything else | not cut | |

A piece the service failed on, a 5xx, a 429 or no answer, is tried again alone; one it refused, a 4xx, is not, and the error names the piece. Cutting sound needs ffmpeg, `pip install vectrixdb[ffmpeg]`, and cutting a PDF needs `pypdf`, which `documents` brings. A caller that is not a VectrixDB reader cuts its own files the same way.

### A PDF's tables

By default a PDF is read from its own text layer by PDFium, from where each run of text sits on its page, and only a page with no text on it, a scan, goes to Document Intelligence. Columns are read one after the other, running heads and feet are left out by their place in the margin, and a table's rows are written with the header over each figure, `Net interest income; 2025 Oct. 31: $ 8,545`, which is how most reports read well at no cost. A table with no lines, ragged columns and headers set at odd places can still come out as loose lines; see [Ingest documents](ingest-documents.md) for the rest of what the reader does.

`VECTRIXDB_EXTRACT_PDF=layout` sends every page to Document Intelligence's layout model instead, and what comes back is the document as a person reads it:

- **Tables as rows**, `Region: EMEA; Revenue 2025: 1,200; Revenue 2024: 1,100`: headings over headings are joined, and a value merged down the rows is in every row it covers.
- **Headings** where the model sees them.
- **The number printed on each page**, `Page 12` kept as `12`, for the readable citation.
- **No running headers and footers.** The report's title at the top of every page is marked as a page header, and taken out, so it is not in every chunk.
- **Every figure, a chart drawn in the PDF included**, as a figure line with the words the model read in it, and cut out of its page for the describer below when there is one.

It needs `AZURE_DOCINTEL_ENDPOINT` and `AZURE_DOCINTEL_KEY`, and it is paid for every page, where the default pays only for scanned ones. A long PDF is still read twenty pages at a time, and the pieces keep their printed numbers and their figures.

### Pages read by sight

Rules read a page from where its text sits, and on prose and on tables whose figures line up that is exact and free. A page laid out to be looked at defeats them: a report's snapshot of big figures with their labels under them comes out as `4.6%` in one paragraph and `2025 Dividend Yield` in the next, so neither is found by what it means. `VECTRIXDB_EXTRACT_PDF=vision` reads those pages the way a person does. The rules still read every page, and name the ones they are unsure of: a page with a chart, big figures set apart from their labels, a page whose file draws its parts out of order, text turned on its side. Each of those is drawn as a picture and given, with its own words, to the chat model that describes pictures, which writes the page out in Markdown: headings, paragraphs in the order they are read, each figure kept with what it measures, tables as rows, charts described in place.

A model that can see can misread, and a misread number looks like any other. So every number it writes is held to the words the page holds. One the page does not print is taken out, with its sentence, its table cell or its value in a row. So is a value that is only a tick on a chart's scale, 7,000 for a bar that reaches the line marked 7,000, and one the model says it guessed, "about 7,100": a chart's rows hold what it prints. Those guesses are a habit of models shown a chart and cost the reading nothing more. A number the page never prints and the model gives as fact is an invention: a reading with more than a tenth of its numbers invented, or one that leaves out more than three words in ten the page holds, is not used, and the page keeps its reading by the rules. So is one the model does not answer. The reply says which pages were read by sight, `pages_read_by_sight`, which were kept by the rules and why, `pages_kept_by_rules`, how many numbers were invented and taken out, `numbers_not_on_page`, and how many guesses, `guesses_left_out`.

| | Pages sent | What a fact looks like |
| --- | --- | --- |
| a 244-page annual report | 28: 12 with charts, 15 drawn out of order, 1 of big figures | `- 4.6% 2025 dividend yield` rather than `4.6%` and `2025 Dividend Yield` apart |
| its 97-page financial statements | 3 | |

It needs the chat model the describer uses, `AZURE_OPENAI_VISION_DEPLOYMENT` with `AZURE_OPENAI_ENDPOINT`, or `VECTRIXDB_DESCRIBER_URL`; `AZURE_OPENAI_PAGE_DEPLOYMENT` names another deployment for pages alone, a stronger model say. It is paid for the pages it reads, about a tenth of a report, and `VECTRIXDB_VISION_PAGES=all` sends it every page with words on it. A scanned page has no words of its own to hold a reading to, and is read by Document Intelligence as before. `scripts/extraction_facts.py` measures the difference on your own documents: give it the facts a page prints, a number and what it measures, and it counts how many a reading keeps together, by the rules and by sight.

### Pictures

A picture inside a PDF, a Word document, a PowerPoint deck or an Excel workbook is described where it sits, by the best describer the service has, asked in this order for each picture:

1. **A chat model that can see**, when `AZURE_OPENAI_VISION_DEPLOYMENT` or `VECTRIXDB_DESCRIBER_URL` names one: a detailed description. What kind of picture it is, the people in it and what they visibly wear and do (never who they are), the setting, every word printed in it exactly as written, and for a chart every value as rows.
2. **Vision**: a caption, short phrases for the parts it sees, and the words. In a region that cannot caption, the words alone.
3. **The words alone**, read by Document Intelligence or RapidOCR, when neither of the others answers.

A picture the model will not describe, or a model still throttled after three tries, goes to the next, and the file is read all the same. A picture whose only caption was its file name, `p4-fig1.jpg`, takes the describer's caption, so it is found and cited by what it shows. The reply's metadata counts who described how many, `"figures_described_by": {"gpt-4o": 12, "azure-image-analysis": 1}`, with `undescribed` for the ones nobody could, and the count is kept in the Markdown's front matter, so the files described by a fallback can be found and read again once the model is back.

A chart a PDF draws with lines and rectangles, rather than pasting in as a picture, has no picture to hand the describer and no values in its text: a bar's value is its height. With a describer there, each one is drawn as a picture of its own region, its title, scales and legend taken in, and described like any other, as `p8-chart1.png` on its page. It stands where the chart is on the page, so the describer reads what is around it, and the words the chart prints leave the page's text: the description says them, as rows where there are values. A chart the describer does not answer for keeps them on one line under its figure, `Words in the picture: ...`. Bars standing on one line are one chart though they never touch, a line through its values is one, a panel of charts is one picture cut to its panel, and a diagram of boxes and arrows is one too. A table, a column of text with shapes about it, and boxes of sentences are not taken for charts. On a 244-page annual report that is 13 pictures, 10 charts and 3 diagrams, found in about four seconds.

The same picture on three pages or more is a letterhead and is never described, and neither is one too small or too plain to be a figure. With nothing that can see, neither a model nor Vision, the pictures are not opened, and a file costs what its words cost.

`/transcribe/image` answers with the describer's account, a chart's values or a table's rows written as rows, `Region: EMEA; Revenue: 1200`, and then the words the picture reader read, which are exact; the describer's own list of the words is left out when the reader found some. The metadata says `described` and by what, `described_by`.

A chart's rows hold only the values it prints. One the describer says it guessed, "about 7,100", is left out of them, and so, for a chart a PDF draws, is one that is not among the words the chart prints or is only a tick on its scale: what a bar reaches against its axis is a guess however it is written, and a row makes a guess read like a figure the document printed. What the chart shows without its values, which bar is higher, which way a line goes, stays in the description's words.

The description comes back in the text, with where each figure is and who described it, and the picture does not, because pictures do not travel in a JSON reply. That is everything search needs: a chart is found by what it shows. If the server reading through it wants the pictures themselves, to show them or to embed them, keep those file types at home and send the service the rest.

### Recordings and videos

A recording goes to Azure AI Speech, told every language it may be in, `VECTRIXDB_SPEECH_LOCALES`, `en-US,fr-CA` by default, and the transcript's `language` is the one it heard most. A call's `?language=` names others for that call alone; several, comma-separated, are all given to Speech, and Whisper takes the first. Up to `VECTRIXDB_SPEECH_SPEAKERS` voices are told apart, four by default. When two or more speak, each phrase starts with who said it and each turn is a line of its own:

```text
Speaker 1: Thanks for calling.
Speaker 2: My card was charged twice.
```

A locale that cannot tell voices apart is asked again without, and `0` asks for none. A busy service, a 429 or a 503, is asked again when it says to come back, waiting 30 seconds at most, three times. A recording with nothing said answers empty with `speech: none`.

A video's sound is read the same way, and its screen too, when a picture reader is set. The whole video is looked over twice a second for the moments its picture changes, a slide coming in or a cut, and a frame is taken half a second after each, when the new picture has settled: at most `VECTRIXDB_VIDEO_FRAMES`, twelve by default, spread over the whole length. What each frame says joins the transcript at its second, `On screen: Q3 RESULTS`, and a slide still showing is not read twice. A video with no sound track is read for its screen alone and says `sound_track: false`. Every frame is a paid page for Document Intelligence; `0` reads the sound alone.

## Who can call it

**Nobody, until you say.** With no `VECTRIXDB_API_KEY` and no sign-in, `create_extraction_app` refuses to start: every call spends money on a paid service, and an open service would spend it for anybody who found the address. That is the same rule `vectrixdb serve` applies before it listens beyond the machine.

- **A full key**, `VECTRIXDB_API_KEY` or its hash in `VECTRIXDB_API_KEY_SHA256`, may call everything.
- **A read-only key may call nothing here** except `/health`. Every route spends money, and a key issued to read does not.
- **With sign-in on**, keys issued by a VectrixDB server and tokens from its identity provider are accepted too, when the two share a sign-in store. See [Sign people in](sign-in.md).
- **Behind API Management** or another gateway that does the asking, set `VECTRIXDB_ALLOW_OPEN=1` to say so, and see [Put it behind a gateway](behind-a-gateway.md) for `VECTRIXDB_PUBLIC_URL` and `VECTRIXDB_TRUSTED_PROXIES`. For a test on a laptop, `create_extraction_app(allow_open=True)`.

## The addresses it may fetch

The address routes fetch only from the hosts in `VECTRIXDB_EXTRACT_URL_HOSTS`: exact names, or `*.example.com` for a domain's subdomains. With none set, every address route refuses. A service that fetches whatever a caller names can be pointed at addresses only the service can reach, such as the cloud's metadata endpoint at `169.254.169.254`.

A redirect is followed only to another allowed host. Checking only the first address is not enough: an allowed host that answers `302` with an internal address would otherwise be followed there.

YouTube is the exception, and needs no setting: the YouTube routes fetch from YouTube's own hosts and nothing else, and take one video, never a playlist, which would transcribe and bill every video in it.

## Long jobs

Azure ends every HTTP request at 230 seconds, whatever the function's own timeout says. A page, a picture, a clip or a translation finishes long before that. A long video does not, so `youtube_save` answers `202` with a job at once, and the work finishes where there is time for it:

- **In memory**, the default: in a thread of the same process, with the transcript written under `VECTRIXDB_EXTRACT_OUTPUT` (`./output`). For one server on one machine; a restart forgets its jobs.
- **In Azure Storage**, `VECTRIXDB_EXTRACT_JOBS=azure`: a record for each job in a blob container, the work on a storage queue that the function app's trigger reads, and the transcript written back beside the record. Any instance can answer `GET /jobs/{job}`, because the record is in the container and not in anybody's memory.

A finished job carries the fields a synchronous save would have answered with, so a caller that was waiting for them finds them in `result`:

```json
{
  "job": "j_7f3a9c...",
  "kind": "youtube_save",
  "status": "done",
  "result": {
    "success": true,
    "video_title": "Quarterly results, explained",
    "transcript_file": "https://<account>.blob.core.windows.net/extraction/transcripts/output/q3Results01_transcript.md",
    "transcript_filename": "q3Results01_transcript.md",
    "media_deleted": "q3Results01.m4a",
    "auto_delete_enabled": true,
    "duration": 212,
    "channel": "Northwind Bank"
  }
}
```

A job that fails says why, in `error`, and is not tried again. A video YouTube refused once it refuses again, and each try is paid for.

`output_dir` is one folder name under the service's own output, never a path: anything with a separator or `..` in it is refused.

## Settings

| Setting | What it does |
| --- | --- |
| `VECTRIXDB_API_KEY`, `VECTRIXDB_API_KEY_SHA256` | the key that may call everything |
| `VECTRIXDB_SIGNIN` | sign-in, so named keys and tokens are accepted |
| `VECTRIXDB_ALLOW_OPEN` | `1`: something in front of it does the asking |
| `AZURE_DOCINTEL_ENDPOINT`, `AZURE_DOCINTEL_KEY` | pictures, and scanned PDF pages |
| `AZURE_SPEECH_ENDPOINT`, `AZURE_SPEECH_KEY` | recordings, videos and YouTube |
| `VECTRIXDB_SPEECH_LOCALES` | the languages a recording may be in, `en-US,fr-CA` by default |
| `VECTRIXDB_SPEECH_SPEAKERS` | how many voices to tell apart, 4 by default; `0` for none |
| `VECTRIXDB_VIDEO_FRAMES` | the most frames of a video read for what is on screen, 12 by default; `0` for none |
| `AZURE_VISION_ENDPOINT`, `AZURE_VISION_KEY` | what a picture shows, and a PDF's figures |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_VISION_DEPLOYMENT`, `AZURE_OPENAI_KEY`, `AZURE_OPENAI_API_VERSION` | a chat model that can see, describing pictures in detail ahead of Vision; with no key, the managed identity |
| `VECTRIXDB_DESCRIBER_URL`, `VECTRIXDB_DESCRIBER_KEY`, `VECTRIXDB_DESCRIBER_MODEL`, `VECTRIXDB_DESCRIBER_KEY_HEADER` | the same, on any OpenAI style chat completions route |
| `AZURE_TRANSLATOR_KEY`, `AZURE_TRANSLATOR_REGION`, `AZURE_TRANSLATOR_ENDPOINT` | translation |
| `VECTRIXDB_EXTRACT_PREFIX` | the path every route lives under, a company's name or `/api` |
| `VECTRIXDB_EXTRACT_GATEWAY_PATHS` | each endpoint's own gateway path, as `route=path` pairs |
| `VECTRIXDB_ROOT_PATH` | one path a proxy puts in front of every route |
| `VECTRIXDB_EXTRACT_URL_HOSTS` | the hosts an address may be on |
| `VECTRIXDB_EXTRACT_PDF` | `text`, the default; `vision`, the pages the rules are unsure of read by sight; or `layout`, every PDF page read by Document Intelligence's layout model |
| `VECTRIXDB_VISION_PAGES` | with `vision`: `hard`, the default, or `all` pages |
| `AZURE_OPENAI_PAGE_DEPLOYMENT` | with `vision`: a deployment to read pages with, when not the describer's own |
| `VECTRIXDB_EXTRACT_JOBS` | `memory` (the default) or `azure` |
| `VECTRIXDB_EXTRACT_OUTPUT` | where memory jobs write, `./output` by default |
| `VECTRIXDB_EXTRACT_STORAGE` | Azure jobs: a connection string or an account URL; `AzureWebJobsStorage` when unset |
| `VECTRIXDB_EXTRACT_CONTAINER`, `VECTRIXDB_EXTRACT_QUEUE` | `extraction` and `extract-jobs` by default |
| `VECTRIXDB_MAX_UPLOAD_BYTES` | the largest file it reads, 100 MB by default |

## Where it differs from a hand-written server

It keeps the paths, the request bodies and the reply shapes of the server it stands in for, and differs in four places, each on purpose:

- **`youtube_save` is a job**, for the reason in [Long jobs](#long-jobs). Its finished job carries the same fields.
- **`output_dir` is a folder name**, not a path the caller chooses.
- **`transcribe/webpage` reads the page's content and not what it embeds.** The menus, banner, footer and hidden parts around the content are left out, as [Ingest documents](ingest-documents.md) describes. Each embedded picture or video is another address, usually on a CDN nobody allowed, so `include_images`, `include_audio` and `include_video` are accepted and not acted on, and the JSON reply says `"embedded_media": "not read"`.
- **`transcribe/auto` takes YouTube, and no other video site.** Handing any address to a video downloader would let it fetch from over a thousand sites.
