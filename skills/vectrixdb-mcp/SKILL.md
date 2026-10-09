---
name: vectrixdb-mcp
description: Answer questions from a VectrixDB server's collections through its MCP tools, as the person you work for, citing every result. Use when a VectrixDB MCP server is connected and the person asks about their documents, a collection, or what they hold.
---

# VectrixDB over MCP

A VectrixDB server is connected, and every tool call is made as the person you work for: their role, the collections their key or sign-in reaches, and each collection's policy decide what comes back. A refusal is the server's answer about this person, not an error to work around. Never try another collection or another tool to get what was refused; say what was refused and why.

## Find your way

- `whoami` says who you are here, your role, what it allows, and which collections you reach. Call it when a refusal surprises you.
- `list_collections` names the collections you can reach. Call it when you do not know a collection's name; never guess one.
- `describe_collection` gives a collection's size, whether it searches by exact words too, and the metadata fields it holds. Call it before writing a `filter`, so the filter names a field that exists.
- `list_documents` says which documents a collection holds, with `title` to narrow them.

## Search

`search` takes a `collection` and a `query`, and returns each result with its relevance, its id and its source. Pick the `mode`:

- `hybrid`, the default: meaning and exact words. Right for most questions.
- `keyword`: exact words. Right for names, codes, error messages and quoted phrases.
- `dense`: meaning alone, for a question phrased nothing like the documents.
- `rerank`: meaning, then the best re-ordered first. Slower; use it when the first results are close but not right.

`filter` narrows by metadata, as a JSON object such as {"department": "legal"}. `facets` names metadata fields to count over the results, which tells the person how the answers spread, by department or by year, say. Counts are over the results returned, not the whole collection; say so.

`similar` finds more like one result, by the `id` a search gave it. `open_source` reads the document a result came from; pass `around` with words from the result to read the part around them, and raise `token_budget` for more.

Every answer's first line says whether anything was cut to the token budget. When it was, the answer may be among what was cut: raise `token_budget`, lower `limit`, or narrow the query before concluding it is not there.

## Answer

Answer only from what the results say. Cite each claim by its source, the citation each result carries, so the person can check it. When the results do not answer the question, say so plainly, and say what you searched for. Do not fill a gap from what you already know without saying that is what you did.

## Prompts and resources

The server offers prompts a person can pick: `answer_from_documents`, `summarise_document`, `whats_in_collection` and `compare_documents`. Its collections and documents are resources too, `vectrixdb://collections/{collection}` and `vectrixdb://collections/{collection}/documents/{+document}`, which a person can attach to the conversation; each is read as them.

## Changing a collection

The tools that change a collection appear only when the server allows writes, `VECTRIXDB_MCP_WRITES`, and the person's role still decides each call. `add_document` adds a text, `create_collection` makes an empty collection, `add_source` keeps a collection up with a feed or a page and `refresh_source` reads it now.

- Ask the person before any change, and say exactly what will change.
- `delete_document` deletes a document and every chunk of it, for good. Ask first, every time, naming the document and the collection.
- Never write to a collection to work around a search that found nothing.
