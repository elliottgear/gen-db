"""Passwords, the role/permission table, and the login rate limiter."""
import pytest

import server
from server import ApiError
from tests.conftest import REAL_PBKDF2_ITERATIONS


class TestPasswords:
    def test_production_iteration_count_is_unchanged(self):
        assert REAL_PBKDF2_ITERATIONS == 260000

    def test_hash_then_verify(self):
        salt, digest = server.hash_password('hunter2!!')
        assert server.verify_password('hunter2!!', salt, digest)
        assert not server.verify_password('hunter2!', salt, digest)

    def test_each_hash_gets_a_fresh_salt(self):
        assert server.hash_password('same')[0] != server.hash_password('same')[0]

    def test_unicode_passwords(self):
        salt, digest = server.hash_password('pässwörd 🔌')
        assert server.verify_password('pässwörd 🔌', salt, digest)


class TestRoles:
    def test_admin_has_every_permission(self):
        assert server.ROLES['admin'] == set(server.PERMISSIONS)

    def test_technician_has_everything_except_invoicing(self):
        assert server.ROLES['technician'] == set(server.PERMISSIONS) - {'service.invoice'}

    def test_technician_is_a_subset_of_admin(self):
        assert server.ROLES['technician'] <= server.ROLES['admin']

    def test_every_role_has_a_label(self):
        assert set(server.ROLE_LABELS) == set(server.ROLES)

    def test_every_route_permission_exists_and_reads_need_none(self):
        for method, pattern, _fn, permission in server.ROUTES:
            if method == 'GET':
                assert permission is None, pattern.pattern
            else:
                assert permission in server.PERMISSIONS, pattern.pattern

    def test_unknown_role_gets_no_permissions(self):
        user = server._user_from_row({'id': 1, 'username': 'x', 'role': 'intern'})
        assert user['permissions'] == set()

    def test_user_json_lists_permissions_sorted(self):
        user = server._user_from_row({'id': 1, 'username': 'x', 'role': 'technician'})
        out = server.user_json(user)
        assert out['role'] == 'technician'
        assert out['permissions'] == sorted(server.ROLES['technician'])
        assert 'id' not in out

    def test_require_permission(self):
        user = server._user_from_row({'id': 1, 'username': 'x', 'role': 'technician'})
        server.require_permission(user, 'service.log')
        server.require_permission(user, None)
        with pytest.raises(ApiError) as exc:
            server.require_permission(user, 'service.invoice')
        assert exc.value.status == 403
        assert exc.value.message == "You don't have permission to mark service records as invoiced. Ask an admin."


class TestLoginRateLimit:
    @pytest.fixture
    def clock(self, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(server.time, 'monotonic', lambda: now[0])
        return now

    def fail(self, key, times):
        for _ in range(times):
            server.record_login_failure(key)

    def test_allows_up_to_the_limit(self, clock):
        self.fail('1.2.3.4', server.LOGIN_MAX_FAILURES - 1)
        server.check_login_allowed('1.2.3.4')

    def test_blocks_at_the_limit(self, clock):
        self.fail('1.2.3.4', server.LOGIN_MAX_FAILURES)
        with pytest.raises(ApiError) as exc:
            server.check_login_allowed('1.2.3.4')
        assert exc.value.status == 429

    def test_limits_are_per_client(self, clock):
        self.fail('1.2.3.4', server.LOGIN_MAX_FAILURES)
        server.check_login_allowed('5.6.7.8')

    def test_failures_age_out_after_the_window(self, clock):
        self.fail('1.2.3.4', server.LOGIN_MAX_FAILURES)
        clock[0] += server.LOGIN_WINDOW_SECONDS + 1
        server.check_login_allowed('1.2.3.4')
        assert '1.2.3.4' not in server._login_failures

    def test_clearing_resets_the_count(self, clock):
        self.fail('1.2.3.4', server.LOGIN_MAX_FAILURES)
        server.clear_login_failures('1.2.3.4')
        server.check_login_allowed('1.2.3.4')
