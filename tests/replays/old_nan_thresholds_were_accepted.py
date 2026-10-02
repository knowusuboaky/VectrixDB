"""Restore a binary quantiser that fits to NaN and reports success.

``np.median`` of an empty slice is NaN, every comparison against NaN is
false, so every vector encoded to the same code while ``_is_fitted`` was
True.
"""

import numpy as np

from vectrixdb.core.quantization.binary import BinaryQuantizer


def _old_fit(self, vectors):
    vectors = np.asarray(vectors, dtype=np.float32)
    if vectors.ndim == 1:
        vectors = vectors.reshape(1, -1)
    if vectors.shape[1] != self.dimension:
        raise ValueError(f"Expected dimension {self.dimension}, got {vectors.shape[1]}")
    if self.learn_thresholds:
        if len(vectors) > self.threshold_samples:
            indices = np.random.choice(len(vectors), self.threshold_samples, replace=False)
            vectors = vectors[indices]
        self._thresholds = np.median(vectors, axis=0)
    else:
        self._thresholds = np.zeros(self.dimension, dtype=np.float32)
    self._is_fitted = True
    return self


def pytest_configure(config):
    BinaryQuantizer.fit = _old_fit
