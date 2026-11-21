"""SlidingWindow: the one counter behind the login limit and the request form limits."""
import pytest

import ratelimit
from ratelimit import SlidingWindow


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(ratelimit.time, 'monotonic', lambda: now[0])
    return now


def test_counts_hits_per_key(clock):
    w = SlidingWindow(60)
    w.hit('a')
    w.hit('a')
    w.hit('b')
    assert (w.count('a'), w.count('b'), w.count('c')) == (2, 1, 0)


def test_hits_age_out_and_idle_keys_are_dropped(clock):
    w = SlidingWindow(60)
    w.hit('a')
    clock[0] += 30
    w.hit('a')
    clock[0] += 31  # the first hit is now 61s old
    assert w.count('a') == 1
    clock[0] += 30
    assert w.count('a') == 0 and w.tracked_keys() == set()


def test_hit_if_under_records_only_while_under_the_limit(clock):
    w = SlidingWindow(60)
    assert [w.hit_if_under('a', 2) for _ in range(4)] == [True, True, False, False]
    assert w.count('a') == 2  # refused attempts aren't recorded
    clock[0] += 61
    assert w.hit_if_under('a', 2) is True


def test_clear_and_reset_all(clock):
    a, b = SlidingWindow(60), SlidingWindow(60)
    a.hit('x')
    b.hit('y')
    a.clear('x')
    assert a.count('x') == 0 and b.count('y') == 1
    SlidingWindow.reset_all()
    assert b.count('y') == 0
