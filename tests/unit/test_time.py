"""Maine ("today") date handling, including the DST changeover days."""
import datetime
import os
import subprocess
import sys

import pytest

import server
from tests.conftest import FROZEN_MAINE_DATE

UTC = datetime.timezone.utc


def utc(*args):
    return datetime.datetime(*args, tzinfo=UTC)


REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_local_zone_is_new_york():
    assert server.LOCAL_TZ.key == 'America/New_York'


def test_startup_fails_loudly_without_a_time_zone_database(tmp_path):
    # No tzdata package and no OS tz database: importing the server must stop with a clear message.
    script = (
        'import sys, zoneinfo\n'
        'sys.modules["tzdata"] = None\n'
        'zoneinfo.reset_tzpath(to=[])\n'
        'import server\n'
    )
    env = dict(os.environ, DB_PATH=str(tmp_path / 'tz.sqlite3'))
    result = subprocess.run([sys.executable, '-c', script], cwd=REPO, env=env,
                            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    assert result.returncode == 1
    assert 'Cannot load the America/New_York time zone' in result.stderr
    assert 'requirements.txt' in result.stderr


class TestToday:
    def test_is_the_maine_date_even_when_utc_has_rolled_over(self, frozen_clock):
        assert server._today() == FROZEN_MAINE_DATE

    def test_winter_evening(self, frozen_clock):
        frozen_clock.frozen = utc(2026, 12, 25, 4, 59)  # 11:59pm Dec 24 EST
        assert server._today() == '2026-12-24'
        frozen_clock.frozen = utc(2026, 12, 25, 5, 0)
        assert server._today() == '2026-12-25'

    def test_timestamps_are_maine_local_with_offset_and_microseconds(self, frozen_clock):
        assert server._timestamp() == '2026-07-04T20:30:00.000000-04:00'


class TestSpringForward:
    # 2026-03-08: clocks jump from 2:00am EST (07:00 UTC) to 3:00am EDT.

    @pytest.mark.parametrize('instant, expected', [
        (utc(2026, 3, 8, 6, 59, 59), '2026-03-08T01:59:59.000000-05:00'),
        (utc(2026, 3, 8, 7, 0), '2026-03-08T03:00:00.000000-04:00'),
    ])
    def test_timestamp_offset_changes_at_2am_est(self, frozen_clock, instant, expected):
        frozen_clock.frozen = instant
        assert server._timestamp() == expected

    def test_midnight_boundaries_on_either_side(self, frozen_clock):
        frozen_clock.frozen = utc(2026, 3, 8, 4, 59)  # 11:59pm Mar 7 EST
        assert server._today() == '2026-03-07'
        frozen_clock.frozen = utc(2026, 3, 8, 5, 0)  # midnight Mar 8 EST
        assert server._today() == '2026-03-08'
        frozen_clock.frozen = utc(2026, 3, 9, 3, 59)  # 11:59pm Mar 8 EDT
        assert server._today() == '2026-03-08'
        frozen_clock.frozen = utc(2026, 3, 9, 4, 0)  # midnight Mar 9 EDT
        assert server._today() == '2026-03-09'


class TestFallBack:
    # 2026-11-01: clocks fall back from 2:00am EDT (06:00 UTC) to 1:00am EST.

    @pytest.mark.parametrize('instant, expected', [
        (utc(2026, 11, 1, 5, 59, 59), '2026-11-01T01:59:59.000000-04:00'),
        (utc(2026, 11, 1, 6, 0), '2026-11-01T01:00:00.000000-05:00'),
    ])
    def test_timestamp_offset_changes_at_2am_edt(self, frozen_clock, instant, expected):
        frozen_clock.frozen = instant
        assert server._timestamp() == expected

    def test_midnight_boundaries_on_either_side(self, frozen_clock):
        frozen_clock.frozen = utc(2026, 11, 1, 3, 59)  # 11:59pm Oct 31 EDT
        assert server._today() == '2026-10-31'
        frozen_clock.frozen = utc(2026, 11, 1, 4, 0)  # midnight Nov 1 EDT
        assert server._today() == '2026-11-01'
        frozen_clock.frozen = utc(2026, 11, 2, 4, 59)  # 11:59pm Nov 1 EST
        assert server._today() == '2026-11-01'
        frozen_clock.frozen = utc(2026, 11, 2, 5, 0)  # midnight Nov 2 EST
        assert server._today() == '2026-11-02'

    def test_next_year_uses_that_years_rules(self, frozen_clock):
        # 2027 changeovers are Mar 14 and Nov 7.
        frozen_clock.frozen = utc(2027, 3, 14, 7, 0)
        assert server._timestamp().endswith('-04:00')
        frozen_clock.frozen = utc(2027, 11, 7, 5, 59)
        assert server._timestamp().endswith('-04:00')
        frozen_clock.frozen = utc(2027, 11, 7, 6, 0)
        assert server._timestamp().endswith('-05:00')
