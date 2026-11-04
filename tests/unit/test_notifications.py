"""notifications.py: due-date rule, message builders, and the SMTP2GO client (against a local stub)."""
import datetime

import pytest

import notifications as n

TODAY = datetime.date(2026, 7, 6)


class TestServiceDueDate:
    def test_next_service_date_wins(self):
        assert n.service_due_date('2026-01-01', '2026-03-15') == datetime.date(2026, 3, 15)

    def test_falls_back_to_a_year_after_last_service(self):
        assert n.service_due_date('2025-07-06', '') == datetime.date(2026, 7, 6)

    def test_leap_day_rolls_to_march_first_like_the_frontend(self):
        assert n.service_due_date('2028-02-29', None) == datetime.date(2029, 3, 1)

    def test_never_serviced_is_not_due(self):
        assert n.service_due_date('', '2026-01-01') is None


def test_fmt_date():
    assert n.fmt_date('2026-07-04') == 'Sat Jul 4, 2026'
    assert n.fmt_date('') == '—'
    assert n.fmt_date('garbage') == 'garbage'


RECORD = {'customer': 'Côté <script>alert(1)</script>', 'phone': '(207) 555-0142', 'site': '18 Birch Rd, Cape Elizabeth',
          'unit': 'Generac G22 (S/N GEN-1)', 'service_date': '2026-07-04', 'next_service_date': '2027-07-04',
          'detail': 'Oil & filter <b>done</b>', 'logged_by': 'tech1'}


class TestServiceRecordEmail:
    def test_subject_and_text(self):
        subject, text, _ = n.service_record_email(RECORD)
        assert subject == 'Service logged: Côté <script>alert(1)</script> — Generac G22 (S/N GEN-1)'
        assert 'Logged by: tech1' in text and 'Next service: Sun Jul 4, 2027' in text

    def test_html_escapes_every_value(self):
        _, _, html_body = n.service_record_email(RECORD)
        assert '<script>' not in html_body and '&lt;script&gt;' in html_body
        assert '<b>done</b>' not in html_body and 'Oil &amp; filter' in html_body

    def test_links_to_the_app_only_when_configured(self):
        assert 'https://ww.example.com' in n.service_record_email(RECORD, 'https://ww.example.com')[2]
        assert 'href' not in n.service_record_email(RECORD)[2]
        assert 'Open Watt Watch: https://ww.example.com' in n.service_record_email(RECORD, 'https://ww.example.com')[1]


def activity(**overrides):
    base = {'serviced': [], 'added': [], 'transferred': [], 'dates_edited': [], 'deleted': [],
            'invoiced_count': 0, 'not_invoiced_total': 0}
    base.update(overrides)
    return base


class TestWeeklyDigestEmail:
    start, end = datetime.date(2026, 6, 26), datetime.date(2026, 7, 3)

    def test_quiet_week_still_sends_with_an_empty_state(self):
        subject, text, html_body = n.weekly_digest_email(activity(), self.start, self.end)
        assert subject == 'Weekly digest: 0 service visits (Jun 26 – Jul 3)'
        assert 'No service visits were logged this week.' in text and 'No service visits' in html_body
        assert 'Transfers' not in html_body  # empty optional sections are left out

    def test_lists_each_kind_of_activity(self):
        a = activity(
            serviced=[{'date': '2026-07-01', 'customer': 'A&B', 'unit': 'U1', 'by': 'tech', 'invoiced': False, 'detail': 'd'}],
            added=[{'customer': 'C', 'unit': 'U2', 'by': 'boss'}],
            transferred=[{'unit': 'U3', 'detail': 'Transferred from X to Y.', 'by': 'tech'}],
            dates_edited=[{'customer': 'D', 'unit': 'U4', 'detail': 'Next service: none → 2027-01-01.', 'by': 'tech'}],
            deleted=[{'entity': 'customer', 'label': 'Old Co', 'by': 'boss'}],
            invoiced_count=2, not_invoiced_total=7)
        subject, text, html_body = n.weekly_digest_email(a, self.start, self.end)
        assert subject.startswith('Weekly digest: 1 service visit (')
        for needle in ('A&amp;B', 'U2', 'Transferred from X to Y.', 'Next service: none', 'Old Co',
                       '2 marked invoiced', '7 service record(s) not yet invoiced'):
            assert needle in html_body, needle
        assert 'SERVICE VISITS' in text and 'A&B | U1 | tech | No | d' in text


class TestHitListEmail:
    def group(self, customer, days):
        due = TODAY + datetime.timedelta(days=days)
        return {'customer': customer, 'phone': '', 'units': [
            {'unit': 'U', 'site': '', 'due': due.isoformat(), 'last': '', 'days': days}]}

    def test_counts_and_wording(self):
        subject, text, html_body = n.hit_list_email([self.group('Late Co', -12)], [self.group('Soon', 0), self.group('Later', 9)], TODAY)
        assert subject == 'Hit list: 1 overdue, 2 due in the next 30 days'
        assert '12 days overdue' in text and '(today)' in text and 'in 9 days' in text
        assert 'Overdue (1)' in html_body

    def test_empty_sections(self):
        _, text, _ = n.hit_list_email([], [], TODAY)
        assert 'Nothing overdue.' in text and 'Nothing due in the next 30 days.' in text


def test_sample_email_names_the_user():
    subject, text, html_body = n.sample_email('betty<3')
    assert subject == 'Watt Watch test email' and 'betty<3' in text and 'betty&lt;3' in html_body


class TestSendEmail:
    def test_not_configured(self):
        assert not n.email_configured()
        with pytest.raises(n.EmailNotConfigured):
            n.send_email('a@example.com', 's', 't', 'h')

    def test_posts_the_message_with_the_api_key_header(self, smtp2go):
        assert n.email_configured()
        assert n.send_email('betty@example.com', 'Subj', 'text', '<p>html</p>') == 'e1'
        [req] = smtp2go.requests
        assert {k.lower(): v for k, v in req['headers'].items()}['x-smtp2go-api-key'] == 'api-test-key'
        assert req['body'] == {'sender': 'Watt Watch <alerts@example.com>', 'to': ['betty@example.com'],
                               'subject': 'Subj', 'text_body': 'text', 'html_body': '<p>html</p>'}
        assert 'api_key' not in req['body']  # key only in the header

    def test_http_error_includes_smtp2go_message(self, smtp2go):
        smtp2go.reply = (400, {'data': {'error': 'Sender not verified', 'error_code': 'E_ApiResponseCodes.X'}})
        with pytest.raises(n.EmailError, match='HTTP 400: Sender not verified'):
            n.send_email('a@example.com', 's', 't', 'h')

    def test_rejected_recipient(self, smtp2go):
        smtp2go.reply = (200, {'data': {'succeeded': 0, 'failed': 1, 'failures': ['bad@ address']}})
        with pytest.raises(n.EmailError, match='did not accept the email: bad@ address'):
            n.send_email('a@example.com', 's', 't', 'h')

    def test_unreadable_response(self, smtp2go):
        smtp2go.reply = (200, 'not an object')
        with pytest.raises(n.EmailError):
            n.send_email('a@example.com', 's', 't', 'h')

    def test_unreachable_endpoint(self, smtp2go, monkeypatch):
        monkeypatch.setenv('SMTP2GO_API_URL', 'http://127.0.0.1:9/nothing-listens-here')
        with pytest.raises(n.EmailError, match='Could not reach SMTP2GO'):
            n.send_email('a@example.com', 's', 't', 'h')

    def test_app_url_trailing_slash_is_trimmed(self, smtp2go):
        assert n.email_settings()['app_url'] == 'https://wattwatch.example.com'
