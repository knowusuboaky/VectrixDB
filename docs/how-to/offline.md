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

## Where a download comes from

`vectrixdb download-models` tries two sources in order, and says which it tried when both fail:

1. **A GitHub release.** Each model in `vectrixdb.models.embedded.MODEL_CONFIG` names a release tag (`dense-en` for `dense_en`, `dense-multi` for `dense`, `mrebel` for `rebel`, and so on), and the downloader fetches `https://github.com/knowusuboaky/VectrixDB/releases/download/<tag>/<type>.zip`: a flat zip of the ONNX INT8 model directory, verified against the checksum manifest. This is the path for a host without HuggingFace access, and it needs no extra.
2. **HuggingFace**, exported to ONNX on your machine, which needs `pip install "vectrixdb[setup-models]"` (optimum or torch). Two models, the English reranker and the English ColBERT, have no HuggingFace path because they ship in the wheel.

A release tag that has not been published answers 404 to everyone, and the first source is only as real as the release behind it. The failure says so: `ModelDownloadError` lists the URL tried and what it answered, the HuggingFace attempt and its error, and, when the release is missing, the command that creates it. `dense_en` is the one that matters most: it is not in the wheel, and a collection written before 2.2 fetches it on first search, so until its release exists that fetch needs HuggingFace access.

Publishing a release is a maintainer's act, in two steps, neither run for you:

```bash
python scripts/publish_models.py dense_en      # zips the model dir into dist/models/dense_en.zip, prints the next line
gh release create dense-en dist/models/dense_en.zip --title "dense_en model" --notes "..."
```

`python scripts/publish_models.py` with no arguments lists every type, its tag, its asset name, and whether its model and checksums are here; record the checksums first (`python scripts/model_checksums.py --write dense_en`) or the download will be refused as unverified. `python scripts/check_model_releases.py` sends a HEAD to every release URL and exits 1 naming the ones that are missing; the nightly workflow runs it as an advisory job.

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

## The speech model

Recordings and the sound of videos are read by faster-whisper (`pip install "vectrixdb[asr]"`), and its model comes from Hugging Face, not from the models directory. Without `VECTRIXDB_OFFLINE` it is downloaded the first time a recording is read, whether or not `VECTRIXDB_AUTO_DOWNLOAD` is set. Fetch it ahead on a machine with a network:

```bash
python -c "from faster_whisper import download_model; download_model('base')"
```

It is kept in the Hugging Face cache, `HF_HOME`, `~/.cache/huggingface` by default; copy that across to a host with no network. With `VECTRIXDB_OFFLINE=1` the model is read from that cache alone, Hugging Face is not asked anything, and a model that is not there raises `ModelDownloadError` naming the command above. `vectrixdb check` says which it is before a start.
