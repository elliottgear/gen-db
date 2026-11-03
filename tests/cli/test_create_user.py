import os
import subprocess
import sys

import pytest

import create_user
import server

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def run(monkeypatch, capsys, db):
    """Runs create_user.main() with argv and scripted password prompts; returns (exit_code, stdout)."""
    def go(*argv, passwords=()):
        answers = iter(passwords)
        monkeypatch.setattr(create_user.getpass, 'getpass', lambda prompt='': next(answers))
        monkeypatch.setattr(sys, 'argv', ['create_user.py', *argv])
        try:
            create_user.main()
            code = 0
        except SystemExit as e:
            code = e.code
        return code, capsys.readouterr().out
    return go


def user(db, name):
    return db.execute('SELECT * FROM users WHERE username=? COLLATE NOCASE', (name,)).fetchone()


class TestCreate:
    def test_creates_a_technician_by_default(self, run, db):
        code, out = run('alice', passwords=['s3cret-pass', 's3cret-pass'])
        assert code == 0 and 'created as technician' in out
        row = user(db, 'alice')
        assert row['role'] == 'technician'
        assert server.verify_password('s3cret-pass', row['password_salt'], row['password_hash'])

    def test_role_flag_before_or_after_the_name(self, run, db):
        assert run('--role', 'admin', 'bob', passwords=['s3cret-pass'] * 2)[0] == 0
        assert run('carol', '--role', 'admin', passwords=['s3cret-pass'] * 2)[0] == 0
        assert user(db, 'bob')['role'] == user(db, 'carol')['role'] == 'admin'

    def test_refuses_an_existing_name_ignoring_case(self, run, make_user):
        make_user('Alice')
        code, out = run('alice', passwords=['s3cret-pass'] * 2)
        assert code == 1 and 'already exists' in out

    def test_short_password(self, run, db):
        code, out = run('alice', passwords=['short'])
        assert code == 1 and 'at least 8' in out and user(db, 'alice') is None

    def test_mismatched_confirmation(self, run, db):
        code, out = run('alice', passwords=['s3cret-pass', 's3cret-pasx'])
        assert code == 1 and 'do not match' in out and user(db, 'alice') is None

    def test_unknown_role(self, run, db):
        code, out = run('alice', '--role', 'boss')
        assert code == 1 and 'Unknown role "boss"' in out

    def test_unicode_username(self, run, db):
        assert run('Gagné', passwords=['s3cret-pass'] * 2)[0] == 0
        assert user(db, 'Gagné')


class TestReset:
    def test_sets_a_new_password_and_signs_out_every_session(self, run, db, make_user):
        uid = make_user('alice')
        server.create_session(db, uid)
        server.create_session(db, uid)
        code, out = run('--reset', 'alice', passwords=['brand-new-pass'] * 2)
        assert code == 0 and '2 session(s) signed out' in out
        row = user(db, 'alice')
        assert server.verify_password('brand-new-pass', row['password_salt'], row['password_hash'])
        assert db.execute('SELECT COUNT(*) FROM sessions WHERE user_id=?', (uid,)).fetchone()[0] == 0

    def test_other_users_sessions_are_untouched(self, run, db, make_user):
        make_user('alice')
        server.create_session(db, make_user('bob'))
        run('--reset', 'alice', passwords=['brand-new-pass'] * 2)
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 1

    def test_unknown_user(self, run):
        code, out = run('--reset', 'ghost')
        assert code == 1 and 'No user named "ghost"' in out


class TestSetRole:
    def test_promotes_and_demotes(self, run, db, make_user):
        make_user('alice')
        code, out = run('--set-role', 'alice', 'admin')
        assert code == 0 and 'technician → admin' in out
        assert user(db, 'alice')['role'] == 'admin'
        run('--set-role', 'ALICE', 'technician')
        assert user(db, 'alice')['role'] == 'technician'

    def test_does_not_prompt_or_touch_the_password(self, run, db, make_user):
        make_user('alice')
        before = user(db, 'alice')['password_hash']
        run('--set-role', 'alice', 'admin', passwords=[])
        assert user(db, 'alice')['password_hash'] == before

    @pytest.mark.parametrize('argv, message', [
        (('--set-role', 'alice'), 'Usage:'),
        (('--set-role', 'alice', 'boss'), 'Unknown role'),
        (('--set-role', 'ghost', 'admin'), 'No user named'),
    ])
    def test_errors(self, run, make_user, argv, message):
        make_user('alice')
        code, out = run(*argv)
        assert code == 1 and message in out


@pytest.mark.parametrize('argv', [(), ('a', 'b'), ('--role',), ('--role', 'admin')])
def test_usage_errors(run, argv):
    code, out = run(*argv)
    assert code == 1 and 'Usage:' in out


def test_blank_username(run):
    code, out = run('   ')
    assert code == 1 and 'cannot be blank' in out


def test_script_exits_nonzero_on_bad_usage_and_uses_db_path_env(tmp_path):
    env = dict(os.environ, DB_PATH=str(tmp_path / 'cli.sqlite3'))
    result = subprocess.run([sys.executable, 'create_user.py'], cwd=REPO, env=env,
                            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    assert result.returncode == 1 and 'Usage:' in result.stdout
    result = subprocess.run([sys.executable, 'create_user.py', '--set-role', 'ghost', 'admin'], cwd=REPO, env=env,
                            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    assert result.returncode == 1
    assert (tmp_path / 'cli.sqlite3').exists()  # the env var, not the real database, was used
