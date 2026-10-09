# Scale out

One server on one machine keeps everything in its data folder: the
collections, the sign-in file, the collection records, the evaluation runs. That
is the default, and it is right until one machine is not enough. This page is
what has to move off the machine before a second server can run beside the
first, what each process keeps for itself, and how ingestion and extraction
run as workers of their own.

## What must be shared

Every server that answers for the same collections has to read the same state.
Each row is a setting, and every server gets the same value:

| State | Setting | Shared stores |
| --- | --- | --- |
| The collections: vectors, text and metadata | `VECTRIXDB_STORAGE_BACKEND` | `azure_search` or `cosmosdb`; see [Choose a storage backend](storage-backends.md) |
| The copy of every chunk the collection pages read | `VECTRIXDB_CHUNK_STORE` | `cosmos://<account>.documents.azure.com/<database>/<container>`, or a store of your own passed as `chunk_store=` |
| Each collection's record: who may search it, who may see it, masking; and the feeds and pages it keeps up with | `VECTRIXDB_COLLECTION_STORE` | `postgresql://`, `cosmos://` or `dynamodb://` |
| Sign-in: people, sessions, links, codes, keys, failed attempts and each key's requests a minute | `VECTRIXDB_SIGNIN_STORE` | `postgresql://`, `cosmos://` or `dynamodb://` |
| The sign-in secret | `VECTRIXDB_SIGNIN_SECRET` | The same value on every server: authenticator secrets are sealed with a key derived from it |
| The access log | `VECTRIXDB_ACCESS_LOG` | `stdout` for the platform to collect, or a Blob address, append blobs every server writes |
| Search decisions under a policy | `VECTRIXDB_AUDIT_STORE` | A Blob address, `s3://` or `postgresql://` |
| Evaluation runs | `VECTRIXDB_EVALUATIONS` | `s3://bucket/prefix` or a Blob address |
| Knowledge graphs | `VECTRIXDB_GRAPH_STORE` | `s3://bucket/prefix` or a Blob address |
| Kept Markdown | `VECTRIXDB_KEEP_SOURCE` | `s3://bucket/prefix` or a Blob address |

Each key for a store, `VECTRIXDB_CHUNK_STORE_KEY`,
`VECTRIXDB_COLLECTION_STORE_KEY` and `VECTRIXDB_SIGNIN_STORE_KEY`, is a setting of
its own and never part of the address. Left empty, the chunk store and a cloud
collection store are reached as the managed identity. The [settings reference](../reference/settings.md) has
every one.

What sharing buys:

- **A rule set on one server is the rule on all of them.** The collection
  record is read by every server. Reads are cached for a short while, so a
  search is not a round trip to the store; when the store cannot be read, the
  copy from a moment ago stands in, and with none the search does not run.
- **A sign-in survives a redeploy and works on any server.** Anything that must
  happen once, a code spent or a link used, happens once however many servers
  are asked at the same moment.
- **A key's requests a minute are one count.** The count lives in the sign-in
  store, so three servers behind a load balancer do not give a caller three
  allowances.
- **A source is refreshed by one server at a time.** A refresh holds a lease on
  each source while it reads it, here or on another server, and one that died
  lets go when the lease runs out.
- **The collection pages show what every server wrote.** Without a chunk store,
  each page counts only what the process drawing it wrote, which on a fresh
  instance is nothing.

`vectrixdb check` reads the storage backend, the chunk store, the collection
store, the sign-in settings, the access log, the audit store and the evaluation
runs before a start. `vectrixdb doctor` asks the storage backend's service, the
sign-in store, the audit store, the evaluation runs and the graph store whether
they answer. See [Check and doctor](troubleshoot.md).

## What each process keeps for itself

| Per process | Why it is fine |
| --- | --- |
| The models, and the line model calls wait in | Each process loads its own and sizes its own line; see [Size the server for load](sizing.md) |
| The index beside the process | A collection on a store is written to the service and to a local index under the same ids. Each process's local index holds only its own writes, so a search that must see every server's writes asks the store; see [Two homes](storage-backends.md#two-homes) |
| The cache | `VECTRIXDB_CACHE_BACKEND=memory`, the default, is per process. `redis` shares one |
| The extraction service's jobs, with `VECTRIXDB_EXTRACT_JOBS=memory` | Fine for one instance. A restart forgets every job, and a second instance cannot see the first one's |

## Replicas and uvicorn workers

`vectrixdb serve` runs one process. There are two ways to run more:

| | Replicas | uvicorn `--workers` |
| --- | --- | --- |
| What runs | One server a container or pod, behind a load balancer | Several processes in one container, started by uvicorn |
| Models | One set a replica | One set a worker, so memory is a set times the workers |
| Probes | Each replica has its own `/health` and `/ready` | One container's probe answers for whichever worker takes it |
| On the default storage | Not possible: each replica would have collections of its own | Several processes writing the same collection files, which must not happen: see [Threads and processes](../explanation/concurrency.md) |
| On shared stores | Supported: the table above | The workers still share one data folder, so anything kept there as files has several writers |

Prefer replicas. The Kubernetes setup in `docker/kubernetes` runs one replica on
a `ReadWriteOnce` volume with the `Recreate` strategy, so two processes never
write the same files. With every row of the shared table set, raise the
replicas and make the strategy `RollingUpdate`; see
[Run it in containers](containers.md#more-than-one-replica).

## An ingestion worker beside the server

Ingestion does not have to happen in the server. `IngestWorker` turns object
events into `add_document` and `delete_document` calls, in a function or a
container of its own, and the server lists and searches what it wrote: a server
opens the collections a shared backend holds as they are asked for, including
ones another process made. [Ingest when a file lands](ingest-on-event.md) has
the worker on AWS, on Azure and on a laptop.

What makes it safe to run behind a queue:

| Behaviour | What it gives you |
| --- | --- |
| Idempotent on the document's bytes | An event delivered twice finds the same bytes already in and does nothing, so retries are safe |
| `handle_all` acts on the last event for each object | A burst with several events for one file reads it once; the earlier events are answered `superseded` |
| One index save a batch | A thousand files do not rewrite the index a thousand times |
| `chunks_per_second=` | A pace on what is written: a burst of files then waits between documents, instead of flooding the store or the model |
| Old chunks are replaced only once new text is there | A failed read leaves the document as it was |

Several workers writing one collection is the same question as several
servers: on a store, each keeps its own local index of its own writes; on local
files, keep one writer.

### Dead letters

A file that cannot be read raises `ExtractionError` by default. On a queue, an
exception is what asks for a retry, and after the queue's last try it moves the
message aside, where a person can find it:

| Queue | Where a file that never reads ends up |
| --- | --- |
| Amazon SQS | The dead-letter queue you give it |
| Azure Storage Queue under a function app | `<queue>-poison`, after `maxDequeueCount` tries, 5 by default |

Put an alert on that queue's length: it is the list of files that would not
read. Give the queue a visibility timeout about six times the function's, so a
long OCR is not handed to a second worker while the first is still reading.

`IngestWorker(..., on_error="record")` returns an outcome with
`action="failed"` and the reason in `error` instead of raising, for a batch
that should not stop at one bad file. Nothing reaches the dead letter then, so
log or count the failed outcomes yourself.

## The extraction service and its queue

The extraction service reads scans, recordings, videos and YouTube addresses for
the server; see [Run an extraction service](extraction-service.md). Most of its
calls answer in the request. A long one, `youtube_save`, answers `202` with a
job at once and does the work later:

| `VECTRIXDB_EXTRACT_JOBS` | Where the job is kept | Where the work runs |
| --- | --- | --- |
| `memory`, the default | In the process | Two threads in the same process. A restart forgets every job |
| `azure` | A record a job in a Blob container, `VECTRIXDB_EXTRACT_CONTAINER`, `extraction` by default | A message on a storage queue, `VECTRIXDB_EXTRACT_QUEUE`, `extract-jobs` by default, read by the function app's queue trigger with `run_extraction_job` |

With `azure`, any instance answers `GET /jobs/{job}`, because the record is in
the container and not in anybody's memory, so the service scales out like the
server. Both are made when they are not there. It needs the `jobs-azure` extra.

A job that fails is recorded as `failed` with the reason and not raised, so the
queue does not try it again: a video YouTube refused once it refuses again, and
each try is paid for. A message delivered twice runs the job once, since a job
already `done` or `failed` is left alone. What reaches the poison queue is a
message the trigger itself could not run.
