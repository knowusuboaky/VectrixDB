<!-- Written by scripts/make_reference.py from the code. Edit the code, then run it again. -->

# Command line

`pip install vectrixdb` puts `vectrixdb` on the path. Every command that opens the data or the list of people takes `--env-file`, read before anything else, and finds the data at `--path`, else `VECTRIXDB_PATH`, else `./vectrixdb_data`, the way the server does.

## break-glass

Emergency sign-in, for while the usual sign-in is down.

### break-glass hash

```text
vectrixdb break-glass hash [OPTIONS]
```

Make the hash of a new emergency password. The password is typed twice, never shown, and kept nowhere.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `--admin` | The emergency admin's username, so the password may not hold it |  |

## check

```text
vectrixdb check [OPTIONS]
```

Test the settings before a start: what a start would refuse, and what looks wrong.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |
| `--template` | Print a settings file with every setting in it, to fill in, and stop | off |
| `--url` | Check a running server from outside instead, through its gateway: https://apim.company.com/vectrixdb |  |
| `--prefix` | With --url: the path every route lives under, as VECTRIXDB_PREFIX. Left out, the setting |  |
| `--gateway-paths` | With --url: the gateway path each route is published under, as VECTRIXDB_GATEWAY_PATHS: api/v1=/files/search,auth=/files/auth. Left out, the setting |  |

## create

```text
vectrixdb create NAME DIMENSION [OPTIONS]
```

Create a new collection.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `NAME` | Collection name | required |
| `DIMENSION` | Vector dimension | required |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |
| `--metric`, `-m` | Distance metric | `cosine` |

## delete

```text
vectrixdb delete NAME [OPTIONS]
```

Delete a collection.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `NAME` | Collection name | required |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |
| `--force`, `-f` | Skip confirmation | off |

## doctor

```text
vectrixdb doctor [OPTIONS]
```

Try every part of this install: the models, the readers, and each service the settings name.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |
| `--offline` | Ask no service over the network; VECTRIXDB_OFFLINE=1 does too | off |
| `--quick` | Leave the models unloaded | off |
| `--json` | print JSON instead of a list | off |

## download-models

```text
vectrixdb download-models [OPTIONS]
```

Fetch the models the package does not hold, once, so later runs need no network.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `--type`, `-t` | all, dense, reranker, or a type an error message named, such as dense_en | `all` |
| `--force`, `-f` | Re-download even if models exist | off |

## evaluate

```text
vectrixdb evaluate NAMES... [OPTIONS]
```

Search every golden question every way the collections can be searched, and pick three.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `NAMES` | Collections to evaluate, the same documents on each | required |
| `--golden`, `-g` | Golden file: a path, s3://bucket/key or a Blob address | required |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |
| `--save-to` | Where runs go: a folder, s3://bucket/prefix or a Blob address. Default: VECTRIXDB_EVALUATIONS, or <path>/evaluations, which is where the server reads them |  |
| `--mode` | The most a collection is opened for; ultimate includes every local method | `ultimate` |
| `--balance` | best_for_balance: the fastest within this many points of the most | `4` |
| `--time` | best_for_time: the fastest within this many points of the most | `8` |

## extract-serve

```text
vectrixdb extract-serve [OPTIONS]
```

Start the extraction service: files, addresses and videos in, text out.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `--port`, `-p` | Port to run on. Default: VECTRIXDB_EXTRACT_LISTEN_PORT, else 7338 | `7338` |
| `--host`, `-h` | Host to bind to | `127.0.0.1` |
| `--prefix` | The path every route lives under, such as /api. Default: VECTRIXDB_EXTRACT_PREFIX, else the root |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |

## golden

Golden questions: the questions whose right answers you know.

### golden cutoff

```text
vectrixdb golden cutoff NAME [OPTIONS]
```

Measure the relevance below which the top result is more likely a near miss than the answer.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `NAME` | Collection to search | required |
| `--golden`, `-g` | Golden file: a path, s3://bucket/key or a Blob address | required |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |
| `--unanswerable`, `-u` | A text file of questions the documents do not answer, one a line |  |
| `--mode` | How the questions are searched; the cut-off belongs to that way of searching | `dense` |
| `--measure` | similarity, the cosine alone, or relevance, the reranker's verdict where one ran | `similarity` |

### golden label

```text
vectrixdb golden label NAME [OPTIONS]
```

Propose the documents that answer each question, for a person to check.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `NAME` | Collection that holds the documents | required |
| `--questions`, `-q` | Questions with reference answers and no labels: a Bedrock evaluation dataset or this library's JSONL | required |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |
| `--out`, `-o` | Where to write the rows | `golden.jsonl` |
| `--top` | How many documents to propose for each question | `1` |

### golden template

```text
vectrixdb golden template NAME [OPTIONS]
```

Write rows to fill in: one sampled document each, its id already in expected.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `NAME` | Collection to sample documents from | required |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |
| `--out`, `-o` | Where to write the rows | `golden.jsonl` |
| `--count`, `-n` | How many documents to sample | `50` |
| `--seed` | The same seed gives the same documents | `0` |

### golden write

```text
vectrixdb golden write NAME [OPTIONS]
```

Draft golden questions with a chat model from the collection's own chunks, every one a draft to check.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `NAME` | Collection whose chunks the questions are written from | required |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |
| `--out`, `-o` | Where to write the rows; never over a file that is there | `golden.jsonl` |
| `--count`, `-n` | How many questions | `50` |
| `--seed` | The same seed, with the same answers, gives the same rows | `0` |
| `--examples` | A text file of questions people really ask, one a line, whose style is copied |  |
| `--scenario` | Who asks, in a few words |  |
| `--task` | What they are after, in a few words |  |
| `--evolve` | How many times each question is made harder; 0 for none | `1` |
| `--cache` | Where answers are kept as they come, so a stopped run starts where it stopped. Default: beside --out |  |

## info

```text
vectrixdb info [PATH] [OPTIONS]
```

Show database information.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `PATH` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |

## ingest

```text
vectrixdb ingest SOURCES... [OPTIONS]
```

Load, chunk and index files: PDF, DOCX, HTML, Markdown, text.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `SOURCES` | Files or directories to ingest | required |
| `--name`, `-n` | Collection name | `docs` |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |
| `--mode` | dense, hybrid, ultimate or graph | `dense` |
| `--chunk` | recursive, sentence, semantic, markdown or fixed | `recursive` |
| `--chunk-size` |  | `1000` |
| `--overlap` |  | `200` |
| `--parent-size` | store sections for search --parents |  |
| `--dedupe` | skip near-duplicates at or above this similarity |  |
| `--glob` | pattern for files inside directories | `*` |

## keys

Named API keys for scripts and apps, managed from the server itself.

### keys add

```text
vectrixdb keys add NAME [OPTIONS]
```

Make a key. It is shown once, here, and kept only as a hash.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `NAME` | What uses the key, so it is clear later: handbook-bot | required |
| `--role`, `-r` | reader, searcher or operator | `searcher` |
| `--collection`, `-c` | A collection it may reach. Repeat for more. Left out, every collection. |  |
| `--days` | How many days it works for. Left out, until it is revoked. |  |
| `--per-minute` | Requests a minute it may make. Left out, the server's setting. |  |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |

### keys list

```text
vectrixdb keys list [OPTIONS]
```

Every key that has not been revoked: what it may do, what it reaches, and when it was last used.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |

### keys revoke

```text
vectrixdb keys revoke KEY_ID [OPTIONS]
```

Revoke a key. Anything using it stops working at once.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `KEY_ID` | The key's id, from: vectrixdb keys list | required |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |

## list

```text
vectrixdb list [PATH] [OPTIONS]
```

List all collections.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `PATH` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |

## mcp

```text
vectrixdb mcp [OPTIONS]
```

Serve a collection over MCP so an assistant can use it as a tool.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `--name` | Collection name | `default` |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |
| `--mode` | dense, hybrid, ultimate or graph |  |
| `--transport` | stdio, sse or streamable-http | `stdio` |

## models-info

```text
vectrixdb models-info
```

Show information about installed models.

## people

The people who may sign in, managed from the server itself.

### people add

```text
vectrixdb people add EMAIL [OPTIONS]
```

Add somebody, and print a one-time link for them to choose how they sign in.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `EMAIL` | Their work email address | required |
| `--role`, `-r` | viewer, operator or admin | `viewer` |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |

### people list

```text
vectrixdb people list [OPTIONS]
```

Everybody on this server's People list, and how they sign in.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |

### people remove

```text
vectrixdb people remove EMAIL [OPTIONS]
```

Take somebody off the list. They are signed out and can no longer sign in.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `EMAIL` | Their work email address | required |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |

### people reset

```text
vectrixdb people reset EMAIL [OPTIONS]
```

Forget somebody's passkeys, authenticator and recovery codes, sign them out, and print a new set-up link.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `EMAIL` | Their work email address | required |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |

## query

```text
vectrixdb query TEXT [OPTIONS]
```

Search a collection from the shell.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `TEXT` | Query text | required |
| `--name`, `-n` | Collection name | `docs` |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |
| `--limit`, `-k` |  | `5` |
| `--mode` | dense, sparse, hybrid, ultimate, graph |  |
| `--parents` | return the enclosing sections | off |
| `--explain` | show score components | off |
| `--json` | print JSON instead of a table | off |

## serve

```text
vectrixdb serve [OPTIONS]
```

Start the VectrixDB server.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `--port`, `-p` | Port to run on. Default: VECTRIXDB_LISTEN_PORT, else 7337 | `7337` |
| `--host`, `-h` | Host to bind to. 0.0.0.0 needs an API key or sign-in | `127.0.0.1` |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--reload`, `-r` | Enable auto-reload | off |
| `--dashboard`, `--no-dashboard` | Enable dashboard | on |
| `--api-key`, `-k` | API key for authentication |  |
| `--read-only-key` | Read-only API key |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |

## sources

Feeds and pages a collection keeps up with, read again on a schedule.

### sources add

```text
vectrixdb sources add ADDRESS [OPTIONS]
```

Keep a collection up with a feed or a page. Nothing is written until a refresh.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `ADDRESS` | The feed's or the page's address. Write ${NAME} where a secret goes. | required |
| `--name`, `-n` | Collection name | `docs` |
| `--every` | How often it is read: 30m, 6h, 1d, 1w | `6h` |
| `--kind` | feed or page. Left out, it is fetched once to tell |  |
| `--articles` | A feed: index the article each entry links to | off |
| `--no-transcribe` | A podcast: its show notes, even with an audio engine | off |
| `--delete-when-gone` | A page: remove its chunks when it answers 404 or 410 | off |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |

### sources list

```text
vectrixdb sources list [OPTIONS]
```

The feeds and pages a collection keeps up with, and how each last went.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `--name`, `-n` | Collection name | `docs` |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |

### sources refresh

```text
vectrixdb sources refresh [OPTIONS]
```

Read the sources that are due and write only what changed. Run it from cron or a scheduled job.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `--name`, `-n` | Collection name. Left out, every collection here that keeps up with a source |  |
| `--force` | Every source, not only the ones due | off |
| `--max-items` | Entries written per source this time | `50` |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |

### sources remove

```text
vectrixdb sources remove SOURCE [OPTIONS]
```

Stop keeping up with a source. Its documents stay unless --delete-documents.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `SOURCE` | The source's id or its address | required |
| `--name`, `-n` | Collection name | `docs` |
| `--delete-documents` | Take the documents it wrote too | off |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |

## stats

```text
vectrixdb stats [OPTIONS]
```

Size, model and mode of a collection.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `--name`, `-n` | Collection name | `docs` |
| `--path`, `-d` | Database path. Default: VECTRIXDB_PATH, or ./vectrixdb_data |  |
| `--env-file` | Read settings from this file first. What the environment already sets wins |  |

## sweep

```text
vectrixdb sweep FILES... [OPTIONS]
```

Build the index every way asked, from the originals, and rank the ways by what the golden questions find.

| Argument or option | What it does | Default |
| --- | --- | --- |
| `FILES` | The original documents, or folders of them | required |
| `--golden`, `-g` | Golden file whose expected names documents | required |
| `--chunk` | A chunker to try; give it again for another | `['recursive', 'markdown']` |
| `--size` | A chunk size to try, in characters | `[500, 1000]` |
| `--overlap` | An overlap to try, in characters | `[100]` |
| `--heading` | Whether the headings go to the embedder: yes, no, or both | `both` |
| `--mode` | A way of searching each build: dense, hybrid, ultimate | `['dense']` |
| `--out`, `-o` | Write the whole report here as JSON |  |

## version

```text
vectrixdb version
```

Show version information.
