# Ingest documents: chunking, parents, duplicates

`add()` takes texts you have already cut up. `add_document()` takes a whole document, cuts it up well, remembers where each piece came from, and can hand back the section a piece belongs to. This page is the ingestion side of RAG, which is where most of its quality is decided.

## One document, chunked

```python
from vectrixdb import Vectrix

db = Vectrix("manual")

guide = """# Install

Install vectrixdb with pip and you are done. The English model ships in the wheel.

## Offline

Nothing downloads unless you ask by name. The offline switch makes that a hard rule.

# Search

Call search with a query string. Results carry text, score and metadata.
"""

n = db.add_document(guide, chunk="markdown", metadata={"source": "guide"})
hit = db.search("does it need the network", limit=1).top
hit.metadata["heading"], hit.metadata["_vx_doc"], hit.metadata["_vx_chunk"]
```

Every chunk carries the document id (`_vx_doc`), its position (`_vx_chunk`), its character offsets, the `heading` it sits under, the `page` when the source has pages, and the document's `title` when it has one. Your own `metadata` is merged in.

`source` can be a string, a file path, or a `LoadedDocument`. Paths ending in `.pdf`, `.docx`, `.doc`, `.rtf`, `.odt`, `.ods`, `.odp`, `.pptx`, `.xlsx`, `.xls`, `.csv`, `.tsv`, `.html`, `.md` or anything else are read by the matching loader. `pip install vectrixdb[documents]` brings what PDF, Word and Excel need: PDFium through `pypdfium2` with `pypdf` beside it, `python-docx`, `openpyxl`, and `xlrd` for the old `.xls`. PowerPoint, OpenDocument, RTF, CSV, HTML, Markdown and text use the standard library. A Markdown pipe table, like a deck's table, is read as one line per row.

A file is known by its first bytes as well as its name. A PDF saved as `.docx` is read as a PDF, and a file whose bytes are not the Office file its name says is refused, saying so, rather than failing inside a package with "Package not found". Text is decoded by its byte order mark, then as UTF-8, then Windows-1252, so a file saved by an old Windows program keeps its accents and quotes; a form feed starts a new page.

A spreadsheet becomes one line per row, `Region: EMEA; Revenue: 1200`, under a heading per sheet, so a row reads like a sentence to the embedding model and its cells are keywords to the sparse index; the citation is `book.xlsx#Sales`. A number reads as the sheet shows it, `$1,200.00`, `12.5%`, `(350)` or `Oct 2025`, not as the number stored under it, `0.125`, so a search for what a person saw finds it. A header is found by what its cells hold: a row of words or years over rows of numbers, two such rows joined per column, `2024 H1`. A sheet with a title over its table, or two tables with a blank row between them, gives each table its own header. A two-column form reads `Applicant name: Jane Doe`. Hidden sheets are left out and counted as `sheets_hidden`. A formula saved with no value, by software that never calculated it, reads as its formula, `=SUM(B2:B4)`, rather than as nothing, and a chart is read from the values it was drawn with. A CSV is read in whatever delimiter and encoding it was written in, `sep=;` on its first line included.

A deck becomes one page per slide with the slide title as its heading, read in the order a person looks at it, top to bottom and left to right, whatever order the shapes were added in. Bullets keep their levels, tables become rows, a chart gives its values, SmartArt its words, a picture its alt text, and its speaker notes follow; the date, footer and slide-number placeholders are left out, and so are hidden slides, counted as `slides_hidden`. It is cited as `deck.pptx#slide=4`.

A Word document is read in order. Its tables become rows where they sit, with a merged cell's value in every row it spans, and the rows Word repeats at the top of each page are the header; list items and headings keep the numbers Word prints, `1.1`, `(a)` or `Article I -`; a table of contents is left out, since its entries are the headings again; a footnote or an endnote follows the paragraph that calls it, `[^1]: ...`; a comment follows the paragraph it is on, `[Comment: ...]`; an equation is written out as text, `a/(b+c) = x^2`; a chart gives its values, SmartArt its words and a picture its alt text; a text box is read once, and a tracked deletion is not read. The page header and footer are kept as `page_header` and `page_footer`, not repeated in the text. Its pages are the ones Word marked the last time it laid the file out, so its chunks cite `policy.docx#page=3`, and a section that numbers its own pages, from 1 again or in roman numerals, gives the printed number. A file saved by software that lays nothing out has no such marks, and its chunks cite their heading. An RTF file keeps its tables as rows and leaves out hidden and deleted text. An OpenDocument text, spreadsheet or presentation keeps its headings, lists and tables as rows, and leaves out tracked changes and hidden text. A web page's tables become rows too, and its lists keep their markers.

A PDF is read by PDFium, from where each run of text sits on its page, not from the order the file stores them in. Columns are read one after the other. A running head or foot is left out by its place in the margin and its repeating there, so a table's `Total` row, in the same place on every page but not in the margin, stays. A word broken by the line is joined, keeping its hyphen only where the document writes it that way elsewhere; a raised footnote number is written `[^3]` and not glued onto its word; a line set larger, or bold and alone, is a heading. A table's rows are written with the header over each figure, `Net interest income; 2025 Oct. 31: $ 8,545`. What a filled form says sits beside its label, and a reviewer's note is kept as a comment. White text on a white page, and invisible text no picture lies under, is left out and counted as `hidden_text_left_out`: that is how a file hides instructions from whatever reads it. A page with no text that nothing read is named in `pages_without_text`. Without `pypdfium2` the plain `pypdf` reader still reads the file.

The rules are exact on prose and on tables whose figures line up, and unsure on a page laid out to be looked at: a snapshot of big figures with their labels under them, a panel of charts, a page whose file draws its parts out of order. `page_reader=` reads those pages by sight. A `PageReader` is given a picture of each such page and the words it holds, and writes the page out as a person reads it, each figure kept with what it measures; every number it writes must be printed on the page, or it is taken out, and a reading with too many taken out is not used. `every_page=True` sends it every page.

```python
from vectrixdb import load_document
from vectrixdb.extract import PageReader

reader = PageReader.azure_openai("https://your-openai.openai.azure.com", "gpt-5.4-mini", key="your-key")
doc = load_document("annual-report.pdf", page_reader=reader)
doc.metadata["pages_read_by_sight"]    # [2, 8, 32, ...]
```

A web page is read as its content, not as the site around it. When the page has a `<main>`, or a `role="main"`, only what is inside it is read; when it has exactly one `<article>`, that is. Everywhere, what the page marks as around its content is left out: navigation, banners, footers, dialogs, form controls, cookie notices, breadcrumbs, and anything hidden by `hidden`, `aria-hidden` or `display: none`. A list that is nothing but links, outside the content, is a menu and is left out too. A `<main>` a script fills in after the page loads holds almost nothing when it is fetched, so then the whole page is read, still without what is around it. A list item keeps its later paragraphs on indented lines under it, a line break is a line, and `<pre>` becomes a fenced code block with its indentation. The page is decoded in the charset it names, and a page that names none is read as UTF-8, else Windows-1252.

A PDF's bookmarks become its headings, each where its title is written on its page, and the headings its type shows nest under them, so its chunks carry the section they sit in. Every file's own title, a PDF's or an Office file's title property, a web page's `<title>`, a Markdown file's front matter or first heading, is on every chunk as `title`. Charts and pictures are read when something describes them; see [Extract, keep, index](extract-keep-index.md). A chart a PDF draws with lines and rectangles, rather than pasting in as a picture, is drawn as a picture of its own region when pictures are asked for, `images=True`, titles, scales and legend included, and becomes a figure named by its page, `p8-chart1.png`, for the describer. The figure stands where the chart is on the page, and the words the chart prints, its title, its scales, a value over each bar, leave the page's text for one line under it, `Words in the picture: ...`, rather than a column of loose figures between paragraphs. A description takes the place of that line; a chart nobody describes keeps it, so what it printed is never lost.

A Markdown file's headings are found outside its code blocks only, underlined headings and `<h2>` count too, and front matter may be YAML or TOML. Inline HTML is taken out of the text, leaving its words.

A file type the built-in readers cannot read, a scanned PDF, a recording, a format of your own, is read by an extractor: a function you register or an endpoint you run. [Extract, keep, index](extract-keep-index.md) covers both, along with the local OCR and speech engines. PDF, DOCX and XLSX come with `pip install vectrixdb[documents]`.

## Strategies

| `chunk=` | What it does | Use it for |
| --- | --- | --- |
| `recursive` | Splits on paragraphs, then lines, sentences, words; merges up to `chunk_size` with `overlap` characters carried over. The default. | Anything, when you do not know better |
| `sentence` | Whole sentences packed to `chunk_size`, overlapping by a sentence. Never cuts mid-sentence. | Prose, transcripts, support tickets |
| `semantic` | Embeds every sentence and starts a chunk at the sharpest topic changes (`threshold` is the fraction of boundaries that split). | Long mixed documents, when embedding cost is fine |
| `markdown` | A chunk per heading section, long sections split recursively, heading kept on metadata. | Docs, READMEs, anything with headings; also HTML and DOCX, whose headings are converted |

The chunkers are plain functions too:

```python
from vectrixdb import chunk, load_document

pieces = chunk(guide, "sentence", size=200, overlap=40)
pieces[0].text, pieces[0].start, pieces[0].heading
```

## Pages and figures

A printer breaks sentences across pages; the PDF loader joins a page to the next with a space when the sentence plainly runs on, so it reaches the chunker whole. A chunk that still spans pages carries `page` for the page it starts on and `page_end` for the one it ends on; its citation names the first. A page holding nothing but a page number is dropped from the text and still counted.

A figure is one unit. A Markdown image or an HTML `<img>` or `<figure>` becomes a `[Figure: caption]` line on its own paragraph, and the chunker keeps it as a chunk of its own with `figure` set to the caption, so a caption is never split and never glued to the paragraph before it. Its citation is the page plus the caption, `report.pdf#page=3(Revenue by region)`. What is searchable is the caption: this library does not describe images, and a figure without a caption is named after its file.

```python
figured = "# Results\n\nRevenue grew in every region.\n\n![Revenue by region](q3.png)\n\nCosts were flat.\n"
n = db.add_document(figured, chunk="markdown", metadata={"source": "q3"})
fig = [h for h in db.search("revenue by region chart", limit=5) if h.metadata.get("figure")][0]
fig.metadata["figure"], fig.citation
```

## Parent-child retrieval

Small chunks match precisely; big chunks read well. Index the small ones and return the big ones:

```python
db = Vectrix("pc")
db.add_document(guide, chunk="sentence", chunk_size=120, parent_size=600, doc_id="guide")

results = db.search("hard rule about downloads", limit=3, parents=True)
results.top.id                          # "guide:parent:0", the section
results.top.metadata["_vx_child"]       # the sentence that matched
```

With `parent_size`, chunks are grouped into sections (by heading when there are headings, else by size on sentence boundaries), the sections are stored beside the collection, and `search(parents=True)` replaces each matching chunk with its section, once per section, at the best of its chunks' scores. Token budgets apply to the returned text, so a `token_budget` still holds.

`delete_document("guide")` removes the chunks and the sections together.

## Near-duplicates

Re-pasted paragraphs and near-identical revisions push better results off the page. `dedupe=` skips them:

```python
db = Vectrix("notes")
db.add("Sourdough starter is flour and water kept alive by wild yeast.")
db.add("Sourdough starter is flour and water kept alive by wild yeast!", dedupe=0.8)
db.last_add_report.skipped     # [(skipped_id, duplicate_of, similarity)]
```

Similarity is MinHash over word 3-shingles, so it is about shared phrasing, not meaning; 0.9 catches copies and light edits, 0.6 catches heavy rewrites of the same paragraph. The index lives in one JSON file beside the collection and is built from the stored texts the first time you ask.

## Extraction quality

A scanned PDF that OCRs badly produces chunks that pass every schema check and destroy retrieval: the fields are all there, the text is nonsense, and the failure shows up later as a ranking problem nobody can explain. `add_document` scores every chunk for extraction quality and stamps the score as `_vx_quality`; a document whose mean score falls below the threshold reads as a failed extraction.

```python
from vectrixdb import Vectrix, extraction_quality
from vectrixdb.quality import degrade

db = Vectrix("scans")
report = "The facility covenant requires a fixed charge coverage ratio of not less than 1.15x, tested quarterly against the borrower's accounts and reported to the agent within thirty days of each quarter end."

print(extraction_quality(report).usable)                      # True
print(extraction_quality(degrade(report, 0.4)).usable)        # False: a simulated bad OCR pass
print(extraction_quality(degrade(report, 0.4)).signals)       # which signal failed
```

`on_low_quality` is `"warn"` by default: the document is written and `ExtractionQualityWarning` names it and its score. `"reject"` refuses it with `ExtractionQualityError`, which is the right setting once you have seen the scores on your own documents. `"allow"` is silent.

```python
db.add_document(report, on_low_quality="reject")
```

The detector is five signals that a bad OCR pass moves and clean prose does not: the share of ordinary characters, of whole words, of pronounceable words, the hit rate against a short list of common English words, and mean word length. A sixth, `varied`, scales the score rather than being weighed into it: it is 1 for text that goes somewhere and falls towards 0 for text that loops, the same word a hundred times, which is how OCR fails on noise, or the same sentence over and over, which is how a vision model fails on a page it cannot read. Looping text is made of perfectly good words, so it scored a perfect 1.0 on the other five. Ordinary prose has `varied` at exactly 1, so the threshold and the numbers it was measured with are where they were. The threshold is measured, not guessed: `scripts/quality_eval.py` rebuilds a labelled set of real prose from these docs and the same prose put through a simulator of what OCR gets wrong, and the numbers it printed are quoted where `vectrixdb.quality.DEFAULT_THRESHOLD` is set. Five percent noise, a page a person can still read, is above the line on purpose; twenty percent and above is below it.

It is measured on English prose. For anything else, calibrate against your own labelled set and pass the threshold you get:

```python
from vectrixdb.quality import calibrate

calibration = calibrate([(report, True), (degrade(report, 0.4), False)])
db.add_document(report, quality_threshold=calibration.threshold)
```

It judges prose. A table's rows, `Region: EMEA; Revenue: 1200`, pipe rows and figure lines are short keyed pieces rather than sentences, and are left out of the score, so a page that is mostly a table scores on the sentences around it, and a page that is all table passes.

What it is not: a language model, a dictionary, or a judgement about meaning. A clean page about nothing scores well. It catches the failure that reads as a permissions or ranking problem when it is an ingestion problem, at the write, where the stack trace points at the pipeline.

## Citing sources

Every chunk `add_document()` writes carries a citation string: `report.pdf#page=3` for a paged source, `deck.pptx#slide=4` for a deck, `call.wav#t=665` for a recording, the second the chunk starts at, `guide#Offline` for a markdown section, the bare name otherwise. It is what an answer should write in square brackets, and it is on every result:

```python
from vectrixdb import Vectrix, CITATION_INSTRUCTION, validate_answer

db = Vectrix("cited")
db.add_document(guide, chunk="markdown", metadata={"source": "guide"})

hits = db.search("does it need the network", limit=3)
hits.citations()
```

`hits.sources()` is the block to put in the prompt, one `[citation]: text` line per result, and `CITATION_INSTRUCTION` is the sentence to put beside it. The part that matters comes after the model answers:

```python
answer = "Nothing downloads unless you ask [guide#Offline]. It is fast [benchmarks.pdf#page=9]."
checked = validate_answer(answer, hits)
checked.cited, checked.rejected
```

A bracket that names a source the model was given stays a citation. One that names anything else becomes plain text and is listed under `rejected`, because a model will write a plausible reference when it has nothing to cite, and a reader must never be shown one as if it were real. `checked.text` is the answer to display, and `checked.numbered()` turns the citations into `[1]`, `[2]` with the list they index, for footnotes.

Beside the answer, show `hit.readable_citation`: the same place as a person reads it, `report.pdf, pp. 39-40`. Where a PDF numbers its own pages, a report whose covers are C1 and C2 say, it is the number printed on the page, read from the PDF's page-label table, while the citation keeps the page's place in the file, which is what opens the right page in a viewer. A chunk that runs onto the next page gives the range, a deck `slide 4`, a recording `11:05`. It is stamped on every chunk as `_vx_readable_citation`.

A citation is a pointer a person can follow, not proof. The proof is the chunk's provenance, its build, document version and quality score, which `db.provenance(ids)` carries and a citation string does not.

## The embedding cache

Vectors are cached by content and model in `_embed_cache.db` beside the collection, so re-ingesting a folder where three files changed embeds three files. It is on for collections with a path and off for custom embedding functions; `Vectrix(..., embedding_cache=False)` turns it off.

## Changing the embedding model

The model a collection was built with is recorded in the collection. Opening it with another model raises `ModelMismatchWarning`, because queries would then be embedded differently from the documents and every score would be noise. The migration is one call:

```python
db = Vectrix("manual")     # warns if the recorded model differs
db.reembed()               # re-embeds every text with the current model, rebuilds the index
```

The new model must produce vectors of the same width; a different dimension needs a new collection, which `export()` and `add()` cover.
