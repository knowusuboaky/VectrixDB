"""Restore the memory monitor calling psutil without checking it is there.

psutil was imported at module scope and never guarded, so every function in
here dereferenced it freely. The import is the half of the bug the AST sweep
in `test_clean_install.py` catches; this is the other half, the behaviour that
follows once the module loads without it.
"""

from vectrixdb.core import scaling


def _old_check_memory(self):
    # No guard: with psutil None this raises AttributeError, which is what
    # the fixed code returns (0.0, False) instead of.
    memory = scaling.psutil.virtual_memory()
    is_pressure = memory.percent >= self.config.memory_high_watermark
    if is_pressure:
        for callback in self._pressure_callbacks:
            callback()
    return memory.percent, is_pressure


def _old_get_available_memory_mb(self):
    return scaling.psutil.virtual_memory().available / (1024 * 1024)


def pytest_configure(config):
    scaling.MemoryManager.check_memory = _old_check_memory
    scaling.MemoryManager.get_available_memory_mb = _old_get_available_memory_mb
