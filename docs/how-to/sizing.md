# Size the server for load

The server spends most of its CPU and memory in two models: the embedding model,
which turns every question and every chunk into a vector, and the reranker,
which reorders candidates when a search asks for it. This page is how the server
schedules that work under load, the settings that size it, and what to give a
container.

Every setting here is an environment variable; the
[settings reference](../reference/settings.md) has them all.

## How the server runs the models

A route that embeds text hands the model call to a worker thread, so the event
loop keeps answering other requests while the model runs. `/health` answers
during a long upload, and an orchestrator never mistakes a busy server for a
dead one.

The model calls wait in one line a process:

- **A batch of 32 texts at a time.** An upload is embedded 32 chunks at a time,
  and each batch takes its own turn, so a search that arrives during a long
  upload waits for one batch, not the whole document.
- **`VECTRIXDB_INFERENCE_CONCURRENCY` batches at once.** 2 when unset. The rest
  wait their turn.
- **`VECTRIXDB_INFERENCE_WAIT_SECONDS` for a turn.** 30 when unset. A batch that
  waits longer is answered `503` with `Retry-After: 5`, and the message says the
  server is busy embedding other requests. A caller told to come back is better
  than a queue that grows until the process runs out of memory.

The routes that embed are the ones that take text: `text-search`,
`text-hybrid-search`, `text-upsert` and the document upload. A search that sends
its own vector, and `keyword-search`, embed nothing.

Both settings are read when a process starts. A running server is not changed
under the requests it is answering.

## Threads a model uses

`VECTRIXDB_THREADS` is how many threads one model session uses. Left out, it is
the CPUs this process may use, at most 4:

| Where it runs | Threads a session gets |
| --- | --- |
| A laptop or a large machine | 4 |
| A container with a CPU limit of 1.5 | 2, the limit rounded up |
| A container with a CPU limit of 1 | 1 |
| A container with no CPU limit | the CPUs the process may run on, at most 4 |
| `VECTRIXDB_THREADS=8` | 8, the setting wins over the cap |

The CPU limit is read from the container's cgroup, `cpu.max` under cgroup v2 or
`cpu.cfs_quota_us` and `cpu.cfs_period_us` under v1, and held against the
process's CPU affinity. A Kubernetes CPU *request* sets no quota, so a pod with
a request and no limit counts the node's CPUs.

Threads beyond the cores a container has take turns on cores it does not have,
and the runtime spins waiting for them, so a model call that takes 20 ms on a
laptop takes seconds. Each batch in the line runs its own session call, so at
most `VECTRIXDB_INFERENCE_CONCURRENCY` times `VECTRIXDB_THREADS` threads embed
at once. Keep that product near the container's CPUs:

| CPUs | `VECTRIXDB_INFERENCE_CONCURRENCY` | `VECTRIXDB_THREADS` |
| --- | --- | --- |
| 1 | 1 | 1 |
| 2 | 2 | 1 |
| 4 | 2 | 2 |
| 8 | 2 | 4, the default |

`VECTRIXDB_THREADS` applies to every model session the library opens, in the
server or in your own process.

## Warm, then ready

With `VECTRIXDB_WARM=1` the server loads the default embedding model as it
starts and embeds one word with it, in a thread of its own, so `/health`
answers while it loads. `vectrixdb serve` turns it on unless it is set, and the
image sets it. `VECTRIXDB_WARM=0` loads the model on the first search instead,
which that search waits for. The reranker is not warmed: it loads the first
time a search reranks.

`/ready` says where that stands. It needs no key.

| `/ready` answers | When |
| --- | --- |
| `503 {"ready": false, "models": "loading"}` | Warming, and not done yet |
| `200 {"ready": true, "models": "loaded", "seconds": 2.1}` | Warmed, and how long it took |
| `503 {"ready": false, "models": "failed", "error": "..."}` | The model would not load, and why |
| `200 {"ready": true, "models": "on first use"}` | Warming is off |

`/health` is whether the process answers at all, and stays `200` through all of
it.

## Probes

The Kubernetes setup in `docker/kubernetes/server.yaml` and the image's own
health check ask the two routes for different things:

| Probe | Route | Settings | Why |
| --- | --- | --- | --- |
| Startup | `/ready` | every 2 s, 60 failures | Traffic waits for the model, for up to two minutes |
| Readiness | `/ready` | every 10 s | A server whose model failed takes no searches |
| Liveness | `/health` | every 20 s, 3 failures | Answers during a long upload, so a busy server is never restarted for being busy |
| Image `HEALTHCHECK` | `/ready` | every 30 s, 5 s timeout, 60 s start period, 3 retries | Docker and Compose report the container healthy once the model is loaded |

Never point a liveness probe at `/ready`. While the model loads it is `503`, and
a liveness probe that fails restarts the container, which loads the model
again.

The same setup asks for `250m` of CPU and `512Mi` of memory and holds the
container to `2Gi`. With no CPU limit, each session gets the node's CPUs, up to
4; add a limit, or set `VECTRIXDB_THREADS`, on a crowded node.

## Memory a process needs

Each process loads its own models, and ONNX Runtime keeps the working memory a
batch needed, so a process grows with the size of what it embeds and reranks,
then levels off.

These figures are the resident memory of one Python process, read with
`psutil` after each step, three runs each in a fresh process. They were taken
on Windows 11 with Python 3.13, onnxruntime 1.28 and 8 CPUs, loading
`DenseEmbedder(language="en")` and `RerankerEmbedder(language="en")`, the
English models the server image carries:

| After | Resident memory |
| --- | --- |
| Python and VectrixDB imported | 45 to 50 MB |
| The embedding model loaded, one word embedded | about 130 MB |
| The reranker loaded too, one pair scored | about 175 MB |
| A batch of 32 chunks of 1,000 characters embedded | about 435 MB |
| The same 32 chunks reranked against a question | 720 to 750 MB |
| Five more rounds of both | 540 to 990 MB |
| 32 texts at the models' 512-token limit, embedded, then reranked | about 870 MB, then 850 MB to 1.75 GB |

Linux counts resident memory differently from Windows, so read these as a
scale. For the English server, plan on about 1 GB a process under steady
uploads and reranked searches, and up to 2 GB with long chunks. The
multilingual image's models are larger on disk, about 560 MB against 34 MB for
the English embedding model, and were not measured here.

To measure on your own machine and models:

```python
import psutil

from vectrixdb.models import DenseEmbedder, RerankerEmbedder

process = psutil.Process()


def resident_mb():
    return round(process.memory_info().rss / 1e6)


embedder = DenseEmbedder(language="en")
embedder.embed(["ready"])
print("embedding model", resident_mb(), "MB")

reranker = RerankerEmbedder(language="en")
reranker.score("refunds", ["Refunds are paid in ten days."])
print("and the reranker", resident_mb(), "MB")

chunk = ("Refunds are paid within ten working days of the request. " * 20)[:1000]
embedder.embed([chunk] * 32)
reranker.score("how long do refunds take", [chunk] * 32)
print("after a batch of 32", resident_mb(), "MB")
```

`psutil` is not a dependency of VectrixDB: `pip install psutil`.

## Workers and processes

`vectrixdb serve` runs one process. Started with uvicorn and `--workers`, each
worker is a process of its own, with its own models, its own line and its own
warm-up, so memory is the figure above times the workers, and two workers run
twice `VECTRIXDB_INFERENCE_CONCURRENCY` batches at once.

Two things `vectrixdb serve` does that uvicorn started by hand does not: it sets
`VECTRIXDB_WARM=1`, and it refuses to listen beyond this machine with no key and
no sign-in. Set the first yourself, and give the server a key.

On the default storage, collections are files under `VECTRIXDB_PATH`, and two
processes must not write the same collection: see
[Threads and processes](../explanation/concurrency.md). So run one worker a
container and add replicas for more, as
[Scale out](scale.md) describes.

## When it goes wrong under load

| What you see | Cause | What fixes it |
| --- | --- | --- |
| The server restarts during uploads | The liveness probe asks `/ready`, which is `503` while a model loads; or the container hit its memory limit, `OOMKilled` in `kubectl describe pod` | Liveness on `/health`, startup and readiness on `/ready`. For memory, raise the limit or lower `VECTRIXDB_INFERENCE_CONCURRENCY`, so fewer batches hold working memory at once |
| Searches take seconds on a small container | Each model session runs more threads than the container has cores, and they spin waiting for each other | `VECTRIXDB_THREADS` at the container's CPUs, or a CPU limit so the quota is read; keep `VECTRIXDB_INFERENCE_CONCURRENCY` times `VECTRIXDB_THREADS` near the CPUs |
| Memory doubles at start | More than one process loaded the models: uvicorn `--workers`, or two replicas on one node. Before 2.2, two first requests could each build the model in one process; 2.2 builds it once a process | `VECTRIXDB_WARM=1`, so the model loads once before traffic; one worker a container; count the processes |
| `503` with "The server is busy embedding other requests" | Every slot in the line stayed taken for `VECTRIXDB_INFERENCE_WAIT_SECONDS` | Retry after `Retry-After`, which the [clients](clients.md) do; add replicas; raise `VECTRIXDB_INFERENCE_CONCURRENCY` only with cores to spare |
| `/ready` stays `503` with `"models": "failed"` | The embedding model would not load; `error` says why | `vectrixdb doctor` loads it and says what to do; see [Check and doctor](troubleshoot.md) |
| The pod never becomes ready | The model takes longer to load than the startup probe allows, two minutes in the setup, on a very small CPU share | Raise the startup probe's `failureThreshold`, or the CPU |
| The first search after a start is slow | `VECTRIXDB_WARM=0`, so the model loads on that search | `VECTRIXDB_WARM=1` |
| The first reranked search is slow | The reranker loads on first use; warm-up loads only the embedding model | Send one reranked search after the server is ready |
