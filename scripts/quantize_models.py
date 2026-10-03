"""Quantize an ONNX model under vectrixdb/models/data to INT8, about a quarter of its size.

    python scripts/quantize_models.py                          # list the models here, their sizes, and which are INT8
    python scripts/quantize_models.py --model bge_base_en      # quantize one, keeping the original as .onnx.backup
    python scripts/quantize_models.py --model all              # every model here that is not INT8 yet
    python scripts/quantize_models.py --restore --model bge_base_en

The models bundled in the wheel are already INT8, so this is for a model newly
exported to ONNX, before it is bundled. The list is read from the directories
on this machine, named as the registry in vectrixdb.models.embedded names
them, so it never offers a model that is not here. Nothing happens without
--model: this rewrites model files in place, and it used to quantize every
model, the bundled ones again included, when run with no arguments.

After quantizing, measure what it cost before shipping it:
``python scripts/compare_models.py`` for retrieval, and
``python scripts/model_checksums.py --write <name>`` to record the new files.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Dict


# ============================================================================
# SETTINGS: where the models are, and the ops quantized
# ============================================================================
#
# Where the models are on this machine, and the ONNX operators whose presence
# marks a graph as already INT8.

SCRIPT_DIR = Path(__file__).resolve().parent
MODELS_DIR = SCRIPT_DIR.parent / "vectrixdb" / "models" / "data"
sys.path.insert(0, str(SCRIPT_DIR.parent))

#: Operators only a quantized graph contains.
QUANTIZED_OPS = {
    "DynamicQuantizeLinear",
    "MatMulInteger",
    "QuantizeLinear",
    "DequantizeLinear",
    "QLinearMatMul",
    "ConvInteger",
}


# ============================================================================
# FINDING THE MODELS
# ============================================================================
#
# INPUT   the data directory
# OUTPUT  every ONNX file there, by name, as the registry in
#         vectrixdb.models.embedded names it
#
# Read from the directories on this machine, so it never offers a model that
# is not here.


def find_models() -> Dict[str, Dict]:
    """Every ONNX file under the data directory, by name, with what the registry calls it."""
    try:
        from vectrixdb.models.embedded import MODEL_CONFIG, MODEL_DIRS
    except Exception:  # the list still works without the package importable
        MODEL_CONFIG, MODEL_DIRS = {}, {}
    by_dir = {MODEL_DIRS.get(key, key): config for key, config in MODEL_CONFIG.items()}
    found: Dict[str, Dict] = {}
    for onnx_file in sorted(MODELS_DIR.glob("*/*.onnx")):
        folder = onnx_file.parent.name
        name = folder if onnx_file.name == "model.onnx" else f"{folder}-{onnx_file.stem}"
        described = by_dir.get(folder, {}).get("name", folder)
        found[name] = {"path": onnx_file, "description": described}
    return found


MODELS = find_models()


# ============================================================================
# QUANTIZING, RESTORING AND SHOWING
# ============================================================================
#
# INPUT   a model's name, and whether to keep a backup
# OUTPUT  the model rewritten in place to INT8 with the original kept as
#         .onnx.backup; the original put back; or the list with sizes and
#         which are INT8
#
# The bundled models are already INT8, so this is for a model newly exported
# to ONNX, before it is bundled. Nothing happens without --model: this
# rewrites model files in place.


def get_file_size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024) if path.exists() else 0.0


def is_quantized(path: Path) -> bool:
    """Whether the graph already holds quantized operators. Needs the onnx package, as quantizing does."""
    import onnx

    graph = onnx.load(str(path), load_external_data=False).graph
    return any(node.op_type in QUANTIZED_OPS for node in graph.node)


def quantize_model(model_name: str, backup: bool = True) -> bool:
    """Quantize one model in place to INT8, the original kept as .onnx.backup."""
    try:
        from onnxruntime.quantization import QuantType, quantize_dynamic
    except ImportError:
        print("onnxruntime.quantization is not available: pip install onnxruntime onnx")
        return False
    if model_name not in MODELS:
        print(f"No model called {model_name!r} here. These are: {', '.join(MODELS) or 'none'}")
        return False

    model_path = MODELS[model_name]["path"]
    if is_quantized(model_path):
        print(
            f"{model_name} is INT8 already; quantizing it again would only lose accuracy. Left as it is."
        )
        return True

    original_size = get_file_size_mb(model_path)
    print(
        f"\n{'=' * 60}\nQuantizing {model_name} ({MODELS[model_name]['description']}), {original_size:.1f} MB\n{'=' * 60}"
    )
    backup_path = model_path.with_suffix(".onnx.backup")
    if backup and not backup_path.exists():
        print(f"Keeping the original as {backup_path.name}")
        shutil.copy(model_path, backup_path)

    temp_path = model_path.with_suffix(".onnx.quantized")
    try:
        quantize_dynamic(
            model_input=str(model_path), model_output=str(temp_path), weight_type=QuantType.QUInt8
        )
        quantized_size = get_file_size_mb(temp_path)
        temp_path.replace(model_path)
        print(
            f"Now {quantized_size:.1f} MB, {(1 - quantized_size / original_size) * 100:.1f}% smaller"
        )
        return True
    except Exception as exc:
        print(f"Quantizing {model_name} failed: {exc}")
        if temp_path.exists():
            temp_path.unlink()
        if backup and backup_path.exists():
            print("The original is put back from its backup.")
            shutil.copy(backup_path, model_path)
        return False


def restore_from_backup(model_name: str) -> bool:
    if model_name not in MODELS:
        print(f"No model called {model_name!r} here.")
        return False
    model_path = MODELS[model_name]["path"]
    backup_path = model_path.with_suffix(".onnx.backup")
    if not backup_path.exists():
        print(f"{model_name} has no backup to put back.")
        return False
    shutil.copy(backup_path, model_path)
    print(f"{model_name} is the original again.")
    return True


def show() -> None:
    print("Models here, and whether they are INT8:")
    total = 0.0
    for name, config in MODELS.items():
        size = get_file_size_mb(config["path"])
        total += size
        try:
            state = "INT8" if is_quantized(config["path"]) else "not quantized"
        except ImportError:
            state = "unknown without the onnx package"
        backup = ", backup kept" if config["path"].with_suffix(".onnx.backup").exists() else ""
        print(f"  {name:<24} {size:8.1f} MB  {state}{backup}  ({config['description']})")
    print(f"  {'total':<24} {total:8.1f} MB")
    print(
        "\nNothing was changed. Name one with --model, or --model all for every model not yet INT8."
    )


# ============================================================================
# MAIN SCRIPT
# ============================================================================
#
# INPUT   --model, or --model all, and --restore
# OUTPUT  the list, or the models quantized or restored
#
# After quantizing, measure what it cost before shipping it: compare_models.py
# for retrieval, and model_checksums.py --write to record the new files.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Quantize a VectrixDB ONNX model to INT8, in place."
    )
    parser.add_argument(
        "--model",
        choices=["all", *MODELS],
        default=None,
        help="the model to quantize, or all; left out, nothing is changed",
    )
    parser.add_argument(
        "--no-backup", action="store_true", help="do not keep the original as .onnx.backup"
    )
    parser.add_argument(
        "--restore", action="store_true", help="put the original back from its backup"
    )
    args = parser.parse_args(argv)

    if args.model is None:
        show()
        return 0
    names = list(MODELS) if args.model == "all" else [args.model]
    if args.restore:
        ok = [restore_from_backup(name) for name in names]
    else:
        ok = [quantize_model(name, backup=not args.no_backup) for name in names]
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
