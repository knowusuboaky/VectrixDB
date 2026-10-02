"""Suite-wide settings.

Hypothesis health checks assume an uninstrumented interpreter. Under
coverage, input generation for the larger strategies crosses the "too slow"
line and the test fails without any input having failed; the deadline check
does the same. Both are timing, not correctness, so they are off here and
the property tests keep their example counts.
"""

from hypothesis import HealthCheck, settings

# pandas, and with it pyarrow, loaded before anything can load scikit-learn.
# The English stemmer imports NLTK, NLTK imports scikit-learn, and a
# scikit-learn wheel that ships its C++ runtime under the plain name
# msvcp140.dll, loaded first, leaves pyarrow crashing the whole process the
# first time pandas builds a column of text: a segfault in test_to_pandas,
# 1,600 tests after the import that caused it, depending only on which test
# happened to import what first. Loaded the other way round, all three work.
# Nothing in the library imports scikit-learn; this keeps the suite's result
# from depending on the machine's wheels.
try:
    import pandas  # noqa: F401
except ImportError:
    pass

settings.register_profile(
    "vectrixdb",
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    deadline=None,
)
settings.load_profile("vectrixdb")
