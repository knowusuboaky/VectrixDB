# Examples

Three scripts, each under a screen long, each runs offline with nothing to install beyond `pip install vectrixdb`. Every one of them is executed by the test suite, so they cannot rot.

| Example | What it shows | Run |
| --- | --- | --- |
| [rag_in_20_lines.py](rag_in_20_lines.py) | Add notes, search under a token budget, build the prompt. The answer step is where your model goes. | `python examples/rag_in_20_lines.py` |
| [chat_memory.py](chat_memory.py) | Turns, pinned facts, a correction that supersedes, a budgeted context block, then consolidation of old turns into facts. | `python examples/chat_memory.py` |
| [graphrag_over_a_book.py](graphrag_over_a_book.py) | Chunk a text, extract a knowledge graph, ask questions that span passages. Pass a Gutenberg URL for a whole book. | `python examples/graphrag_over_a_book.py` |

The same three as notebooks are in [notebooks/](notebooks/), generated from the scripts by `python scripts/make_notebooks.py`, so the two never disagree.

## The whole thing on Azure, one step at a time

[azure/](azure/) is a walkthrough rather than a reference: fourteen numbered
scripts that make a real deployment on your own account, put your own files
through it, and delete all of it again. A PDF lands in a blob container,
nobody presses anything, and a minute later it is in Azure AI Search with its
pictures described by a vision model.

Every script does one thing, prints every `az` command before it runs it,
takes `--dry-run`, and is safe to run twice. Everything goes into one
resource group so that `99_delete_everything.py` is a single command that
cannot miss a resource. Roughly 10 to 25 CAD for a few days, almost all of it
the search service, which is billed by the hour while it exists.

Nothing in it is seeded: every number on the dashboard at the end came from
your documents going through the real pipeline. Start at
[azure/README.md](azure/README.md).

## Reference deployments

[deploy/](deploy/) holds code to read and change, not to run as it is, and the test suite does not run it, because every line of it talks to a cloud.

| File | What it shows |
| --- | --- |
| [deploy/aws_ingest_stack.py](deploy/aws_ingest_stack.py) | Ingest-on-event on AWS, made and unmade in dependency order: `plan`, `create`, `status`, `delete`. A separate bucket for the kept Markdown, a queue with a dead letter queue, and bucket notifications merged into what is already there rather than replacing it. |
| [deploy/lambda_handler.py](deploy/lambda_handler.py) | The function behind it: OpenSearch with policy pushdown, Bedrock embeddings, Textract for scans, the document store in S3, and partial batch failures. |
| [deploy/azure_evaluate/function_app.py](deploy/azure_evaluate/function_app.py) | A Function App that runs every setup whenever the golden file is saved: Event Grid, one Durable orchestration per golden file, a minute's wait for more saves, one activity per setup, and the report, one file holding the three picks, saved beside the golden file. |
| [deploy/aws_evaluate_handler.py](deploy/aws_evaluate_handler.py) | The same run behind an S3 notification, in one Lambda. |
| [deploy/azure_ingest_queue/](deploy/azure_ingest_queue/) | Event Grid to a Storage Queue to a function, so a long read is never cut off half way. [azure/](azure/) deploys this one for real. |
| [extraction_server/server.py](extraction_server/server.py) | An extraction server the library calls: a key on the way in, page spans from a PDF route, minute offsets from an audio route. |

## Going further

- [Ingest documents](https://knowusuboaky.github.io/VectrixDB/how-to/ingest-documents/): PDF, DOCX, HTML and Markdown with page and heading metadata, parent-child retrieval, near-duplicate detection.
- [Conversation memory](https://knowusuboaky.github.io/VectrixDB/how-to/conversation-memory/): the full memory API, forgetting, contradiction detection, and how it scores on LoCoMo and LongMemEval.
- [LangChain, LlamaIndex, plugins, CLI](https://knowusuboaky.github.io/VectrixDB/how-to/integrations/): the same collections from the frameworks you already use.
- [Benchmarks](https://knowusuboaky.github.io/VectrixDB/explanation/benchmarks/): every number, with the scripts that produced it.

Each script writes to `./example_data` in the current directory; delete that folder to start clean.
