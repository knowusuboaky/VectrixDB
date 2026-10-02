# Archived results

Results that `scripts/compare_models.py` can no longer reproduce as it is,
kept because the model table on the [benchmarks page](../../docs/explanation/benchmarks.md)
was pasted from them.

| File | What it holds |
| --- | --- |
| `models.json` | e5-small-v2 INT8 (bare, prefixed, and prefixed with 512-token padding), bge-small-en-v1.5 fp32 and snowflake-arctic-embed-xs fp32 (bare and prefixed) on the fixture set and SciFact |
| `models_int8.json` | bge-small-en-v1.5, dynamic INT8 export |
| `models_int8_pc.json` | bge-small-en-v1.5, per-channel INT8 export with reduced range: the export in the wheel |
| `models_int8_mm.json` | bge-small-en-v1.5, INT8 on MatMul only, per-channel |

All four were written on 11 September 2026 while 2.2 was being developed,
before the scripts recorded a date, machine or version in what they save: the
run that chose bge-small-en-v1.5 as the 2.2 default. The script that wrote
them offered `arctic_xs` and three INT8 exports of bge-small as `--models`;
the library never shipped `snowflake-arctic-embed-xs`, `MODEL_CONFIG` in
`vectrixdb/models/embedded.py` has no entry for it, and
`scripts/quantize_models.py` makes only the dynamic export, so the script
now offers the two bundled models, `bge_small_en` and `dense_en`, both as
their INT8 exports. A rerun of `python scripts/compare_models.py` writes a
fresh `benchmarks/models.json` for those two and does not reproduce these
rows.
