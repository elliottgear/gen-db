"""Session storage and expiry against a real database."""
import pytest

import server


class Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def cookie(token):
    return Headers({'Cookie': f'{server.SESSION_COOKIE}={token}'})


def age_session(db, token, days):
    db.execute("UPDATE sessions SET created_at = datetime('now', ?) WHERE token=?", (f'-{days} days', token))
    db.commit()


@pytest.fixture
def token(db, make_user):
    return server.create_session(db, make_user('tech', 'technician'))


def test_valid_session_resolves_to_its_user_and_role(db, token):
    user = server.get_session_user(db, cookie(token))
    assert (user['username'], user['role']) == ('tech', 'technician')
    assert 'service.invoice' not in user['permissions']


def test_tokens_are_long_and_unique(db, make_user):
    user_id = make_user('a')
    tokens = {server.create_session(db, user_id) for _ in range(20)}
    assert len(tokens) == 20 and all(len(t) >= 40 for t in tokens)


def test_session_is_valid_until_the_ttl(db, token):
    age_session(db, token, server.SESSION_TTL_DAYS - 1)
    assert server.get_session_user(db, cookie(token))


def test_session_expires_after_the_ttl(db, token):
    age_session(db, token, server.SESSION_TTL_DAYS + 1)
    assert server.get_session_user(db, cookie(token)) is None


def test_old_date_only_timestamps_count_as_expired(db, token):
    db.execute("UPDATE sessions SET created_at='2026-07-18' WHERE token=?", (token,))
    assert server.get_session_user(db, cookie(token)) is None


def test_purge_removes_only_expired_sessions(db, make_user, token):
    old = server.create_session(db, make_user('old'))
    age_session(db, old, server.SESSION_TTL_DAYS + 1)
    server.purge_expired_sessions(db)
    assert [r[0] for r in db.execute('SELECT token FROM sessions')] == [token]


def test_role_changes_apply_to_existing_sessions(db, token):
    db.execute("UPDATE users SET role='admin' WHERE username='tech'")
    assert server.get_session_user(db, cookie(token))['role'] == 'admin'


@pytest.mark.parametrize('headers', [
    Headers(), Headers({'Cookie': ''}), Headers({'Cookie': 'other=1'}),
    Headers({'Cookie': 'a="\\'}), Headers({'Cookie': f'{server.SESSION_COOKIE}=not-a-real-token'}),
])
def test_missing_malformed_or_unknown_cookies_mean_signed_out(db, headers):
    assert server.get_session_user(db, headers) is None


def test_cookie_parse_errors_mean_signed_out(db, monkeypatch):
    def refuse(self, raw):
        raise server.CookieError('illegal cookie')
    monkeypatch.setattr(server.SimpleCookie, 'load', refuse)
    assert server.get_session_user(db, cookie('anything')) is None
