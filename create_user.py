#!/usr/bin/env python3
"""
Provision a Watt Watch login, or reset an existing one's password.

Accounts are pre-provisioned only — there is no in-app signup screen.
Run this on the machine hosting the app to create a username/password.

Usage:  python3 create_user.py <username>            create a user
        python3 create_user.py --reset <username>    set a new password and sign the user out everywhere
"""
import getpass
import sys
import datetime
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import DB_PATH, get_conn, init_db, hash_password  # noqa: E402

USAGE = 'Usage: python3 create_user.py [--reset] <username>'


def prompt_password():
    password = getpass.getpass('Password: ')
    if len(password) < 8:
        print('Password must be at least 8 characters.')
        sys.exit(1)
    confirm = getpass.getpass('Confirm password: ')
    if password != confirm:
        print('Passwords do not match.')
        sys.exit(1)
    return hash_password(password)


def main():
    args = sys.argv[1:]
    reset = '--reset' in args
    args = [a for a in args if a != '--reset']
    if len(args) != 1:
        print(USAGE)
        sys.exit(1)
    username = args[0].strip()
    if not username:
        print('Username cannot be blank.')
        sys.exit(1)

    init_db()  # ensures the users table exists even on a brand-new database
    conn = get_conn()
    try:
        existing = conn.execute('SELECT id, username FROM users WHERE username=? COLLATE NOCASE', (username,)).fetchone()
        if reset:
            if not existing:
                print(f'No user named "{username}".')
                sys.exit(1)
            salt_hex, hash_hex = prompt_password()
            conn.execute('UPDATE users SET password_hash=?, password_salt=? WHERE id=?',
                         (hash_hex, salt_hex, existing['id']))
            revoked = conn.execute('DELETE FROM sessions WHERE user_id=?', (existing['id'],)).rowcount
            conn.commit()
            print(f'Password for "{existing["username"]}" reset; {revoked} session(s) signed out. Database: {DB_PATH}')
            return

        if existing:
            print(f'A user named "{username}" already exists. Use --reset to change the password.')
            sys.exit(1)
        salt_hex, hash_hex = prompt_password()
        conn.execute(
            'INSERT INTO users (username,password_hash,password_salt,created_at) VALUES (?,?,?,?)',
            (username, hash_hex, salt_hex, datetime.date.today().isoformat())
        )
        conn.commit()
        print(f'User "{username}" created. Database: {DB_PATH}')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
