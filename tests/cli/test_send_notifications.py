import pytest

import send_notifications


@pytest.fixture
def run(db, capsys):
    def go(*argv):
        try:
            send_notifications.main(list(argv))
            code = 0
        except SystemExit as e:
            code = e.code
        return code, capsys.readouterr().out
    return go


def test_preview_prints_without_sending(run, smtp2go):
    code, out = run('hit_list', '--preview')
    assert code == 0 and out.startswith('Subject: Hit list:')
    assert smtp2go.requests == []


def test_preview_works_without_email_configured(run):
    assert run('weekly_digest', '--preview')[1].startswith('Subject: Weekly digest:')


def test_send_to_one_address(run, smtp2go):
    code, out = run('weekly_digest', '--to', 'me@example.com')
    assert code == 0 and 'Sent "Weekly digest' in out
    assert [to for to, _ in smtp2go.sent] == ['me@example.com']


def test_send_to_one_address_reports_failure(run):
    code, out = run('hit_list', '--to', 'me@example.com')
    assert code == 1 and 'Not sent: Email is not set up' in out


def test_send_to_subscribers(run, smtp2go, make_user):
    make_user('betty', 'admin', email='betty@example.com', notify=['hit_list'])
    make_user('tech', 'technician', email='tech@example.com', notify=['hit_list'])
    code, out = run('hit_list', '--subscribers')
    assert code == 0 and 'to 1 of 1 subscriber(s)' in out
    assert [to for to, _ in smtp2go.sent] == ['betty@example.com']


def test_subscribers_with_failures_exit_nonzero(run, smtp2go, make_user):
    make_user('betty', 'admin', email='betty@example.com', notify=['hit_list'])
    smtp2go.reply = (500, {'data': {'error': 'down'}})
    code, out = run('hit_list', '--subscribers')
    assert code == 1 and 'Failed: betty@example.com' in out


def test_no_subscribers(run):
    assert run('hit_list', '--subscribers') == (1, 'Nobody is subscribed to hit_list.\n')


@pytest.mark.parametrize('argv', [(), ('hit_list',), ('monthly', '--preview'), ('hit_list', '--send'),
                                  ('hit_list', '--preview', 'extra'), ('hit_list', '--to'),
                                  ('hit_list', '--to', 'not-an-email')])
def test_usage_errors(run, argv):
    assert run(*argv)[0] == 1


def test_request_digest_with_nothing_to_send(run, smtp2go):
    code, out = run('request_digest', '--to', 'me@example.com')
    assert code == 0 and out.startswith('Nothing to send') and smtp2go.requests == []


def test_request_digest_preview(run, db):
    db.execute("INSERT INTO appointment_requests (created_at, updated_at, status, name, phone, phone_digits, email, "
               "service_street, service_town, brand, need) VALUES (?, ?, 'new', 'Pat Doe', '2075550142', "
               "'2075550142', 'p@example.com', '12 Main St', 'Saco', 'Kohler', 'repair')",
               ('2026-01-01T09:00:00.000000-05:00',) * 2)
    db.commit()
    code, out = run('request_digest', '--preview')
    assert code == 0 and out.startswith('Subject: Requests today: 0 new, 1 waiting') and 'Pat Doe' in out
