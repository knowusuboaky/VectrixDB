# Measure retrieval

A retrieval number only means something over questions whose answers you know, from your own documents. `vectrixdb.evaluation` reads such questions, scores what comes back for them, and, with a judge you bring, scores the answers written from it.

## Questions

One JSON object a line: the question, the answer a person would accept, and the ids of the documents that hold it.

```python
from vectrixdb import Vectrix
from vectrixdb.evaluation import Question, load_questions, retrieval_report, save_questions

db = Vectrix("policies")
db.add_document("Payment deferment: customers in disaster areas can defer loan payments with no late fees.", doc_id="deferment")
db.add_document("Emergency fund access: expedited access to funds and waived fees for displaced customers.", doc_id="emergency-fund")

save_questions([Question("Can a customer postpone a loan payment after a wildfire?", "Yes, payments can be deferred.", ["deferment"], "q1")], "golden.jsonl")
report = retrieval_report(db, load_questions("golden.jsonl"), k=(1, 3))
report["recall"], report["mrr"], report["misses"]
```

`by="doc"`, the default, counts a hit when any chunk of an expected document comes back, which is the right grain when the labels name documents. An entry may name one page of one instead, `report.pdf#page=41`, the page's place in the file as its citation gives it: a chunk counts when it covers that page, and the results are ranked by the pages they cover. That is the grain for a long report, which is one document that every search finds. `by="chunk"` wants chunk ids. `by="evidence"` is finer than a page: for a question whose row has `evidence`, a chunk counts only when it holds those words, all of a quote or half of it in a row where the chunk ends inside it, so on a dense page the paragraph beside the answer does not count as the answer. A question without evidence is counted by its pages, and `evaluate()` takes the same `by`. Everything after `k` and `by` goes to `search()`, so the same questions can be asked with another `mode`, `rerank=False`, or on a store with two vectors `vectors="azure"`, and the reports compared. Questions without labels are counted and left out, and a report over none of them says so rather than reporting a perfect score. `misses` lists each question that found nothing right, with what came back instead, which is where the work is.

## From a Bedrock evaluation dataset

`load_questions()` recognises the prompt dataset a Bedrock RAG evaluation job takes, `conversationTurns` with a prompt and reference responses. That file has reference answers and no document ids, so retrieval cannot be scored from it until somebody says which documents are right. `suggest_expected()` proposes them, by searching with the reference answer, which finds the document that says it far more reliably than the question does:

```python
from vectrixdb.evaluation import suggest_expected

questions = load_questions("golden.jsonl")
suggest_expected(db, questions)      # {"q1": ["deferment"]}
```

It proposes and does not apply: a person reads the suggestions and keeps the right ones. Searching with the answer is a fair way to propose a label and an unfair way to score one.

From a terminal, `vectrixdb golden label NAME --questions bedrock.jsonl --out golden.jsonl` writes the same proposals into a golden file, each marked `"draft": true`. Read each against its document, correct `expected` where it is wrong, and delete the mark from the ones you checked. It never writes over a file that is already there, since that file may hold labels somebody checked.

## Check a golden file before a run

A golden file is held to one schema, [golden.schema.json](../reference/golden.schema.json), which says what a line may hold: `question` and `expected` always, and `id`, `reference`, `evidence`, `hint` and `draft` if it has them. `evidence` is the words on the expected pages that answer the question, exactly as they are there, one quote an item. `check_golden()` finds every way a file falls short of it at once, with the line each is on, so a file with five mistakes is put right in one go rather than five runs:

```python
from vectrixdb.evaluation import check_golden

check = check_golden("golden.jsonl", db)
check.ok, check.ready
print(check.summary())
```

An error stops a run: a line that is not JSON, a field the schema does not have, a question or `expected` missing or empty, a value of the wrong kind, an id or a question used twice, or no question ready at all. A misspelt field is the one that mattered most, since `"expeted"` used to drop its row from the scores without a word. Notes do not stop a run: template rows still waiting for a question, drafts nobody has checked, and, when the collection is given, documents it does not hold. `expected` names a document by its id, `deferment`, or one page of it, `report.pdf#page=41`, the page's place in the file as its citation gives it. A Bedrock dataset names no documents, so it is refused until it is labelled. The file is checked the same way wherever it comes from, a path, `s3://` or a Blob address, and `check.golden` is the file as `read_golden()` reads it once it passes. The same rules are what an editor or a pipeline can check a file against with the published schema.

## Draft the questions with a model

Questions written by hand are the best there are and the slowest to get. `write_golden()` has a chat model draft them from the collection's own chunks, the ones a search returns, following the steps of DeepEval's synthesizer, and checks each one before a person does:

```python
from vectrixdb.evaluation import ChatWriter, write_golden

writer = ChatWriter("http://localhost:11434/v1/chat/completions", model="phi4-mini")
written = write_golden(db, "drafts.jsonl", n=50, writer=writer, examples=["Can I skip a loan payment after a flood?"])
print(written.summary())
```

1. **Passages.** Every chunk is looked at without a model first: a picture's description, a contents page, a table of bare numbers, a scrap and text the reading's own quality check scored poor are passed over. The rest are spread across the collections, then their documents, then their sections: every collection and every document gets a share while the questions last, and within a document at least one a section and the rest in proportion to size, so a long report is asked about all the way through. A critic scores each chosen passage for clarity, depth, structure and relevance; under `threshold`, 0.7, the next passage of the section is tried, up to `tries`, 3, and the best is kept.
2. **Kinds, short to long.** `mix` shares the questions between searches as typed in a search box, 0.2, facts, 0.3, how and why, 0.2, questions that need two places, 0.15, and long questions that give the asker's situation first, 20 to 45 words, 0.15. Half are short, because that is most of what people type; the long ones are what a search tuned on short queries finds hardest. A question that needs two places takes its passage and up to two more that the collection's own model finds alike, at `similarity` 0.5 or more, on other pages; the passages are embedded for it rather than looked up with `similar()`, whose index beside a store holds only what that process wrote.
3. **Checks no model makes.** The model answers with the question, the answer and the words of the passage that give it. Each quote must be in its passage word for word, and the page it is on is the one `expected` names, so a chunk that runs over two pages is labelled with the page that answers. A question that copies the passage's wording, points at "the passage" or "this table", or repeats one already written is sent back with the reason; two that differ only in a year or a figure are two questions.
4. **A critic.** It scores each question for standing alone, being clear and being answered. One whose mean is under the threshold, or whose answer the passages do not give (under 0.7), or which does not stand alone (under 0.6), is sent back with its feedback: a good mean never carries a wrong answer. A search stands alone when it names what it looks for. One that never passes is set aside, and another passage is tried in its place.
5. **Harder.** `evolve`, 1, rewrites each question once: more concrete, a comparison, from another angle, or needing both places. A rewrite that fails a check keeps the question before it, and a search stays a search.

Every row is `"draft": true`, its `evidence` holds the quotes it was checked against, whole, and its `hint` names the page and quotes the words that answer it, so a row is checked in seconds; `check_golden()` notes the drafts until somebody deletes the mark. `examples`, a few questions people really ask, set the style, and `scenario` and `task` say who asks and why. The passages go to the model marked as data, with anything that could pass for a chat template's control tokens made inert, so a document cannot give it instructions. A collection under a policy is read as somebody, `db.as_principal({...})`, and only what they may see is sent.

`cache=`, a file, keeps every answer as it comes, so a run that stops starts again where it stopped, and the same run again costs nothing. A model that refuses, with a key it will not take or a deployment that is not there, or a server that is not running, stops the run at once with what the service said, and nothing is written. `path` is never written over.

The model is any OpenAI style chat completions route: Ollama or vLLM on your machine, a model on Azure AI Foundry, OpenAI, or an Azure OpenAI deployment, `ChatWriter.azure_openai(endpoint, deployment, key=...)`. Left out, it is the one the settings name: `VECTRIXDB_WRITER_URL` with `VECTRIXDB_WRITER_MODEL`, `VECTRIXDB_WRITER_KEY` and `VECTRIXDB_WRITER_KEY_HEADER`, or `AZURE_OPENAI_WRITER_DEPLOYMENT` with `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_KEY`, or the managed identity when there is no key. The critic is the writer unless `critic=` names another. `vectrixdb golden write NAME --out drafts.jsonl -n 50 --examples asked.txt` does it from a terminal.

A model's question about a passage it has just read is easier than the ones people ask, and it names only the page it was written from, when a report may say the same thing on another. So scores from a drafted file are for comparing setups with one another; checked rows, and real questions from people, are what make them numbers to quote.

## Where to stop answering

A search always has a top result, so a program that answers from it answers questions the documents do not cover. The relevance below which to decline is not a number to borrow: it depends on the model and on the documents. `answer_cutoff()` measures it. Every labelled question is searched, and its top result is either the answer, a document the question expects, or a near miss, the best the collection had and wrong. Questions you know the documents do not answer go in as `unanswerable`; whatever comes top for one is a near miss by definition, and they are what makes the number honest about questions from outside.

```python
from vectrixdb.evaluation import answer_cutoff

found = answer_cutoff(db, load_questions("golden.jsonl"), unanswerable=["How do zebras sleep?"])
found["cutoff"], found.get("reason")     # None: one answer and one near miss is not a measurement
```

With at least five answers and five near misses the reply holds `cutoff`, the similarity that best separates the two; `separation`, how often an answer outscores a near miss, where 0.5 is a coin; and `table`, what each candidate costs, to choose a stricter or a looser one from. With fewer, `cutoff` is `None` and `reason` says which side is short. `measure="relevance"` reads the reranker's verdict where one ran, and anything else goes to `search()`, since a cut-off belongs to one way of searching. `vectrixdb golden cutoff NAME --golden golden.jsonl --unanswerable outside.txt` prints the table.

Measured on SciFact's 300 judged queries with the bundled model, `scripts/answer_cutoff_bench.py`, raw figures in `benchmarks/answer_cutoff_scifact_bge.json`:

| Answer at or above | right when it answers | answers kept | near misses declined |
| ---: | ---: | ---: | ---: |
| 0.726 | 62% | 97% | 15% |
| 0.795 | 71% | 80% | 52% |
| 0.831 (chosen) | 83% | 59% | 82% |
| 0.872 | 92% | 26% | 97% |

An answer outscores a near miss 74.5% of the time there, which is the honest part of the result: every SciFact near miss is another abstract on the same subject, so similarity alone separates them poorly, and no cut-off keeps most answers while declining most misses. Questions from outside your documents separate far better than that. It is an example of the method on one dataset, not a cut-off to use.

## How to cut the documents

`evaluate()` compares ways of searching one index. The chunker, the chunk size and whether the headings went to the embedder are decided at ingestion, so comparing those means building the index again for each. `sweep()` does that in a scratch folder, from the originals, and asks the golden questions of every build:

```python
from vectrixdb.evaluation import sweep, sweep_markdown

report = sweep(
    {"deferment": "# Deferment\n\nCustomers in disaster areas can defer loan payments with no late fees.",
     "emergency-fund": "# Emergency fund\n\nExpedited access to funds and waived fees for displaced customers."},
    load_questions("golden.jsonl"),
    chunk=["recursive", "markdown"], chunk_size=[1000], embed_heading=[False, True],
    search=[{"mode": "dense"}],
)
print(sweep_markdown(report))
report["best"]["chunk"], report["best"]["chunk_size"]
```

`documents` is paths, or `{doc_id: path or text}` when the golden file names ids the files would not get by themselves. Labels have to survive a change of chunker, so they name documents, or pages of them, which a change of chunker does not move; `by="doc"` is the grain that means the same in two builds. Rows are ranked by nDCG, then MRR, then time; each has its recall at k, the chunks it made, the seconds its build took and the median search. Nothing is changed: to use the best one, ingest with those settings. `vectrixdb sweep FILES --golden golden.jsonl --chunk recursive --chunk markdown --size 500 --size 1000` does it from a terminal. A sweep is not saved for the dashboard; the comparison below is.

## Compare the chunking techniques

A sweep ranks builds by where the right chunk landed, and a technique that cuts small puts more chunks in the top 10 than one that cuts large, so the two are not handed the same amount to read. `compare_chunking()` hands every technique the same characters instead: for each golden question a build's best hits are handed over, best first, until the budget is spent, 6,000 characters unless told otherwise. With a model the answer is written from those characters alone and judged against the golden answer, and answered right decides. Found means the words the row quotes as its `evidence` were among them, a quote cut across two chunks counting when both were handed; a row without evidence is found when a page it expects is among them. Without a model, found decides.

```python
from vectrixdb.evaluation import ChatWriter, compare_chunking

report = compare_chunking(
    {"financial": {"td/ar2025.pdf": "data/start/financial/td/ar2025.pdf"}},
    "golden.jsonl",
    chat=ChatWriter.from_environment(),
    save_to="evaluations",
)
report["best"], [(t["name"], t["right"], t["tie"]) for t in report["techniques"]]
```

The plan is six techniques that need no model: Structure-aware, cut at headings and then long sections; Parent-child, small chunks that find and the sections they sit in handed over; Recursive; Sentence; Semantic; and Fixed-size, every so many characters at a space. Each is built at 500, 1,000 and 2,000 characters, Parent-child's small chunks at 250, 500 and 1,000, with the heading path embedded in front of each chunk and without: thirty-six builds, a fifth of the size carried over. `techniques=`, `sizes=` and `headings=` narrow it. Each technique is ranked by its best build; equal counts go to the one with fewer chunks.

With a model, `chat=`, three more things are built. LLM-based, where the model reads the paragraphs and says where each topic starts, at every size. And each technique once more at its middle size with a note from the model in front of every chunk for the embedder, a sentence or two placing it in its document: one model call a chunk, so it is not built at every size. Late chunking, where each chunk's vector is the mean of its own tokens read in the whole document, is built at the middle size when the embedding model pools the mean of its tokens; the bundled bge-small pools its first token, so with it late chunking is left out and the report says why, in `skipped`. `cut_with=`, `context_with=` and `late=` give or refuse each one on its own. The same three are there for ingestion: `add_document(chunk="llm", cut_with=llm_cutter(chat))`, `add_document(context_with=context_writer(chat))` and `add_document(late=True)`, from `vectrixdb.chunk_models`.

Two techniques that disagree on only a few questions may differ by luck, so each pair is put to an exact test on the questions only one of them got right. A technique whose gap to the best could be luck is marked a tie: the questions cannot tell the two apart, and more of them would.

`documents` is what the collections hold, as originals or as the Markdown the library kept, `{collection: {doc_id: ...}}` for several collections or `{doc_id: ...}` for one; each question is asked of the collection that holds its documents. Kept Markdown keeps its pages, so a question labelled with a page is scored on that page. `answer=` and `judge=` pass your own model instead of `chat=`. With `save_to`, the run is saved as `chunking/runs/<id>/report.json` beside the retrieval runs in `retrieval/runs/`, the same folder, `s3://` or Blob address, and the server's Evaluate page draws it on its Chunking tab. Nothing is switched: to use the pick, ingest with its settings.

## Answers

```python
from vectrixdb.evaluation import answer_report

def judge(metric, question, reference, answer, sources):
    return 1.0   # your model goes here: a number from 0 to 1

answer_report(questions, {"q1": {"answer": "Yes, loan payments can be deferred.", "sources": ["deferment"]}}, judge)["metrics"]
```

The four metrics are the ones Bedrock's managed evaluation reports, correctness, completeness, faithfulness and helpfulness, so the two can be read side by side. A score outside 0 to 1 is an error, because a judge that returns 7 has not understood the question and averaging it in would hide that.

Retrieval is the number to watch while changing a chunker or an embedder: it is cheap, it needs no model, and it moves when retrieval moves and for no other reason.

To put every way a collection can be searched side by side, with the time each takes and three picks to choose between, see [Evaluate every setup](evaluate-setups.md).
