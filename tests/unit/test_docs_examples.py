"""Every Python example in the README and the docs runs.

The tutorial's first metadata example passed `where=` to `search()`, which
takes `filter=`; a new user following the docs hit a TypeError on their second
snippet. Examples are prose that can be wrong, so they are executed.

This covered eight pages of eighteen and skipped any fence containing one of a
long list of substrings. Those skips were doing real damage: the tutorial's
fifth step raised, and the README's model-download example could not work,
and both passed a green build because a skip marker happened to appear in
them. Every page with a Python fence is listed now, and a fence is skipped
only when it cannot run here for a reason that is nothing to do with whether
the code is right: it is not Python, it needs an optional package, or it needs
a cloud account that the mocks-only rule keeps out of the suite.

Fences run in order within a page, sharing one namespace, in a temporary
working directory.
"""

import re
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

PAGES = [
    "docs/explanation/search-modes.md",
    "docs/explanation/relevance.md",
    "README.md",
    "docs/index.md",
    "docs/tutorial/getting-started.md",
    "docs/explanation/concurrency.md",
    "docs/explanation/hnsw.md",
    "docs/explanation/larger-than-ram.md",
    "docs/explanation/why-vectrixdb.md",
    "docs/how-to/conversation-memory.md",
    "docs/how-to/entitlements.md",
    "docs/how-to/lineage.md",
    "docs/how-to/export-import.md",
    "docs/how-to/handle-errors.md",
    "docs/how-to/ingest-documents.md",
    "docs/how-to/extract-keep-index.md",
    "docs/how-to/ingest-on-event.md",
    "docs/how-to/sources.md",
    "docs/how-to/extraction-service.md",
    "docs/how-to/measure-retrieval.md",
    "docs/how-to/evaluate-setups.md",
    "docs/how-to/integrations.md",
    "docs/how-to/knowledge-graph.md",
    "docs/how-to/mcp-server.md",
    "docs/how-to/offline.md",
    "docs/how-to/rest-api.md",
    "docs/how-to/build-an-app.md",
    "docs/how-to/clients.md",
    "docs/how-to/wrap-it.md",
    "docs/how-to/storage-backends.md",
    "docs/how-to/tips.md",
    "docs/how-to/tracing.md",
    "docs/how-to/async.md",
    "docs/how-to/chunking.md",
    "docs/how-to/embedding-models.md",
    "docs/how-to/extraction-quality.md",
    "docs/how-to/mcp-local.md",
    "docs/how-to/recordings.md",
    "docs/how-to/search-options.md",
    "docs/how-to/sizing.md",
    "docs/how-to/troubleshoot.md",
    "docs/reference/filters.md",
]

# Pages whose Python is one session with a server, each fence carrying on from
# the one before: none runs alone here. test_client.py makes the same calls
# against a real server in the suite, and sdk/conformance/serve.py does for
# the other languages.
NEEDS_A_SERVER = ("docs/how-to/clients.md",)

# Not Python, or not a complete program: shell lines, REPL transcripts and
# snippets written around a placeholder the reader is meant to replace.
NOT_EXECUTABLE = (
    "pip install",
    "python -m",
    "vectrixdb-mcp",
    "vectrixdb mcp",
    ">>> import vectrixdb.api",
    # Starts a server and never returns.
    "run_server(",
    "uvicorn.run(",
    "/abs/path",
    "your-",
    "YOUR_",
    "my_llm(",
    "summarize=",
    # Written around ids the reader supplies, over a graph an extraction pass
    # has already populated.
    "new_employer_id",
    # Somebody else's VectrixDB server, at an address the reader replaces.
    # The page is about calling one over HTTP, and there is none here; what
    # those calls return is held to in test_signin_key_scope.py instead.
    "vectors.company.com",
    # The same, through a company's gateway; the gateway options are held to
    # in test_client.py against a stand-in transport.
    "gateway.company.com",
    # An extraction service, which refuses to be built without the key the
    # reader puts in their .env, and whose Azure services are the reader's
    # own. Every route it serves is held to in test_extraction_app.py.
    "create_extraction_app(",
    # A VectrixDB server at an address the reader replaces. Every call is held
    # to its word against a real server in test_client.py.
    "vectrixdb.connect(",
    "AsyncVectrixClient(",
    # A chat model the reader serves, cutting or describing chunks.
    "llm_cutter(",
    "context_writer(",
    # A recording and the speech model or service that hears it.
    "Whisper(",
    "AzureSpeech(",
)

# Needs a package that is not a dependency, or a network hop.
OPTIONAL_DEPENDENCY = (
    "tiktoken",
    "import polars",
    "sentence_transformers",
    "llama_index",
    "langchain",
    "import openai",
    "openai:",
    "base_url=",
    "statsd",
    "import torch",
    "from transformers",
    "plugin:",
    # A YouTube video: yt-dlp and a network hop to YouTube, which the suite
    # never makes. test_youtube.py and test_youtube_captions.py hold
    # load_youtube to its word with stand-ins for both.
    "load_youtube(",
    # A chat model the reader serves, on their machine or a service: a
    # network hop to a model that is not here. What write_golden does with
    # its answers is held to in test_golden_writer.py, with fake models.
    "/v1/chat/completions",
    # A fetch the reader asks for by name. The suite runs offline, where it is
    # refused; test_models.py holds the refusal and the fetch to their words.
    "download_models(",
    # A feed or a page on the network, which the suite does not have. Every
    # step is held to its words with a fake one in test_sources.py and
    # test_sources_api.py; the page's own Source runs here.
    '.sources.add("https://',
)

# A cloud backend. Excluded because the suite runs offline against fakes, not
# because the example is wrong; the storage contract covers these paths.
CLOUD_BACKEND = (
    "storage_backend=",
    "VectrixDB.with_",
    "cosmos_",
    "lakebase_",
    "delta_",
    "opensearch_",
    "azure_search",
    # A DSN for a database that is not running here. PostgresAuditSink is
    # covered against the fake_postgres stand-in in test_audit.py instead,
    # where its real SQL executes; this fence is the deployment shape.
    "postgresql://",
)

SKIP_IF_CONTAINS = NOT_EXECUTABLE + OPTIONAL_DEPENDENCY + CLOUD_BACKEND


def _fences(page: Path):
    text = page.read_text(encoding="utf-8")
    for match in re.finditer(r"```python\n(.*?)```", text, re.S):
        # A fence inside a tab is indented with it.
        yield textwrap.dedent(match.group(1))


def _runnable(code: str) -> bool:
    return not any(marker in code for marker in SKIP_IF_CONTAINS)


@pytest.mark.parametrize("page", PAGES)
def test_examples_run(page, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VECTRIXDB_OFFLINE", "1")
    fences = list(_fences(ROOT / page))
    runnable = [f for f in fences if _runnable(f)]
    if not runnable:
        pytest.skip(f"{page}: every fence needs something this suite does not have")

    namespace = {"__name__": "__docs__"}
    for index, code in enumerate(runnable):
        try:
            exec(compile(code, f"{page}#fence{index}", "exec"), namespace)  # noqa: S102
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"{page}, fence {index} failed with {type(exc).__name__}: {exc}\n{code}")


def test_the_page_list_covers_every_page_with_a_python_fence():
    """A new page with examples joins this list, rather than quietly not
    being checked. Eight of eighteen were covered before, which is how two
    broken examples shipped."""
    with_fences = {
        p.relative_to(ROOT).as_posix()
        for p in [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
        if "```python\n" in p.read_text(encoding="utf-8")
    }
    missing = sorted(with_fences - set(PAGES) - set(NEEDS_A_SERVER))
    assert not missing, f"pages with examples that nothing runs: {missing}"
