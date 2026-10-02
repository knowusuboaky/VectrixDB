# Run without a network

VectrixDB works offline after `pip install vectrixdb`. The English models it needs for dense search, reranking and late interaction ship inside the wheel, which is why the wheel is about 68 MiB against PyPI's 100 MiB limit. Nothing is fetched at import time, and nothing is fetched on first use unless you ask.

## What is bundled

| Purpose | Model | Where |
| --- | --- | --- |
| Dense embeddings (English, default) | `bge_small_en` | in the wheel |
| Dense embeddings (English, pre-2.2 default) | `dense_en` | fetched once, see below |
| Reranking (English) | `reranker_en` | in the wheel |
| Late interaction / ColBERT | `colbert` | in the wheel |
| Sparse (BM25) | `sparse` | in the wheel |
| Dense embeddings (multilingual) | `dense` | downloaded |
| Reranking (multilingual) | `reranker` | downloaded |
| GraphRAG relation extraction | `rebel` | downloaded |

`Vectrix("notes")` with no arguments uses only bundled models. `vectrixdb download-models` with no type fetches the two multilingual models, about 230 MB; an English-only install needs neither.

`dense_en` is e5-small-v2, the English default before 2.2. It left the wheel in 2.2 because 21.5 MiB of a 100 MiB wheel for a model nothing new is built with was the wrong trade, and the room it freed is what the next model needs. A collection written before 2.2 still opens with it, and the first search either fetches it once (`vectrixdb download-models --type dense_en`, or `VECTRIXDB_AUTO_DOWNLOAD=1`) or you move the collection to the current default with `Vectrix(name, path=..., dense_model="bge-small").reembed()`. On an air-gapped host, fetch it once on a machine with a network and copy it across with the rest of the models directory.

## Two switches

`VECTRIXDB_OFFLINE=1` refuses every download, explicit ones included. Set it on air-gapped hosts and in CI, where a surprise fetch is a flaky test waiting to happen. Anything that would have downloaded raises, and every one of those errors is a `VectrixError`: `ModelDownloadError` when a fetch was refused, `ModelNotFoundError` when the model simply is not on disk. The second is also a `FileNotFoundError`, so older handlers still catch it.

`VECTRIXDB_AUTO_DOWNLOAD=1` permits the implicit downloads: a model fetched because first use found it missing, or a spaCy model pulled while a graph extractor was being built. It is off by default, so a first use that would need a download raises instead and tells you the explicit command to run.

The explicit paths never need the second switch:

```bash
vectrixdb download-models --type dense
```

```python
from vectrixdb.models import download_models

download_models("dense")
```

## Where models live

Downloaded models go to the package's models directory, or to `VECTRIXDB_MODELS_DIR` when it is set. On a locked-down host, download once on a machine with a network, copy the directory across, and point the variable at it:

```bash
export VECTRIXDB_MODELS_DIR=/srv/vectrixdb-models
```

## Checksums

Downloads are verified against `vectrixdb/models/checksums.json`. A mismatch raises `ModelDownloadError` and names the file; a model with no entry on record is accepted with a warning that it was not verified. The manifest is generated from a known-good copy:

```bash
python scripts/model_checksums.py --write dense
python scripts/model_checksums.py --verify dense
```

## Graph mode without spaCy

Graph mode's default extractor uses spaCy when it is installed. Without it, a regex fallback runs and says so once at warning level. To get real entity extraction:

```bash
pip install "vectrixdb[nlp]"
python -m spacy download en_core_web_sm
```

The second line is the download; it is never run for you unless `VECTRIXDB_AUTO_DOWNLOAD=1`.
