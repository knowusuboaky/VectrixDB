# Evaluate every setup

Which engine, which embedding model and which search method should a collection use? The honest answer comes from your own questions. `vectrixdb.evaluation.evaluate` searches every golden question every way your collections can be searched, times each search, ranks the ways, and names three of them:

| Pick | The rule |
| --- | --- |
| `finds_the_most` | the most questions with the right answer in the top 10; a tie goes to more right answers first, then to the faster |
| `best_for_balance` | the fastest setup within 4 percentage points of `finds_the_most` |
| `best_for_time` | the fastest setup within 8 points of it |

Within is inclusive: at 4 points, 92% counts when the most is 96%. The 4 and the 8 are settings. All three can be the same setup. Nothing is switched: the picks are for a person to choose between, and the choice goes into the ingestion and search settings by hand.

## The golden questions

A golden file is JSONL: a question, the ids of the documents that answer it, and, if answers are to be scored too, the answer a person would accept. `golden_template` writes one to fill in from the collection's own documents, a row per sampled document with its id already in `expected` and the start of its text in `hint`:

```python
from vectrixdb import Vectrix
from vectrixdb.evaluation import Question, golden_template, read_golden, save_questions

db = Vectrix("policies", mode="hybrid")
db.add_document("Payment deferment: customers in disaster areas can defer loan payments with no late fees.", doc_id="deferment")
db.add_document("Emergency fund access: expedited access to funds and waived fees for displaced customers.", doc_id="emergency-fund")
db.add_document("Early divestment guidance for customers who sell investments before they mature.", doc_id="divestment")

rows = golden_template(db, "template.jsonl", n=50, seed=0)
rows[0]["expected"], rows[0]["question"]      # (['deferment'], '')
```

A person writes, in `question`, what somebody would ask that the document answers, in their words and not the document's. Rows left empty are skipped when the file is read and counted in `read_golden(path).unfilled`, so a file can be filled a few rows at a time. The same seed on the same collection gives the same rows. A `writer=` callable, any model call from a document's text to a question, drafts the questions instead, and marks each `"draft": true` for a person to check: a question written by a model that has just read the document is easier than the ones people ask. `write_golden()` does that job properly, from the chunks with a critic and checks no model makes: see [Draft the questions with a model](measure-retrieval.md#draft-the-questions-with-a-model).

`by="page"` writes a row a page instead, `expected` naming it, `report.pdf#page=41`, and `hint` saying which page it is, its printed number and the heading it sits under. A report of two hundred pages is one document, so every setup finds "the document" for every question about it and the setups all tie; the page is what tells them apart. The pages are spread evenly through the collection, so every part of a long report is asked about, pages with next to no text on them are left out, and a document without pages, a recording or a picture, keeps its one row. Pages come from the kept Markdown, so the collection keeps its documents. A list of handles samples several collections into one golden file.

The file can live anywhere the evaluation runs from: a path, `s3://bucket/key`, or a Blob address, `https://<account>.blob.core.windows.net/<container>/<name>`. S3 uses boto3's default credentials and Blob the default Azure credential, which is the managed identity inside a function; `fetcher=` takes any object with `fetch(uri) -> bytes`, the fetchers in `vectrixdb.worker` for example, around a client you built. `read_golden` also returns the file's sha256, which is how a run is filed and how a function watching the file tells a real change from a save that changed nothing.

## A run

```python
from vectrixdb.evaluation import evaluate

questions = [
    Question("Can a customer postpone a loan payment after a wildfire?", expected=["deferment"]),
    Question("A displaced customer needs cash today. What can we offer?", expected=["emergency-fund"]),
    Question("Can I sell my bonds before they mature?", expected=["divestment"]),
]
save_questions(questions, "golden.jsonl")

report = evaluate(db, "golden.jsonl", save_to="vectrixdb_data/evaluations")
report["picks"]                               # the three keys
for setup in report["setups"][:3]:
    print(setup["rank"], setup["method_label"], "on", setup["engine"], setup["summary"]["found"]["10"], setup["summary"]["median_ms"])
```

Every setup the handle can do is run: keyword alone, dense, hybrid without the reranker, hybrid reranked, and ultimate. A handle offers what it was opened for, so open a collection with `mode="ultimate"` to include every method; opened with the default, it offers dense and keyword. A collection with two dense models, a local `dense_model=[...]` list or a store built with `embeddings="both"`, is searched with each model alone and with both fused.

Before any searching, every document the golden file expects is looked up on every engine. One an index does not hold, removed or renamed since the question was written, is a miss on every setup and lowers every score, so the run warns about it at once, with the questions that cannot be found, and keeps it under `missing` in the report. The questions stay in: the scores say what searching that index really finds. Correct the ids in the golden file, or add the documents.

Each search is timed as a caller sees it, the query's embedding and the reranking included, after one untimed search that loads the models, and with the search cache out of the way. Positions count results as they come back, so a document that fills three of the top ten takes three places. A setup whose first search fails, a graph that was never built for instance, is reported with its error and is never picked.

The report holds, for every setup, the share of questions found at 1, 3, 5, 10 and 20, how far down each answer was, the median and 95th percentile search times, the position of each question's answer, and the setups one change away: another ranker, one model fewer, the same search on another engine. It is one file a run, and it holds the three picks.

## More than one engine

Pass every handle to compare, the same documents on each, as a mapping from a name people will read to the handle, or as `Target` objects:

```python
from vectrixdb import VectrixDB
from vectrixdb.evaluation import Target, evaluate

azure = VectrixDB.with_azure_search("https://acme.search.windows.net", key=YOUR_KEY, semantic=True, embeddings="both",
                                    azure_embedding={"endpoint": YOUR_OPENAI, "deployment": "text-embedding-3-small", "dimensions": 1536})
report = evaluate(
    [
        Target(Vectrix("policies", mode="ultimate"), name="VectrixDB"),
        Target(Vectrix("policies", storage_backend=azure, mode="hybrid"), name="Azure AI Search"),
    ],
    "s3://evals/policies/golden.jsonl",
    save_to="s3://evals/policies",
)
```

Three things decide what an engine offers:

- **Azure AI Search's semantic ranker is chosen search by search.** On an index with a semantic configuration, keyword search runs with the service's ranker and without it, and hybrid search with it, with MiniLM here instead, and with neither. So one index gives every setup; with two sets of vectors, fourteen. Opening an index with `semantic=True` adds a configuration to one that has none, and one it has, made here or in the portal, is kept whichever way a handle opens it.
- **ColBERT and the graph are local,** so a store offers neither.
- **Keyword search is the local words index while it holds anything, and the service's own otherwise.** A handle opened only to search an index somebody else filled holds nothing locally, and its keyword search goes to Azure AI Search or OpenSearch.

One Azure index built with `embeddings="both"` and a semantic configuration, opened with `mode="hybrid"`:

| Method | Runs as | Setups |
| --- | --- | --- |
| Keyword | BM25 over the words | 1 |
| Keyword + semantic ranker | the service's BM25, then its ranker | 1 |
| Dense | each vector alone, and both fused | 3 |
| Hybrid | words and vectors fused, not reranked | 3 |
| Hybrid, reranked | then MiniLM, here | 3 |
| Hybrid + semantic ranker | then the service's ranker | 3 |

## Read the results

The server's Evaluate page shows the newest run on its Retrieval tab, beside the Chunking tab of [Compare the chunking techniques](measure-retrieval.md#compare-the-chunking-techniques): the three picks, found in the top 10 against the median search time with the setups nothing beats on both joined by a line, the numbered setups found at each depth, and every setup ranked, seven to a page. A setup's own page has how far down its answers were, leading with the answer's place on average and the mean reciprocal rank in its tooltip, its search times, the setups one change away, and its top 10 run by run over the runs with the same questions, with a mark where the index changed between two of them.

A line above the picks names the golden data, how many questions it holds, and the collection the run searched. The menu at its end opens an older run, twelve at a time, each named by its collection, and an older run on screen says so, with a way back to the newest. A run records its collections when it is saved, and one saved before that is read off its targets, so every run in the menu says where it ran. When a run found expected documents that an index does not hold, a note above the picks names them and the questions that cannot be found, in the words the command line uses.

**Downloading the golden file.** An admin signed in as themselves sees a download button beside the golden data. It gives back the exact file the run read, with every question, reference answer and expected id, even after the file has changed since. Each version is kept once, as `golden_dataset/<sha256>.jsonl` beside `retrieval/runs/`, by `evaluate` and by the functions described below; one kept before as `golden/<sha256>.jsonl` is still read. Nobody else can download it, a key included, not even the server's own admin key: a run holds scores and ids, but the golden file holds the questions' text, so it goes only to a person the access log can name. A run saved before files were kept, or made from questions held in memory, has no file, and the button is not shown.

The server reads runs from `VECTRIXDB_EVALUATIONS`: a folder, `s3://bucket/prefix` or a Blob address. Left unset, it is the `evaluations` folder under the server's data path. `vectrixdb evaluate` saves to the same place: `--save-to` when given, else `VECTRIXDB_EVALUATIONS`, else the `evaluations` folder under its `--path`. A run holds scores and ids, never the text of a question or a document, so anybody who may see a collection's health may see it, guests included; the action is `evaluation.read`.

| Route | What it answers |
| --- | --- |
| `GET /api/v1/evaluations?limit=20&offset=0` | the runs, newest first; `offset` skips that many of the newest, for the next page |
| `GET /api/v1/evaluations/{run}` | one run's report, `latest` for the newest, with `missing_notes` in words and `golden_download` saying whether this caller may download its file |
| `GET /api/v1/evaluations/{run}/golden` | the golden file that run read; `403` unless the caller is an admin signed in as a person, `404` when no copy was kept |

## The names elsewhere

The page says what each number means in plain words. Somebody arriving from
a paper or from a RAG evaluation tool knows the same numbers by other names:

| On the page | Elsewhere |
| --- | --- |
| Found in the top k | recall at k, or hit rate at k: the share of questions whose expected document was in the first k results |
| How far down the answer was, and the place it leads with | MRR, mean reciprocal rank: the average of 1 over the answer's position, 1 when it is first every time; the tooltip on the place gives it |
| Answered right, on the Chunking tab | the nearest thing to context recall: whether what was retrieved holds what the reference answer needs, checked by the reference answer's words rather than by a judge model |
| The score on every hit | cosine similarity, brought from each engine's own scale to 0 to 1 |
| Choosing an embedding model | MTEB, the public benchmark that ranks embedding models on retrieval and other tasks; a run on your own questions is the test that counts |

Precision at k is not shown: with one expected document a question it is
recall at k divided by k, the same curve on another scale. Context precision
needs a judge model reading the retrieved text, which a run does not do.

## From the command line

```bash
vectrixdb golden template policies --out golden.jsonl -n 50
vectrixdb evaluate policies --golden golden.jsonl
vectrixdb evaluate policies --golden s3://evals/policies/golden.jsonl --save-to s3://evals/policies --balance 3 --time 6
```

`evaluate` opens each collection read only, with `--mode ultimate` unless told otherwise, says at once when the golden file expects a document a collection does not hold, prints the ranked setups and the three picks, and saves the run. On the server's own machine, `--env-file` reads the server's settings file, so the run finds the same data and lands where the Evaluate page reads it: see [Deploy the server](deploy.md).

## Every time the golden file changes

A run is minutes of searching, so it belongs beside the golden file, not on a button. A Function App, on the Flex Consumption or Premium plan, runs one whenever `golden.jsonl` is saved:

1. Event Grid sends `BlobCreated` for names ending in `golden.jsonl`.
2. The trigger reads the file's sha256 and starts one Durable Functions orchestration named by the collection and the hash, so the same event twice, or the same bytes saved twice, starts one run.
3. The orchestration waits a minute and reads the hash again. When a newer save replaced the file, that save's own run covers it, so three saves in a row cost one run.
4. It lists the setups and runs each as its own activity, in parallel. They only search: the index already exists, and it is opened with the semantic ranker, so every setup runs on it.
5. It looks up every document the golden file expects, then saves the report, one file holding the three picks and anything `missing`, as `retrieval/runs/<id>/report.json` beside the golden file. A run saved before 2.2, at `runs/<id>/report.json`, is still read.
6. A person chooses. Nothing in it changes what production searches with.

Its `evaluate_now` route runs the same thing on demand, which is what an ingestion pipeline calls when it finishes: new documents change the picks as much as new questions do. The subscription:

```bash
az eventgrid event-subscription create --name golden-changed \
  --source-resource-id "$STORAGE_ACCOUNT_ID" \
  --endpoint-type azurefunction --endpoint "$FUNCTION_APP_ID/functions/golden_changed" \
  --included-event-types Microsoft.Storage.BlobCreated --subject-ends-with golden.jsonl
```

The function reaches the index the way any client does, so a collection kept in SQLite on a VectrixDB server's own disk cannot be evaluated from it: run `vectrixdb evaluate` on that server instead, on a timer or from the same event. On AWS, the same run goes behind an S3 notification; past Lambda's fifteen minutes, a Step Functions map over `setups_of` does what the orchestration does.

## What it does not do

- **Chunking is not compared.** A run searches the index as it was built; how documents are cut is an ingestion setting.
- **Answers are not scored here.** `answer_report` in [Measure retrieval](measure-retrieval.md) does that, with a judge you bring.
