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
| `models.json` | `python scripts/compare_models.py` | [Benchmarks](../docs/explanation/benchmarks.md), the model table |
| `models_int8.json`, `models_int8_pc.json`, `models_int8_mm.json` | the same, for three INT8 exports of bge-small-en-v1.5: dynamic, per-channel with reduced range, and MatMul only | the model table; `_pc` is the export in the wheel |
| `shards_warm.json` | `python scripts/shard_bench.py --json benchmarks/shards_warm.json` | [Larger than RAM](../docs/explanation/larger-than-ram.md) |
| `shards_ef400.json` | `python scripts/shard_bench.py --ef 400 --shards 1 --json benchmarks/shards_ef400.json` | the matched-recall comparison there |
| `shards_deleted.json` | `python scripts/shard_bench.py --deleted 0.2 --shards 1 10 50 --json benchmarks/shards_deleted.json` | the deletions paragraph there |
| `shards_cold.json` | `python scripts/shard_bench.py --cold --shards 1 10 50 --json benchmarks/shards_cold.json` | the cold-open paragraph there |
| `memory_longmemeval.json` | `python scripts/memory_bench.py longmemeval --limit 150 --json benchmarks/memory_longmemeval.json` | [Conversation memory](../docs/how-to/conversation-memory.md) and the README |
| `memory_locomo.json` | `python scripts/memory_bench.py locomo --json benchmarks/memory_locomo.json` | the same |

Two tables are not here because their scripts write them straight into the
page: `scripts/compare_modes.py --doc docs/explanation/search-modes.md` and
`scripts/measure_memory.py --doc ...`. The recall-by-size table on the
benchmarks page is printed by `python scripts/benchmark_recall.py --sizes 500 --k 10`.

The files written before the scripts recorded provenance are the model
comparisons from 11 September 2026. Their rows name the model and the export,
and the date is the one above.
