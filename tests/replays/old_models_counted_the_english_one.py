"""Restore the install check that let the bundled English model answer for the multilingual one.

A fresh install holds bge-small-en-v1.5 and the English reranker. Counting
them as the multilingual dense model and reranker meant neither could be
fetched: the first-use download and ``vectrixdb download-models --type
dense``, the command the error message names, both decided it was already
there. The models list said so too, and called it part of the wheel.
"""

from vectrixdb.models import embedded


def _old_is_models_installed(model_type="all", *, exact=False):
    models_dir = embedded.get_models_dir()
    kinds = ["dense", "sparse", "reranker", "colbert"] if model_type == "all" else [model_type]
    for kind in kinds:
        config = embedded.MODEL_CONFIG.get(kind, {})
        if kind == "sparse":
            wanted = config.get("vocab_file", "vocab.json")
        elif kind == "rebel":
            wanted = config.get("onnx_encoder_file", "encoder.onnx")
        else:
            wanted = config.get("onnx_file", "model.onnx")
        names = (embedded.model_dir_name(kind), *embedded.MODEL_DIR_ALTERNATIVES.get(kind, ()))
        if not any((models_dir / name / wanted).exists() for name in names):
            return False
    return True


def pytest_configure(config):
    embedded.is_models_installed = _old_is_models_installed
