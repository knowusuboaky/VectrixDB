# What VectrixDB does not do

The limits, in one place, with what to do about each. Every item is here
because it is true of the code as it stands; when one stops being true it
comes off this page, not out of it quietly.

## Scale

- **One machine.** There is no sharding across machines, no replication and
  no cluster. Sealed shards split an index on one machine, and two processes
  can share a collection through SQLite, as the
  [concurrency page](concurrency.md) sets out. For several boxes, use a server
  product, or Azure AI Search or OpenSearch as the store behind a collection.
- **The index is in memory while it is written.** A read-only open is memory
  mapped and can be larger than RAM, but building a billion-vector index is
  not this library's job. See [Writing past RAM](larger-than-ram.md).
- **Measured at ten thousand vectors.** The comparison on the
  [benchmarks page](benchmarks.md) is at 10,000. HNSW recall drifts down as a
  graph grows and is edited in place, and larger runs will be added when they
  have been run. `rebuild_index()` restores a graph that deletes have worn.
- **Two builds of the same vectors are not the same graph.** usearch inserts
  on every core at once, so the graph, and with it the tail of an approximate
  search, differs from one build to the next. On SciFact five builds of the
  same vectors scored between 0.704 and 0.708 nDCG@10 in dense mode; built on
  one thread, every build matched exact search, 0.707.
  `VECTRIXDB_BUILD_THREADS=1` builds that way: two builds of the same vectors
  are then the same graph and answer every query the same, at the cost of a
  slower build, which is why it is a setting and not the default. Without it,
  a test that pins exact results should search one build, or compare with a
  tolerance.
- **Reading order on a scan is worked out from where the text sits, and
  that is a heuristic.** Columns are found by the empty band between them
  and told from a table by whether the lines fill their column. A page laid
  out like a newspaper, with articles that wrap around each other, or with
  text set at an angle, will still be read wrongly. Rotated pages are not
  turned. `compare_extractors` is how to find out on your own documents.
- **The pure Python index is for reading, not for use.** `NativeHNSWIndex`
  shows the graph algorithm in plain NumPy and is fit for about a hundred
  vectors. Nothing inside the library uses it; a collection indexes with
  usearch. See [HNSW and its limits](hnsw.md).

## Models and search quality

- **The bundled models are English and run on the CPU.** Embedding is the
  slowest thing the library does, and on a GPU machine a dedicated embedding
  service will beat it by a wide margin. Pass `embed_fn=` or vectors of your
  own to skip the bundled model.
- **A small encoder scores like one.** bge-small-en-v1.5 is 384-dimensional
  and scores about 0.71 nDCG@10 on SciFact dense, 0.74 hybrid: above BM25,
  below the large models. A larger model is a download away, and
  `vectrixdb evaluate` shows whether it earns its size on your documents.
- **An INT8 vector depends a little on its batch.** The bundled models
  quantize their activations per batch, so the same text embedded beside
  different texts gets a slightly different vector: cosine 0.997 to itself
  embedded alone. Embedding alone is repeatable, and the embedding cache
  hands back the first vector a text got. On SciFact, exact search scored
  0.713 nDCG@10 on vectors embedded in one set of batches and 0.707 on
  another. Where vectors must be reproducible bit for bit, embed one text at a
  time or keep the cache.
- **The bundled model was chosen on retrieval, and conversation memory
  disagrees.** bge-small-en-v1.5 beats e5-small-v2 by six points of nDCG@10 on
  SciFact and loses to it by eight points of evidence recall on LongMemEval
  (0.721 against 0.797), on questions whose evidence is spread across
  sessions. `dense_model="e5-small"` opens a collection with the older model,
  and one that carries both scores 0.814, at twice the embedding and the disk.
  See [Conversation memory](../how-to/conversation-memory.md#measured).
- **Keyword search stems English only.** With `text_language` set to anything
  else, words are matched as written, with no stemming and no stop words.
- **The reranker is trained on web passages, and hybrid runs it by
  default.** On scientific claims it lowered nDCG@10 from 0.736 to 0.693.
  Hybrid and ultimate mode rerank unless told not to, so measure it on your
  own questions before keeping it; `rerank=False` turns it off.
- **Ultimate mode is for short texts.** A local collection keeps no
  late-interaction vectors, so every candidate is embedded again with that
  model at each query: a hundred of them for a top 10. On SciFact's abstracts,
  about three hundred tokens each, one search took 46 seconds on a laptop and
  93 with the reranker after it, against half a second for hybrid, and over
  five queries its top 10 was hybrid's own. On the 48 short documents of
  `scripts/compare_modes.py` it is 1.4 seconds. Dense is the default mode, and
  `vectrixdb evaluate` opens a collection for every method it can run, so on
  long documents pass `--mode hybrid` or the run takes hours.
- **Relevance cut-offs are for reading a page.** The words strong, fair and
  weak on a result use fixed cut-offs. A cut-off for deciding whether to answer
  at all is measured per model on labelled questions, with
  `evaluation.answer_cutoff()`, and nothing applies it for you: `search()` has
  no floor, so declining is a line of your own code. On in-domain near misses
  similarity separates poorly, 74.5% on SciFact; see
  [Measure retrieval](../how-to/measure-retrieval.md#where-to-stop-answering).
- **Graph extraction without spaCy sees capitalised phrases.** The regex
  extractor finds capitalised phrases, acronyms, quoted terms and repeated
  phrases. `pip install "vectrixdb[nlp]"` and a spaCy model extract real
  entities.
- **Communities depend on what is installed.** Community detection uses
  leidenalg and igraph when they are there, else networkx, else connected
  components, and each finds a different number of communities in the same
  graph.

## Evaluation

- **A run does not compare chunking.** `evaluate()` and the Evaluate page
  search the index as it was built. `evaluation.sweep()` compares chunkers,
  sizes and headings by building scratch copies from the originals, which
  costs an embedding of every document for every combination.
- **Two homes are not offered under a policy.** `search(homes="both")` fuses
  the store's list with the local index's, and a decision record names one
  engine and one set of counts. Under a policy the store stays the engine and
  a search it cannot answer raises.
- **Image vectors come from a model you bring.** No picture model is bundled.
  `image_embedder=` is asked for by name, `vectors="image"`, and is not fused
  into an ordinary search, where the best-looking figure would sit near the
  top of every list.
- **Answers are not scored.** `answer_report` scores written answers with a
  judge you bring; `evaluate` scores retrieval only.
- **A Function App reaches only a cloud index.** A collection kept in SQLite on
  a server's own disk is evaluated on that server, with `vectrixdb evaluate`.

## The server and sign-in

- **Roles are server-wide.** An operator reads every collection that has no
  entitlement policy. What a person may see inside a collection is a policy's
  job, see [Restrict what a search can see](../how-to/entitlements.md), and a
  collection with a policy cannot be shared with guests.
- **An API key is nobody in particular.** A collection with a policy stays
  closed to keys, because a policy needs to know who is asking. Serve it to
  people, or from your own service with `as_principal`.
- **Guest searches are counted per process.** Thirty a minute from one
  address, counted in memory, so several servers behind a load balancer each
  allow thirty.
- **The access log file does not rotate.** It grows until something rotates
  it. On a container platform, send it to the server's output with
  `VECTRIXDB_ACCESS_LOG=stdout`.
- **Masking is not a guarantee.** It finds email addresses, phone numbers and
  card numbers by their shape, not names or formats it does not know, and a
  search still finds a chunk by the number it holds. See
  [Masking identifiers](../how-to/sign-in.md#masking-identifiers).
- **No tenancy, metering or billing.** The server is for one team's
  collections. Whatever embeds the library owns those.
- **The graph chart comes from a CDN.** The dashboard serves everything else
  itself; where cdnjs is blocked, only the Graph tab is lost.

## Stores

- **Filters are pushed into the store on Azure AI Search and OpenSearch.** On
  the other stores the collection asks for twice the results it needs and
  filters them in the process, so a very selective filter can come back short.
- **Cloud stores are tested against fakes on every change.** The fakes speak
  each SDK's types and each vendor's documented scoring. The same contract
  suite runs against real services only in a nightly job that has credentials,
  and the Delta Lake backend has not yet been run against a live warehouse in
  this release.
