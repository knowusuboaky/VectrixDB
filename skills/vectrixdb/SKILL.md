---
name: vectrixdb
description: Add search over documents or records to a Python project with VectrixDB, or answer a question from a VectrixDB collection, citing every result. Use when the person asks for search, retrieval, RAG or question answering over their files in a Python project, names VectrixDB, or has a VectrixDB collection to answer from.
---

# VectrixDB

VectrixDB is a Python vector database that works offline: `pip install vectrixdb` holds its English models. These are the rules for using it in someone's project. For everything else read https://knowusuboaky.github.io/VectrixDB/llms.txt, and the pages it links that the task needs, before writing code. Use the library as documented and do not invent parameters.

## Install the smallest set

Look at the project first and say what you plan, in a few lines, before installing anything. `vectrixdb` alone searches inside one Python process. Add only the extras the plan needs:

- `documents` for PDF, Word and Excel files
- `api` for the REST server and its dashboard, `vectrixdb serve`
- `mcp` for the MCP server
- `ocr`, `asr`, `video` or `youtube` for pictures, recordings, videos or YouTube addresses
- a storage extra such as `azure` or `postgres` only when the person already uses that store

On a server, `vectrixdb check` says which kinds of file it can read and what to install for the rest.

## Open, add, search, cite

```python
from vectrixdb import Vectrix

db = Vectrix("docs", path="./data/vectrixdb", keep_source=True)
db.add_document("handbook.pdf", doc_id="handbook.pdf")
db.add(["Refunds take five working days."], metadata=[{"source": "faq.md"}])
for result in db.search("how long do refunds take", limit=5):
    print(result.readable_citation, result.text)
```

- Open the collection in one place, with a `path=` the project chooses.
- Use `add_document` for files and `add` for records. Give each file a `doc_id`, its name say, so a golden file can name it; without one its id is a hash of its text. Give records `metadata` that says where each came from.
- `keep_source=True` keeps each document's Markdown, so its chunking can change later without reading the files again.
- An answer cites each result by its `citation`. Show its `readable_citation`, the file and page as a person reads them, beside the answer, and its `relevance` if the app shows a number.
- Catch `vectrixdb.exceptions.VectrixError` where the app can say something useful, never a bare `Exception`.

## Pick the mode by evaluation

The modes are `dense`, `sparse`, `hybrid`, `ultimate` and `graph`. Do not guess one. Write five to ten of the person's own questions, each with the ids of the documents that answer it, and run `vectrixdb.evaluation.evaluate` on the collection opened with `mode="ultimate"`; opened with the default, it offers only dense and keyword search to compare:

```python
from vectrixdb import Vectrix
from vectrixdb.evaluation import Question, evaluate, save_questions

save_questions([Question("How long do refunds take?", expected=["handbook.pdf"])], "golden.jsonl")
report = evaluate(Vectrix("docs", path="./data/vectrixdb", mode="ultimate"), "golden.jsonl")
print(report["picks"])
```

The picks are `finds_the_most`, `best_for_balance` and `best_for_time`. `vectrixdb.evaluation.golden_template` writes a file to fill in from the collection's own documents, each row with its id already there. Show the person the report and let them choose; nothing is switched for them.

## Ask before these

- **Re-chunking.** `rechunk()` deletes and rewrites every chunk of the documents it picks. Show the person `rechunk_preview()` with the same arguments first; it writes nothing.
- **Deleting** a collection or documents, with `vectrixdb delete`, `db.clear()`, `db.delete(...)` or `db.delete_document(...)`. There is no undo on a store without its own backup.
- **A deployed store or server**, Azure AI Search, Cosmos DB, Postgres or a running `vectrixdb serve`: other people use it.
- **Tracing.** It is off; never turn it on silently. Offer it: `pip install "vectrixdb[tracing]"` with `OTEL_EXPORTER_OTLP_ENDPOINT`, or `vectrixdb.tracing.enable()`. Spans carry counts and timings, never query or document text.

## Answer from a collection

To answer from an existing collection in this session, open it with the same name and `path=` it was written with, search it, and cite each result by its `citation`. To give an assistant the collection as a tool, run the MCP server, `vectrixdb mcp --name docs --path ./data/vectrixdb` (`pip install "vectrixdb[mcp]"`), and register that command with the client; its `search` tool returns each result with its id, which is what to cite.
