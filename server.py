#!/usr/bin/env python3
"""
Watt Watch backend — customers & generators registry.

Serves the static frontend, the public request page and a small JSON REST API on top of a
local SQLite database (gen_db.sqlite3, created automatically next to this file on first run).
Feature code lives beside it: core.py (shared helpers), ratelimit.py, appointment_requests.py,
matching.py, turnstile.py, notifications.py.

Run:  python3 server.py
Then open http://localhost:8787/
"""
import datetime
import hashlib
import json
import os
import queue
import re
import secrets
import socket
import sqlite3
import threading
import time
import traceback
import unicodedata
import uuid
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import appointment_requests
import checklist
import geocoding
import notifications
import turnstile
from core import (EMAIL_RE, LOCAL_TZ, LONG_TEXT, MAX_ID, SHORT_TEXT, ApiError,  # noqa: F401 (re-exported)
                  _local_now, _timestamp, _today, audit, bool_field, check_not_stale, date_field, id_field, mask_email,
                  require, text_field, uuid_field, write_lock)
from ratelimit import SlidingWindow, client_ip, rate_limit_key

# Semantic version shown in the page footer; bump it per CLAUDE.md and add a CHANGELOG.md entry.
APP_VERSION = '0.9.0'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('DB_PATH') or os.path.join(BASE_DIR, 'gen_db.sqlite3')
STATIC_FILE = os.path.join(BASE_DIR, 'generator-manager.html')
PORTAL_FILE = os.path.join(BASE_DIR, 'customer-request.html')
PORTAL_THANKS_FILE = os.path.join(BASE_DIR, 'customer-thanks.html')
PORTAL_SCRIPT_FILE = os.path.join(BASE_DIR, 'customer-request.js')
PORTAL_CSS_FILE = os.path.join(BASE_DIR, 'portal.css')
FAVICON_FILE = os.path.join(BASE_DIR, 'assets', 'favicon.png')
PORT = int(os.environ.get('PORT', 8787))
# Railway sets PORT; locally, stay off the LAN unless HOST=0.0.0.0 is given.
HOST = os.environ.get('HOST') or ('0.0.0.0' if 'PORT' in os.environ else '127.0.0.1')

# Uploads live next to the database by default so they land on the same persistent volume.
UPLOADS_DIR = os.environ.get('UPLOADS_DIR') or os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), 'uploads')
os.makedirs(UPLOADS_DIR, exist_ok=True)
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30
STREAM_CHUNK = 64 * 1024

# ---------------------------------------------------------------- database --

def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = 10000')
    return conn


def init_db():
    conn = get_conn()
    conn.execute('PRAGMA journal_mode = WAL')
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            physical_address TEXT,
            billing_address TEXT,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS generators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            make TEXT NOT NULL,
            model TEXT NOT NULL,
            serial TEXT NOT NULL,
            physical_address TEXT,
            install_date TEXT,
            last_service_date TEXT,
            installer_name TEXT,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS generator_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            generator_id INTEGER NOT NULL REFERENCES generators(id) ON DELETE CASCADE,
            date TEXT,
            event TEXT,
            detail TEXT,
            invoiced INTEGER NOT NULL DEFAULT 0,
            invoiced_by TEXT,
            invoiced_at TEXT
        );
        CREATE TABLE IF NOT EXISTS generator_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            generator_id INTEGER NOT NULL REFERENCES generators(id) ON DELETE CASCADE,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            content_type TEXT NOT NULL,
            kind TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            uploaded_by TEXT,
            uploaded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            username TEXT,
            action TEXT NOT NULL,
            entity TEXT NOT NULL,
            entity_id INTEGER,
            detail TEXT
        );
    ''')
    conn.commit()
    _migrate(conn)
    purge_expired_sessions(conn)
    if conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0] == 0:
        seed(conn)
    conn.close()
    _remove_incomplete_uploads()


def _add_column(conn, table, column, decl):
    cols = [r['name'] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()]
    if column not in cols:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {decl}')


def _migration_1(conn):
    # Columns added before migrations were versioned; existing databases may already have some.
    _add_column(conn, 'generators', 'next_service_date', 'TEXT')
    _add_column(conn, 'generators', 'physical_address', 'TEXT')
    for table in ('customers', 'generators'):
        _add_column(conn, table, 'created_by', 'TEXT')
        _add_column(conn, table, 'updated_by', 'TEXT')
    _add_column(conn, 'generator_history', 'created_by', 'TEXT')
    _add_column(conn, 'generator_history', 'invoiced', 'INTEGER NOT NULL DEFAULT 0')
    _add_column(conn, 'generator_history', 'invoiced_by', 'TEXT')
    _add_column(conn, 'generator_history', 'invoiced_at', 'TEXT')


def _migration_2(conn):
    # Edit forms send this back so a PUT can't silently overwrite someone else's change.
    for table in ('customers', 'generators'):
        _add_column(conn, table, 'updated_at', 'TEXT')


def _migration_3(conn):
    # Session tokens were exposed in git history and never expired: revoke all of them once.
    conn.execute('DELETE FROM sessions')


def _migration_4(conn):
    # Roles: every existing account starts as a technician; promote admins with create_user.py --set-role.
    _add_column(conn, 'users', 'role', "TEXT NOT NULL DEFAULT 'technician'")


def _migration_5(conn):
    # Email: an address for every user (future password reset), per-user notification choices,
    # a log of every send attempt, and scheduled-run claims so a digest goes out once per week.
    _add_column(conn, 'users', 'email', 'TEXT')
    _add_column(conn, 'generator_history', 'created_at', 'TEXT')  # when it was logged (date = when it happened)
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS user_notifications (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            PRIMARY KEY (user_id, kind)
        );
        CREATE TABLE IF NOT EXISTS email_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            kind TEXT NOT NULL,
            recipient TEXT NOT NULL,
            subject TEXT,
            status TEXT NOT NULL,
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS scheduled_runs (
            kind TEXT NOT NULL,
            period TEXT NOT NULL,
            claimed_at TEXT NOT NULL,
            PRIMARY KEY (kind, period)
        );
    ''')


def _migration_6(conn):
    # Customer request portal: requests from the public /customer page, reviewed by admins.
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS appointment_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'matched', 'done', 'dismissed')),
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            phone_digits TEXT NOT NULL,
            email TEXT NOT NULL,
            service_address TEXT NOT NULL,
            brand TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            need TEXT NOT NULL CHECK (need IN ('maintenance', 'repair', 'not_running')),
            notes TEXT NOT NULL DEFAULT '',
            customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
            generator_id INTEGER REFERENCES generators(id) ON DELETE SET NULL,
            dismiss_reason TEXT NOT NULL DEFAULT '',
            handled_by TEXT,
            handled_at TEXT,
            updated_by TEXT,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_appointment_requests_status ON appointment_requests (status, created_at);
    ''')


def _migration_7(conn):
    # Requests: 'done' becomes 'scheduled', and the service address splits into street and town.
    # SQLite can't change a CHECK constraint or rename-and-split columns in place, so the table is
    # rebuilt in one transaction. Old addresses go into the street; their town is left blank.
    cols = [r['name'] for r in conn.execute('PRAGMA table_info(appointment_requests)').fetchall()]
    if 'service_street' in cols:
        return  # already rebuilt (e.g. a crash after the rebuild but before user_version was saved)
    conn.executescript('''
        BEGIN;
        CREATE TABLE appointment_requests_v7 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'matched', 'scheduled', 'dismissed')),
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            phone_digits TEXT NOT NULL,
            email TEXT NOT NULL,
            service_street TEXT NOT NULL,
            service_town TEXT NOT NULL DEFAULT '',
            brand TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            need TEXT NOT NULL CHECK (need IN ('maintenance', 'repair', 'not_running')),
            notes TEXT NOT NULL DEFAULT '',
            customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
            generator_id INTEGER REFERENCES generators(id) ON DELETE SET NULL,
            dismiss_reason TEXT NOT NULL DEFAULT '',
            handled_by TEXT,
            handled_at TEXT,
            updated_by TEXT,
            updated_at TEXT
        );
        INSERT INTO appointment_requests_v7
            (id, created_at, status, name, phone, phone_digits, email, service_street, service_town, brand, model,
             need, notes, customer_id, generator_id, dismiss_reason, handled_by, handled_at, updated_by, updated_at)
        SELECT id, created_at, CASE status WHEN 'done' THEN 'scheduled' ELSE status END, name, phone, phone_digits,
               email, service_address, '', brand, model, need, notes, customer_id, generator_id, dismiss_reason,
               handled_by, handled_at, updated_by, updated_at
        FROM appointment_requests;
        DROP TABLE appointment_requests;
        ALTER TABLE appointment_requests_v7 RENAME TO appointment_requests;
        CREATE INDEX idx_appointment_requests_status ON appointment_requests (status, created_at);
        COMMIT;
    ''')


def _migration_8(conn):
    # Requests: the ZIP code, so a customer card made from a request has a complete address.
    # Older requests keep '' (they were sent before the form asked for it).
    _add_column(conn, 'appointment_requests', 'service_zip', "TEXT NOT NULL DEFAULT ''")


def _migration_9(conn):
    # The generator map: coordinates for each address, looked up once and cached (geocoding.py).
    # Keyed by the normalized address text, so editing an address leads to a fresh lookup.
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS geocodes (
            address_key TEXT PRIMARY KEY,
            lat REAL,
            lng REAL,
            status TEXT NOT NULL CHECK (status IN ('found', 'not_found', 'incomplete', 'failed')),
            matched_address TEXT,
            updated_at TEXT NOT NULL
        );
    ''')


def _migration_10(conn):
    # Requests: the billing address, when the customer says it differs from the service address.
    # Older requests read as "same", which is how they were always treated.
    _add_column(conn, 'appointment_requests', 'billing_same', 'INTEGER NOT NULL DEFAULT 1')
    for column in ('billing_street', 'billing_town', 'billing_state', 'billing_zip'):
        _add_column(conn, 'appointment_requests', column, "TEXT NOT NULL DEFAULT ''")


SERVICE_DETAIL_NEXT = re.compile(r'^(.*?)\s*Next service scheduled for (\d{4}-\d{2}-\d{2})\.$', re.S)
ROUTINE_SERVICE = 'Routine service.'


def _service_detail(notes, next_service_date):
    """The history text for a service visit. add_service_record and update_service_record both use it."""
    detail = notes if notes else ROUTINE_SERVICE
    if next_service_date:
        detail += f' Next service scheduled for {next_service_date}.'
    return detail


def _parse_service_detail(detail):
    """(notes, next service date) back out of a _service_detail() text. Used to backfill older records."""
    detail = (detail or '').strip()
    m = SERVICE_DETAIL_NEXT.match(detail)
    notes, next_date = (m.group(1).strip(), m.group(2)) if m else (detail, '')
    return ('' if notes == ROUTINE_SERVICE else notes), next_date


def _migration_11(conn):
    # Service records become editable until invoiced: keep the notes and next-service date as fields
    # (they were only inside the detail text) and who edited last. Older records are filled in from
    # their detail text, which itself is left as it was.
    for column in ('notes', 'next_service_date', 'updated_by', 'updated_at'):
        _add_column(conn, 'generator_history', column, 'TEXT')
    rows = conn.execute("SELECT id, detail FROM generator_history WHERE event='Serviced' AND notes IS NULL").fetchall()
    conn.executemany('UPDATE generator_history SET notes=?, next_service_date=? WHERE id=?',
                     [(*_parse_service_detail(r['detail']), r['id']) for r in rows])


def _migration_12(conn):
    # v0.10.0: the service checklist and offline-safe saves.
    # - checklist_versions: the checklist definition, one immutable row per version (triggers refuse
    #   UPDATE and DELETE), so a record answered against version N is judged by version N forever.
    # - generator_history: the answers (JSON), the version they answer, the "Needs follow-up" flag and
    #   reasons, and who marked the follow-up handled. Older records: no checklist, no flag.
    # - idempotency_keys: the client-generated id of each save, so a retry never saves twice.
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS checklist_versions (
            id INTEGER PRIMARY KEY,
            items TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT,
            note TEXT
        );
        CREATE TRIGGER IF NOT EXISTS checklist_versions_no_update BEFORE UPDATE ON checklist_versions
        BEGIN SELECT RAISE(ABORT, 'checklist versions are immutable; add a new version'); END;
        CREATE TRIGGER IF NOT EXISTS checklist_versions_no_delete BEFORE DELETE ON checklist_versions
        BEGIN SELECT RAISE(ABORT, 'checklist versions are immutable; add a new version'); END;
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            key TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            entity_id INTEGER,
            created_at TEXT NOT NULL
        );
    ''')
    for column, decl in (('checklist_version_id', 'INTEGER REFERENCES checklist_versions(id)'),
                         ('checklist_answers', 'TEXT'),
                         ('needs_follow_up', 'INTEGER NOT NULL DEFAULT 0'),
                         ('follow_up_reasons', 'TEXT'),
                         ('follow_up_handled_by', 'TEXT'),
                         ('follow_up_handled_at', 'TEXT'),
                         ('follow_up_note', 'TEXT')):
        _add_column(conn, 'generator_history', column, decl)
    if not conn.execute('SELECT 1 FROM checklist_versions WHERE id=1').fetchone():
        conn.execute('INSERT INTO checklist_versions (id, items, created_at, created_by, note) VALUES (1,?,?,?,?)',
                     (json.dumps(checklist.VERSION_1), _timestamp(), 'migration 12',
                      'Starting list. Ranges are provisional until the team confirms them.'))


MIGRATIONS = [_migration_1, _migration_2, _migration_3, _migration_4, _migration_5, _migration_6, _migration_7,
              _migration_8, _migration_9, _migration_10, _migration_11, _migration_12]


def _migrate(conn):
    version = conn.execute('PRAGMA user_version').fetchone()[0]
    for number, migration in enumerate(MIGRATIONS[version:], start=version + 1):
        migration(conn)
        conn.execute(f'PRAGMA user_version = {number}')
        conn.commit()


def seed(conn):
    customers = [
        dict(name='Marlene Ouellette', email='marlene.o@example.com', phone='(207) 555-0142',
             physical_address='18 Birch Hollow Rd\nCape Elizabeth, ME 04107',
             billing_address='18 Birch Hollow Rd\nCape Elizabeth, ME 04107',
             notes='Prefers morning appointments. Dog on property (friendly).'),
        dict(name='Dennis & Karen Roy', email='droy@example.com', phone='(207) 555-0198',
             physical_address='402 Tidewater Ln\nFalmouth, ME 04105',
             billing_address='PO Box 220\nFalmouth, ME 04105',
             notes=''),
        dict(name='Harborview Property Mgmt', email='service@harborviewpm.example', phone='(207) 555-0177',
             physical_address='9 Ocean Ave, Unit 3\nPortland, ME 04101',
             billing_address='1100 Commercial St, Suite 200\nPortland, ME 04101',
             notes='Manages several rental properties — confirm unit number when scheduling.'),
    ]
    cust_ids = []
    for c in customers:
        cur = conn.execute(
            'INSERT INTO customers (name,email,phone,physical_address,billing_address,notes) VALUES (?,?,?,?,?,?)',
            (c['name'], c['email'], c['phone'], c['physical_address'], c['billing_address'], c['notes'])
        )
        cust_ids.append(cur.lastrowid)

    generators = [
        dict(customer_id=cust_ids[0], make='Generac', model='Guardian 22kW', serial='GEN-884213-A',
             physical_address=customers[0]['physical_address'],
             install_date='2021-05-14', last_service_date='2025-04-02', installer_name='Coastal Power Solutions',
             notes='Runs on propane. 500 gal tank buried east side of house.',
             history=[('2021-05-14', 'Installed', 'Installed for Marlene Ouellette by Coastal Power Solutions.')]),
        dict(customer_id=cust_ids[1], make='Kohler', model='14RESAL', serial='KOH-551029-C',
             physical_address=customers[1]['physical_address'],
             install_date='2019-09-30', last_service_date='2024-02-11', installer_name='Downeast Generator Co.',
             notes='',
             history=[('2019-09-30', 'Installed', 'Installed for Dennis & Karen Roy by Downeast Generator Co.')]),
        dict(customer_id=cust_ids[2], make='Generac', model='Protector 48kW', serial='GEN-702255-B',
             physical_address=customers[2]['physical_address'],
             install_date='2022-01-20', last_service_date='2026-06-01', installer_name='Coastal Power Solutions',
             notes='Commercial unit, natural gas fed. Access via rear alley gate.',
             history=[('2022-01-20', 'Installed', 'Installed for Harborview Property Mgmt by Coastal Power Solutions.')]),
    ]
    for g in generators:
        cur = conn.execute(
            '''INSERT INTO generators
               (customer_id,make,model,serial,physical_address,install_date,last_service_date,installer_name,notes)
               VALUES (?,?,?,?,?,?,?,?,?)''',
            (g['customer_id'], g['make'], g['model'], g['serial'], g['physical_address'], g['install_date'],
             g['last_service_date'], g['installer_name'], g['notes'])
        )
        gen_id = cur.lastrowid
        for date, event, detail in g['history']:
            conn.execute(
                'INSERT INTO generator_history (generator_id,date,event,detail) VALUES (?,?,?,?)',
                (gen_id, date, event, detail)
            )
    conn.commit()


# ---------------------------------------------------------------------- auth --

PBKDF2_ITERATIONS = 260000
SESSION_COOKIE = 'session_token'
SESSION_TTL_DAYS = 30
# Failed sign-ins: per connection, and per account from any connection (so spreading guesses over
# many addresses doesn't help). Someone who knows a username can lock that account for up to an hour
# on purpose; restarting the service clears every count.
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_ACCOUNT_MAX_FAILURES = 20
LOGIN_ACCOUNT_WINDOW_SECONDS = 60 * 60

LOGIN_FAILURES = SlidingWindow(LOGIN_WINDOW_SECONDS)                  # key: rate_limit_key of the client
LOGIN_ACCOUNT_FAILURES = SlidingWindow(LOGIN_ACCOUNT_WINDOW_SECONDS)  # key: username, lowercased

# Write actions a role may take; reads are open to every signed-in user.
# To lock technicians down further, remove permissions from their set.
PERMISSIONS = {
    'customers.edit': 'add or edit customers',
    'customers.delete': 'delete customers',
    'customers.view_email': "see customers' email addresses",
    'generators.edit': 'add or edit generators',
    'generators.delete': 'remove generators',
    'generators.transfer': 'transfer generators',
    'service.log': 'log service records',
    'service.invoice': 'mark service records as invoiced',
    'service.edit_any': "edit other people's service records",
    'service.follow_up': 'mark service follow-ups as handled',
    'attachments.upload': 'upload files',
    'attachments.delete': 'delete files',
    'account.edit': 'change your account settings',
    'notifications.receive': 'receive email notifications',
    'requests.manage': 'view and handle appointment requests',
}
ROLES = {
    'admin': set(PERMISSIONS),
    'technician': set(PERMISSIONS) - {'service.invoice', 'service.edit_any', 'notifications.receive', 'requests.manage',
                                      'customers.view_email', 'service.follow_up'},
}
ROLE_LABELS = {'admin': 'Admin', 'technician': 'Technician'}


def user_json(user):
    return {'username': user['username'], 'role': user['role'], 'permissions': sorted(user['permissions'])}


def _user_from_row(row):
    role = row['role']
    return {'id': row['id'], 'username': row['username'], 'role': role,
            'permissions': ROLES.get(role, set())}  # unknown role → no write access


def require_permission(user, permission):
    if permission and permission not in user['permissions']:
        raise ApiError(403, f"You don't have permission to {PERMISSIONS[permission]}. Ask an admin.")


def hash_password(password):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PBKDF2_ITERATIONS)
    return salt.hex(), digest.hex()


def verify_password(password, salt_hex, hash_hex):
    salt = bytes.fromhex(salt_hex)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PBKDF2_ITERATIONS)
    return secrets.compare_digest(digest.hex(), hash_hex)


def create_session(conn, user_id):
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at) VALUES (?,?,datetime('now'))",
        (token, user_id)
    )
    conn.commit()
    return token


def purge_expired_sessions(conn):
    conn.execute("DELETE FROM sessions WHERE created_at < datetime('now', ?)", (f'-{SESSION_TTL_DAYS} days',))
    conn.commit()


def _session_token_from_headers(headers):
    raw = headers.get('Cookie')
    if not raw:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(raw)
    except CookieError:
        return None
    morsel = cookie.get(SESSION_COOKIE)
    return morsel.value if morsel else None


def get_session_user(conn, headers):
    token = _session_token_from_headers(headers)
    if not token:
        return None
    row = conn.execute(
        '''SELECT u.id, u.username, u.role FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token = ? AND s.created_at >= datetime('now', ?)''',
        (token, f'-{SESSION_TTL_DAYS} days')
    ).fetchone()
    return _user_from_row(row) if row else None


# ------------------------------------------------------------- attachments --

def _is_jpeg(data):
    return data[:3] == b'\xff\xd8\xff'


def _is_png(data):
    return data[:8] == b'\x89PNG\r\n\x1a\n'


def _is_gif(data):
    return data[:4] == b'GIF8'


def _is_webp(data):
    return data[:4] == b'RIFF' and data[8:12] == b'WEBP'


def _is_pdf(data):
    return data[:5] == b'%PDF-'


def _isobmff_brand(data):
    if len(data) < 12 or data[4:8] != b'ftyp':
        return None
    return data[8:12]


def _is_heic(data):
    return _isobmff_brand(data) in (b'heic', b'heix', b'hevc', b'hevx', b'mif1', b'msf1', b'heim', b'heis')


def _is_mp4_or_mov(data):
    return _isobmff_brand(data) in (b'isom', b'iso2', b'mp41', b'mp42', b'M4V ', b'M4A ', b'qt  ', b'avc1', b'3gp4', b'3gp5')


def _is_webm(data):
    return data[:4] == b'\x1a\x45\xdf\xa3'


# extension -> (kind, content-type we serve it as, magic-byte check).
# The browser's declared Content-Type is ignored: it's unreliable for HEIC/MOV, and the magic bytes are the real check.
ALLOWED_TYPES = {
    '.jpg': ('image', 'image/jpeg', _is_jpeg),
    '.jpeg': ('image', 'image/jpeg', _is_jpeg),
    '.png': ('image', 'image/png', _is_png),
    '.gif': ('image', 'image/gif', _is_gif),
    '.webp': ('image', 'image/webp', _is_webp),
    '.heic': ('image', 'image/heic', _is_heic),
    '.pdf': ('pdf', 'application/pdf', _is_pdf),
    '.mp4': ('video', 'video/mp4', _is_mp4_or_mov),
    '.mov': ('video', 'video/quicktime', _is_mp4_or_mov),
    '.webm': ('video', 'video/webm', _is_webm),
}
MAGIC_BYTES_NEEDED = 16
MAX_PART_HEADER_BYTES = 16 * 1024


class _BodyReader:
    """Buffers a request body read in chunks, never past Content-Length."""

    def __init__(self, rfile, length):
        self.rfile = rfile
        self.remaining = length
        self.buf = b''

    def fill(self):
        if self.remaining <= 0:
            return False
        chunk = self.rfile.read(min(STREAM_CHUNK, self.remaining))
        if not chunk:
            self.remaining = 0
            return False
        self.remaining -= len(chunk)
        self.buf += chunk
        return True

    def ensure(self, n):
        while len(self.buf) < n:
            if not self.fill():
                raise ApiError(400, 'Malformed upload.')

    def drain(self):
        try:
            while self.remaining > 0:
                chunk = self.rfile.read(min(STREAM_CHUNK, self.remaining))
                if not chunk:
                    break
                self.remaining -= len(chunk)
        except OSError:
            pass  # timed out or reset: nothing more to read, and the original error is what matters


def _multipart_boundary(content_type_header):
    if not content_type_header or 'multipart/form-data' not in content_type_header:
        raise ApiError(400, 'Expected multipart/form-data.')
    for piece in content_type_header.split(';'):
        piece = piece.strip()
        if piece.startswith('boundary='):
            boundary = piece[len('boundary='):].strip('"')
            if boundary:
                return boundary.encode('latin-1')
    raise ApiError(400, 'Missing multipart boundary.')


def _copy_until(reader, delimiter, out):
    """Moves bytes up to (not including) the next delimiter into `out`; leaves the delimiter in the buffer."""
    size = 0
    head = b''
    while True:
        idx = reader.buf.find(delimiter)
        if idx >= 0:
            data, reader.buf = reader.buf[:idx], reader.buf[idx:]
        else:
            # Keep enough of the tail that a delimiter split across two reads is still found.
            cut = max(len(reader.buf) - (len(delimiter) - 1), 0)
            data, reader.buf = reader.buf[:cut], reader.buf[cut:]
        if data:
            size += len(data)
            if len(head) < MAGIC_BYTES_NEEDED:
                head += data[:MAGIC_BYTES_NEEDED - len(head)]
            if out:
                out.write(data)
        if idx >= 0:
            return size, head
        if not reader.fill():
            raise ApiError(400, 'The upload was cut off before it finished.')


def _read_part_headers(reader):
    while True:
        idx = reader.buf.find(b'\r\n\r\n')
        if idx >= 0:
            headers_text = reader.buf[:idx].decode('utf-8', errors='replace')
            reader.buf = reader.buf[idx + 4:]
            return headers_text
        if len(reader.buf) > MAX_PART_HEADER_BYTES or not reader.fill():
            raise ApiError(400, 'Malformed upload.')


def receive_upload(rfile, length, content_type_header, dest_path):
    """Streams the first file field of a multipart body to dest_path without holding it in memory."""
    delimiter = b'\r\n--' + _multipart_boundary(content_type_header)
    reader = _BodyReader(rfile, length)
    reader.buf = b'\r\n'  # lets the opening boundary match the same delimiter as the rest
    result = None
    try:
        _copy_until(reader, delimiter, None)  # preamble
        while True:
            reader.buf = reader.buf[len(delimiter):]
            reader.ensure(2)
            if reader.buf[:2] == b'--':
                break
            headers_text = _read_part_headers(reader)
            filename = re.search(r'filename="([^"\r\n]*)"', headers_text, re.IGNORECASE)
            if filename and filename.group(1) and result is None:
                with open(dest_path, 'wb') as out:
                    size, head = _copy_until(reader, delimiter, out)
                result = {'filename': filename.group(1), 'size': size, 'head': head}
            else:
                _copy_until(reader, delimiter, None)
    finally:
        reader.drain()
    require(result, 400, 'No file provided.')
    return result


def clean_filename(name):
    name = name.replace('\\', '/').rsplit('/', 1)[-1]
    name = ''.join(ch for ch in name if unicodedata.category(ch) not in ('Cc', 'Cf', 'Zl', 'Zp')).strip()
    stem, ext = os.path.splitext(name)
    return (stem[:150] + ext[:10]) or 'upload'


def validate_upload(filename, head):
    ext = os.path.splitext(filename)[1].lower()
    require(ext in ALLOWED_TYPES, 400, f'Unsupported file type: {ext or "unknown"}.')
    kind, content_type, magic_check = ALLOWED_TYPES[ext]
    require(magic_check(head), 400, 'File content does not match its file type.')
    return kind, ext, content_type


def content_disposition(filename):
    ascii_name = ''.join(ch for ch in filename if 32 <= ord(ch) < 127 and ch not in '"\\') or 'download'
    return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


RANGE_RE = re.compile(r'^bytes=(\d*)-(\d*)$')


def parse_range(header, size):
    """(start, end) for a single byte range, None to serve the whole file, False if unsatisfiable."""
    m = RANGE_RE.match((header or '').strip())
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if m.group(1):
        start = int(m.group(1))
        end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
    else:
        start = max(size - int(m.group(2)), 0)
        end = size - 1
    if start >= size or start > end:
        return False
    return start, end


def _remove_files(stored_filenames):
    for name in stored_filenames:
        try:
            os.remove(os.path.join(UPLOADS_DIR, name))
        except OSError:
            pass


def _remove_incomplete_uploads():
    _remove_files(n for n in os.listdir(UPLOADS_DIR) if n.startswith('.incoming-'))


# ------------------------------------------------------------ serialization --

def customer_row_to_json(row, show_email=True):
    """show_email=False (technicians) sends a masked address instead, with emailHidden so the page
    knows not to offer it for editing."""
    hidden = {} if show_email else {'emailHidden': True}
    return {
        **hidden,
        'id': row['id'],
        'name': row['name'],
        'email': (row['email'] or '') if show_email else mask_email(row['email']),
        'phone': row['phone'] or '',
        'physicalAddress': row['physical_address'] or '',
        'billingAddress': row['billing_address'] or '',
        'notes': row['notes'] or '',
        'createdBy': row['created_by'] or '',
        'updatedBy': row['updated_by'] or '',
        'updatedAt': row['updated_at'] or '',
    }


def _checklist_json(h):
    """A service record's checklist fields (other events get none). Answers only: the page joins them with
    the definitions from /api/checklist-versions, which keeps /api/generators small on a weak signal."""
    if h['event'] != 'Serviced':
        return {}
    return {'checklistVersion': h['checklist_version_id'],
            'answers': json.loads(h['checklist_answers']) if h['checklist_answers'] else None,
            'needsFollowUp': bool(h['needs_follow_up']),
            'followUpReasons': json.loads(h['follow_up_reasons']) if h['follow_up_reasons'] else [],
            'followUpHandledBy': h['follow_up_handled_by'] or '', 'followUpHandledAt': h['follow_up_handled_at'] or '',
            'followUpNote': h['follow_up_note'] or ''}


def generator_row_to_json(row, history_rows, attachment_rows=()):
    return {
        'id': row['id'],
        'customerId': row['customer_id'],
        'make': row['make'],
        'model': row['model'],
        'serial': row['serial'],
        'physicalAddress': row['physical_address'] or '',
        'installDate': row['install_date'] or '',
        'lastServiceDate': row['last_service_date'] or '',
        'nextServiceDate': row['next_service_date'] or '',
        'installerName': row['installer_name'] or '',
        'notes': row['notes'] or '',
        'createdBy': row['created_by'] or '',
        'updatedBy': row['updated_by'] or '',
        'updatedAt': row['updated_at'] or '',
        'history': [
            {'id': h['id'], 'date': h['date'] or '', 'event': h['event'], 'detail': h['detail'], 'by': h['created_by'] or '',
             'invoiced': bool(h['invoiced']), 'invoicedBy': h['invoiced_by'] or '', 'invoicedAt': h['invoiced_at'] or '',
             'notes': h['notes'] or '', 'nextServiceDate': h['next_service_date'] or '',
             'updatedBy': h['updated_by'] or '', 'updatedAt': h['updated_at'] or '', **_checklist_json(h)}
            for h in history_rows
        ],
        'attachments': [
            {'id': a['id'], 'filename': a['original_filename'], 'contentType': a['content_type'],
             'kind': a['kind'], 'sizeBytes': a['size_bytes'], 'uploadedBy': a['uploaded_by'] or '',
             'uploadedAt': a['uploaded_at'], 'url': f"/api/attachments/{a['id']}/file"}
            for a in attachment_rows
        ],
    }


def fetch_generator(conn, gen_id):
    row = conn.execute('SELECT * FROM generators WHERE id=?', (gen_id,)).fetchone()
    if not row:
        return None
    hist = conn.execute(
        'SELECT * FROM generator_history WHERE generator_id=? ORDER BY id ASC', (gen_id,)
    ).fetchall()
    attachments = conn.execute(
        'SELECT * FROM generator_attachments WHERE generator_id=? ORDER BY id ASC', (gen_id,)
    ).fetchall()
    return generator_row_to_json(row, hist, attachments)


# --------------------------------------------------------- input validation --
# The shared validators (text_field, date_field, id_field, check_not_stale) live in core.py.

def check_next_after(earlier, next_date, earlier_label):
    # ISO dates compare correctly as strings.
    if earlier and next_date:
        require(next_date > earlier, 400, f'Next service date must be after the {earlier_label}.')


EARLIEST_SERVICE_DATE = '2000-01-01'
MAX_NEXT_SERVICE_YEARS = 5


def _years_after(iso, years):
    d = datetime.date.fromisoformat(iso)
    try:
        return d.replace(year=d.year + years).isoformat()
    except ValueError:  # Feb 29 -> Feb 28
        return d.replace(year=d.year + years, day=28).isoformat()


def check_service_dates(service_date, next_date, service_label='service date'):
    """Sanity limits that catch typos (2207 for 2027) and pre-dated visits: a service date can't be in
    the future (Maine today) or before 2000, and the next service is after it and within 5 years of
    it (or of today, when there's no service date)."""
    if service_date:
        require(service_date >= EARLIEST_SERVICE_DATE, 400, f'The {service_label} can\'t be before 2000.')
        require(service_date <= _today(), 400, f'The {service_label} can\'t be in the future.')
    check_next_after(service_date, next_date, service_label)
    if next_date:
        base = service_date or _today()
        require(next_date <= _years_after(base, MAX_NEXT_SERVICE_YEARS), 400,
                f'Next service date must be within {MAX_NEXT_SERVICE_YEARS} years of the '
                f'{service_label if service_date else "current date"}.')
        require(next_date >= EARLIEST_SERVICE_DATE, 400, 'Next service date can\'t be before 2000.')


# ------------------------------------------------------------------ routes --

def _sees_emails(user):
    return 'customers.view_email' in user['permissions']


def list_customers(conn, user):
    rows = conn.execute('SELECT * FROM customers ORDER BY name COLLATE NOCASE ASC').fetchall()
    return [customer_row_to_json(r, _sees_emails(user)) for r in rows]


def get_customer(conn, cust_id, user):
    """The customer as this user may see it (emails masked without customers.view_email)."""
    row = conn.execute('SELECT * FROM customers WHERE id=?', (cust_id,)).fetchone()
    require(row, 404, 'Customer not found.')
    return customer_row_to_json(row, _sees_emails(user))


def _customer_values(body):
    name = text_field(body, 'name', 'Name')
    require(name, 400, 'Name is required.')
    return (name, text_field(body, 'email', 'Email'), text_field(body, 'phone', 'Phone'),
            text_field(body, 'physicalAddress', 'Physical address', LONG_TEXT),
            text_field(body, 'billingAddress', 'Billing address', LONG_TEXT),
            text_field(body, 'notes', 'Notes', LONG_TEXT))


def _insert_customer(conn, body, user):
    """Validates and inserts a customer without committing; returns its id."""
    return conn.execute(
        '''INSERT INTO customers (name,email,phone,physical_address,billing_address,notes,created_by,updated_by,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)''',
        _customer_values(body) + (user['username'], user['username'], _timestamp())
    ).lastrowid


def create_customer(conn, body, user):
    cust_id = _insert_customer(conn, body, user)
    conn.commit()
    return get_customer(conn, cust_id, user)


def create_customer_from_request(conn, req_id, body, user):
    """Adds a customer from an appointment request and links the request to them, in one transaction."""
    require_permission(user, 'customers.edit')
    row = appointment_requests.row_for_linking(conn, req_id, body)
    cust_id = _insert_customer(conn, body, user)
    request = appointment_requests.link(conn, row, user, cust_id, None, 'create_customer', f'customer {cust_id}')
    return {'request': request, 'customer': get_customer(conn, cust_id, user)}


def update_customer(conn, cust_id, body, user):
    write_lock(conn)
    row = conn.execute('SELECT * FROM customers WHERE id=?', (cust_id,)).fetchone()
    require(row, 404, 'Customer not found.')
    check_not_stale(row, body, 'customer')
    values = _customer_values(body)
    if not _sees_emails(user):
        # They're shown a masked address and can't change it: keep what's stored, whatever was sent.
        values = (values[0], row['email'] or '') + values[2:]
    conn.execute(
        '''UPDATE customers SET name=?, email=?, phone=?, physical_address=?, billing_address=?, notes=?,
           updated_by=?, updated_at=? WHERE id=?''',
        values + (user['username'], _timestamp(), cust_id)
    )
    conn.commit()
    return get_customer(conn, cust_id, user)


def delete_customer(conn, cust_id, user):
    row = conn.execute('SELECT * FROM customers WHERE id=?', (cust_id,)).fetchone()
    require(row, 404, 'Customer not found.')
    gen_count = conn.execute('SELECT COUNT(*) FROM generators WHERE customer_id=?', (cust_id,)).fetchone()[0]
    if gen_count > 0:
        raise ApiError(409, f'This customer has {gen_count} generator(s) on file. '
                             f'Transfer or remove them before deleting this customer.')
    conn.execute('DELETE FROM customers WHERE id=?', (cust_id,))
    audit(conn, user, 'delete', 'customer', cust_id, json.dumps(customer_row_to_json(row)))
    conn.commit()
    return {'ok': True}


def _group_by_generator(rows):
    grouped = {}
    for r in rows:
        grouped.setdefault(r['generator_id'], []).append(r)
    return grouped


def list_generators(conn):
    rows = conn.execute('SELECT * FROM generators ORDER BY id ASC').fetchall()
    history = _group_by_generator(conn.execute('SELECT * FROM generator_history ORDER BY id ASC'))
    attachments = _group_by_generator(conn.execute('SELECT * FROM generator_attachments ORDER BY id ASC'))
    return [generator_row_to_json(r, history.get(r['id'], []), attachments.get(r['id'], [])) for r in rows]


def get_generator(conn, gen_id):
    g = fetch_generator(conn, gen_id)
    require(g, 404, 'Generator not found.')
    return g


def _generator_values(body):
    make = text_field(body, 'make', 'Make')
    model = text_field(body, 'model', 'Model')
    serial = text_field(body, 'serial', 'Serial')
    require(make and model and serial, 400, 'Make, model, and serial are required.')
    return dict(
        make=make, model=model, serial=serial,
        physical_address=text_field(body, 'physicalAddress', 'Site address', LONG_TEXT),
        install_date=date_field(body, 'installDate', 'Install date'),
        last_service_date=date_field(body, 'lastServiceDate', 'Last service date'),
        next_service_date=date_field(body, 'nextServiceDate', 'Next service date'),
        installer_name=text_field(body, 'installerName', 'Installer name'),
        notes=text_field(body, 'notes', 'Notes', LONG_TEXT),
    )


def add_history(conn, gen_id, date, event, detail, user, notes=None, next_service_date=None):
    return conn.execute(
        'INSERT INTO generator_history (generator_id,date,event,detail,created_by,created_at,notes,next_service_date) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (gen_id, date, event, detail, user['username'], _timestamp(), notes, next_service_date)
    ).lastrowid


def check_serial_unique(conn, make, serial, exclude_id=None):
    dup = conn.execute(
        'SELECT id FROM generators WHERE make=? COLLATE NOCASE AND serial=? COLLATE NOCASE AND id IS NOT ?',
        (make, serial, exclude_id)
    ).fetchone()
    require(not dup, 409, f'A {make} generator with serial {serial} is already on file.')


def create_generator(conn, body, user):
    v = _generator_values(body)
    customer_id = id_field(body, 'customerId', 'Make, model, serial, and customer are required.')
    cust = conn.execute('SELECT * FROM customers WHERE id=?', (customer_id,)).fetchone()
    require(cust, 400, 'Linked customer does not exist.')
    check_serial_unique(conn, v['make'], v['serial'])
    check_service_dates(v['last_service_date'], v['next_service_date'], 'last service date')
    cur = conn.execute(
        '''INSERT INTO generators
           (customer_id,make,model,serial,physical_address,install_date,last_service_date,next_service_date,
            installer_name,notes,created_by,updated_by,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (customer_id, v['make'], v['model'], v['serial'], v['physical_address'], v['install_date'],
         v['last_service_date'], v['next_service_date'], v['installer_name'], v['notes'],
         user['username'], user['username'], _timestamp())
    )
    gen_id = cur.lastrowid
    add_history(conn, gen_id, v['install_date'] or _today(), 'Added to registry', f"Linked to {cust['name']}.", user)
    conn.commit()
    return get_generator(conn, gen_id)


def update_generator(conn, gen_id, body, user):
    write_lock(conn)
    row = conn.execute('SELECT * FROM generators WHERE id=?', (gen_id,)).fetchone()
    require(row, 404, 'Generator not found.')
    check_not_stale(row, body, 'generator')
    v = _generator_values(body)
    if (v['make'].lower(), v['serial'].lower()) != (row['make'].lower(), row['serial'].lower()):
        check_serial_unique(conn, v['make'], v['serial'], exclude_id=gen_id)
    # Only when the dates change, so older records with bad dates can still have other fields edited.
    if (v['last_service_date'], v['next_service_date']) != (row['last_service_date'] or '', row['next_service_date'] or ''):
        check_service_dates(v['last_service_date'], v['next_service_date'], 'last service date')
    conn.execute(
        '''UPDATE generators SET make=?, model=?, serial=?, physical_address=?, install_date=?, last_service_date=?,
           next_service_date=?, installer_name=?, notes=?, updated_by=?, updated_at=? WHERE id=?''',
        (v['make'], v['model'], v['serial'], v['physical_address'], v['install_date'], v['last_service_date'],
         v['next_service_date'], v['installer_name'], v['notes'], user['username'], _timestamp(), gen_id)
    )
    changes = []
    for col, label in (('last_service_date', 'Last service'), ('next_service_date', 'Next service')):
        old = row[col] or ''
        if old != v[col]:
            changes.append(f'{label}: {old or "none"} → {v[col] or "none"}.')
    if changes:
        add_history(conn, gen_id, _today(), 'Service dates edited', ' '.join(changes), user)
    conn.commit()
    return get_generator(conn, gen_id)


def delete_generator(conn, gen_id, user):
    g = fetch_generator(conn, gen_id)
    require(g, 404, 'Generator not found.')
    stored = [r['stored_filename'] for r in conn.execute(
        'SELECT stored_filename FROM generator_attachments WHERE generator_id=?', (gen_id,))]
    conn.execute('DELETE FROM generator_attachments WHERE generator_id=?', (gen_id,))
    conn.execute('DELETE FROM generator_history WHERE generator_id=?', (gen_id,))
    conn.execute('DELETE FROM generators WHERE id=?', (gen_id,))
    audit(conn, user, 'delete', 'generator', gen_id, json.dumps(g))
    conn.commit()
    _remove_files(stored)
    return {'ok': True}


def transfer_generator(conn, gen_id, body, user):
    row = conn.execute('SELECT * FROM generators WHERE id=?', (gen_id,)).fetchone()
    require(row, 404, 'Generator not found.')
    new_customer_id = id_field(body, 'newCustomerId', 'A destination customer is required.')
    require(new_customer_id != row['customer_id'], 400, 'Generator is already linked to that customer.')
    from_cust = conn.execute('SELECT * FROM customers WHERE id=?', (row['customer_id'],)).fetchone()
    to_cust = conn.execute('SELECT * FROM customers WHERE id=?', (new_customer_id,)).fetchone()
    require(to_cust, 400, 'Destination customer does not exist.')
    note = text_field(body, 'note', 'Note', LONG_TEXT)
    detail = f"Transferred from {from_cust['name'] if from_cust else 'previous owner'} to {to_cust['name']}."
    if note:
        detail += f' Note: {note}'
    conn.execute('UPDATE generators SET customer_id=?, updated_by=?, updated_at=? WHERE id=?',
                 (new_customer_id, user['username'], _timestamp(), gen_id))
    add_history(conn, gen_id, _today(), 'Transferred', detail, user)
    conn.commit()
    return get_generator(conn, gen_id)


def _replayed_save(conn, body, user, action):
    """(key, entity id) for a save carrying a client-generated `clientId`. The entity id is set when that
    key was already used, i.e. this is a retry of a save that went through (its answer was lost on a
    weak signal): the caller then returns the current state instead of saving again. Call it after
    write_lock(), so two copies of the same retry can't both miss."""
    key = uuid_field(body, 'clientId', 'Client id')
    if not key:
        return '', None
    row = conn.execute('SELECT * FROM idempotency_keys WHERE key=?', (key,)).fetchone()
    if not row:
        return key, None
    require(row['username'].lower() == user['username'].lower() and row['action'] == action, 409,
            'This save was already used for something else. Reload the page and try again.')
    return key, row['entity_id']


def _remember_save(conn, key, user, action, entity_id):
    if key:
        conn.execute('INSERT INTO idempotency_keys (key, username, action, entity_id, created_at) VALUES (?,?,?,?,?)',
                     (key, user['username'], action, entity_id, _timestamp()))


def _replay_result(conn, hist_id):
    row = conn.execute("SELECT generator_id FROM generator_history WHERE id=? AND event='Serviced'", (hist_id,)).fetchone()
    require(row, 404, 'This service record was saved, but it has since been deleted.')
    return {**get_generator(conn, row['generator_id']), 'serviceRecordId': hist_id, 'replayed': True}


def _checklist_values(conn, body, version_id, service_date):
    """(version id, answers JSON, needs_follow_up, reasons JSON) to store for a record, from the request's
    `answers` judged by checklist version `version_id`. No answers: only allowed for a visit before today
    (entered from paper), and stored as no checklist."""
    raw = body.get('answers')
    if raw is None:
        require(service_date < _today(), 400, 'Fill in the checklist. Only a visit from before today can be '
                                              'saved without one (entered from paper).')
        return None, None, 0, None
    version = checklist.get_version(conn, version_id)
    require(version, 400, 'Unknown checklist version. Reload the page and try again.')
    answers = checklist.validate_answers(version, raw)
    flagged, reasons = checklist.evaluate(version, answers)
    return version['id'], json.dumps(answers), int(flagged), json.dumps(reasons)


def add_service_record(conn, gen_id, body, user):
    """Logs a visit. A repeat of the same `clientId` (a retry from a phone that lost the answer) returns the
    record already saved, with no second record and no second email."""
    write_lock(conn)
    key, replayed = _replayed_save(conn, body, user, 'service_record.create')
    if replayed:
        return _replay_result(conn, replayed)
    row = conn.execute('SELECT * FROM generators WHERE id=?', (gen_id,)).fetchone()
    require(row, 404, 'Generator not found.')
    service_date = date_field(body, 'serviceDate', 'Service date')
    next_service_date = date_field(body, 'nextServiceDate', 'Next service date')
    notes = text_field(body, 'notes', 'Notes', LONG_TEXT)
    require(service_date, 400, 'Service date is required.')
    check_service_dates(service_date, next_service_date)
    version_id = id_field(body, 'checklistVersion', 'Checklist version is required.') \
        if body.get('answers') is not None else None
    version_id, answers, flagged, reasons = _checklist_values(conn, body, version_id, service_date)
    detail = _service_detail(notes, next_service_date)
    # A backdated visit (catch-up data entry) is recorded in history but mustn't roll the schedule backwards.
    if not row['last_service_date'] or service_date >= row['last_service_date']:
        conn.execute(
            'UPDATE generators SET last_service_date=?, next_service_date=?, updated_by=?, updated_at=? WHERE id=?',
            (service_date, next_service_date, user['username'], _timestamp(), gen_id)
        )
    hist_id = add_history(conn, gen_id, service_date, 'Serviced', detail, user, notes, next_service_date)
    conn.execute('UPDATE generator_history SET checklist_version_id=?, checklist_answers=?, needs_follow_up=?, '
                 'follow_up_reasons=? WHERE id=?', (version_id, answers, flagged, reasons, hist_id))
    _remember_save(conn, key, user, 'service_record.create', hist_id)
    conn.commit()
    cust = conn.execute('SELECT * FROM customers WHERE id=?', (row['customer_id'],)).fetchone()
    version = checklist.get_version(conn, version_id) if version_id else None
    enqueue_email_job(notify_service_record, {
        'customer': cust['name'] if cust else 'Unknown customer', 'phone': cust['phone'] if cust else '',
        'site': (row['physical_address'] or '').replace('\n', ', '),
        'unit': f"{row['make']} {row['model']} (S/N {row['serial']})",
        'service_date': service_date, 'next_service_date': next_service_date, 'detail': detail,
        'logged_by': user['username'],
        'follow_up': json.loads(reasons) if flagged else [],
        'checklist': checklist.rows(version, json.loads(answers)) if version else None,
    })
    return {**get_generator(conn, gen_id), 'serviceRecordId': hist_id}


def _latest_visit(conn, gen_id):
    return conn.execute("SELECT * FROM generator_history WHERE generator_id=? AND event='Serviced' "
                        "ORDER BY date DESC, id DESC LIMIT 1", (gen_id,)).fetchone()


def _changeable_service_record(conn, hist_id, user, verb):
    """The service record, locked for writing, after checking it may still be edited or deleted:
    it exists, it's a visit, it's the user's own (or they have service.edit_any), and it isn't invoiced."""
    write_lock(conn)
    row = conn.execute('SELECT * FROM generator_history WHERE id=?', (hist_id,)).fetchone()
    require(row, 404, 'Service record not found.')
    require(row['event'] == 'Serviced', 400, f'Only service records can be {verb}.')
    action = {'edited': 'edit', 'deleted': 'delete'}[verb]
    require(row['created_by'] == user['username'] or 'service.edit_any' in user['permissions'], 403,
            f'You can only {action} service records you logged.')
    require(not row['invoiced'], 409,
            "This service record is invoiced, so it can't be changed. An admin can undo invoiced first.")
    return row


def _follow_latest_visit(conn, gen_id, before_latest, hist_id, user, reason):
    """After a visit was edited or deleted: if it was (or now is) the unit's latest, the generator's
    last/next service dates follow the latest visit (or are cleared when no visits are left). A
    last-service date set by hand to something newer than every visit is never overwritten."""
    after_latest = _latest_visit(conn, gen_id)
    if hist_id not in (before_latest['id'], after_latest['id'] if after_latest else None):
        return
    gen = conn.execute('SELECT * FROM generators WHERE id=?', (gen_id,)).fetchone()
    current = gen['last_service_date'] or ''
    new_last = after_latest['date'] if after_latest else ''
    new_next = (after_latest['next_service_date'] or '') if after_latest else ''
    if current and current != before_latest['date'] and current > new_last:
        return
    changes = [f'{label}: {old or "none"} → {new or "none"}.'
               for label, old, new in (('Last service', current, new_last),
                                       ('Next service', gen['next_service_date'] or '', new_next))
               if old != new]
    if changes:
        conn.execute('UPDATE generators SET last_service_date=?, next_service_date=?, updated_by=?, '
                     'updated_at=? WHERE id=?', (new_last, new_next, user['username'], _timestamp(), gen_id))
        add_history(conn, gen_id, _today(), 'Service dates edited', ' '.join(changes) + f' {reason}', user)


def update_service_record(conn, hist_id, body, user):
    """Changes a visit's date, next-service date and notes, until it's invoiced. The logger may edit their
    own records; service.edit_any (admins) may edit anyone's. When the visit is (or becomes) the unit's
    latest, the generator's last/next service dates follow it."""
    write_lock(conn)
    key, replayed = _replayed_save(conn, body, user, 'service_record.edit')
    if replayed:
        return _replay_result(conn, replayed)
    row = _changeable_service_record(conn, hist_id, user, 'edited')
    check_not_stale(row, body, 'service record')
    service_date = date_field(body, 'serviceDate', 'Service date')
    next_service_date = date_field(body, 'nextServiceDate', 'Next service date')
    notes = text_field(body, 'notes', 'Notes', LONG_TEXT)
    require(service_date, 400, 'Service date is required.')
    check_service_dates(service_date, next_service_date)

    # The checklist is judged by the record's own (frozen) version. Leaving `answers` out keeps them.
    version_id = row['checklist_version_id']
    answers, flagged, reasons = row['checklist_answers'], row['needs_follow_up'], row['follow_up_reasons']
    if body.get('answers') is not None:
        require(version_id, 400, 'This service record was saved without a checklist, so it has none to edit.')
        _, answers, flagged, reasons = _checklist_values(conn, body, version_id, service_date)
    handled = (row['follow_up_handled_by'], row['follow_up_handled_at'], row['follow_up_note'])
    if reasons != row['follow_up_reasons']:
        handled = (None, None, None)  # new or different flags reopen the follow-up

    gen_id = row['generator_id']
    before_latest = _latest_visit(conn, gen_id)
    old_notes, old_next = (row['notes'] or '', row['next_service_date'] or '')
    conn.execute('UPDATE generator_history SET date=?, notes=?, next_service_date=?, detail=?, updated_by=?, '
                 'updated_at=?, checklist_answers=?, needs_follow_up=?, follow_up_reasons=?, '
                 'follow_up_handled_by=?, follow_up_handled_at=?, follow_up_note=? WHERE id=?',
                 (service_date, notes, next_service_date, _service_detail(notes, next_service_date),
                  user['username'], _timestamp(), answers, flagged, reasons, *handled, hist_id))
    _follow_latest_visit(conn, gen_id, before_latest, hist_id, user, 'From an edited service record.')
    audit(conn, user, 'edit', 'service_record', hist_id, json.dumps({
        'before': {'date': row['date'], 'nextServiceDate': old_next, 'notes': old_notes,
                   'answers': json.loads(row['checklist_answers'] or 'null'), 'needsFollowUp': bool(row['needs_follow_up'])},
        'after': {'date': service_date, 'nextServiceDate': next_service_date, 'notes': notes,
                  'answers': json.loads(answers or 'null'), 'needsFollowUp': bool(flagged)}}))
    _remember_save(conn, key, user, 'service_record.edit', hist_id)
    conn.commit()
    return {**get_generator(conn, gen_id), 'serviceRecordId': hist_id}


def delete_service_record(conn, hist_id, user):
    """Removes a visit logged by mistake, until it's invoiced (same rules as editing). The audit log keeps
    a snapshot. If it was the unit's latest visit, the generator falls back to the previous one."""
    row = _changeable_service_record(conn, hist_id, user, 'deleted')
    gen_id = row['generator_id']
    before_latest = _latest_visit(conn, gen_id)
    conn.execute('DELETE FROM generator_history WHERE id=?', (hist_id,))
    _follow_latest_visit(conn, gen_id, before_latest, hist_id, user, 'From a deleted service record.')
    audit(conn, user, 'delete', 'service_record', hist_id, json.dumps({
        'generatorId': gen_id, 'date': row['date'], 'nextServiceDate': row['next_service_date'] or '',
        'notes': row['notes'] or '', 'detail': row['detail'], 'loggedBy': row['created_by'],
        'loggedAt': row['created_at']}))
    conn.commit()
    return get_generator(conn, gen_id)


def set_invoiced(conn, hist_id, body, user):
    write_lock(conn)
    row = conn.execute('SELECT * FROM generator_history WHERE id=?', (hist_id,)).fetchone()
    require(row, 404, 'Service record not found.')
    require(row['event'] == 'Serviced', 400, 'Only service records can be invoiced.')
    invoiced = body.get('invoiced')
    require(isinstance(invoiced, bool), 400, 'invoiced must be true or false.')
    invoiced_by = user['username'] if invoiced else None
    invoiced_at = _today() if invoiced else None
    conn.execute(
        'UPDATE generator_history SET invoiced=?, invoiced_by=?, invoiced_at=? WHERE id=?',
        (1 if invoiced else 0, invoiced_by, invoiced_at, hist_id)
    )
    if invoiced:
        audit(conn, user, 'invoice', 'service_record', hist_id)
    else:
        audit(conn, user, 'uninvoice', 'service_record', hist_id,
              f"Was invoiced by {row['invoiced_by'] or 'unknown'} on {row['invoiced_at'] or 'unknown'}.")
    conn.commit()
    return get_generator(conn, row['generator_id'])


def set_follow_up(conn, hist_id, body, user):
    """The office marks a flagged visit's follow-up handled (or reopens it). Allowed after invoicing too:
    it's office work, not a change to the tech's record, so it doesn't touch updated_at."""
    write_lock(conn)
    row = conn.execute('SELECT * FROM generator_history WHERE id=?', (hist_id,)).fetchone()
    require(row and row['event'] == 'Serviced', 404, 'Service record not found.')
    require(row['needs_follow_up'], 400, 'This service record has nothing to follow up.')
    handled = bool_field(body, 'handled', 'handled')
    note = text_field(body, 'note', 'Note', checklist.NOTE_MAX)
    conn.execute('UPDATE generator_history SET follow_up_handled_by=?, follow_up_handled_at=?, follow_up_note=? '
                 'WHERE id=?', (user['username'], _today(), note, hist_id) if handled else (None, None, None, hist_id))
    audit(conn, user, 'follow_up_handled' if handled else 'follow_up_reopened', 'service_record', hist_id, note)
    conn.commit()
    return get_generator(conn, row['generator_id'])


def delete_attachment(conn, att_id, user):
    row = conn.execute('SELECT * FROM generator_attachments WHERE id=?', (att_id,)).fetchone()
    require(row, 404, 'Attachment not found.')
    gen_id = row['generator_id']
    conn.execute('DELETE FROM generator_attachments WHERE id=?', (att_id,))
    audit(conn, user, 'delete', 'attachment', att_id, json.dumps(
        {'generatorId': gen_id, 'filename': row['original_filename'], 'uploadedBy': row['uploaded_by']}))
    conn.commit()
    _remove_files([row['stored_filename']])
    return get_generator(conn, gen_id)


def login(conn, body, client_key):
    require(LOGIN_FAILURES.count(client_key) < LOGIN_MAX_FAILURES, 429,
            'Too many failed sign-in attempts. Try again in 15 minutes.')
    username = text_field(body, 'username', 'Username')
    password = body.get('password')
    require(username and isinstance(password, str) and password, 400, 'Username and password are required.')
    # Counted for any username, real or not, so the answer never shows which accounts exist.
    account_key = username.lower()
    require(LOGIN_ACCOUNT_FAILURES.count(account_key) < LOGIN_ACCOUNT_MAX_FAILURES, 429,
            'Too many failed sign-in attempts for this account. Try again later.')
    row = conn.execute('SELECT * FROM users WHERE username=? COLLATE NOCASE', (username,)).fetchone()
    if row:
        ok = verify_password(password, row['password_salt'], row['password_hash'])
    else:
        verify_password(password, '00' * 16, '')  # same cost as a real check, so timing doesn't reveal usernames
        ok = False
    if not ok:
        LOGIN_FAILURES.hit(client_key)
        LOGIN_ACCOUNT_FAILURES.hit(account_key)
        raise ApiError(401, 'Incorrect username or password.')
    LOGIN_FAILURES.clear(client_key)
    LOGIN_ACCOUNT_FAILURES.clear(account_key)
    token = create_session(conn, row['id'])
    return user_json(_user_from_row(row)), token


def logout(conn, headers):
    token = _session_token_from_headers(headers)
    if token:
        conn.execute('DELETE FROM sessions WHERE token=?', (token,))
        conn.commit()
    return {'ok': True}


def whoami(conn, user):
    require(user, 401, 'Not authenticated.')
    out = user_json(user)
    row = conn.execute('SELECT email FROM users WHERE id=?', (user['id'],)).fetchone()
    out['email'] = row['email'] or ''
    out['notifications'] = sorted(r['kind'] for r in conn.execute(
        'SELECT kind FROM user_notifications WHERE user_id=?', (user['id'],)))
    out['notificationKinds'] = [{'kind': k, 'label': v} for k, v in notifications.NOTIFICATION_KINDS.items()
                                if _kind_offered(user['permissions'], k)]
    if 'notifications.receive' in user['permissions']:
        out['emailConfigured'] = notifications.email_configured()
    return out


def update_account(conn, body, user):
    email = text_field(body, 'email', 'Email', 254)
    require(not email or EMAIL_RE.match(email), 400, 'Enter a valid email address.')
    kinds = body.get('notifications')
    if kinds is not None:
        require(isinstance(kinds, list) and all(isinstance(k, str) for k in kinds), 400,
                'notifications must be a list.')
        unknown = sorted(set(kinds) - set(notifications.NOTIFICATION_KINDS))
        require(not unknown, 400, f"Unknown notification type: {', '.join(unknown)}.")
        if kinds:
            require_permission(user, 'notifications.receive')
        for kind in kinds:
            require_permission(user, notifications.KIND_PERMISSIONS.get(kind))
        require(email or not kinds, 400, 'Add an email address to get notifications.')
    conn.execute('UPDATE users SET email=? WHERE id=?', (email or None, user['id']))
    if kinds is not None:
        conn.execute('DELETE FROM user_notifications WHERE user_id=?', (user['id'],))
        conn.executemany('INSERT INTO user_notifications (user_id, kind) VALUES (?,?)',
                         [(user['id'], k) for k in sorted(set(kinds))])
    conn.commit()
    return whoami(conn, user)


def send_test_email(conn, user):
    row = conn.execute('SELECT email FROM users WHERE id=?', (user['id'],)).fetchone()
    require(row['email'], 400, 'Save an email address first.')
    require(notifications.email_configured(), 400,
            "Email isn't set up on the server yet (SMTP2GO_API_KEY and EMAIL_FROM).")
    subject, text, html_body = notifications.sample_email(user['username'], notifications.email_settings()['app_url'])
    error = deliver(conn, 'test', row['email'], subject, text, html_body)
    require(not error, 502, f'The test email could not be sent: {error}')
    return {'ok': True, 'sentTo': row['email']}


# ------------------------------------------------------------------ email --
# Sends happen off the request thread (a single worker) or on the scheduler thread, and never
# raise: every attempt is recorded in email_log instead.

SCHEDULED_EMAILS = {  # kind -> (weekday Mon=0, or None for every day, hour, minute), Maine time
    'weekly_digest': (4, 16, 0),
    'hit_list': (0, 7, 0),
    'request_digest': (None, 18, 0),
}
SCHEDULE_CATCH_UP = datetime.timedelta(hours=12)  # sent late after a restart, but not later than this
SCHEDULER_POLL_SECONDS = 60

_email_jobs = queue.Queue()


def enqueue_email_job(fn, *args):
    _email_jobs.put((fn, args))


def _run_email_job(fn, args):
    try:
        fn(*args)
    except Exception:
        traceback.print_exc()


def run_email_jobs():
    """Runs queued jobs on the calling thread until the queue is empty (tests use this; the worker blocks instead)."""
    while True:
        try:
            fn, args = _email_jobs.get_nowait()
        except queue.Empty:
            return
        _run_email_job(fn, args)


def _email_worker():
    while True:
        _run_email_job(*_email_jobs.get())


def deliver(conn, kind, recipient, subject, text, html_body):
    """Sends one email and logs the attempt. Returns None on success, else the error message."""
    status, error = 'sent', None
    try:
        notifications.send_email(recipient, subject, text, html_body)
    except notifications.EmailNotConfigured as e:
        status, error = 'skipped', str(e)
    except notifications.EmailError as e:
        status, error = 'failed', str(e)
        print(f'[email] {kind} to {recipient} failed: {e}')
    conn.execute('INSERT INTO email_log (at, kind, recipient, subject, status, error) VALUES (?,?,?,?,?,?)',
                 (_timestamp(), kind, recipient, subject, status, error))
    conn.commit()
    return error


def notification_recipients(conn, kind):
    """Users who opted in to `kind`, have an email address, and whose role can receive email right now."""
    rows = conn.execute(
        '''SELECT u.username, u.email, u.role FROM users u JOIN user_notifications n ON n.user_id = u.id
           WHERE n.kind = ? AND u.email IS NOT NULL AND u.email != '' ORDER BY u.username''', (kind,)).fetchall()
    return [r for r in rows if _may_receive(ROLES.get(r['role'], set()), kind)]


def _kind_offered(permissions, kind):
    """Whether Account settings lists `kind` (greyed out for people who can't receive email at all)."""
    needed = notifications.KIND_PERMISSIONS.get(kind)
    return needed is None or needed in permissions


def _may_receive(permissions, kind):
    """Whether a user with `permissions` may get `kind` (some kinds need more than notifications.receive)."""
    return 'notifications.receive' in permissions and _kind_offered(permissions, kind)


def _send_to_subscribers(conn, kind, message):
    subject, text, html_body = message
    recipients = notification_recipients(conn, kind)
    for r in recipients:
        deliver(conn, kind, r['email'], subject, text, html_body)
    return len(recipients)


def notify_service_record(record):
    conn = get_conn()
    try:
        recipients = notification_recipients(conn, 'service_record')
        if recipients:
            _send_to_subscribers(conn, 'service_record', notifications.service_record_email(
                record, notifications.email_settings()['app_url']))
    finally:
        conn.close()


def notify_new_request(request_id):
    conn = get_conn()
    try:
        if notification_recipients(conn, 'request_new'):
            data = appointment_requests.get(conn, request_id)
            suggestion = data['candidates'][0] if data['candidates'] else None
            _send_to_subscribers(conn, 'request_new', notifications.request_email(
                data['request'], suggestion, notifications.email_settings()['app_url']))
    finally:
        conn.close()


def _localize(naive):
    return naive.replace(tzinfo=LOCAL_TZ)


def scheduled_time(kind, now):
    """The most recent scheduled send of `kind` at or before `now` (an aware Maine datetime)."""
    weekday, hour, minute = SCHEDULED_EMAILS[kind]
    period = 1 if weekday is None else 7
    day = now.date() if weekday is None else now.date() - datetime.timedelta(days=(now.weekday() - weekday) % 7)
    at = _localize(datetime.datetime.combine(day, datetime.time(hour, minute)))
    if at > now:  # today is the day but it's not time yet: the previous one
        at = _localize(datetime.datetime.combine(day - datetime.timedelta(days=period), datetime.time(hour, minute)))
    return at


def build_scheduled_email(conn, kind, at):
    """(subject, text, html) for the run due at `at`, or None when there's nothing worth sending."""
    app_url = notifications.email_settings()['app_url']
    if kind == 'weekly_digest':
        start = at - datetime.timedelta(days=7)
        activity = notifications.weekly_activity(conn, start.isoformat(timespec='microseconds'),
                                                 at.isoformat(timespec='microseconds'))
        return notifications.weekly_digest_email(activity, start.date(), at.date(), app_url)
    if kind == 'hit_list':
        overdue, due_soon = notifications.hit_list(conn, at.date())
        return notifications.hit_list_email(overdue, due_soon, at.date(), app_url=app_url)
    if kind == 'request_digest':
        start = _localize(datetime.datetime.combine(at.date() - datetime.timedelta(days=1), at.time()))
        received = notifications.requests_received(conn, start.isoformat(timespec='microseconds'),
                                                   at.isoformat(timespec='microseconds'),
                                                   appointment_requests.HONEYPOT_REASON)
        waiting = notifications.requests_waiting(conn)
        if not received and not waiting:
            return None  # nothing came in and nothing is waiting: no email
        return notifications.request_digest_email(received, waiting, start, at, appointment_requests.NEEDS, app_url)
    raise ValueError(f'Unknown scheduled email: {kind}')


def run_due_jobs(now=None):
    """Sends any scheduled email that's due and not yet sent for its period (week or day). Safe to call from several
    processes at once: the scheduled_runs primary key decides which one sends."""
    now = now or _local_now()
    sent = []
    for kind in SCHEDULED_EMAILS:
        at = scheduled_time(kind, now)
        if now >= at + SCHEDULE_CATCH_UP:
            continue
        conn = get_conn()
        try:
            claimed = conn.execute('INSERT OR IGNORE INTO scheduled_runs (kind, period, claimed_at) VALUES (?,?,?)',
                                   (kind, at.date().isoformat(), _timestamp())).rowcount
            conn.commit()
            if claimed:
                if notification_recipients(conn, kind):
                    message = build_scheduled_email(conn, kind, at)
                    if message:
                        _send_to_subscribers(conn, kind, message)
                sent.append(kind)
        finally:
            conn.close()
    return sent


GEOCODE_POLL_SECONDS = 120


def _geocode_loop():
    """Looks up new or edited addresses for the generator map, a couple of minutes after they appear."""
    while True:
        conn = None
        try:
            conn = get_conn()
            counts = geocoding.geocode_pending(conn)
            if counts:
                print(f'[geocode] {counts}', flush=True)
        except Exception:
            traceback.print_exc()
        finally:
            if conn is not None:
                conn.close()
        time.sleep(GEOCODE_POLL_SECONDS)


def _scheduler_loop():
    while True:
        try:
            run_due_jobs()
        except Exception:
            traceback.print_exc()
        time.sleep(SCHEDULER_POLL_SECONDS)


def start_background_threads():
    threading.Thread(target=_email_worker, name='email-worker', daemon=True).start()
    threading.Thread(target=_scheduler_loop, name='email-scheduler', daemon=True).start()
    threading.Thread(target=_geocode_loop, name='geocoder', daemon=True).start()


# ---------------------------------------------------------------- dispatch --

# (method, path, handler, permission required — None for reads)
ROUTES = [
    ('GET', re.compile(r'^/api/customers$'), lambda conn, m, body, user: list_customers(conn, user), None),
    ('POST', re.compile(r'^/api/customers$'), lambda conn, m, body, user: create_customer(conn, body, user), 'customers.edit'),
    ('GET', re.compile(r'^/api/customers/(\d+)$'), lambda conn, m, body, user: get_customer(conn, int(m.group(1)), user), None),
    ('PUT', re.compile(r'^/api/customers/(\d+)$'), lambda conn, m, body, user: update_customer(conn, int(m.group(1)), body, user), 'customers.edit'),
    ('DELETE', re.compile(r'^/api/customers/(\d+)$'), lambda conn, m, body, user: delete_customer(conn, int(m.group(1)), user), 'customers.delete'),

    ('GET', re.compile(r'^/api/generators$'), lambda conn, m, body, user: list_generators(conn), None),
    ('POST', re.compile(r'^/api/generators$'), lambda conn, m, body, user: create_generator(conn, body, user), 'generators.edit'),
    ('GET', re.compile(r'^/api/generator-map$'), lambda conn, m, body, user: geocoding.generator_map(conn), None),
    ('GET', re.compile(r'^/api/generators/(\d+)$'), lambda conn, m, body, user: get_generator(conn, int(m.group(1))), None),
    ('PUT', re.compile(r'^/api/generators/(\d+)$'), lambda conn, m, body, user: update_generator(conn, int(m.group(1)), body, user), 'generators.edit'),
    ('DELETE', re.compile(r'^/api/generators/(\d+)$'), lambda conn, m, body, user: delete_generator(conn, int(m.group(1)), user), 'generators.delete'),
    ('POST', re.compile(r'^/api/generators/(\d+)/transfer$'), lambda conn, m, body, user: transfer_generator(conn, int(m.group(1)), body, user), 'generators.transfer'),
    ('POST', re.compile(r'^/api/generators/(\d+)/service-records$'), lambda conn, m, body, user: add_service_record(conn, int(m.group(1)), body, user), 'service.log'),
    ('PUT', re.compile(r'^/api/service-records/(\d+)$'), lambda conn, m, body, user: update_service_record(conn, int(m.group(1)), body, user), 'service.log'),
    ('DELETE', re.compile(r'^/api/service-records/(\d+)$'), lambda conn, m, body, user: delete_service_record(conn, int(m.group(1)), user), 'service.log'),
    ('POST', re.compile(r'^/api/service-records/(\d+)/invoice$'), lambda conn, m, body, user: set_invoiced(conn, int(m.group(1)), body, user), 'service.invoice'),
    ('POST', re.compile(r'^/api/service-records/(\d+)/follow-up$'), lambda conn, m, body, user: set_follow_up(conn, int(m.group(1)), body, user), 'service.follow_up'),
    ('GET', re.compile(r'^/api/checklist-versions$'), lambda conn, m, body, user: checklist.list_versions(conn), None),
    ('DELETE', re.compile(r'^/api/attachments/(\d+)$'), lambda conn, m, body, user: delete_attachment(conn, int(m.group(1)), user), 'attachments.delete'),

    # Admin only, reads included: requests hold the public's contact details and match results.
    ('GET', re.compile(r'^/api/appointment-requests$'), lambda conn, m, body, user: appointment_requests.list_all(conn), 'requests.manage'),
    ('GET', re.compile(r'^/api/appointment-requests/(\d+)$'), lambda conn, m, body, user: appointment_requests.get(conn, int(m.group(1))), 'requests.manage'),
    ('POST', re.compile(r'^/api/appointment-requests/(\d+)/match$'), lambda conn, m, body, user: appointment_requests.match(conn, int(m.group(1)), body, user), 'requests.manage'),
    ('POST', re.compile(r'^/api/appointment-requests/(\d+)/create-customer$'), lambda conn, m, body, user: create_customer_from_request(conn, int(m.group(1)), body, user), 'requests.manage'),
    ('POST', re.compile(r'^/api/appointment-requests/(\d+)/status$'), lambda conn, m, body, user: appointment_requests.set_status(conn, int(m.group(1)), body, user), 'requests.manage'),

    ('PUT', re.compile(r'^/api/account$'), lambda conn, m, body, user: update_account(conn, body, user), 'account.edit'),
    ('POST', re.compile(r'^/api/account/test-email$'), lambda conn, m, body, user: send_test_email(conn, user), 'notifications.receive'),
]

# Routes that stream request/response bodies themselves instead of returning JSON-able results.
UPLOAD_ROUTE = re.compile(r'^/api/generators/(\d+)/attachments$')
FILE_ROUTE = re.compile(r'^/api/attachments/(\d+)/file$')

SECURITY_HEADERS = [
    ('X-Content-Type-Options', 'nosniff'),
    ('X-Frame-Options', 'DENY'),
    ('Referrer-Policy', 'same-origin'),
]
# Inline scripts/handlers are how the page is built, so 'unsafe-inline' stays; the rest still limits
# where an injected script could load from or send data to.
HTML_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
            "img-src 'self' data: blob: https://tile.openstreetmap.org; media-src 'self'; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
# The public request page: no inline scripts at all (customer-request.js does everything), and
# Cloudflare only where Turnstile needs it (its script and its iframe).
PORTAL_CSP = ("default-src 'self'; script-src 'self' https://challenges.cloudflare.com; "
              "frame-src https://challenges.cloudflare.com; style-src 'self' 'unsafe-inline'; "
              "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
              "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


HTML = 'text/html; charset=utf-8'


def app_replacements():
    return {'{{APP_VERSION}}': APP_VERSION}


def portal_replacements():
    return appointment_requests.page_replacements(APP_VERSION)


# Everything served from a file: path -> (file, content type, CSP, placeholder values or None).
PAGES = {
    '/': (STATIC_FILE, HTML, HTML_CSP, app_replacements),
    '/generator-manager.html': (STATIC_FILE, HTML, HTML_CSP, app_replacements),
    '/customer': (PORTAL_FILE, HTML, PORTAL_CSP, portal_replacements),
    '/customer/': (PORTAL_FILE, HTML, PORTAL_CSP, portal_replacements),
    # Where the form goes after a successful submit. It shows no request data, so anyone may open it.
    '/customer/thanks': (PORTAL_THANKS_FILE, HTML, PORTAL_CSP, portal_replacements),
    '/customer-request.js': (PORTAL_SCRIPT_FILE, 'text/javascript; charset=utf-8', None, None),
    '/portal.css': (PORTAL_CSS_FILE, 'text/css; charset=utf-8', None, None),
    # One icon for every page. Browsers also ask for /favicon.ico on their own; a PNG there is fine.
    '/favicon.png': (FAVICON_FILE, 'image/png', None, None),
    '/favicon.ico': (FAVICON_FILE, 'image/png', None, None),
    # Leaflet 1.9.4 (BSD-2, vendored; see vendor/leaflet/LICENSE) for the generator map.
    '/vendor/leaflet.js': (os.path.join(BASE_DIR, 'vendor', 'leaflet', 'leaflet.js'),
                           'text/javascript; charset=utf-8', None, None),
    '/vendor/leaflet.css': (os.path.join(BASE_DIR, 'vendor', 'leaflet', 'leaflet.css'), 'text/css; charset=utf-8', None, None),
}


class Handler(BaseHTTPRequestHandler):
    server_version = f'WattWatch/{APP_VERSION}'
    # Per socket read/write, not per request: a slow but steady 100MB upload is fine, a stalled client is cut off.
    timeout = REQUEST_TIMEOUT_SECONDS

    def version_string(self):
        # The Server header: our version only, without the Python version the base class appends.
        return self.server_version

    def send_response(self, code, message=None):
        self._response_started = True
        super().send_response(code, message)

    def end_headers(self):
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        super().end_headers()

    def _send_json(self, status, payload, extra_headers=None):
        data = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        for name, value in (extra_headers or []):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _send_page(self, page, head=False):
        path, content_type, csp, replacements = page
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError:
            raise ApiError(404, 'Not found.')
        for placeholder, value in (replacements() if replacements else {}).items():
            data = data.replace(placeholder.encode(), value.encode())
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache')
        if csp:
            self.send_header('Content-Security-Policy', csp)
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def _content_length(self):
        try:
            return int(self.headers.get('Content-Length') or 0)
        except ValueError:
            raise ApiError(400, 'Invalid Content-Length.')

    def _read_body(self, max_bytes=MAX_JSON_BYTES):
        length = self._content_length()
        if length == 0:
            return {}
        require(length <= max_bytes, 413, 'Request body is too large.')
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, 'Malformed JSON body.')
        require(isinstance(body, dict), 400, 'Request body must be a JSON object.')
        return body

    def _client(self):
        """(ip, source) of the visitor; see ratelimit.client_ip() for which headers are trusted and why."""
        return client_ip(self.headers, self.client_address[0])

    # Routes that work without a session. Each returns (payload, extra headers); see PUBLIC_ROUTES.
    def _login(self, conn):
        payload, token = login(conn, self._read_body(), rate_limit_key(self._client()[0]))
        return payload, [self._session_cookie(token, SESSION_TTL_DAYS * 86400)]

    def _logout(self, conn):
        return logout(conn, self.headers), [self._session_cookie('', 0)]

    def _me(self, conn):
        return whoami(conn, get_session_user(conn, self.headers)), []

    def _public_request(self, conn):
        # The only write that needs no sign-in: it creates one request and returns {"ok": true}.
        body = self._read_body(appointment_requests.MAX_BODY_BYTES)
        payload, new_id = appointment_requests.submit(conn, body, *self._client())
        if new_id:
            enqueue_email_job(notify_new_request, new_id)
        return payload, []

    def _session_cookie(self, token, max_age):
        secure = '; Secure' if self.headers.get('X-Forwarded-Proto') == 'https' else ''
        return ('Set-Cookie', f'{SESSION_COOKIE}={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age={max_age}{secure}')

    def _dispatch(self, method):
        self._response_started = False
        conn = None
        try:
            path = self.path.split('?', 1)[0]
            if method == 'GET' and path in PAGES:
                self._send_page(PAGES[path])
                return
            conn = get_conn()
            public = PUBLIC_ROUTES.get((method, path))
            if public:
                payload, headers = public(self, conn)
                self._send_json(200, payload, headers)
                return

            upload_match = UPLOAD_ROUTE.match(path) if method == 'POST' else None
            file_match = FILE_ROUTE.match(path) if method == 'GET' else None
            route = None
            if not (upload_match or file_match):
                route = next(((pattern.match(path), fn, permission) for m, pattern, fn, permission in ROUTES
                              if m == method and pattern.match(path)), None)
                require(route, 404, 'Not found.')

            # A number too big to be a row id (30 digits) is simply not found; SQLite would overflow on it.
            matched = upload_match or file_match or route[0]
            require(all(int(g) <= MAX_ID for g in matched.groups() if g and g.isdigit()), 404, 'Not found.')
            user = get_session_user(conn, self.headers)
            require(user, 401, 'Not authenticated.')
            if upload_match:
                require_permission(user, 'attachments.upload')
                self._handle_upload(conn, int(upload_match.group(1)), user)
            elif file_match:
                self._handle_file(conn, int(file_match.group(1)))
            else:
                match, fn, permission = route
                require_permission(user, permission)
                body = self._read_body() if method in ('POST', 'PUT') else {}
                self._send_json(200, fn(conn, match, body, user))
        except ApiError as e:
            if not self._response_started:
                self._send_json(e.status, {'error': e.message, 'code': e.code} if e.code else {'error': e.message})
        except socket.timeout:
            if not self._response_started:
                self._send_json(408, {'error': 'Request timed out.'})
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away (e.g. a video seek aborted a range request)
        except Exception:
            traceback.print_exc()
            if not self._response_started:
                self._send_json(500, {'error': 'Internal server error.'})
        finally:
            if conn:
                conn.close()

    def _handle_upload(self, conn, gen_id, user):
        length = self._content_length()
        require(0 < length <= MAX_UPLOAD_BYTES, 413, 'File exceeds the 100MB upload limit.')
        require(conn.execute('SELECT id FROM generators WHERE id=?', (gen_id,)).fetchone(), 404, 'Generator not found.')
        tmp_path = os.path.join(UPLOADS_DIR, f'.incoming-{uuid.uuid4().hex}')
        try:
            info = receive_upload(self.rfile, length, self.headers.get('Content-Type'), tmp_path)
            filename = clean_filename(info['filename'])
            kind, ext, content_type = validate_upload(filename, info['head'])
            stored_filename = f"{uuid.uuid4().hex}{ext}"
            os.replace(tmp_path, os.path.join(UPLOADS_DIR, stored_filename))
            try:
                conn.execute(
                    '''INSERT INTO generator_attachments
                       (generator_id,original_filename,stored_filename,content_type,kind,size_bytes,uploaded_by,uploaded_at)
                       VALUES (?,?,?,?,?,?,?,?)''',
                    (gen_id, filename, stored_filename, content_type, kind, info['size'], user['username'], _today())
                )
                conn.commit()
            except Exception:
                _remove_files([stored_filename])
                raise
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        self._send_json(200, get_generator(conn, gen_id))

    def _handle_file(self, conn, att_id):
        row = conn.execute('SELECT * FROM generator_attachments WHERE id=?', (att_id,)).fetchone()
        require(row, 404, 'Attachment not found.')
        path = os.path.join(UPLOADS_DIR, row['stored_filename'])
        try:
            f = open(path, 'rb')
        except OSError:
            raise ApiError(404, 'File not found on disk.')
        with f:
            size = os.fstat(f.fileno()).st_size
            byte_range = parse_range(self.headers.get('Range'), size) if size else None
            if byte_range is False:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            start, end = byte_range or (0, size - 1)
            self.send_response(206 if byte_range else 200)
            self.send_header('Content-Type', row['content_type'])
            self.send_header('Content-Length', str(end - start + 1))
            self.send_header('Accept-Ranges', 'bytes')
            if byte_range:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            # Attachment ids are never reused and files never change, so browsers can keep them.
            self.send_header('Cache-Control', 'private, max-age=31536000, immutable')
            self.send_header('Content-Disposition', content_disposition(row['original_filename']))
            self.end_headers()
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(STREAM_CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_GET(self):
        self._dispatch('GET')

    def do_POST(self):
        self._dispatch('POST')

    def do_PUT(self):
        self._dispatch('PUT')

    def do_DELETE(self):
        self._dispatch('DELETE')

    def do_HEAD(self):
        """For uptime monitors: the pages answer HEAD like GET without the body; the API doesn't."""
        path = self.path.split('?', 1)[0]
        if path in PAGES:
            self._send_page(PAGES[path], head=True)
            return
        self.send_response(405)
        self.send_header('Allow', 'GET, POST, PUT, DELETE')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def send_error(self, code, message=None, explain=None):
        """The standard library's errors (unknown method, bad request line, URI too long) as JSON like the
        rest of the API, instead of its HTML page. end_headers() adds the security headers."""
        short = self.responses.get(code, ('Error',))[0]
        text = {501: 'Method not supported.', 400: 'Bad request.', 414: 'Request URI too long.'}.get(code, message or short)
        self.log_error('code %d, message %s', code, message or short)
        if self.request_version == 'HTTP/0.9':  # unparsed request line: answer with a status line anyway
            self.request_version = 'HTTP/1.0'
        self.send_response(code, short)
        self.send_header('Connection', 'close')
        body = b''
        if code >= 200 and code not in (204, 205, 304) and getattr(self, 'command', None) != 'HEAD':
            body = json.dumps({'error': text}).encode('utf-8')
            self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        print('[%s] %s' % (self.log_date_time_string(), fmt % args))


PUBLIC_ROUTES = {
    ('POST', '/api/login'): Handler._login,
    ('POST', '/api/logout'): Handler._logout,
    ('GET', '/api/me'): Handler._me,
    ('POST', '/api/public/appointment-requests'): Handler._public_request,
}


def main():
    init_db()
    start_background_threads()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f'Watt Watch running at http://localhost:{PORT}/ (listening on {HOST})')
    if turnstile.disabled():
        print('WARNING: TURNSTILE_DISABLED is set: the request form has no robot check '
              '(rate limits and the honeypot still apply).', flush=True)
    print(f'Database file: {DB_PATH}')
    print(f'Uploads folder: {UPLOADS_DIR}')
    print('Email: ' + ('SMTP2GO configured' if notifications.email_configured()
                       else 'not configured (set SMTP2GO_API_KEY and EMAIL_FROM); emails are logged as skipped'))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nShutting down.')
        server.shutdown()


if __name__ == '__main__':
    main()
