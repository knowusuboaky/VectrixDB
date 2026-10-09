# Add VectrixDB with your coding agent

A coding agent, such as Claude Code, Cursor, Copilot or Codex, can add VectrixDB to a project for you. Give it the prompt below. It points the agent at [llms.txt](../llms.txt), a short index of these docs written for agents, and tells it what to ask you before it changes anything that is hard to undo.

## The prompt

Copy this into your agent, in the project you want search in:

```text
Add VectrixDB (https://github.com/knowusuboaky/VectrixDB) to this project for search.

1. Read https://knowusuboaky.github.io/VectrixDB/llms.txt first, and the pages it links
   that this project needs. Use the library as documented; do not invent parameters.
2. Look at this project: its language, where its documents or records come from, and
   whether it needs a server or runs in one Python process. Tell me what you found and
   what you plan, in a few lines, before installing anything.
3. Install the smallest set: `vectrixdb` alone for in-process search, plus only the
   extras the plan needs (`documents` for PDF and Office files, `api` for the REST
   server and dashboard, a storage extra such as `azure` only if I already use it).
4. Add ingestion with `add_document` for files, or `add` for records with metadata,
   keeping the source so `rechunk` works later (`keep_source=True`).
5. Add search with `db.search(...)`, and show each result's `citation` wherever the
   app shows an answer.
6. Write five to ten real questions about my data with the documents that answer
   them, as a golden file, and run `vectrixdb.evaluation.evaluate` to pick the mode.
   Show me the report instead of guessing a mode.
7. Handle `vectrixdb.exceptions.VectrixError` where the app can say something useful.
8. Ask me before: changing chunking on data already indexed (show me
   `rechunk_preview` first), deleting a collection, or touching a deployed store.
9. Offer, and do not turn on unless I say yes: tracing with OpenTelemetry
   (`vectrixdb[tracing]` and OTEL_EXPORTER_OTLP_ENDPOINT), which sends counts and
   timings, never query or document text, to the tracing tool I name.

Keep the change small and run the project's tests when you are done.
```

## Or install the skill

The same rules come as a skill, which an agent loads by itself when a task needs it, so you do not paste anything: the folder [`skills/vectrixdb`](https://github.com/knowusuboaky/VectrixDB/tree/main/skills/vectrixdb) in the repository, one file, `SKILL.md`. Claude Code reads skills from `.claude/skills/` in a project, for everyone who works on it, and from `~/.claude/skills/` for you alone. Put the folder in one of them:

```bash
mkdir -p .claude/skills/vectrixdb
curl -fsSL -o .claude/skills/vectrixdb/SKILL.md \
  https://raw.githubusercontent.com/knowusuboaky/VectrixDB/v2.2.0/skills/vectrixdb/SKILL.md
```

Take it from the tag of the version you install, `v2.2.0` here, and again when you upgrade: a test holds every name in it to the code of that version. An agent that reads skills from another folder takes the same folder there. The agent then uses it when you ask for search, retrieval or answers over your files, or name VectrixDB.

## What a good result looks like

- One place that opens the collection, with a path that is not the working directory.
- Ingestion that keeps the source, so the chunking can change later without reading the files again. See [Extract, keep, index](extract-keep-index.md).
- Search that shows a citation beside every answer. See [Trace an answer to its source](lineage.md).
- A golden file and an evaluation report checked into the project, so the choice of mode can be checked again when the data changes. See [Evaluate every setup](evaluate-setups.md).
- Tracing off unless you asked for it. See [Trace searches and ingestion](tracing.md).

## When the agent should stop and ask

An agent can add code freely, because you review it. These it should not do without your word, and the prompt says so:

- **Re-chunking indexed data.** `rechunk()` deletes and rewrites every chunk of the documents it picks. `rechunk_preview()` with the same arguments shows what would change, and writes nothing.
- **Deleting a collection or documents.** There is no undo on a store without its own backup.
- **A deployed store.** Azure AI Search, Cosmos DB, Postgres and the rest are shared with whoever else uses them.
- **Turning tracing on.** It sends data to another service, even though that data is counts and timings.

## For an agent that uses VectrixDB itself

To let an assistant search a collection as a tool, rather than write code for it, run the [MCP server](mcp-server.md).
