#!/usr/bin/env python3
"""
Provision a Watt Watch login, reset its password, change its role, or list every login.

Accounts are pre-provisioned only — there is no in-app signup screen.
Run this on the machine hosting the app to create a username/password.

Usage:  python3 create_user.py <username> [--role admin|technician]   create a user (default: technician)
        python3 create_user.py --reset <username>                     set a new password and sign the user out everywhere
        python3 create_user.py --set-role <username> <admin|technician>
        python3 create_user.py --list                                 list every user with role, email and signed-in sessions
"""
import getpass
import sys
import datetime
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import DB_PATH, ROLES, SESSION_TTL_DAYS, get_conn, init_db, hash_password  # noqa: E402

USAGE = __doc__[__doc__.index('Usage:'):].strip()


def fail(message):
    print(message)
    sys.exit(1)


def prompt_password():
    password = getpass.getpass('Password: ')
    if len(password) < 8:
        fail('Password must be at least 8 characters.')
    confirm = getpass.getpass('Confirm password: ')
    if password != confirm:
        fail('Passwords do not match.')
    return hash_password(password)


def check_role(role):
    if role not in ROLES:
        fail(f'Unknown role "{role}". Choose one of: {", ".join(sorted(ROLES))}.')
    return role


def parse_args(args):
    if '--list' in args:
        if len(args) != 1:
            fail(USAGE)
        return 'list', None, None
    mode, role = 'create', 'technician'
    rest = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == '--reset':
            mode = 'reset'
        elif arg == '--set-role':
            mode = 'set-role'
        elif arg == '--role':
            if i + 1 >= len(args):
                fail(USAGE)
            role = check_role(args[i + 1])
            i += 1
        else:
            rest.append(arg)
        i += 1
    if mode == 'set-role':
        if len(rest) != 2:
            fail(USAGE)
        return mode, rest[0].strip(), check_role(rest[1])
    if len(rest) != 1:
        fail(USAGE)
    return mode, rest[0].strip(), role


def list_users(conn):
    rows = conn.execute(
        "SELECT u.username, u.role, COALESCE(u.email, '') AS email, u.created_at, "
        "(SELECT COUNT(*) FROM sessions s WHERE s.user_id = u.id AND s.created_at >= datetime('now', ?)) AS sessions "
        'FROM users u ORDER BY u.role, u.username COLLATE NOCASE', (f'-{SESSION_TTL_DAYS} days',)).fetchall()
    if not rows:
        print(f'No users yet. Database: {DB_PATH}')
        return
    table = [('USERNAME', 'ROLE', 'EMAIL', 'CREATED', 'SESSIONS')] + [
        (r['username'], r['role'], r['email'] or '-', (r['created_at'] or '')[:10], str(r['sessions'])) for r in rows]
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
    for row in table:
        print('  '.join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
    print(f'{len(rows)} user(s). Database: {DB_PATH}')


def main():
    mode, username, role = parse_args(sys.argv[1:])
    if mode == 'list':
        init_db()
        conn = get_conn()
        try:
            list_users(conn)
        finally:
            conn.close()
        return
    if not username:
        fail('Username cannot be blank.')

    init_db()  # ensures the users table exists even on a brand-new database
    conn = get_conn()
    try:
        existing = conn.execute('SELECT id, username, role FROM users WHERE username=? COLLATE NOCASE',
                                (username,)).fetchone()
        if mode in ('reset', 'set-role') and not existing:
            fail(f'No user named "{username}".')

        if mode == 'reset':
            salt_hex, hash_hex = prompt_password()
            conn.execute('UPDATE users SET password_hash=?, password_salt=? WHERE id=?',
                         (hash_hex, salt_hex, existing['id']))
            revoked = conn.execute('DELETE FROM sessions WHERE user_id=?', (existing['id'],)).rowcount
            conn.commit()
            print(f'Password for "{existing["username"]}" reset; {revoked} session(s) signed out. Database: {DB_PATH}')
            return

        if mode == 'set-role':
            conn.execute('UPDATE users SET role=? WHERE id=?', (role, existing['id']))
            conn.commit()
            print(f'"{existing["username"]}": {existing["role"]} → {role}. Takes effect on their next page load. '
                  f'Database: {DB_PATH}')
            return

        if existing:
            fail(f'A user named "{username}" already exists. Use --reset to change the password.')
        salt_hex, hash_hex = prompt_password()
        conn.execute(
            'INSERT INTO users (username,password_hash,password_salt,created_at,role) VALUES (?,?,?,?,?)',
            (username, hash_hex, salt_hex, datetime.date.today().isoformat(), role)
        )
        conn.commit()
        print(f'User "{username}" created as {role}. Database: {DB_PATH}')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
