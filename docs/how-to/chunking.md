# Chunking

`add_document()` cuts a document into chunks before anything is embedded. How it cuts decides what a search can find: a chunk is the unit that is scored, returned and cited. This page is every way it can cut, what each costs, and how to cut a collection again later without reading its files again.

For what `add_document()` reads and what every chunk carries, see [Ingest documents](ingest-documents.md). To find out which way of cutting suits your own documents, measure it: see [Compare the chunking techniques](measure-retrieval.md#compare-the-chunking-techniques).

## The strategies

`chunk=` names the strategy. There are six, and `vectrixdb.ingest.STRATEGIES` lists them:

| `chunk=` | How it cuts | Needs |
| --- | --- | --- |
| `recursive`, the default | Splits on paragraphs, then lines, then sentences, then words, and merges the pieces up to `chunk_size` characters, with `overlap` characters carried into the next chunk | nothing |
| `sentence` | Packs whole sentences up to `chunk_size`, and carries the last sentences of a chunk into the next one, about `overlap` characters of them. Never cuts inside a sentence | nothing |
| `semantic` | Embeds every sentence and starts a new chunk where neighbouring sentences are least alike. `threshold`, 0.1 by default, is the share of boundaries that split: 0.1 splits at the sharpest tenth of topic changes. `chunk_size` is a hard cap | one embedding a sentence, with the collection's own model |
| `markdown` | One chunk per heading section, the heading kept on the chunk's metadata rather than in its text. A section longer than `chunk_size` is cut recursively inside the section | headings; HTML, Word and PDF bookmarks become headings when they are read |
| `fixed` | Every `chunk_size` characters, `overlap` carried over, whatever the paragraphs and sentences say. Only a word is never cut: the window ends at the space before it | nothing |
| `llm` | A model reads the paragraphs in order and says where a new topic starts. Each topic is a chunk, packed to `chunk_size` where it runs longer | `cut_with=`, see [A model says where topics start](#a-model-says-where-topics-start) |

Two rules hold for all six. A figure line, `[Figure: caption]`, is always a chunk of its own, so no strategy splits a caption or glues it to the paragraph before it. And a piece with no letter or digit in it, the `|` a layout reader leaves where an empty table was, is not kept.

```python
from vectrixdb import Vectrix

guide = """# Install

Install vectrixdb with pip and you are done. The English model ships in the wheel.

## Offline

Nothing downloads unless you ask by name. The offline switch makes that a hard rule.

# Search

Call search with a query string. Results carry text, score and metadata.
"""

db = Vectrix("manual")
db.add_document(guide, doc_id="guide", chunk="markdown")
db.add_document(guide, doc_id="guide-sentences", chunk="sentence", chunk_size=120, overlap=40)
```

The chunker is a plain function too, for looking at what a strategy does before anything is written:

```python
from vectrixdb import chunk

for piece in chunk(guide, "sentence", size=120, overlap=40):
    print(piece.index, piece.start, piece.end, piece.heading, piece.text[:40])
```

`chunk()` takes `size` and `overlap` where `add_document()` takes `chunk_size` and `overlap`. It raises when `overlap` is not smaller than `size`. `add_document()` does not: a small `chunk_size` with the default `overlap` of 200 is a request for small chunks, so the overlap becomes a fifth of the size.

## Size and overlap

`chunk_size` is in characters, 1,000 by default, and `overlap` is 200. A chunk is what a result shows and what a model reads, so the size is a trade:

- **Smaller chunks match precisely.** A question about one sentence finds that sentence, and a reranker scores less padding.
- **Larger chunks read well.** An answer that needs the paragraph around a fact gets it.
- **Overlap keeps a sentence that straddles a cut findable from both sides.** It costs index space: an overlap of a fifth stores about a fifth more text.

Parent sections give you both ends of the first trade: see below.

Every chunk records how it was cut, as `_vx_chunk_strategy`, `_vx_chunk_size` and `_vx_chunk_overlap`, beside `_vx_doc_version`, a hash of the text it was cut from. The same text cut differently is a different set of chunks, and these stamps are how the two are told apart.

## The heading goes to the embedder

`embed_heading=True` puts the headings a chunk sits under in front of the text the embedding model reads, `Install > Offline: Nothing downloads unless you ask by name.`, and nowhere else. The stored text, the keyword index and what a result shows are the chunk as written. A section that never names its own subject is found by it that way.

```python
db = Vectrix("headed")
db.add_document(guide, chunk="markdown", embed_heading=True)
db.search("offline install", limit=1).top.metadata["_vx_embed_prefix"]   # 'Install > Offline'
```

The prefix is kept on the chunk as `_vx_embed_prefix`, so `reembed()` embeds the same thing again. A figure always carries its headings to the embedder, asked or not: a bar chart's numbers say nothing about which section they belong to. See also [The heading goes to the embedder](extract-keep-index.md#the-heading-goes-to-the-embedder).

## Small chunks that return their section

`parent_size` indexes small chunks and hands back the section each one sits in. The sections are cut by heading when the document has headings, and otherwise into windows of about `parent_size` characters on sentence boundaries. They are stored beside the collection, not embedded.

```python
terms = ("Payment is due within thirty days of the invoice date. Invoices are sent on the first of each month. "
         "A customer may ask for a copy at any time. "
         "Interest accrues monthly on any overdue balance. It is charged at two percent a month. "
         "A notice goes out after thirty days, and the account is suspended after sixty.")

db = Vectrix("parents")
db.add_document(terms, doc_id="terms", chunk="sentence", chunk_size=80, parent_size=200)

results = db.search("when is an account suspended", limit=3, parents=True)
results.top.id                        # 'terms:parent:1', the section
results.top.metadata["_vx_child"]     # 'terms:5', the sentence that matched
```

`search(parents=True)` replaces each matching chunk with its section, once per section, at the best score among its chunks. Every chunk carries `_vx_parent`, the id of its section. `delete_document()` removes the chunks and the sections together. Where several processes serve one collection, keep the sections where all of them can read them: `Vectrix(..., parent_store=...)` takes a path or a `cosmos://` address. More in [Parent-child retrieval](ingest-documents.md#parent-child-retrieval).

## A model says where topics start

`chunk="llm"` asks a model where each new topic starts. `cut_with=` is what it asks: any callable that takes the text and its paragraphs' `(start, end)` offsets and returns the numbers of the paragraphs that start a chunk, counting from 0. `vectrixdb.chunk_models.llm_cutter` makes one from a chat model:

```python
from vectrixdb import Vectrix
from vectrixdb.chunk_models import llm_cutter
from vectrixdb.evaluation import ChatWriter

chat = ChatWriter("http://localhost:11434/v1/chat/completions", model="llama3.1")   # any OpenAI-style chat route
db = Vectrix("topics")
db.add_document(guide, chunk="llm", cut_with=llm_cutter(chat), chunk_size=1500)
```

The model reads the paragraphs numbered, about 12,000 characters of them at a time (`llm_cutter(chat, window=...)`), and answers with the numbers that start a chunk. A reply that is not those numbers cuts that window by size alone. The same paragraphs asked about twice are answered from the first reply, so building one document at several sizes asks once. A paragraph longer than `chunk_size` is split into its sentences before the model sees it.

`ChatWriter.from_environment()` builds the chat model from settings instead: `AZURE_OPENAI_WRITER_DEPLOYMENT` with `AZURE_OPENAI_ENDPOINT`, or `VECTRIXDB_WRITER_URL` with `VECTRIXDB_WRITER_KEY` and `VECTRIXDB_WRITER_MODEL`. Any callable from chat messages to the model's text works as `chat`.

What the model is sent is marked as data, and anything in the document that could pass for a chat template's control tokens is made inert, so a document cannot give the model instructions. A model that refuses, or says nothing three times running, stops the run with `WriterUnavailable` rather than paying for every chunk to fail.

## A note in front of every chunk

`context_with=` has a model write a sentence or two for every chunk that places it in its document: the document's subject, the section, what the chunk refers to that it does not name. The note goes in front of the chunk for the embedder only, after the heading path when `embed_heading` is on too. The stored text is the chunk as written, and the note is kept on it as `_vx_context`.

```python
from vectrixdb.chunk_models import context_writer

db = Vectrix("noted")
db.add_document(guide, chunk="markdown", context_with=context_writer(chat))
```

`context_writer(chat, around=8000)` sends the model about that many characters of the document around the chunk, with the document's opening as well when the window starts later, since that is where a title and a subject usually are. It is one model call a chunk, so it costs what the collection's size says. A figure gets no note. `context_with` is any callable `(document, chunk, start, end)` returning the note, or `None` for none.

## Late chunking

`late=True` embeds every chunk from the whole document. The embedding model reads the document token by token, in windows as long as it reads, and each chunk's vector is the mean of its own tokens, each read with the words around it. A chunk that says "it is charged at two percent" is embedded knowing what "it" was.

A question is embedded the usual way, which matches the mean of a chunk's tokens only for a model that pools the mean of its tokens. The bundled default, bge-small, pools its first token instead, so a collection on it refuses `late=True` with a `ConfigurationError` that says why. Two kinds of model can do it:

- **e5-small**, `dense_model="e5-small"`, which pools the mean. It is not in the wheel: `vectrixdb download-models --type dense_en` fetches it once. See [Embedding models](embedding-models.md).
- **A model of your own** given as `embed_fn=`, an object that has `embed_tokens(texts)` returning a `(tokens, dimension)` array for each text, read in order.

```python
db = Vectrix("late", dense_model="e5-small")
db.add_document(guide, chunk="sentence", chunk_size=120, late=True)
db.search("downloads", limit=1).top.metadata["_vx_late"]     # True
```

Every chunk embedded late carries `_vx_late`. `reembed()` embeds each stored chunk on its own, so a collection built late is rebuilt with `rechunk()`, which embeds it late again.

## Keep the source, and cut again later

`keep_source=True` keeps the Markdown every document was indexed from, beside the collection, with the settings it was cut with. Then `rechunk()` cuts kept documents again and re-indexes them, reading the kept Markdown and never calling an extractor: a new chunk size over ten thousand scanned pages costs an embedding pass, not an OCR bill.

```python
db = Vectrix("kept", keep_source=True)
db.add_document(guide, doc_id="guide", chunk="markdown")

planned = db.rechunk_preview(chunk="sentence", chunk_size=120, overlap=0)
print(planned)                     # 1 documents, 1 would change: 3 chunks now, 3 after
for doc in planned.changed:
    print(doc.doc_id, doc.chunks_now, "->", doc.chunks_after, doc.chunking_after)

written = db.rechunk(chunk="sentence", chunk_size=120, overlap=0)
```

The two take the same arguments:

| Argument | What it is |
| --- | --- |
| `doc_id` | one document's id, or a list; left out, every kept document |
| `where` | a function of a document's kept entry that says whether to include it |
| `**options` | `add_document()`'s cutting settings: `chunk`, `chunk_size`, `overlap`, `parent_size`, `embed_heading`, `threshold`, `cut_with`, `context_with`, `late`, `dedupe`, `on_low_quality`, `quality_threshold`, `progress`. Anything else is a `TypeError` |

What is not given is what the document was last cut with, and its metadata is what it was written with. A note from `context_with` is recorded as a flag and cannot be called again from one, so pass `context_with=` again to keep the notes. `late` is recorded and used again.

`rechunk()` returns the number of chunks written. Each document is deleted and written again as an ingestion of its own, with its own record and build. On an empty collection opened over the same store, it fills the index from the store alone.

`rechunk_preview()` writes, deletes and embeds nothing. It returns a `RechunkPreview`:

| Field | What it holds |
| --- | --- |
| `documents` | one `RechunkedDocument` a kept document: `doc_id`, `chunks_now`, `chunks_after`, `chunking_now`, `chunking_after`, `changed` |
| `chunks_now`, `chunks_after` | the totals |
| `changed` | the documents whose chunk count or settings would change |
| `to_dict()` | all of it as JSON, for a review step in a pipeline |

Semantic chunking still asks the embedder where topics turn and `llm` chunking still asks `cut_with`, since that is where their cuts come from. A note is not written, since it does not move a cut. The counts are before `dedupe`, which can only lower them.

`add_document(..., index=False)` keeps a document's Markdown and stops: nothing is cut, embedded or indexed, and `rechunk()` cuts it later. That is how a deployment reads its files before it has decided how to cut them. See [Keep what was extracted](extract-keep-index.md#keep-what-was-extracted) for where the store lives and how deletion works.

## Which to pick

| Your documents | Start with | Why |
| --- | --- | --- |
| Anything, when you do not know yet | `recursive`, 1,000 and 200 | it respects paragraphs and needs nothing |
| Docs, READMEs, policies, reports with headings | `markdown`, with `embed_heading=True` | a section is a unit of meaning, and the heading names what it is about |
| Prose, transcripts, support tickets, chat | `sentence` | no chunk starts or ends mid-sentence |
| Long mixed documents with no headings | `semantic` | cuts where the subject changes; costs an embedding a sentence |
| Logs, code, text with no structure to respect | `fixed` | predictable sizes, and nothing to detect |
| Questions about a sentence, answers that need its section | any of the above with `parent_size` | small chunks find, sections answer |
| Documents whose sections run into each other | `llm` with `cut_with` | a model sees topic changes a rule does not; a model call a window |
| Chunks that lean on what came before, "it", "the plan" | `context_with`, or `late=True` on a model that pools the mean | each chunk is embedded knowing its document |

These are starting points. Which wins depends on your documents and your questions, so build two or three and measure them on your own golden questions: [Compare the chunking techniques](measure-retrieval.md#compare-the-chunking-techniques) gives each the same amount of text to read and says which answered more, and [How to cut the documents](measure-retrieval.md#how-to-cut-the-documents) sweeps the sizes. [Evaluate every setup](evaluate-setups.md) compares ways of searching one index, which comes after the cut is chosen. With `keep_source`, trying another cut is `rechunk_preview()` and `rechunk()`, not a second extraction.
