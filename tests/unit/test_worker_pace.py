"""The pace on index writes: a token bucket a burst of files waits on.

What is being held to. ``rate`` tokens a second with a burst of two
seconds' worth; a take within the burst waits nothing; a take past it waits
exactly what the shortfall costs at the rate; time refills it; a rate of
nought is refused. The clock and the sleep are handed in, so no real second
passes.
"""

from __future__ import annotations

import pytest

from vectrixdb.worker import TokenBucket


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_a_burst_costs_nothing_and_the_rest_waits_for_the_rate():
    clock, slept = Clock(), []

    def sleep(seconds):
        slept.append(seconds)
        clock.now += seconds

    pace = TokenBucket(10, clock=clock, sleep=sleep)
    assert pace.take(20) == 0.0 and slept == [], "two seconds' worth is the burst"
    assert pace.take(5) == pytest.approx(0.5) and slept == [pytest.approx(0.5)], "five short at ten a second is half a second"
    clock.now += 10
    assert pace.take(20) == 0.0, "ten seconds later the bucket is full again, and no fuller"


def test_a_rate_of_nought_is_refused_and_the_burst_can_be_set():
    with pytest.raises(ValueError):
        TokenBucket(0)
    pace = TokenBucket(1, burst=1, clock=Clock(), sleep=lambda s: None)
    assert pace.take(1) == 0.0 and pace.take(1) == pytest.approx(1.0)
