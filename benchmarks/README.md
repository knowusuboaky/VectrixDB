# Benchmark results

The raw output of the benchmark scripts, kept so a number on a docs page can be
traced to the run that printed it. Each file was written by the command beside
it; the newer ones also record the date, the machine, the versions and the
model they ran with. Rerun a command to check a number on your own machine:
latency is the machine's, and quality should match to the third decimal.

| File | Written by | Quoted on |
| --- | --- | --- |
| `compare.json` | `python scripts/benchmark_compare.py --size 10000 --queries 200 --k 10` | [Benchmarks](../docs/explanation/benchmarks.md), the README, and the chart `docs/images/benchmark.png` |
| `beir_scifact_bge.json` | `python scripts/beir_eval.py scifact --json benchmarks/beir_scifact_bge.json` | [Benchmarks](../docs/explanation/benchmarks.md), BEIR section |
| `beir_scifact_e5.json` | the same, on 11 September 2026 with e5-small-v2, the default before 2.2 | the e5 comparison in the BEIR section |
| `archive/models.json` | an earlier `scripts/compare_models.py`, on 11 September 2026, with `arctic_xs` and fp32 bge-small among its choices; the script no longer has them, see [archive/README.md](archive/README.md) | [Benchmarks](../docs/explanation/benchmarks.md), the model table |
| `archive/models_int8.json`, `archive/models_int8_pc.json`, `archive/models_int8_mm.json` | the same, for three INT8 exports of bge-small-en-v1.5: dynamic, per-channel with reduced range, and MatMul only; `quantize_models.py` makes only the first now | the model table; `_pc` is the export in the wheel |
| `shards_warm.json` | `python scripts/shard_bench.py --json benchmarks/shards_warm.json` | [Larger than RAM](../docs/explanation/larger-than-ram.md) |
| `shards_ef400.json` | `python scripts/shard_bench.py --ef 400 --shards 1 --json benchmarks/shards_ef400.json` | the matched-recall comparison there |
| `shards_deleted.json` | `python scripts/shard_bench.py --deleted 0.2 --shards 1 10 50 --json benchmarks/shards_deleted.json` | the deletions paragraph there |
| `shards_cold.json` | `python scripts/shard_bench.py --cold --shards 1 10 50 --json benchmarks/shards_cold.json` | the cold-open paragraph there |
| `memory_longmemeval.json` | `python scripts/memory_bench.py longmemeval --limit 150 --json benchmarks/memory_longmemeval.json` | [Conversation memory](../docs/how-to/conversation-memory.md) and the README |
| `memory_longmemeval_both.json` | `python scripts/memory_bench.py longmemeval --limit 150 --dense-model bge-small --dense-model e5-small --json benchmarks/memory_longmemeval_both.json`, a collection carrying both models, fused | the two-model row on the conversation memory page, the [search modes](../docs/explanation/search-modes.md) page and the CHANGELOG's 0.814 |
| `answer_cutoff_scifact_bge.json` | `python scripts/answer_cutoff_bench.py scifact --json benchmarks/answer_cutoff_scifact_bge.json`, over the dense index `beir_eval.py` built | [Measure retrieval](../docs/how-to/measure-retrieval.md), the answer cut-off table: the 0.83 that separates answers from near misses on SciFact, with precision and declined beside it |
| `memory_locomo.json` | `python scripts/memory_bench.py locomo --json benchmarks/memory_locomo.json` | the same |

Two tables are not here because their scripts write them straight into the
page: `scripts/compare_modes.py --doc docs/explanation/search-modes.md` and
`scripts/measure_memory.py --doc ...`. The recall-by-size table on the
benchmarks page is printed by `python scripts/benchmark_recall.py --sizes 500 --k 10`.

The files written before the scripts recorded provenance are the model
comparisons from 11 September 2026, now under `archive/`. Their rows name the
model and the export, and the date is the one above.

One quoted number has no file here: LongMemEval with e5-small-v2 alone, the
0.797 evidence recall (0.642 on the multi-session questions) that the
conversation memory page, the benchmarks page and the CHANGELOG set against
bge-small's 0.721. It was measured before `memory_bench.py` wrote its model
into the file, and the file was not kept. To measure it again:
`python scripts/memory_bench.py longmemeval --limit 150 --dense-model e5-small --json benchmarks/memory_longmemeval_e5.json`.
