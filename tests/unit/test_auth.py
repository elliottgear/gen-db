"""Passwords, the role/permission table, and the sign-in limit settings."""
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

    def test_technician_has_everything_except_invoicing_email_requests_and_others_records(self):
        assert server.ROLES['technician'] == set(server.PERMISSIONS) - {
            'service.invoice', 'service.edit_any', 'notifications.receive', 'requests.manage', 'customers.view_email',
            'service.follow_up'}

    def test_only_admins_can_manage_appointment_requests(self):
        assert [role for role, perms in server.ROLES.items() if 'requests.manage' in perms] == ['admin']

    def test_technician_is_a_subset_of_admin(self):
        assert server.ROLES['technician'] <= server.ROLES['admin']

    def test_every_role_has_a_label(self):
        assert set(server.ROLE_LABELS) == set(server.ROLES)

    def test_every_route_permission_exists_and_reads_need_none(self):
        # Reads are open to every signed-in user, except appointment requests (admins only, reads too).
        for method, pattern, _fn, permission in server.ROUTES:
            if pattern.pattern.startswith('^/api/appointment-requests'):
                assert permission == 'requests.manage', pattern.pattern
            elif method == 'GET':
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


class TestLoginLimits:
    """The counting itself is SlidingWindow's (tests/unit/test_ratelimit.py); sign-in behaviour is in
    tests/api/test_auth.py. Here: the windows are the sizes the constants say."""
    def test_windows_match_their_settings(self):
        assert server.LOGIN_FAILURES.window == server.LOGIN_WINDOW_SECONDS == 15 * 60
        assert server.LOGIN_ACCOUNT_FAILURES.window == server.LOGIN_ACCOUNT_WINDOW_SECONDS == 60 * 60

    def test_the_account_limit_is_looser_than_one_connections(self):
        assert server.LOGIN_ACCOUNT_MAX_FAILURES > server.LOGIN_MAX_FAILURES

