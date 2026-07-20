#!/usr/bin/env python3
"""
Provision a Watt Watch login.

Accounts are pre-provisioned only — there is no in-app signup screen.
Run this on the machine hosting the app to create a username/password.

Usage:  python3 create_user.py <username>
"""
import getpass
import sqlite3
import sys
import datetime
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import DB_PATH, get_conn, init_db, hash_password  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print('Usage: python3 create_user.py <username>')
        sys.exit(1)
    username = sys.argv[1].strip()
    if not username:
        print('Username cannot be blank.')
        sys.exit(1)

    init_db()  # ensures the users table exists even on a brand-new database
    conn = get_conn()
    existing = conn.execute('SELECT id FROM users WHERE username=? COLLATE NOCASE', (username,)).fetchone()
    if existing:
        print(f'A user named "{username}" already exists.')
        conn.close()
        sys.exit(1)

    password = getpass.getpass('Password: ')
    if len(password) < 8:
        print('Password must be at least 8 characters.')
        conn.close()
        sys.exit(1)
    confirm = getpass.getpass('Confirm password: ')
    if password != confirm:
        print('Passwords do not match.')
        conn.close()
        sys.exit(1)

    salt_hex, hash_hex = hash_password(password)
    conn.execute(
        'INSERT INTO users (username,password_hash,password_salt,created_at) VALUES (?,?,?,?)',
        (username, hash_hex, salt_hex, datetime.date.today().isoformat())
    )
    conn.commit()
    conn.close()
    print(f'User "{username}" created. Database: {DB_PATH}')


if __name__ == '__main__':
    main()
