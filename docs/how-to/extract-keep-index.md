# Extract, keep, index

A file becomes an index entry in three steps: something turns the file into text, that text is cut up, and the pieces are embedded and written. This page is about the first step and who does it, about keeping what it produced, and about figures. The library reads what it can read offline. A scanned page, an hour of audio, or a format nobody here has heard of is read by somebody else, and that somebody is a function you hand in or an endpoint you run.

## Who reads what

| File | Read by | Needs |
|---|---|---|
| `.md` `.txt` `.html` `.csv` `.pptx` | built in | nothing |
| `.pdf` `.docx` `.xlsx` | built in | `pip install vectrixdb[documents]` |
| `.png` `.jpg` `.tif`, and scanned PDF pages once `RapidOcr()` is registered for `.pdf` | `RapidOcr` | `pip install vectrixdb[ocr]` |
| `.wav` `.mp3` `.m4a` `.flac` | `Whisper` | `pip install vectrixdb[asr]` |
| `.mp4` `.mov` `.webm` | `Video`, the sound only | `pip install vectrixdb[video]` |
| anything | a function, or an endpoint | whatever it needs |

`vectrixdb[ffmpeg]` is ffmpeg alone, for a host that has a service transcribe: the sound out of a video, and a long recording cut into pieces for a reader that answers one call at a time.

An extra is named for what the file becomes, not for whose model does it. `pip install vectrixdb[extract]` is every local reader at once. The cloud versions of the same jobs take the SDK as a suffix: `ocr-azure`, `asr-azure`, `ocr-aws`, `asr-aws`.

## A function reads a file type

An extractor takes the bytes and the file's name and returns Markdown. Register it for a collection and every `.pdf` handed to `add_document` goes through it; every other suffix is read as before.

```python
from vectrixdb import Vectrix

def read_scan(data: bytes, name: str) -> str:
    # Your OCR goes here. It returns Markdown: headings, paragraphs, tables.
    return "# Terms\n\nPayment is due within thirty days.\n\n## Late fees\n\nInterest accrues monthly."

with open("scan.pdf", "wb") as fh:
    fh.write(b"%PDF a scanned contract")

db = Vectrix("contracts", extractors={".pdf": read_scan})
db.add_document("scan.pdf", chunk="markdown")
hit = db.search("what happens if I pay late", limit=1).top
hit.text, hit.citation
```

The same registration for the whole process, for a script that opens several collections:

```python
from vectrixdb import extract

extract.register(".pdf", read_scan)
extract.registered()      # ['.pdf']
extract.unregister(".pdf")
```

A suffix the library never knew works the same way. `extractors={".wav": transcribe}` and a recording is a document.

What an extractor may return:

* **A string**, read as Markdown. Headings are found, images become figures, and pipe tables become one line per row, `Region: EMEA; Revenue: 1200`, which is what a spreadsheet row becomes too. A row of pipes is noise to a sentence embedding; a row of `Header: value` reads like a sentence and keeps every cell an exact keyword.
* **A mapping**, when the extractor knows where the pages break: `{"text": ..., "pages": [[0, 1], [1842, 2]], "headings": [[0, "Terms", 1]], "metadata": {...}}`. Offsets count characters into `text`. `pages` is what turns the citation `scan.pdf` into `scan.pdf#page=4`. Offsets that run past the text, or go backwards, are refused rather than believed. `"page_labels": {"41": "39"}` is the number printed on a page where the file numbers its own; a PDF's can be left out, because they are read from the original's page-label table anyway.
* **A `LoadedDocument`**, as is.

An extractor that takes a third argument, `source`, is told where the bytes came from, a path or an object's address. Whatever an extractor raises arrives as `ExtractionError`, and nothing is written.

## An endpoint reads a file type

`HttpExtractor` sends the file to a service you run and reads the reply. `routes` maps a suffix to a path, and a route may carry a query string.

```python
from vectrixdb import HttpExtractor

reader = HttpExtractor(
    "http://127.0.0.1:9001",
    routes={
        ".pdf": "/extract/pdf",
        ".png": "/transcribe/image",
        ".wav": "/transcribe/audio?language=en-US",
        ".mp4": "/transcribe/video",
    },
    body="raw",                   # the bytes are the request body
    filename_header="X-Filename", # and the name travels in a header
    timeout=300,
)
reader.suffixes()
```

`body="raw"` is for a service that reads the request body as it is, which is how a FastAPI route written around `await request.body()` wants it. The default, `body="multipart"`, sends a form field named `file`. `headers=` carries a key for the service when it has one.

The reply is plain text, read as Markdown, or the JSON mapping above. Anything but a success raises `ExtractionError` with the route and the status on it.

```python
db = Vectrix("docs", extractors=HttpExtractor("https://your-extraction-server", routes={".pdf": "/extract/pdf"}))
db.add_document("scan.pdf", chunk="markdown")
```

Two things to keep in mind. The bytes of every file with a routed suffix are sent to that address, so it is an address you chose. And the service is asked to read a file, nothing more: the fields an entitlement policy decides by come from your ingestion, `metadata=` or the worker's `metadata_of=`, never from the reply.

### A reference server

A server of your own needs the three things one written in an afternoon tends to leave out. **A key on the way in**: the keys in such a server's settings are usually the ones it sends to Azure or AWS, and nothing checks who is calling it, so anybody who finds the port reads documents through your OCR account. Refuse to start without a key unless told the network does the asking, take the key as `api-key`, `x-api-key` or a Bearer token, and compare it in constant time. **Page spans from the PDF route**: the reply says where each page starts, and that is what makes `scan.pdf#page=4` possible; a blank page has no entry and still counts. **Minute offsets from the audio route**: the reply makes a page of every minute, the same arithmetic as the library's own, so `call.wav#page=12` is the twelfth minute. A server that sends its timed phrases as well, as the library's own does, has its recordings cited by the second instead.

## Behind the ingestion worker

The [worker](ingest-on-event.md) uses the extractors its collection was opened with, so a Function App or a Lambda needs no reader installed for the types an endpoint handles. It hands the fetched bytes straight to the extractor, without a temporary file.

A failed extraction raises by default, because on a queue an exception is what asks for the retry and, in the end, the dead letter. `IngestWorker(..., on_error="record")` returns an outcome with `action="failed"` and the reason in `error` instead, for a batch that should not stop at one bad file. Either way the document's existing chunks are left alone: old chunks are replaced only once there is new text to replace them with.

## Local engines

```python
from vectrixdb import Vectrix
from vectrixdb.extract.engines import RapidOcr, Whisper

db = Vectrix("records", extractors={".pdf": RapidOcr(), ".wav": Whisper("base")})
```

`RapidOcr` reads an image whole. On a PDF it keeps every page that already has text and reads only the ones that have next to none, so a report with three scanned appendices costs three pages of OCR. Images and audio with no extractor registered go to these engines by themselves, and say which extra to install when it is missing, rather than being decoded as text.

Speech becomes pages by the minute, and every phrase keeps its time. A hit in a transcript is cited `call.wav#t=665`, the second it starts at, which a media player goes straight to, and a person is shown `call.wav, 11:05`.

### What an OCR engine leaves to whoever calls it

An engine returns boxes of text. Four things follow from where the boxes sit, and each goes wrong in silence when nobody does it. `RapidOcr` and `Textract` do them; the functions are in `vectrixdb.extract.layout` for an engine of your own.

- **Reading order.** `reading_order(items)` takes `(box, text)` pairs and returns the lines as a person reads them: words on a line left to right, a page in columns down one column and then the next, a heading across the columns where it sits, and a table across its rows, since its columns are short cells and not prose. Before 2.2 the boxes were sorted by their tops, which read a two-column scan `L1 R1 L2 R2` and put the words of a line out of order whenever a scan set their tops a pixel apart. Textract returns lines in no reading order at all, so its pages go through the same function.
- **Blank pages.** `looks_blank(image)` says from the pixels whether a page has next to nothing on it, speckle included, and such a page is not sent to the engine. That costs nothing with RapidOCR and matters when `engine=` is a vision model, because those write boilerplate for an empty page. The same check stops a describer being asked about an empty image. `RapidOcr(skip_blank=False)` turns it off.
- **Running heads and feet.** `drop_running_lines(pages)` takes out the lines that repeat at the top or bottom of most pages, the report's title and "Page 3 of 10", and returns them, so nothing goes in secret: a document says what it lost in `running_lines`. They used to land in the middle of chunks and stop a sentence that ran over a page break from being joined. The top and the bottom of a page are counted together, since a reader can put a left-hand page's header after its text. A short line counts with its numbers ignored, the page number on either side of it included. A longer line counts when it carries its own page's printed number, found from the numbers most pages print, so a footer that names the part of the report it is in goes part by part, and so does one the reader put inside the page or onto the end of the text. A document of fewer than four pages is left alone. The built-in PDF reader does the same, and never takes a page's figure lines for running lines. `RapidOcr(drop_running=False)` turns it off.
- **Which pages were read.** `ocr_pages` lists them, and each chunk's `ocr` is true only when its own page was read by OCR. In a report with three scanned appendices, a chunk from the typed body says `ocr: false`, which is what lets an answer treat a number from a scan with more care than one from text.

Measured on the real engine, not on boxes made up for a test.
`scripts/ocr_order_check.py` draws pages the way a scanner gives them, reads
each with RapidOCR, and compares the text word for word with what a person
reads, first with the boxes sorted by their tops, which is all the library did
before, and then in reading order:

| Page | By their tops | Reading order |
| --- | ---: | ---: |
| two columns | 0.543 | 0.942 |
| two columns, turned 1.2 degrees, speckled | 0.531 | 1.000 |
| a heading across two columns | 0.528 | 0.910 |
| three columns | 0.415 | 0.950 |
| a table, read across | 0.600 | 1.000 |
| one column | 0.985 | 0.985 |

What is left under 1.0 on the right is the engine's own reading and not the
order: it drops the space between sentences, `fees.Interest`, which
`mend_sentence_breaks` puts back, and it runs some words together,
`maturityisshown`, which nothing here can know to split. Give the script your
own page images and it prints each as the library now reads it:
`python scripts/ocr_order_check.py scan1.png scan2.png`.

An extractor's reply may carry a table as HTML, which is how the models that read a page in one pass write them. It gets the rendering every other table gets, `Region: EMEA; Revenue: 1,200`, one line a row.

### Choosing between readers

A benchmark says how an OCR model does on somebody else's documents. Run the candidates over fifty or a hundred of your own, the awkward ones included:

```python
from vectrixdb.extract.compare import compare_extractors


def ours(data, name):
    """Any reader: the bytes and the name in, Markdown out."""
    return data.decode("utf-8").replace("accrues", "is charged")


note = b"# Late fees\n\nInterest accrues monthly on the unpaid balance, and a notice goes out after thirty days."
found = compare_extractors([("fees.md", note)], {"built in": None, "ours": ours})
print(found.to_markdown())       # quality, characters, pages, pages by OCR and seconds, per file and reader
print(found.disagreements(below=1.0))   # (file, reader, other reader, how alike), the least alike first
```

With your own documents, `files` is a list of paths, and the readers are the ones you are choosing between: `None` for the built-in reader, `RapidOcr()`, and an `HttpExtractor` pointed at a service.

It measures and does not judge. Two readers that agree can both be wrong, and the one that disagrees can be the one that read the second column: scrambled columns are made of perfectly good words, so the quality score passes them, and only another reader shows the difference. A reader that fails on a file is recorded with what it said and the rest still run.

The built-in PDF reader is `pypdf`, and OCR draws pages with `pypdfium2`; both are permissively licensed. When `pypdf` is missing and PyMuPDF happens to be installed, it is used instead. PyMuPDF is AGPL, VectrixDB does not depend on it or install it, and installing `vectrixdb[documents]` means it is never reached.

## Cloud engines

Each is built around a client, or a key, that you made. Nothing here reads a credential from the environment.

| Engine | Service | Notes |
|---|---|---|
| `AzureDocumentIntelligence(client)` | Document Intelligence, layout | Markdown with tables and headings, cited by page; the page's printed number kept, its running header and footer taken out; `crops=True` cuts every figure out of its page, a drawn chart included, for a describer |
| `AzureSpeech(endpoint, key)` | Speech, fast transcription | one request, phrases with offsets |
| `Textract(client)` | Amazon Textract | an image goes in the request; a multi-page PDF is read in S3 as a job |
| `Transcribe(client, s3, output_bucket)` | Amazon Transcribe | reads media from S3, so it runs behind the worker with an S3 fetcher |

## A YouTube video

```python
from vectrixdb import load_youtube

doc = load_youtube("https://youtu.be/q3Results01")                         # the uploader's captions, else the sound
doc = load_youtube("https://youtu.be/q3Results01", captions="automatic")  # YouTube's own captions will do
doc.metadata["transcript_source"]                                          # "captions" or "speech"
```

`load_youtube` takes one video, never a playlist, and gives you its words as a transcript cited by the minute, like any recording. It reads the video's captions first and its sound only when they will not do, because captions cost nothing: no sound is downloaded, no ffmpeg runs and no speech engine is paid. The sound, when it is read, goes to `audio=`, `AzureSpeech(endpoint, key)` or Whisper on the machine by default. `pip install vectrixdb[youtube]` brings yt-dlp, which does the fetching.

`captions=` says which captions will do:

| `captions` | Reads | Costs |
| --- | --- | --- |
| `"uploaded"`, the default | the captions the uploader made, else the sound | nothing when the uploader made some; a download and a transcription when not |
| `"automatic"` | the uploader's, else YouTube's automatic captions, else the sound | nothing for most videos, and rougher words |
| `"translated"` | as `"automatic"`, then YouTube's machine translation into `language`, else the sound | nothing, and words nobody said |
| `"never"` | always the sound, which is what every call did before 2.2 | a download and a transcription, every time |

The uploader's captions are the default because somebody wrote and checked them. YouTube's automatic captions have no punctuation and more mistakes than Azure Speech makes, so a video that has only those is still read from its sound unless you ask for them. A machine translation is never read unless you ask: YouTube lists one in every language it knows.

`language="fr-FR"` picks the track and is what speech listens for. `fr-FR` takes an `fr` track, and a video with no French track is heard in French rather than read in English. Left out, the track is the video's own language, then English, then the uploader's first.

The metadata says where the words came from. `transcript_source` is `captions` or `speech`; with captions, `caption_language` and `caption_automatic`, and `caption_translated_from` for a translation; with speech, `captions_unused` says why the captions were not read. `save_to="output"` writes the transcript as `<video id>_transcript.md`, and its `- Words: the uploader's captions` line says it too. `keep_audio=True` keeps the sound beside it when there is one: with captions nothing was downloaded, so nothing is kept, and `captions="never"` always downloads it.

YouTube refuses more and more requests from cloud addresses as coming from a bot, captions and sound alike, so a video that reads on your laptop can fail in a function app; the error says when that is why. The extraction service takes the same choice: see [YouTube videos](extraction-service.md#youtube-videos).

## Keep what was extracted

By default the document is gone the moment it is chunked: the index holds pieces, and nothing holds the whole. That is fine for a folder of text files and wrong for anything that cost money to read. `keep_source=True` keeps the Markdown every document was indexed from, beside the collection.

```python
from vectrixdb import Vectrix

db = Vectrix("kept", keep_source=True)
db.add_document(
    "# Terms\n\nPayment is due within thirty days of the invoice date.\n\n## Late fees\n\nInterest accrues monthly on any overdue balance.",
    doc_id="acme/msa.pdf",
    chunk="markdown",
    metadata={"client_id": "acme"},
)
db.document("acme/msa.pdf").text[:7], db.documents.ids()
```

```text
./vectrixdb_data/
    kept.db                            the chunks
    kept.documents/
        _index.json                    a listing, rebuilt from the files if it is lost
        acme/msa.pdf.md                the Markdown, with front matter
        acme/msa.pdf.figures/          the images its figures name
        _deleted/20260918T140211Z/...  what delete_document moved aside
```

The file is the original's whole name plus `.md`, so `notes.md` is kept as `notes.md.md` and an original that is itself Markdown can never be mistaken for its own extraction. Its front matter carries what Markdown cannot: where the pages break, the number printed on each where the PDF numbers its own, the headings, the figures, the metadata, which extractor read it and the version of the original it was read from. It opens in any front matter reader, and it reads back exactly.

**The store is a root of its own.** Never the folder or the container the originals are in: a watcher on the originals would see the store's files land and ingest them, and those would land too. `keep_source` takes a folder, or a `DocumentStore` over a bucket or a container:

```python
from vectrixdb import DocumentStore, S3Files, Vectrix

store = DocumentStore(S3Files(s3_client, "your-extracted-bucket", prefix="contracts"), retain_days=365)
db = Vectrix("kept", keep_source=store)
```

`BlobFiles(blob_service_client, "extracted")` is the same on Azure. Two guards hold the line: `LocalWatcher` refuses to start on a folder that overlaps the store, and the worker answers `ignored` to any event for a file inside it.

### Cut it again without reading it again

```python
db.rechunk("acme/msa.pdf", chunk="sentence", chunk_size=120, overlap=0)
db.rechunk()            # every kept document, with what each was last cut with
```

`rechunk()` reads the kept Markdown and never calls an extractor, so a new chunk size over ten thousand OCRed pages costs an embedding pass and not an OCR bill. What is not given is what the document was last indexed with, and its metadata is what it was written with. On an empty collection opened over the same store it fills the index from the store alone, which is the recovery path for a backend that has no backup of its own. Every document is its own ingestion, with its own record and build, as it was when it first went in.

To see what a rechunk would do before it does it, ask for a preview with the same arguments. Each kept document is cut and counted; nothing is written, deleted or embedded:

```python
planned = db.rechunk_preview(chunk_size=500)
print(planned)                    # 1,204 documents, 1,204 would change: 9,880 chunks now, 18,113 after
for doc in planned.changed[:5]:
    print(doc.doc_id, doc.chunks_now, "->", doc.chunks_after, doc.chunking_after)
```

`planned.to_dict()` is the same as JSON, for a review step in a pipeline. Semantic chunking still asks the embedder where topics turn and llm chunking still asks `cut_with`, since that is where their cuts come from; a model's note is not written, since it does not move a cut.

`reextract(where=...)` is for when the reader got better. It fetches originals from the `source` kept with each document and reads only the ones the function picks:

```python
db.reextract(where=lambda entry: entry.get("extractor") != "ocr-v2")
```

A document whose text comes back the same is left alone.

### Deleting

`delete_document()` moves the kept file under `_deleted` with a timestamp rather than removing it, because what was indexed is part of the record of what was answered from. `DocumentStore(..., retain_days=365)` and `store.purge_deleted()` remove what is older; with no retention named, nothing is ever removed.

`DocumentStore(..., keep_deleted=False)` removes a deleted document's Markdown and its figures at once instead, and keeps no copy. That is for a host whose rule is that a file deleted at its source is gone everywhere, which a worker then carries out on the delete event.

### Front matter you wrote

A Markdown original's own front matter becomes its metadata, and so every chunk's. That is where the fields a policy decides by can come from:

```python
with open("deferment.md", "w", encoding="utf-8") as fh:
    fh.write("---\ndoc_id: wildfire-deferment\nclient_id: acme\naudience: [personal, wealth]\n---\n\n# Program\n\nPayments can be deferred.\n")

db.add_document("deferment.md", chunk="markdown")
db.get_one("wildfire-deferment:0").metadata["audience"]
```

`key: value` lines, `[a, b]` lists and block lists of `- item` are read; anything nested deeper is left out rather than guessed at. `metadata=` wins where both name a key, and `doc_id` there is the document's id when none is passed.

### The worker reads an original once

With a store, the worker decides in two stages. The original's version, the ETag on the event or else a hash of its bytes, decides whether to extract at all: the same version is `unchanged` without the extractor being called, and with an ETag without the object even being fetched. Then the text decides whether to index: a file saved again with the same words costs one extraction and no write.

### Each step written down before the next

By default the Markdown is kept last, after the index took the chunks, so the store never holds a document the index refused. Where reading costs money and writing does not, the other order is better, and two options give it:

```python
staged = Vectrix("staged", keep_source=True, keep_chunks=True, markdown_first=True)
staged.add_document(
    "# Terms\n\nPayment is due within thirty days of the invoice date.",
    doc_id="acme/msa.pdf",
    chunk="markdown",
)
[chunk["text"] for chunk in staged.kept_chunks.get("acme/msa.pdf")]
```

```text
./vectrixdb_data/
    staged.documents/acme/msa.pdf.md     1. the Markdown, kept before anything is cut
    staged.chunks/acme/msa.pdf.jsonl     2. the chunks cut from it, a line each
    staged.db                            3. the index, last
```

`markdown_first=True` keeps the Markdown before anything is cut, then reads it back and cuts what was kept. It needs `keep_source`. A later step that fails, an embedding service that is down say, leaves the Markdown where it is, and the worker's next try starts from it: the original is not read again, and with an ETag not even fetched.

`keep_chunks` writes what each document was cut into before anything embeds it: one JSON Lines file a document, a line a chunk with its id, its text and its metadata, which says where in the document it came from. It takes `True` for a folder beside the collection, a folder, or files like `keep_source`'s, `S3Files` or `BlobFiles`, in a root of their own. The file is named like the Markdown with `.jsonl` for `.md`. A rechunk writes it again and `delete_document()` removes it, since the chunks can always be cut again from the Markdown; `staged.kept_chunks.get(doc_id)` reads it back.

## Figures

A figure is a block: an image line with a short caption, and the blockquote directly under it, only a blank line between, as the long description.

```python
report = """## 4.2 Regional performance

Revenue grew in every region, as shown in Figure 3.

![Figure 3: Revenue by region, FY2025](charts/revenue.png)

> **Figure 3.** Bar chart, USD thousands, three bars.
> AMER 2,100. EMEA 1,200. APAC 950.
> AMER is the largest and grew 18 percent year on year. APAC is flat.
> Text in image: "FY2025 actuals, unaudited".

Costs were flat across the period.
"""
db = Vectrix("reports")
db.add_document(report, doc_id="q3.md", chunk="markdown")
fig = [h for h in db.search("which region had the highest revenue", limit=5) if h.metadata.get("figure")][0]
fig.citation, fig.metadata["figure_id"]
```

The caption is what is cited, `q3.md#4.2 Regional performance(Figure 3: Revenue by region, FY2025)`, and the description is what is searched: the values, the trend, and the words inside the image, which are exact keywords for the sparse index. The block is one chunk whatever the strategy. Write the unit once and the values plainly, `AMER 2,100`, and both halves of a hybrid search can use them.

Three things happen around the block:

* **It is embedded with its context.** The model sees the headings above it, the block, and the sentence that mentions it, `Mentioned as: Revenue grew in every region, as shown in Figure 3.` The stored text is the block alone.
* **It is linked both ways.** The paragraph that says `Figure 3` carries `refers_to`, the figure carries `referenced_by`, and `db.with_figures(hits)` brings a paragraph's figures along, through the same policy check as any other read. `Figure 31` is not `Figure 3`.
* **It has a stable id**, the document, the page and its place on it: `acme-q3.pdf#p4-fig1`.

```python
hits = db.with_figures(db.search("regional performance", limit=3))
[h.metadata.get("figure_id") for h in hits.figures(top=2)]
```

`hits.figures(top=2)` is what to hand a model that can see, beside the text, and with `keep_source` `db.figure_bytes(hit)` is the image itself.

### Somebody describes the figures

A Markdown file's images are read from beside it, and a PDF's images are taken off each page when something is there to describe them:

```python
def describe(image: bytes, context: dict) -> str:
    # context: caption, name, page, heading, and the text before and after
    return "Bar chart of revenue by region. AMER is the largest."

db = Vectrix("described", describe_figures=describe, keep_source=True)
```

The describer runs once, at ingestion, on figures that have an image and no description yet, and what it wrote is kept in the Markdown, so it is never paid for twice. It may return a mapping instead: `caption`, `description`, `table` for a picture of a table, whose rows are rendered the way a spreadsheet's are, or `decorative`. `HttpDescriber(url, body="raw")` points it at an image route you run.

How much gets written depends on who writes it. `AzureImageAnalysis`, Azure AI Vision, gives a caption, short phrases for the parts it sees and the words printed in the picture. `ChatDescriber` asks a chat model that can see, GPT-4o or GPT-4.1 on Azure OpenAI, a model on Azure AI Foundry or OpenAI, or one served on your own machine, for a detailed description: what kind of picture it is, the people in it and what they visibly wear and do, never who they are, the setting, every word printed in it exactly as written, and for a chart every value it shows, as rows. `WordsOnly` turns an OCR reader into a describer that gives the words alone. `Fallback` asks them in order, one picture at a time:

```python
from vectrixdb.extract import ChatDescriber, Fallback, WordsOnly
from vectrixdb.extract.engines import AzureDocumentIntelligence, AzureImageAnalysis

describe = Fallback(
    ChatDescriber.azure_openai("https://your-openai.openai.azure.com", "gpt-4o", key=openai_key),
    AzureImageAnalysis("https://your-vision.cognitiveservices.azure.com", vision_key),
    WordsOnly(AzureDocumentIntelligence(docintel_client, model="prebuilt-read")),
)
db = Vectrix("described", describe_figures=describe, keep_source=True)
```

A picture the model will not describe, or a model still throttled after three tries, goes to the next describer, and none of them ever fails the document. `ChatDescriber(url, model=..., key=...)` is any OpenAI style chat completions route, and `ChatDescriber.from_environment()` reads the same settings the extraction service does. It keeps a description to `max_chars`, 1,500 by default, so a figure stays one chunk the embedding model reads whole.

A figure whose only caption was its file name, `p4-fig1.jpg`, takes the describer's caption instead, so it is found and cited by what it shows; a document's own caption, `Figure 3: Revenue by region`, is kept. Each figure records who described it as `described_by`, and the document counts them in `figures_described_by`, `{"gpt-4o": 12, "azure-image-analysis": 1}`, with `undescribed` for the ones nobody could. The count is in the kept Markdown's front matter, so the documents a fallback described can be found and read again once the model is back.

### Found by what it looks like

A description is words about a picture. For questions about the picture itself, "the chart with the stacked columns", bring a model that embeds pictures and words into one space, CLIP or SigLIP, as an object with `embed_images(list of bytes)`, `embed_texts(list of str)` and `dimension`:

```python
import io

import torch
from PIL import Image
from transformers import CLIPModel, CLIPProcessor


class Clip:
    dimension = 512

    def __init__(self, name="openai/clip-vit-base-patch32"):
        self.model, self.processor = CLIPModel.from_pretrained(name).eval(), CLIPProcessor.from_pretrained(name)

    def _unit(self, features):
        return torch.nn.functional.normalize(features, dim=-1).numpy()

    def embed_images(self, images):
        pictures = [Image.open(io.BytesIO(data)).convert("RGB") for data in images]
        with torch.no_grad():
            return self._unit(self.model.get_image_features(**self.processor(images=pictures, return_tensors="pt")))

    def embed_texts(self, texts):
        with torch.no_grad():
            return self._unit(self.model.get_text_features(**self.processor(text=list(texts), return_tensors="pt", padding=True, truncation=True)))


db = Vectrix("reports", image_embedder=Clip(), keep_source=True)
db.add_document("q3.md", chunk="markdown")
db.search("a chart with stacked columns", vectors="image")     # figures, by their pictures
db.search("revenue by region", vectors="all", explain=True)      # words and pictures, fused by rank
```

Every figure whose image is at hand is embedded into a vector index of its own, under the figure chunk's id, so what comes back is the figure chunk: its caption, its description, `figure_bytes()`. The question goes through the model's text side. An ordinary search is untouched and never asks the picture model: pictures are asked for by name, because fused into every search the best-looking figure would sit near the top of lists that are not about a figure. The index follows the collection through a delete, a replaced document and `rechunk()`, which reads the kept images. Not under an entitlement policy, like every named vector. No picture model is bundled.

A document fetched from a bucket keeps its pictures the same way one read from a file does: the worker asks the collection, and a collection opened with `describe_figures` or `image_embedder` says yes. One limit is in the nature of the formats. A PDF, a Word document, a PowerPoint deck and an Excel workbook carry their pictures inside their own bytes, so fetching one is enough. A Markdown file's pictures are separate files beside it, and bytes fetched on their own have no neighbours, so its figure lines survive and its pictures do not; keep the Markdown and its `.figures` folder together, or ingest it from a folder rather than a blob.

Decoration is taken out before anybody is asked to describe it: an image under about fifty pixels a side, one more than eight times as long as it is wide, and an image that turns up three times or more in a document, which is a letterhead and not a figure. An image path that leaves the document's folder, or a remote address, is not followed.

## The heading goes to the embedder

```python
db = Vectrix("policy")
db.add_document(
    "# Terms\n\nPayment is due within thirty days.\n\n## Late fees\n\nInterest accrues monthly.",
    chunk="markdown",
    embed_heading=True,
)
```

With `embed_heading=True` the model is handed `Terms > Late fees: Interest accrues monthly.` and the collection stores `Interest accrues monthly.` A section that never names its own subject is found by it; what a result shows, and what the keyword index holds, is the text as written. The prefix is kept on the chunk, so `reembed()` embeds the same thing again.

## When the index is lost

A search service keeps no backup of an index, and a local collection is one disk. What survives either is the pair the index was built from: the originals, wherever they are, and the kept Markdown. Recovery is `rechunk()` on a new collection opened over the same store.

```python
from vectrixdb import DocumentStore, LocalFiles, Vectrix

store = DocumentStore(LocalFiles("/srv/extracted/contracts"))
db = Vectrix("contracts_rebuilt", keep_source=store)
written = db.rechunk()          # every kept document, no extractor called
written, len(db.documents)
```

1. **Open an empty collection over the store**, with the same model, policy and backend the lost one had. The store is read, never rewritten by this.
2. **`rechunk()`.** Each document is cut with what it was last cut with and written with the metadata it was written with, so a policy's fields come back with it. It costs an embedding pass.
3. **Check it** against what the store says should be there: `len(db.documents)` documents, and `db.count()` chunks.
4. **Point readers at it.** Every chunk carries a new build id, because it is a new index; a decision record from before names a build that no longer exists, which is the honest answer.

What is not in the store is not recovered: anything written with plain `add()`, conversation memory and the knowledge graph. If those matter, [`export()`](export-import.md) is their backup. And a store is only as safe as where it lives, so keep it off the machine the index is on.

## Over REST

The server reads files the same way. `POST /api/v1/collections/{name}/documents` takes a file as the request body, `VECTRIXDB_EXTRACTOR_URL` hands file types to a service you run, and `VECTRIXDB_KEEP_SOURCE=1` keeps the Markdown, which the dashboard's Ingest page and its Open document action both use. See [Run the REST API](rest-api.md#documents).

## A page by its address

```python
from vectrixdb import load_url

doc = load_url("https://your-site/handbook/leave")
```

What the address ends in decides who reads it, as with a file. It fetches whatever address it is given, so it is for addresses you chose, not ones a request named.
