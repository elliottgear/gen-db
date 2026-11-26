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


# ------------------------------------------------------- appointment requests --

REQUEST = {'id': 7, 'name': 'Mike Jones', 'phone': '207 475 5657', 'email': 'mike@example.com',
           'serviceStreet': '65 Oak St', 'serviceTown': 'Windham', 'brand': 'Kohler', 'model': '20RESA',
           'need': 'repair', 'needLabel': 'Repair', 'notes': 'Gate code 1234',
           'createdAt': '2026-11-25T17:05:00.000000-05:00'}
SUGGESTION = {'customerName': 'Elliott Gear', 'tier': 'likely', 'reasons': ['phone matches', 'name and address differ']}


class TestRequestEmail:
    def test_subject_body_and_links(self):
        subject, text, body = n.request_email(REQUEST, SUGGESTION, 'https://ww.example.com')
        assert subject == 'New request: Mike Jones, Windham (Repair)'
        assert 'Phone: (207) 475-5657' in text and 'Gate code 1234' in text
        assert 'Received: Wed Nov 25, 2026, 5:05 PM' in text
        assert 'Suggested match: Likely match: Elliott Gear · phone matches, name and address differ' in text
        assert 'href="tel:+12074755657"' in body and 'href="mailto:mike@example.com"' in body
        assert 'Kohler 20RESA' in body and 'https://ww.example.com' in body

    def test_no_match(self):
        assert 'Suggested match: No match on file' in n.request_email(REQUEST, None)[1]

    def test_user_input_is_escaped_and_the_subject_stays_one_line(self):
        evil = dict(REQUEST, name='<b>Eve</b>\r\nBcc: x@evil.test', notes='<script>alert(1)</script>',
                    email='"><img src=x>@example.com', serviceTown='Saco\n')
        subject, _, body = n.request_email(evil, dict(SUGGESTION, customerName='<i>C</i>'))
        assert '\n' not in subject and '\r' not in subject
        assert subject == 'New request: <b>Eve</b> Bcc: x@evil.test, Saco (Repair)'  # plain text, not HTML
        for raw in ('<b>Eve', '<script>', '<img', '<i>C'):
            assert raw not in body

    def test_long_names_are_shortened_in_the_subject(self):
        subject = n.request_email(dict(REQUEST, name='A' * 200), None)[0]
        assert len(subject) < 140 and subject.count('A') == 79

    def test_phone_that_isnt_ten_digits_is_shown_as_typed_without_a_link(self):
        _, text, body = n.request_email(dict(REQUEST, phone='555-0142'), None)
        assert 'Phone: 555-0142' in text and 'tel:' not in body


def _row(**fields):
    base = {'created_at': '2026-11-25T09:15:00.000000-05:00', 'name': 'Pat Doe', 'phone': '2075550142',
            'service_street': '12 Main St', 'service_town': 'Saco', 'need': 'maintenance'}
    base.update(fields)
    return base


class TestRequestDigest:
    START = datetime.datetime(2026, 11, 24, 18, 0)
    END = datetime.datetime(2026, 11, 25, 18, 0)
    NEEDS = {'maintenance': 'Routine maintenance', 'repair': 'Repair'}

    def test_lists_today_and_still_new(self):
        subject, text, body = n.request_digest_email([_row()], [_row(), _row(name='Old One', need='repair')],
                                                     self.START, self.END, self.NEEDS)
        assert subject == 'Requests today: 1 new, 2 waiting'
        assert 'Nov 24 6:00 PM – Nov 25 6:00 PM' in text
        assert 'RECEIVED TODAY (1)' in text and 'STILL IN NEW (2)' in text
        assert 'Old One | (207) 555-0142 | 12 Main St, Saco | Repair' in text
        assert 'Routine maintenance' in body

    def test_nothing_new_today_but_some_waiting(self):
        subject, text, _ = n.request_digest_email([], [_row()], self.START, self.END, self.NEEDS)
        assert subject == 'Requests today: 0 new, 1 waiting' and 'No new requests today.' in text

    def test_escaped(self):
        body = n.request_digest_email([_row(name='<script>x</script>')], [], self.START, self.END, self.NEEDS)[2]
        assert '<script>' not in body
