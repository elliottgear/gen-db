"""Appointment-request emails: one per new request, and the 6pm daily summary. Admins only."""
import datetime

import pytest

import appointment_requests as requests_mod
import server

SUBMIT = '/api/public/appointment-requests'


def form(**overrides):
    body = {'name': 'Mike Jones', 'phone': '207-475-5657', 'email': 'mike@example.com',
            'serviceStreet': '65 Oak St', 'serviceTown': 'Windham', 'serviceZip': '04062', 'brand': 'Kohler', 'need': 'repair',
            'model': '', 'notes': 'Gate code 1234', requests_mod.HONEYPOT_FIELD: ''}
    body.update(overrides)
    return body


@pytest.fixture
def no_check(monkeypatch):
    monkeypatch.setenv('TURNSTILE_DISABLED', '1')


def submit(client, **overrides):
    status, body = client().json('POST', SUBMIT, form(**overrides), headers={'X-Real-IP': '198.51.100.9'})
    server.run_email_jobs()
    return status, body


# ---------------------------------------------------------- each new request --

def test_a_new_request_emails_subscribed_admins(client, make_user, make_customer, smtp2go, no_check):
    make_user('betty', 'admin', email='betty@example.com', notify=['request_new'])
    make_user('ann', 'admin', email='ann@example.com', notify=['service_record'])
    make_customer(name='Elliott Gear', phone='2074755657', physicalAddress='33 Beach St')
    status, body = submit(client)
    assert (status, body) == (200, {'ok': True})  # the public answer is unchanged
    assert smtp2go.sent == [('betty@example.com', 'New request: Mike Jones, Windham (Repair)')]
    text = smtp2go.requests[0]['body']['text_body']
    assert 'Gate code 1234' in text and 'Likely match: Elliott Gear' in text


def test_honeypot_hits_send_nothing(client, make_user, smtp2go, no_check):
    make_user('betty', 'admin', email='betty@example.com', notify=['request_new'])
    assert submit(client, **{requests_mod.HONEYPOT_FIELD: 'bot'})[0] == 200
    assert smtp2go.requests == []


def test_nobody_subscribed_sends_nothing(client, make_user, smtp2go, no_check):
    make_user('betty', 'admin', email='betty@example.com', notify=['weekly_digest'])
    submit(client)
    assert smtp2go.requests == []


def test_technicians_cant_subscribe_or_see_the_request_kinds(tech, admin):
    status, body = tech.json('PUT', '/api/account', {'email': 't@example.com', 'notifications': ['request_new']})
    assert status == 403
    kinds = lambda c: [k['kind'] for k in c.json('GET', '/api/me')[1]['notificationKinds']]  # noqa: E731
    assert 'request_new' not in kinds(tech) and 'request_digest' not in kinds(tech)
    assert {'request_new', 'request_digest'} <= set(kinds(admin))


def test_admins_can_subscribe(admin):
    status, body = admin.json('PUT', '/api/account', {'email': 'b@example.com', 'notifications': ['request_new', 'request_digest']})
    assert status == 200 and body['notifications'] == ['request_digest', 'request_new']


def test_a_technician_row_in_the_database_still_gets_nothing(client, make_user, smtp2go, no_check):
    make_user('calvin', 'technician', email='calvin@example.com', notify=['request_new', 'request_digest'])
    submit(client)
    assert server.notification_recipients(server.get_conn(), 'request_digest') == []
    assert smtp2go.requests == []


# ------------------------------------------------------------ 6pm daily summary --

def at(y, m, d, hh, mm=0):
    return server._localize(datetime.datetime(y, m, d, hh, mm))


def add_request(db, created, status='new', name='Pat Doe', reason=''):
    db.execute("INSERT INTO appointment_requests (created_at, updated_at, status, dismiss_reason, name, phone, "
               "phone_digits, email, service_street, service_town, brand, need) "
               "VALUES (?,?,?,?,?,'2075550142','2075550142','p@example.com','12 Main St','Saco','Kohler','repair')",
               (created.isoformat(timespec='microseconds'),) * 2 + (status, reason, name))
    db.commit()


@pytest.fixture
def digest_subscriber(make_user):
    make_user('betty', 'admin', email='betty@example.com', notify=['request_digest'])


def test_digest_at_6pm_lists_the_day_and_goes_once(db, digest_subscriber, smtp2go):
    add_request(db, at(2026, 11, 24, 17, 0), name='Before Window')     # yesterday before 6pm: not "today"
    add_request(db, at(2026, 11, 25, 9, 0), name='This Morning')
    add_request(db, at(2026, 11, 25, 10, 0), status='dismissed', name='Spammy', reason=requests_mod.HONEYPOT_REASON)
    add_request(db, at(2026, 11, 25, 18, 0), name='Right At Six')      # belongs to tomorrow's summary
    assert 'request_digest' in server.run_due_jobs(at(2026, 11, 25, 18, 0))
    [(to, subject)] = smtp2go.sent
    assert to == 'betty@example.com' and subject == 'Requests today: 1 new, 3 waiting'
    text = smtp2go.requests[0]['body']['text_body']
    today = text.split('RECEIVED TODAY')[1].split('STILL IN NEW')[0]
    assert 'This Morning' in today and 'Before Window' not in today and 'Spammy' not in text
    assert 'request_digest' not in server.run_due_jobs(at(2026, 11, 25, 18, 5))
    assert len(smtp2go.requests) == 1


def test_digest_goes_when_nothing_came_in_but_some_are_waiting(db, digest_subscriber, smtp2go):
    add_request(db, at(2026, 11, 20, 9, 0))
    server.run_due_jobs(at(2026, 11, 25, 18, 0))
    assert [s for _, s in smtp2go.sent] == ['Requests today: 0 new, 1 waiting']


def test_no_digest_when_nothing_came_in_and_nothing_is_waiting(db, digest_subscriber, smtp2go):
    add_request(db, at(2026, 11, 20, 9, 0), status='matched')  # an older request, already handled
    assert 'request_digest' in server.run_due_jobs(at(2026, 11, 25, 18, 0))  # the day is still claimed
    assert smtp2go.requests == []
