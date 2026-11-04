"""When the scheduled emails fall due (Maine time, DST-aware)."""
import datetime

import pytest

import server


def maine(y, m, d, hh=0, mm=0):
    return server._localize(datetime.datetime(y, m, d, hh, mm))


def test_job_times_are_friday_4pm_and_monday_7am():
    assert server.SCHEDULED_EMAILS == {'weekly_digest': (4, 16, 0), 'hit_list': (0, 7, 0)}


@pytest.mark.parametrize('now, expected', [
    (maine(2026, 7, 3, 16, 0), maine(2026, 7, 3, 16, 0)),     # Friday, exactly on time
    (maine(2026, 7, 3, 15, 59), maine(2026, 6, 26, 16, 0)),   # Friday, a minute early → last week's
    (maine(2026, 7, 4, 9, 0), maine(2026, 7, 3, 16, 0)),      # Saturday
    (maine(2026, 7, 9, 23, 0), maine(2026, 7, 3, 16, 0)),     # Thursday
])
def test_weekly_digest_schedule(now, expected):
    assert server.scheduled_time('weekly_digest', now) == expected


@pytest.mark.parametrize('now, expected', [
    (maine(2026, 7, 6, 7, 0), maine(2026, 7, 6, 7, 0)),
    (maine(2026, 7, 6, 6, 59), maine(2026, 6, 29, 7, 0)),
    (maine(2026, 7, 5, 12, 0), maine(2026, 6, 29, 7, 0)),     # Sunday
])
def test_hit_list_schedule(now, expected):
    assert server.scheduled_time('hit_list', now) == expected


def test_monday_after_dst_ends_is_still_7am_local():
    # DST ends Sunday 2026-11-01; Monday 7am is now 12:00 UTC rather than 11:00 UTC.
    at = server.scheduled_time('hit_list', maine(2026, 11, 2, 8, 0))
    assert at.utcoffset() == datetime.timedelta(hours=-5)
    assert at.astimezone(datetime.timezone.utc).hour == 12


def test_friday_before_dst_starts_is_edt_vs_est():
    assert server.scheduled_time('weekly_digest', maine(2026, 3, 6, 17)).utcoffset() == datetime.timedelta(hours=-5)
    assert server.scheduled_time('weekly_digest', maine(2026, 3, 13, 17)).utcoffset() == datetime.timedelta(hours=-4)


def test_works_without_a_tz_database(monkeypatch):
    expected = server.scheduled_time('hit_list', maine(2026, 11, 2, 8, 0))
    monkeypatch.setattr(server, 'LOCAL_TZ', None)
    now = server._localize(datetime.datetime(2026, 11, 2, 8, 0))
    assert now.utcoffset() == datetime.timedelta(hours=-5)
    assert server.scheduled_time('hit_list', now) == expected
