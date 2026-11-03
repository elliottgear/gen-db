"""Maine ("today") date handling, including the no-tz-database fallback."""
import datetime

import pytest

import server
from tests.conftest import FROZEN_MAINE_DATE

UTC = datetime.timezone.utc


def offset_hours(utc_instant):
    return utc_instant.astimezone(server._us_eastern_tz(utc_instant)).utcoffset().total_seconds() / 3600


@pytest.mark.parametrize('year, month, n, expected', [
    (2026, 3, 2, datetime.date(2026, 3, 8)),
    (2026, 11, 1, datetime.date(2026, 11, 1)),
    (2027, 3, 2, datetime.date(2027, 3, 14)),
    (2027, 11, 1, datetime.date(2027, 11, 7)),
])
def test_nth_sunday(year, month, n, expected):
    assert server._nth_sunday(year, month, n) == expected


class TestEasternFallback:
    def test_winter_is_utc_minus_5_and_summer_is_utc_minus_4(self):
        assert offset_hours(datetime.datetime(2026, 1, 15, 12, tzinfo=UTC)) == -5
        assert offset_hours(datetime.datetime(2026, 7, 15, 12, tzinfo=UTC)) == -4

    def test_spring_forward_happens_at_2am_est(self):
        # 2026-03-08 02:00 EST == 07:00 UTC
        assert offset_hours(datetime.datetime(2026, 3, 8, 6, 59, 59, tzinfo=UTC)) == -5
        assert offset_hours(datetime.datetime(2026, 3, 8, 7, 0, tzinfo=UTC)) == -4

    def test_fall_back_happens_at_2am_edt(self):
        # 2026-11-01 02:00 EDT == 06:00 UTC
        assert offset_hours(datetime.datetime(2026, 11, 1, 5, 59, 59, tzinfo=UTC)) == -4
        assert offset_hours(datetime.datetime(2026, 11, 1, 6, 0, tzinfo=UTC)) == -5

    @pytest.mark.skipif(server.LOCAL_TZ is None, reason='no tz database on this machine')
    def test_matches_the_tz_database_every_7_hours_for_three_years(self):
        start = datetime.datetime(2026, 1, 1, tzinfo=UTC)
        for hours in range(0, 24 * 366 * 3, 7):
            t = start + datetime.timedelta(hours=hours)
            assert t.astimezone(server._us_eastern_tz(t)).utcoffset() == t.astimezone(server.LOCAL_TZ).utcoffset(), t


class TestToday:
    def test_is_the_maine_date_even_when_utc_has_rolled_over(self, frozen_clock):
        assert server._today() == FROZEN_MAINE_DATE

    def test_uses_the_fallback_rules_when_there_is_no_tz_database(self, frozen_clock, monkeypatch):
        monkeypatch.setattr(server, 'LOCAL_TZ', None)
        assert server._today() == FROZEN_MAINE_DATE

    def test_winter_evening(self, frozen_clock):
        frozen_clock.frozen = datetime.datetime(2026, 12, 25, 4, 59, tzinfo=UTC)  # 11:59pm Dec 24 EST
        assert server._today() == '2026-12-24'
        frozen_clock.frozen = datetime.datetime(2026, 12, 25, 5, 0, tzinfo=UTC)
        assert server._today() == '2026-12-25'

    def test_timestamps_are_maine_local_with_offset_and_microseconds(self, frozen_clock):
        assert server._timestamp() == '2026-07-04T20:30:00.000000-04:00'
