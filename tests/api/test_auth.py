import server
from tests.conftest import PASSWORD


def test_login_sets_an_httponly_cookie_and_returns_role(make_user, client):
    make_user('tech', 'technician')
    c = client()
    res, _ = c.request('POST', '/api/login', {'username': 'tech', 'password': PASSWORD})
    assert res.status == 200
    cookie = res.getheader('Set-Cookie')
    assert 'HttpOnly' in cookie and 'SameSite=Lax' in cookie and f'Max-Age={30 * 86400}' in cookie
    assert 'Secure' not in cookie  # plain HTTP locally
    status, me = c.json('GET', '/api/me')
    assert (status, me['username'], me['role']) == (200, 'tech', 'technician')


def test_cookie_is_secure_behind_an_https_proxy(make_user, client):
    make_user('tech')
    res, _ = client().request('POST', '/api/login', {'username': 'tech', 'password': PASSWORD},
                              headers={'X-Forwarded-Proto': 'https'})
    assert 'Secure' in res.getheader('Set-Cookie')


def test_username_is_case_insensitive(make_user, client):
    make_user('Tech1')
    assert client().login('tech1', PASSWORD)['username'] == 'Tech1'


def test_wrong_password_and_unknown_user_get_the_same_answer(make_user, client):
    make_user('tech')
    assert client().json('POST', '/api/login', {'username': 'tech', 'password': 'nope'}) == \
        (401, {'error': 'Incorrect username or password.'})
    assert client().json('POST', '/api/login', {'username': 'ghost', 'password': 'nope'}) == \
        (401, {'error': 'Incorrect username or password.'})


def test_login_requires_both_fields(client):
    for body in ({}, {'username': 'x'}, {'password': 'x'}, {'username': '', 'password': 'x'},
                 {'username': 'x', 'password': 5}):
        assert client().json('POST', '/api/login', body)[0] == 400


def test_logout_ends_the_session(tech):
    status, _ = tech.json('POST', '/api/logout')
    assert status == 200 and tech.cookie is None
    assert tech.json('GET', '/api/me')[0] == 401


def test_logout_only_ends_this_device(make_user, client):
    make_user('tech')
    laptop, phone = client('tech'), client('tech')
    laptop.json('POST', '/api/logout')
    assert phone.json('GET', '/api/me')[0] == 200


def test_logout_without_a_session_is_harmless(client):
    assert client().json('POST', '/api/logout')[0] == 200


def test_every_api_route_requires_a_session(client, db):
    anon = client()
    for method, pattern, _fn, _perm in server.ROUTES:
        path = pattern.pattern.strip('^$').replace(r'(\d+)', '1')
        assert anon.json(method, path, {} if method in ('POST', 'PUT') else None)[0] == 401, (method, path)
    assert anon.json('GET', '/api/attachments/1/file')[0] == 401
    assert anon.request('POST', '/api/generators/1/attachments', raw=b'x',
                        headers={'Content-Type': 'multipart/form-data; boundary=b'})[0].status == 401


def test_expired_session_is_rejected(tech, db):
    db.execute("UPDATE sessions SET created_at = datetime('now', '-31 days')")
    db.commit()
    assert tech.json('GET', '/api/me')[0] == 401


def test_rate_limit_locks_out_a_client_after_repeated_failures(make_user, client):
    make_user('tech')
    anon = client()
    for _ in range(server.LOGIN_MAX_FAILURES):
        assert anon.json('POST', '/api/login', {'username': 'tech', 'password': 'wrong'})[0] == 401
    assert anon.json('POST', '/api/login', {'username': 'tech', 'password': PASSWORD})[0] == 429


def test_rate_limit_keys_on_the_last_forwarded_hop(make_user, client):
    make_user('tech')
    anon = client()
    for i in range(server.LOGIN_MAX_FAILURES):
        # A spoofed first hop shouldn't let an attacker dodge the limit.
        anon.json('POST', '/api/login', {'username': 'tech', 'password': 'x'},
                  headers={'X-Forwarded-For': f'10.0.0.{i}, 203.0.113.9'})
    assert anon.json('POST', '/api/login', {'username': 'tech', 'password': PASSWORD},
                     headers={'X-Forwarded-For': '10.9.9.9, 203.0.113.9'})[0] == 429
    assert anon.json('POST', '/api/login', {'username': 'tech', 'password': PASSWORD},
                     headers={'X-Forwarded-For': '198.51.100.1'})[0] == 200


def test_successful_login_resets_the_failure_count(make_user, client):
    make_user('tech')
    anon = client()
    for _ in range(server.LOGIN_MAX_FAILURES - 1):
        anon.json('POST', '/api/login', {'username': 'tech', 'password': 'wrong'})
    anon.login('tech', PASSWORD)
    for _ in range(server.LOGIN_MAX_FAILURES - 1):
        anon.json('POST', '/api/login', {'username': 'tech', 'password': 'wrong'})
    assert anon.json('POST', '/api/login', {'username': 'tech', 'password': PASSWORD})[0] == 200
