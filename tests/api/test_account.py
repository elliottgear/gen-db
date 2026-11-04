"""Account settings (email + notification choices), the test-email endpoint, and service-record emails."""
import pytest

import server


def me(c):
    return c.json('GET', '/api/me')[1]


class TestMe:
    def test_includes_email_choices_and_the_kinds_on_offer(self, tech):
        body = me(tech)
        assert (body['email'], body['notifications']) == ('', [])
        assert [k['kind'] for k in body['notificationKinds']] == ['service_record', 'weekly_digest', 'hit_list']

    def test_email_configured_flag_is_only_for_people_who_can_receive(self, tech, admin, smtp2go):
        assert 'emailConfigured' not in me(tech)
        assert me(admin)['emailConfigured'] is True

    def test_email_configured_false_when_server_has_no_smtp2go(self, admin):
        assert me(admin)['emailConfigured'] is False


class TestUpdateAccount:
    def test_technician_can_save_an_email(self, tech):
        status, body = tech.json('PUT', '/api/account', {'email': '  tech1@example.com '})
        assert (status, body['email']) == (200, 'tech1@example.com')
        assert me(tech)['email'] == 'tech1@example.com'

    def test_technician_cannot_turn_on_notifications(self, tech):
        status, body = tech.json('PUT', '/api/account', {'email': 't@example.com', 'notifications': ['hit_list']})
        assert status == 403 and 'receive email notifications' in body['error']
        assert me(tech)['email'] == ''  # nothing saved

    def test_technician_saving_with_an_empty_list_is_fine(self, tech):
        assert tech.json('PUT', '/api/account', {'email': 't@example.com', 'notifications': []})[0] == 200

    def test_admin_picks_notifications(self, admin):
        status, body = admin.json('PUT', '/api/account', {'email': 'betty@example.com',
                                                          'notifications': ['hit_list', 'service_record', 'hit_list']})
        assert status == 200 and body['notifications'] == ['hit_list', 'service_record']
        _, body = admin.json('PUT', '/api/account', {'email': 'betty@example.com', 'notifications': ['weekly_digest']})
        assert body['notifications'] == ['weekly_digest']

    def test_omitting_notifications_leaves_them_alone(self, admin):
        admin.json('PUT', '/api/account', {'email': 'b@example.com', 'notifications': ['hit_list']})
        _, body = admin.json('PUT', '/api/account', {'email': 'new@example.com'})
        assert (body['email'], body['notifications']) == ('new@example.com', ['hit_list'])

    def test_clearing_the_email(self, admin, db):
        admin.json('PUT', '/api/account', {'email': 'b@example.com'})
        assert admin.json('PUT', '/api/account', {'email': ''})[1]['email'] == ''
        assert db.execute("SELECT email FROM users WHERE username='boss'").fetchone()[0] is None

    def test_notifications_need_an_email(self, admin):
        assert admin.json('PUT', '/api/account', {'email': '', 'notifications': ['hit_list']}) == \
            (400, {'error': 'Add an email address to get notifications.'})

    @pytest.mark.parametrize('email', ['not-an-email', 'a@b', 'a b@example.com', '@example.com', 'x' * 250 + '@ex.com'])
    def test_invalid_email(self, tech, email):
        assert tech.json('PUT', '/api/account', {'email': email})[0] == 400

    @pytest.mark.parametrize('kinds', ['hit_list', [1], {'hit_list': True}])
    def test_notifications_must_be_a_list_of_strings(self, admin, kinds):
        assert admin.json('PUT', '/api/account', {'email': 'b@example.com', 'notifications': kinds})[0] == 400

    def test_unknown_kind(self, admin):
        status, body = admin.json('PUT', '/api/account', {'email': 'b@example.com', 'notifications': ['spam']})
        assert status == 400 and 'spam' in body['error']

    def test_each_user_only_changes_their_own_account(self, tech, admin):
        tech.json('PUT', '/api/account', {'email': 'tech@example.com'})
        admin.json('PUT', '/api/account', {'email': 'boss@example.com'})
        assert me(tech)['email'] == 'tech@example.com'


class TestTestEmail:
    def test_technicians_cannot(self, tech):
        assert tech.json('POST', '/api/account/test-email')[0] == 403

    def test_needs_a_saved_email(self, admin, smtp2go):
        assert admin.json('POST', '/api/account/test-email') == (400, {'error': 'Save an email address first.'})

    def test_needs_email_set_up_on_the_server(self, admin):
        admin.json('PUT', '/api/account', {'email': 'b@example.com'})
        status, body = admin.json('POST', '/api/account/test-email')
        assert status == 400 and 'SMTP2GO_API_KEY' in body['error']

    def test_sends_to_your_own_address(self, admin, smtp2go, db):
        admin.json('PUT', '/api/account', {'email': 'betty@example.com'})
        assert admin.json('POST', '/api/account/test-email') == (200, {'ok': True, 'sentTo': 'betty@example.com'})
        assert smtp2go.sent == [('betty@example.com', 'Watt Watch test email')]
        assert db.execute('SELECT kind, status FROM email_log').fetchone()[:] == ('test', 'sent')

    def test_reports_smtp2go_errors(self, admin, smtp2go):
        admin.json('PUT', '/api/account', {'email': 'betty@example.com'})
        smtp2go.reply = (400, {'data': {'error': 'Sender address not verified'}})
        status, body = admin.json('POST', '/api/account/test-email')
        assert status == 502 and 'Sender address not verified' in body['error']


class TestServiceRecordEmails:
    @pytest.fixture
    def subscribed_admin(self, make_user, client):
        make_user('boss', 'admin', email='boss@example.com', notify=['service_record'])
        return client('boss')

    def log(self, c, gen, **body):
        body.setdefault('serviceDate', '2026-06-01')
        status, _ = c.json('POST', f"/api/generators/{gen['id']}/service-records", body)
        assert status == 200
        server.run_email_jobs()

    def test_opted_in_admins_get_one_email_per_visit(self, tech, subscribed_admin, make_user, make_generator, smtp2go):
        make_user('ann', 'admin', email='ann@example.com', notify=['hit_list'])  # opted in to something else
        gen = make_generator()
        self.log(tech, gen, nextServiceDate='2027-06-01', notes='Replaced battery')
        assert [to for to, _ in smtp2go.sent] == ['boss@example.com']
        body = smtp2go.requests[0]['body']
        assert body['subject'] == f"Service logged: Test Customer — Generac Guardian 22kW (S/N {gen['serial']})"
        assert 'Replaced battery' in body['text_body'] and 'Logged by: tech' in body['text_body']
        assert 'https://wattwatch.example.com' in body['html_body']

    def test_the_admin_who_logged_it_gets_it_too(self, subscribed_admin, make_generator, smtp2go):
        self.log(subscribed_admin, make_generator(subscribed_admin))
        assert [to for to, _ in smtp2go.sent] == ['boss@example.com']

    def test_email_problems_never_affect_saving(self, tech, subscribed_admin, make_generator, smtp2go, db):
        smtp2go.reply = (500, {'data': {'error': 'down'}})
        self.log(tech, make_generator())
        assert db.execute("SELECT status FROM email_log").fetchone()[0] == 'failed'

    def test_nobody_subscribed_sends_nothing(self, tech, make_generator, smtp2go):
        self.log(tech, make_generator())
        assert smtp2go.requests == []

    def test_emails_wait_for_the_worker_not_the_request(self, tech, subscribed_admin, make_generator, smtp2go):
        gen = make_generator()
        tech.json('POST', f"/api/generators/{gen['id']}/service-records", {'serviceDate': '2026-06-01'})
        assert smtp2go.requests == []  # queued, not sent during the request
        server.run_email_jobs()
        assert len(smtp2go.requests) == 1


def test_a_failing_job_does_not_stop_the_queue(smtp2go, capsys):
    calls = []
    server.enqueue_email_job(lambda: 1 / 0)
    server.enqueue_email_job(calls.append, 'ran')
    server.run_email_jobs()
    assert calls == ['ran'] and 'ZeroDivisionError' in capsys.readouterr().err
