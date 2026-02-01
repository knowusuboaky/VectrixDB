"""
VectrixDB Model Quantization Script

Reduces ONNX model sizes by ~75% using INT8 dynamic quantization.

Before:
  - dense/model.onnx:    450MB
  - reranker/model.onnx: 450MB
  - colbert/model.onnx:  128MB
  - Total:               ~1GB

After INT8 Quantization:
  - dense/model.onnx:    ~115MB
  - reranker/model.onnx: ~115MB
  - colbert/model.onnx:  ~33MB
  - Total:               ~263MB

Usage:
    python scripts/quantize_models.py
    python scripts/quantize_models.py --model dense
    python scripts/quantize_models.py --model all --backup
"""

import argparse
import shutil
from pathlib import Path

try:
    from onnxruntime.quantization import quantize_dynamic, QuantType
    ONNX_QUANTIZATION_AVAILABLE = True
except ImportError:
    ONNX_QUANTIZATION_AVAILABLE = False
    print("Warning: onnxruntime.quantization not available")
    print("Install: pip install onnxruntime")


# Paths
SCRIPT_DIR = Path(__file__).parent
MODELS_DIR = SCRIPT_DIR.parent / "vectrixdb" / "models" / "data"

# Models to quantize
MODELS = {
    "dense": {
        "path": MODELS_DIR / "dense" / "model.onnx",
        "description": "Dense embedder (all-MiniLM-L6-v2)",
    },
    "reranker": {
        "path": MODELS_DIR / "reranker" / "model.onnx",
        "description": "Cross-encoder reranker (ms-marco-MiniLM)",
    },
    "colbert": {
        "path": MODELS_DIR / "colbert" / "model.onnx",
        "description": "ColBERT late interaction embedder",
    },
    "rebel-encoder": {
        "path": MODELS_DIR / "rebel" / "encoder.onnx",
        "description": "REBEL encoder (mREBEL knowledge extraction)",
    },
    "rebel-decoder": {
        "path": MODELS_DIR / "rebel" / "decoder.onnx",
        "description": "REBEL decoder (mREBEL knowledge extraction)",
    },
}


def get_file_size_mb(path: Path) -> float:
    """Get file size in MB."""
    if path.exists():
        return path.stat().st_size / (1024 * 1024)
    return 0


def quantize_model(model_name: str, backup: bool = True) -> bool:
    """
    Quantize a single ONNX model to INT8.

    Args:
        model_name: Name of model (dense, reranker, colbert)
        backup: Whether to backup original model

    Returns:
        True if successful, False otherwise
    """
    if not ONNX_QUANTIZATION_AVAILABLE:
        print("Error: onnxruntime.quantization not available")
        print("Install: pip install onnxruntime")
        return False

    if model_name not in MODELS:
        print(f"Error: Unknown model '{model_name}'")
        print(f"Available: {list(MODELS.keys())}")
        return False

    config = MODELS[model_name]
    model_path = config["path"]

    if not model_path.exists():
        print(f"Error: Model not found: {model_path}")
        return False

    # Get original size
    original_size = get_file_size_mb(model_path)
    print(f"\n{'='*60}")
    print(f"Quantizing: {model_name}")
    print(f"Description: {config['description']}")
    print(f"Original size: {original_size:.1f} MB")
    print(f"{'='*60}")

    # Backup original
    backup_path = model_path.with_suffix(".onnx.backup")
    if backup:
        if not backup_path.exists():
            print(f"Backing up to: {backup_path.name}")
            shutil.copy(model_path, backup_path)
        else:
            print(f"Backup already exists: {backup_path.name}")

    # Quantize
    temp_path = model_path.with_suffix(".onnx.quantized")

    try:
        print("Quantizing to INT8 (dynamic quantization)...")

        quantize_dynamic(
            model_input=str(model_path),
            model_output=str(temp_path),
            weight_type=QuantType.QUInt8,
        )

        # Replace original with quantized
        quantized_size = get_file_size_mb(temp_path)
        reduction = (1 - quantized_size / original_size) * 100

        # Move quantized to original location
        temp_path.replace(model_path)

        print(f"Quantized size: {quantized_size:.1f} MB")
        print(f"Reduction: {reduction:.1f}%")
        print(f"Success!")

        return True

    except Exception as e:
        print(f"Error during quantization: {e}")

        # Cleanup temp file
        if temp_path.exists():
            temp_path.unlink()

        # Restore backup if exists
        if backup and backup_path.exists():
            print("Restoring from backup...")
            shutil.copy(backup_path, model_path)

        return False


def quantize_all(backup: bool = True) -> dict:
    """
    Quantize all models.

    Returns:
        Dict of {model_name: success}
    """
    results = {}

    print("\n" + "="*60)
    print("VectrixDB Model Quantization")
    print("="*60)

    # Show before sizes
    print("\nBefore quantization:")
    total_before = 0
    for name, config in MODELS.items():
        size = get_file_size_mb(config["path"])
        total_before += size
        print(f"  {name}: {size:.1f} MB")
    print(f"  Total: {total_before:.1f} MB")

    # Quantize each model
    for name in MODELS:
        results[name] = quantize_model(name, backup=backup)

    # Show after sizes
    print("\n" + "="*60)
    print("After quantization:")
    total_after = 0
    for name, config in MODELS.items():
        size = get_file_size_mb(config["path"])
        total_after += size
        status = "OK" if results[name] else "FAILED"
        print(f"  {name}: {size:.1f} MB [{status}]")
    print(f"  Total: {total_after:.1f} MB")
    print(f"  Saved: {total_before - total_after:.1f} MB ({(1 - total_after/total_before)*100:.1f}%)")
    print("="*60)

    return results


def restore_from_backup(model_name: str) -> bool:
    """Restore a model from backup."""
    if model_name not in MODELS:
        print(f"Error: Unknown model '{model_name}'")
        return False

    model_path = MODELS[model_name]["path"]
    backup_path = model_path.with_suffix(".onnx.backup")

    if not backup_path.exists():
        print(f"Error: No backup found for {model_name}")
        return False

    print(f"Restoring {model_name} from backup...")
    shutil.copy(backup_path, model_path)
    print("Restored!")
    return True


def restore_all() -> dict:
    """Restore all models from backups."""
    results = {}
    for name in MODELS:
        results[name] = restore_from_backup(name)
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Quantize VectrixDB ONNX models to INT8"
    )
    parser.add_argument(
        "--model",
        choices=["all", "dense", "reranker", "colbert", "rebel-encoder", "rebel-decoder", "rebel"],
        default="all",
        help="Which model to quantize (default: all)"
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        default=True,
        help="Backup original models before quantization (default: True)"
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Don't backup original models"
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help="Restore models from backup instead of quantizing"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Just show current model sizes"
    )

    args = parser.parse_args()

    # Check mode
    if args.check:
        print("\nCurrent model sizes:")
        total = 0
        for name, config in MODELS.items():
            size = get_file_size_mb(config["path"])
            total += size
            backup_exists = config["path"].with_suffix(".onnx.backup").exists()
            backup_str = " (backup exists)" if backup_exists else ""
            print(f"  {name}: {size:.1f} MB{backup_str}")
        print(f"  Total: {total:.1f} MB")
        return

    # Restore mode
    if args.restore:
        if args.model == "all":
            restore_all()
        else:
            restore_from_backup(args.model)
        return

    # Quantize mode
    backup = not args.no_backup

    if args.model == "all":
        quantize_all(backup=backup)
    elif args.model == "rebel":
        # Quantize both rebel encoder and decoder
        quantize_model("rebel-encoder", backup=backup)
        quantize_model("rebel-decoder", backup=backup)
    else:
        quantize_model(args.model, backup=backup)


if __name__ == "__main__":
    main()
