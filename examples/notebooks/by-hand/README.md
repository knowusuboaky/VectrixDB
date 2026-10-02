# Notebooks written by hand

Ten notebooks from February 2026, written against VectrixDB 2.0: quick start,
tiers and modes, loading data, custom models, direct embedders, the document
index, storage backends, sync, LangChain and Databricks.

They sat in `tests/` until 2.2, where nothing ran them and they were not
tests. They are kept here as they were written. The suite does not run them,
so a cell may use a name that has since moved; the three notebooks one folder
up are generated from `examples/*.py` by `scripts/make_notebooks.py`, and a
test fails when one of those goes stale. Start with those.
