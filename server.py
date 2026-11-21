#!/usr/bin/env python3
"""
Watt Watch backend — customers & generators registry.

Zero external dependencies: serves the static frontend and a small JSON
REST API on top of a local SQLite database (gen_db.sqlite3, created
automatically next to this file on first run).

Run:  python3 server.py
Then open http://localhost:8787/
"""
import datetime
import hashlib
import html
import ipaddress
import json
import os
import queue
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
import traceback
import unicodedata
import uuid
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import matching
import notifications
import turnstile

# Semantic version shown in the page footer; bump it per CLAUDE.md and add a CHANGELOG.md entry.
APP_VERSION = '0.2.2'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('DB_PATH') or os.path.join(BASE_DIR, 'gen_db.sqlite3')
STATIC_FILE = os.path.join(BASE_DIR, 'generator-manager.html')
PORTAL_FILE = os.path.join(BASE_DIR, 'customer-request.html')
PORTAL_THANKS_FILE = os.path.join(BASE_DIR, 'customer-thanks.html')
PORTAL_SCRIPT_FILE = os.path.join(BASE_DIR, 'customer-request.js')
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

SHORT_TEXT = 200
LONG_TEXT = 5000

# ------------------------------------------------------- customer request portal --
# The public /customer page. Brands are a fixed list on purpose: the public page must never read
# existing data. brands_missing_from_form() flags database brands that aren't offered here.
REQUEST_BRANDS = ('Generac', 'Kohler', 'Cummins', 'Briggs & Stratton', 'Champion', 'Honeywell', 'Other', 'Not sure')
REQUEST_NEEDS = {'maintenance': 'Maintenance', 'repair': 'Repair', 'not_running': 'Not running'}
# The flow: a request comes in (new) -> an admin links it to a customer (matched) -> the office books
# the visit (scheduled; booking itself happens outside the app for now) -> the service is logged
# on the generator as usual.
REQUEST_STATUSES = ('new', 'matched', 'scheduled', 'dismissed')
# Allowed status changes an admin can make directly (matching/creating a customer moves new -> matched).
REQUEST_TRANSITIONS = {('new', 'dismissed'), ('dismissed', 'new'), ('matched', 'scheduled'), ('matched', 'new'),
                       ('scheduled', 'matched')}
# A field real people never see or fill. A nonsense name, so browsers have nothing to autofill.
HONEYPOT_FIELD = 'qz7_vk2'
HONEYPOT_REASON = 'spam filter (honeypot)'
PORTAL_MAX_BODY_BYTES = 16 * 1024
PORTAL_MAX_PER_IP = 5            # submission attempts per client per hour
PORTAL_IP_WINDOW_SECONDS = 60 * 60
PORTAL_MAX_PER_DAY = 100         # accepted requests per 24 hours, from everyone together
PORTAL_DAY_WINDOW_SECONDS = 24 * 60 * 60
REQUEST_NOTES_MAX = 2000
REQUEST_STREET_MAX = 300
REQUEST_TOWN_MAX = 100


# -------------------------------------------------------------------- time --
# The business only operates in Maine, so "today" always means today in US Eastern.
# The `tzdata` package (requirements.txt) supplies the zone where the OS has no tz database.

try:
    LOCAL_TZ = ZoneInfo('America/New_York')
except ZoneInfoNotFoundError as exc:
    raise SystemExit(
        'Cannot load the America/New_York time zone. Install the dependencies with '
        '`pip install -r requirements.txt` (it provides tzdata) and run with that Python '
        f'(this is {sys.executable}).') from exc


def _local_now():
    return datetime.datetime.now(datetime.timezone.utc).astimezone(LOCAL_TZ)


def _today():
    return _local_now().date().isoformat()


def _timestamp():
    return _local_now().isoformat(timespec='microseconds')


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


MIGRATIONS = [_migration_1, _migration_2, _migration_3, _migration_4, _migration_5, _migration_6, _migration_7]


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


def audit(conn, user, action, entity, entity_id, detail=''):
    conn.execute(
        'INSERT INTO audit_log (at,username,action,entity,entity_id,detail) VALUES (?,?,?,?,?,?)',
        (_timestamp(), user['username'] if user else None, action, entity, entity_id, detail)
    )


# ---------------------------------------------------------------------- auth --

PBKDF2_ITERATIONS = 260000
SESSION_COOKIE = 'session_token'
SESSION_TTL_DAYS = 30
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 15 * 60

_login_failures = {}
_login_lock = threading.Lock()

# Write actions a role may take; reads are open to every signed-in user.
# To lock technicians down further, remove permissions from their set.
PERMISSIONS = {
    'customers.edit': 'add or edit customers',
    'customers.delete': 'delete customers',
    'generators.edit': 'add or edit generators',
    'generators.delete': 'remove generators',
    'generators.transfer': 'transfer generators',
    'service.log': 'log service records',
    'service.invoice': 'mark service records as invoiced',
    'attachments.upload': 'upload files',
    'attachments.delete': 'delete files',
    'account.edit': 'change your account settings',
    'notifications.receive': 'receive email notifications',
    'requests.manage': 'view and handle appointment requests',
}
ROLES = {
    'admin': set(PERMISSIONS),
    'technician': set(PERMISSIONS) - {'service.invoice', 'notifications.receive', 'requests.manage'},
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


def _recent_login_failures(key):
    now = time.monotonic()
    failures = [t for t in _login_failures.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
    if failures:
        _login_failures[key] = failures
    else:
        _login_failures.pop(key, None)
    return failures


def check_login_allowed(key):
    with _login_lock:
        failures = _recent_login_failures(key)
    require(len(failures) < LOGIN_MAX_FAILURES, 429, 'Too many failed sign-in attempts. Try again in 15 minutes.')


def record_login_failure(key):
    with _login_lock:
        _recent_login_failures(key)
        _login_failures.setdefault(key, []).append(time.monotonic())


def clear_login_failures(key):
    with _login_lock:
        _login_failures.pop(key, None)


# ---------------------------------------------------------------- client IP --
# Who is on the other end, for rate limits. On Railway, the app only ever talks to Railway's edge
# proxy, which puts the address that connected to it in X-Real-IP. For gen.bridgeselec.com that
# address is a Cloudflare edge server, and Cloudflare puts the visitor in CF-Connecting-IP.
#
#   1. Start from the socket peer.
#   2. Behind Railway (TRUST_PROXY, on by default when PORT is set), use X-Real-IP instead.
#   3. Only if that address is one of Cloudflare's published ranges, use CF-Connecting-IP.
#
# X-Forwarded-For is never used: a client can put anything in it. CF-Connecting-IP is ignored unless
# the request really came from Cloudflare, because the app is also reachable without Cloudflare
# (the *.up.railway.app domain), where anyone can send that header.
TRUST_PROXY = os.environ.get('TRUST_PROXY', '1' if 'PORT' in os.environ else '0') == '1'

# https://www.cloudflare.com/ips-v4 and /ips-v6, fetched 2026-11-20. Refresh if Cloudflare changes them.
CLOUDFLARE_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    '173.245.48.0/20', '103.21.244.0/22', '103.22.200.0/22', '103.31.4.0/22', '141.101.64.0/18',
    '108.162.192.0/18', '190.93.240.0/20', '188.114.96.0/20', '197.234.240.0/22', '198.41.128.0/17',
    '162.158.0.0/15', '104.16.0.0/13', '104.24.0.0/14', '172.64.0.0/13', '131.0.72.0/22',
    '2400:cb00::/32', '2606:4700::/32', '2803:f800::/32', '2405:b500::/32', '2405:8100::/32',
    '2a06:98c0::/29', '2c0f:f248::/32',
))


def _parse_ip(value):
    try:
        ip = ipaddress.ip_address((value or '').strip())
    except ValueError:
        return None
    return ip.ipv4_mapped or ip if ip.version == 6 else ip


def is_cloudflare(ip):
    return ip is not None and any(ip in net for net in CLOUDFLARE_NETWORKS if net.version == ip.version)


def client_ip(headers, socket_peer):
    """(ip, source) for a request: source is 'cloudflare', 'proxy' or 'socket'. ip may be None."""
    ip, source = _parse_ip(socket_peer), 'socket'
    if TRUST_PROXY:
        real = _parse_ip(headers.get('X-Real-IP'))
        if real:
            ip, source = real, 'proxy'
    if is_cloudflare(ip):
        visitor = _parse_ip(headers.get('CF-Connecting-IP'))
        if visitor:
            ip, source = visitor, 'cloudflare'
    return ip, source


def rate_limit_key(ip):
    """IPv4 addresses count alone; IPv6 by /64, since one household or phone gets a whole /64."""
    if ip is None:
        return 'unknown'
    if ip.version == 6:
        return str(ipaddress.ip_network(f'{ip}/64', strict=False))
    return str(ip)


# ------------------------------------------------------- portal rate limits --

_portal_attempts = {}   # rate-limit key -> monotonic times of recent submission attempts
_portal_accepted = []   # monotonic times of recently stored requests, from everyone
_portal_lock = threading.Lock()


def _recent(times, window, now):
    return [t for t in times if now - t < window]


def check_portal_allowed(key):
    """Counts this attempt; raises 429 past the per-client or the overall limit."""
    now = time.monotonic()
    with _portal_lock:
        attempts = _recent(_portal_attempts.get(key, []), PORTAL_IP_WINDOW_SECONDS, now)
        _portal_accepted[:] = _recent(_portal_accepted, PORTAL_DAY_WINDOW_SECONDS, now)
        if len(attempts) >= PORTAL_MAX_PER_IP:
            _portal_attempts[key] = attempts
            raise ApiError(429, 'Too many requests from your connection. Please try again later, or call the office.')
        attempts.append(now)
        _portal_attempts[key] = attempts
        if len(_portal_accepted) >= PORTAL_MAX_PER_DAY:
            raise ApiError(429, "We've had a lot of requests today. Please call the office.")


def record_portal_accepted():
    with _portal_lock:
        _portal_accepted.append(time.monotonic())


def reset_rate_limits():
    with _login_lock:
        _login_failures.clear()
    with _portal_lock:
        _portal_attempts.clear()
        _portal_accepted.clear()


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

def customer_row_to_json(row):
    return {
        'id': row['id'],
        'name': row['name'],
        'email': row['email'] or '',
        'phone': row['phone'] or '',
        'physicalAddress': row['physical_address'] or '',
        'billingAddress': row['billing_address'] or '',
        'notes': row['notes'] or '',
        'createdBy': row['created_by'] or '',
        'updatedBy': row['updated_by'] or '',
        'updatedAt': row['updated_at'] or '',
    }


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
             'invoiced': bool(h['invoiced']), 'invoicedBy': h['invoiced_by'] or '', 'invoicedAt': h['invoiced_at'] or ''}
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


# --------------------------------------------------------------- API errors --

class ApiError(Exception):
    def __init__(self, status, message, code=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def require(cond, status, message):
    if not cond:
        raise ApiError(status, message)


# --------------------------------------------------------- input validation --

DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def text_field(body, key, label, max_len=SHORT_TEXT):
    value = body.get(key)
    if value is None:
        return ''
    require(isinstance(value, str), 400, f'{label} must be text.')
    value = value.strip()
    require(len(value) <= max_len, 400, f'{label} is too long (max {max_len} characters).')
    return value


def date_field(body, key, label):
    value = text_field(body, key, label, LONG_TEXT)
    if value:
        try:
            valid = bool(DATE_RE.match(value)) and bool(datetime.date.fromisoformat(value))
        except ValueError:
            valid = False
        require(valid, 400, f'{label} must be a valid date.')
    return value


def id_field(body, key, message):
    value = body.get(key)
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    require(isinstance(value, int) and not isinstance(value, bool) and value > 0, 400, message)
    return value


def check_next_after(earlier, next_date, earlier_label):
    # ISO dates compare correctly as strings.
    if earlier and next_date:
        require(next_date > earlier, 400, f'Next service date must be after the {earlier_label}.')


def check_not_stale(row, body, what):
    expected = body.get('expectedUpdatedAt')
    if expected is None:
        return
    if (row['updated_at'] or '') != expected:
        who = row['updated_by'] or 'another user'
        raise ApiError(409, f'This {what} was changed by {who} after you opened it. '
                            f'The latest version has been loaded — please make your edit again.', code='stale')


# ------------------------------------------------------------------ routes --

def list_customers(conn):
    rows = conn.execute('SELECT * FROM customers ORDER BY name COLLATE NOCASE ASC').fetchall()
    return [customer_row_to_json(r) for r in rows]


def get_customer(conn, cust_id):
    row = conn.execute('SELECT * FROM customers WHERE id=?', (cust_id,)).fetchone()
    require(row, 404, 'Customer not found.')
    return customer_row_to_json(row)


def _customer_values(body):
    name = text_field(body, 'name', 'Name')
    require(name, 400, 'Name is required.')
    return (name, text_field(body, 'email', 'Email'), text_field(body, 'phone', 'Phone'),
            text_field(body, 'physicalAddress', 'Physical address', LONG_TEXT),
            text_field(body, 'billingAddress', 'Billing address', LONG_TEXT),
            text_field(body, 'notes', 'Notes', LONG_TEXT))


def create_customer(conn, body, user):
    cur = conn.execute(
        '''INSERT INTO customers (name,email,phone,physical_address,billing_address,notes,created_by,updated_by,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)''',
        _customer_values(body) + (user['username'], user['username'], _timestamp())
    )
    conn.commit()
    return get_customer(conn, cur.lastrowid)


def update_customer(conn, cust_id, body, user):
    row = conn.execute('SELECT * FROM customers WHERE id=?', (cust_id,)).fetchone()
    require(row, 404, 'Customer not found.')
    check_not_stale(row, body, 'customer')
    conn.execute(
        '''UPDATE customers SET name=?, email=?, phone=?, physical_address=?, billing_address=?, notes=?,
           updated_by=?, updated_at=? WHERE id=?''',
        _customer_values(body) + (user['username'], _timestamp(), cust_id)
    )
    conn.commit()
    return get_customer(conn, cust_id)


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


def add_history(conn, gen_id, date, event, detail, user):
    conn.execute(
        'INSERT INTO generator_history (generator_id,date,event,detail,created_by,created_at) VALUES (?,?,?,?,?,?)',
        (gen_id, date, event, detail, user['username'], _timestamp())
    )


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
    check_next_after(v['last_service_date'], v['next_service_date'], 'last service date')
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
    row = conn.execute('SELECT * FROM generators WHERE id=?', (gen_id,)).fetchone()
    require(row, 404, 'Generator not found.')
    check_not_stale(row, body, 'generator')
    v = _generator_values(body)
    if (v['make'].lower(), v['serial'].lower()) != (row['make'].lower(), row['serial'].lower()):
        check_serial_unique(conn, v['make'], v['serial'], exclude_id=gen_id)
    # Only when the dates change, so older records with bad dates can still have other fields edited.
    if (v['last_service_date'], v['next_service_date']) != (row['last_service_date'] or '', row['next_service_date'] or ''):
        check_next_after(v['last_service_date'], v['next_service_date'], 'last service date')
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


def add_service_record(conn, gen_id, body, user):
    row = conn.execute('SELECT * FROM generators WHERE id=?', (gen_id,)).fetchone()
    require(row, 404, 'Generator not found.')
    service_date = date_field(body, 'serviceDate', 'Service date')
    next_service_date = date_field(body, 'nextServiceDate', 'Next service date')
    notes = text_field(body, 'notes', 'Notes', LONG_TEXT)
    require(service_date, 400, 'Service date is required.')
    check_next_after(service_date, next_service_date, 'service date')
    detail = notes if notes else 'Routine service.'
    if next_service_date:
        detail += f' Next service scheduled for {next_service_date}.'
    # A backdated visit (catch-up data entry) is recorded in history but mustn't roll the schedule backwards.
    if not row['last_service_date'] or service_date >= row['last_service_date']:
        conn.execute(
            'UPDATE generators SET last_service_date=?, next_service_date=?, updated_by=?, updated_at=? WHERE id=?',
            (service_date, next_service_date, user['username'], _timestamp(), gen_id)
        )
    add_history(conn, gen_id, service_date, 'Serviced', detail, user)
    conn.commit()
    cust = conn.execute('SELECT * FROM customers WHERE id=?', (row['customer_id'],)).fetchone()
    enqueue_email_job(notify_service_record, {
        'customer': cust['name'] if cust else 'Unknown customer', 'phone': cust['phone'] if cust else '',
        'site': (row['physical_address'] or '').replace('\n', ', '),
        'unit': f"{row['make']} {row['model']} (S/N {row['serial']})",
        'service_date': service_date, 'next_service_date': next_service_date, 'detail': detail,
        'logged_by': user['username'],
    })
    return get_generator(conn, gen_id)


def set_invoiced(conn, hist_id, body, user):
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
    check_login_allowed(client_key)
    username = text_field(body, 'username', 'Username')
    password = body.get('password')
    require(username and isinstance(password, str) and password, 400, 'Username and password are required.')
    row = conn.execute('SELECT * FROM users WHERE username=? COLLATE NOCASE', (username,)).fetchone()
    if row:
        ok = verify_password(password, row['password_salt'], row['password_hash'])
    else:
        verify_password(password, '00' * 16, '')  # same cost as a real check, so timing doesn't reveal usernames
        ok = False
    if not ok:
        record_login_failure(client_key)
        raise ApiError(401, 'Incorrect username or password.')
    clear_login_failures(client_key)
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
    out['notificationKinds'] = [{'kind': k, 'label': v} for k, v in notifications.NOTIFICATION_KINDS.items()]
    if 'notifications.receive' in user['permissions']:
        out['emailConfigured'] = notifications.email_configured()
    return out


EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


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

SCHEDULED_EMAILS = {  # kind -> (weekday Mon=0, hour, minute), Maine time
    'weekly_digest': (4, 16, 0),
    'hit_list': (0, 7, 0),
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
    return [r for r in rows if 'notifications.receive' in ROLES.get(r['role'], set())]


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


def _localize(naive):
    return naive.replace(tzinfo=LOCAL_TZ)


def scheduled_time(kind, now):
    """The most recent scheduled send of `kind` at or before `now` (an aware Maine datetime)."""
    weekday, hour, minute = SCHEDULED_EMAILS[kind]
    day = now.date() - datetime.timedelta(days=(now.weekday() - weekday) % 7)
    at = _localize(datetime.datetime.combine(day, datetime.time(hour, minute)))
    if at > now:  # today is the day but it's not time yet: the previous one
        at = _localize(datetime.datetime.combine(day - datetime.timedelta(days=7), datetime.time(hour, minute)))
    return at


def build_scheduled_email(conn, kind, at):
    app_url = notifications.email_settings()['app_url']
    if kind == 'weekly_digest':
        start = at - datetime.timedelta(days=7)
        activity = notifications.weekly_activity(conn, start.isoformat(timespec='microseconds'),
                                                 at.isoformat(timespec='microseconds'))
        return notifications.weekly_digest_email(activity, start.date(), at.date(), app_url)
    if kind == 'hit_list':
        overdue, due_soon = notifications.hit_list(conn, at.date())
        return notifications.hit_list_email(overdue, due_soon, at.date(), app_url=app_url)
    raise ValueError(f'Unknown scheduled email: {kind}')


def run_due_jobs(now=None):
    """Sends any scheduled email that's due and not yet sent this week. Safe to call from several
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
                    _send_to_subscribers(conn, kind, build_scheduled_email(conn, kind, at))
                sent.append(kind)
        finally:
            conn.close()
    return sent


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


# ---------------------------------------------------------------- dispatch --

# (method, path, handler, permission required — None for reads)
# ------------------------------------------------------- appointment requests --

def _request_values(body):
    """Validated fields from the public form, in column order."""
    name = text_field(body, 'name', 'Name')
    phone = text_field(body, 'phone', 'Phone')
    email = text_field(body, 'email', 'Email')
    street = text_field(body, 'serviceStreet', 'Street address', REQUEST_STREET_MAX)
    town = text_field(body, 'serviceTown', 'Town', REQUEST_TOWN_MAX)
    brand = text_field(body, 'brand', 'Generator brand')
    need = text_field(body, 'need', 'What you need')
    model = text_field(body, 'model', 'Model')
    notes = text_field(body, 'notes', 'Notes', REQUEST_NOTES_MAX)
    require(name, 400, 'Please enter your name.')
    digits = matching.phone_digits(phone)
    require(digits, 400, 'Please enter a 10-digit phone number.')
    require(email and EMAIL_RE.match(email), 400, 'Please enter a valid email address.')
    require(street, 400, 'Please enter the street address.')
    require(town, 400, 'Please enter the town.')
    require(brand in REQUEST_BRANDS, 400, 'Please choose a generator brand.')
    require(need in REQUEST_NEEDS, 400, 'Please choose what you need.')
    return name, phone, digits, email, street, town, brand, model, need, notes


def submit_appointment_request(conn, body, ip, source):
    """The public form. Creates one request and returns {'ok': True}; never reads or returns other data."""
    values = _request_values(body)
    try:
        turnstile.verify(body.get('turnstileToken'), str(ip) if source == 'cloudflare' and ip else None)
    except turnstile.TurnstileUnavailable as e:
        print(f'turnstile unavailable: {e}', flush=True)
        raise ApiError(503, "Online requests aren't available right now. Please call the office.")
    except turnstile.TurnstileRejected:
        raise ApiError(400, "We couldn't confirm you're not a robot. Please try the check again.")
    honeypot = body.get(HONEYPOT_FIELD)
    spam = honeypot is not None and honeypot != ''
    now = _timestamp()
    conn.execute(
        '''INSERT INTO appointment_requests (created_at, status, name, phone, phone_digits, email, service_street,
           service_town, brand, model, need, notes, dismiss_reason, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (now, 'dismissed' if spam else 'new') + values + (HONEYPOT_REASON if spam else '', now))
    conn.commit()
    record_portal_accepted()
    print(f'appointment request stored (client ip via {source}{", honeypot" if spam else ""})', flush=True)
    return {'ok': True}


def request_json(row):
    return {
        'id': row['id'], 'createdAt': row['created_at'], 'status': row['status'],
        'name': row['name'], 'phone': row['phone'], 'email': row['email'],
        'serviceStreet': row['service_street'], 'serviceTown': row['service_town'],
        'brand': row['brand'], 'model': row['model'],
        'need': row['need'], 'needLabel': REQUEST_NEEDS.get(row['need'], row['need']), 'notes': row['notes'],
        'customerId': row['customer_id'], 'generatorId': row['generator_id'],
        'dismissReason': row['dismiss_reason'], 'handledBy': row['handled_by'], 'handledAt': row['handled_at'],
        'updatedBy': row['updated_by'], 'updatedAt': row['updated_at'] or '',
    }


def _match_data(conn):
    customers = [dict(r) for r in conn.execute(
        'SELECT id, name, phone, email, physical_address, billing_address FROM customers')]
    generators = [dict(r) for r in conn.execute(
        'SELECT id, customer_id, make, model, serial, physical_address FROM generators')]
    return customers, generators


def _match(row, data):
    # The matcher compares the street line (house number + street); the town is shown to the admin.
    return matching.match_request(dict(row, service_address=row['service_street']), *data)


def brands_missing_from_form(conn):
    """Brands on file that the public form doesn't offer (so customers would have to pick "Other")."""
    offered = {b.lower() for b in REQUEST_BRANDS}
    makes = {r[0].strip() for r in conn.execute('SELECT DISTINCT make FROM generators') if r[0] and r[0].strip()}
    return sorted((m for m in makes if m.lower() not in offered), key=str.lower)


def list_appointment_requests(conn):
    rows = conn.execute('SELECT * FROM appointment_requests ORDER BY created_at DESC, id DESC').fetchall()
    data = _match_data(conn) if any(r['status'] == 'new' for r in rows) else None
    out = []
    for row in rows:
        item = request_json(row)
        if row['status'] == 'new':
            result = _match(row, data)
            item['suggestion'] = result['candidates'][0] if result['candidates'] else None
            item['conflict'] = result['conflict']
        out.append(item)
    return {'requests': out, 'formBrandGaps': brands_missing_from_form(conn)}


def _request_row(conn, req_id):
    row = conn.execute('SELECT * FROM appointment_requests WHERE id=?', (req_id,)).fetchone()
    require(row, 404, 'Request not found.')
    return row


def get_appointment_request(conn, req_id):
    row = _request_row(conn, req_id)
    result = _match(row, _match_data(conn))
    return {'request': request_json(row), 'candidates': result['candidates'], 'conflict': result['conflict']}


def _require_new(row):
    require(row['status'] == 'new', 409, f"This request is already {row['status']}. Reload to see the latest.")


def _link_values(conn, body, customer_id):
    gen_id = body.get('generatorId')
    if gen_id in (None, ''):
        return None
    gen_id = id_field(body, 'generatorId', 'Invalid generator.')
    gen = conn.execute('SELECT customer_id FROM generators WHERE id=?', (gen_id,)).fetchone()
    require(gen and gen['customer_id'] == customer_id, 400, "That generator doesn't belong to this customer.")
    return gen_id


def match_appointment_request(conn, req_id, body, user):
    row = _request_row(conn, req_id)
    check_not_stale(row, body, 'request')
    _require_new(row)
    customer_id = id_field(body, 'customerId', 'Choose a customer.')
    require(conn.execute('SELECT 1 FROM customers WHERE id=?', (customer_id,)).fetchone(), 400, 'Customer not found.')
    gen_id = _link_values(conn, body, customer_id)
    now = _timestamp()
    conn.execute('''UPDATE appointment_requests SET status='matched', customer_id=?, generator_id=?, handled_by=?,
                    handled_at=?, updated_by=?, updated_at=? WHERE id=?''',
                 (customer_id, gen_id, user['username'], now, user['username'], now, req_id))
    audit(conn, user, 'match', 'appointment_request', req_id, f'customer {customer_id}, generator {gen_id or "-"}')
    conn.commit()
    return request_json(_request_row(conn, req_id))


def create_customer_from_request(conn, req_id, body, user):
    require_permission(user, 'customers.edit')
    row = _request_row(conn, req_id)
    check_not_stale(row, body, 'request')
    _require_new(row)
    now = _timestamp()
    cur = conn.execute(
        '''INSERT INTO customers (name,email,phone,physical_address,billing_address,notes,created_by,updated_by,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)''',
        _customer_values(body) + (user['username'], user['username'], now))
    conn.execute('''UPDATE appointment_requests SET status='matched', customer_id=?, generator_id=NULL, handled_by=?,
                    handled_at=?, updated_by=?, updated_at=? WHERE id=?''',
                 (cur.lastrowid, user['username'], now, user['username'], now, req_id))
    audit(conn, user, 'create_customer', 'appointment_request', req_id, f'customer {cur.lastrowid}')
    conn.commit()
    return {'request': request_json(_request_row(conn, req_id)), 'customer': get_customer(conn, cur.lastrowid)}


def set_appointment_request_status(conn, req_id, body, user):
    row = _request_row(conn, req_id)
    check_not_stale(row, body, 'request')
    status = text_field(body, 'status', 'Status')
    require(status in REQUEST_STATUSES, 400, 'Unknown status.')
    require((row['status'], status) in REQUEST_TRANSITIONS, 400,
            f"A {row['status']} request can't be marked {status}.")
    reason = row['dismiss_reason']
    customer_id, gen_id = row['customer_id'], row['generator_id']
    if status == 'dismissed':
        reason = text_field(body, 'reason', 'Reason') or 'dismissed by admin'
    elif status == 'new':
        reason, customer_id, gen_id = '', None, None
    now = _timestamp()
    conn.execute('''UPDATE appointment_requests SET status=?, dismiss_reason=?, customer_id=?, generator_id=?,
                    handled_by=?, handled_at=?, updated_by=?, updated_at=? WHERE id=?''',
                 (status, reason, customer_id, gen_id, user['username'], now, user['username'], now, req_id))
    audit(conn, user, status, 'appointment_request', req_id, reason if status == 'dismissed' else '')
    conn.commit()
    return request_json(_request_row(conn, req_id))


ROUTES = [
    ('GET', re.compile(r'^/api/customers$'), lambda conn, m, body, user: list_customers(conn), None),
    ('POST', re.compile(r'^/api/customers$'), lambda conn, m, body, user: create_customer(conn, body, user), 'customers.edit'),
    ('GET', re.compile(r'^/api/customers/(\d+)$'), lambda conn, m, body, user: get_customer(conn, int(m.group(1))), None),
    ('PUT', re.compile(r'^/api/customers/(\d+)$'), lambda conn, m, body, user: update_customer(conn, int(m.group(1)), body, user), 'customers.edit'),
    ('DELETE', re.compile(r'^/api/customers/(\d+)$'), lambda conn, m, body, user: delete_customer(conn, int(m.group(1)), user), 'customers.delete'),

    ('GET', re.compile(r'^/api/generators$'), lambda conn, m, body, user: list_generators(conn), None),
    ('POST', re.compile(r'^/api/generators$'), lambda conn, m, body, user: create_generator(conn, body, user), 'generators.edit'),
    ('GET', re.compile(r'^/api/generators/(\d+)$'), lambda conn, m, body, user: get_generator(conn, int(m.group(1))), None),
    ('PUT', re.compile(r'^/api/generators/(\d+)$'), lambda conn, m, body, user: update_generator(conn, int(m.group(1)), body, user), 'generators.edit'),
    ('DELETE', re.compile(r'^/api/generators/(\d+)$'), lambda conn, m, body, user: delete_generator(conn, int(m.group(1)), user), 'generators.delete'),
    ('POST', re.compile(r'^/api/generators/(\d+)/transfer$'), lambda conn, m, body, user: transfer_generator(conn, int(m.group(1)), body, user), 'generators.transfer'),
    ('POST', re.compile(r'^/api/generators/(\d+)/service-records$'), lambda conn, m, body, user: add_service_record(conn, int(m.group(1)), body, user), 'service.log'),
    ('POST', re.compile(r'^/api/service-records/(\d+)/invoice$'), lambda conn, m, body, user: set_invoiced(conn, int(m.group(1)), body, user), 'service.invoice'),
    ('DELETE', re.compile(r'^/api/attachments/(\d+)$'), lambda conn, m, body, user: delete_attachment(conn, int(m.group(1)), user), 'attachments.delete'),

    # Admin only, reads included: requests hold the public's contact details and match results.
    ('GET', re.compile(r'^/api/appointment-requests$'), lambda conn, m, body, user: list_appointment_requests(conn), 'requests.manage'),
    ('GET', re.compile(r'^/api/appointment-requests/(\d+)$'), lambda conn, m, body, user: get_appointment_request(conn, int(m.group(1))), 'requests.manage'),
    ('POST', re.compile(r'^/api/appointment-requests/(\d+)/match$'), lambda conn, m, body, user: match_appointment_request(conn, int(m.group(1)), body, user), 'requests.manage'),
    ('POST', re.compile(r'^/api/appointment-requests/(\d+)/create-customer$'), lambda conn, m, body, user: create_customer_from_request(conn, int(m.group(1)), body, user), 'requests.manage'),
    ('POST', re.compile(r'^/api/appointment-requests/(\d+)/status$'), lambda conn, m, body, user: set_appointment_request_status(conn, int(m.group(1)), body, user), 'requests.manage'),

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
            "img-src 'self' data: blob:; media-src 'self'; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
# The public request page: no inline scripts at all (customer-request.js does everything), and
# Cloudflare only where Turnstile needs it (its script and its iframe).
PORTAL_CSP = ("default-src 'self'; script-src 'self' https://challenges.cloudflare.com; "
              "frame-src https://challenges.cloudflare.com; style-src 'self' 'unsafe-inline'; "
              "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
              "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def _options_html(options):
    return ''.join(f'<option value="{html.escape(value)}">{html.escape(label)}</option>' for value, label in options)


def portal_replacements():
    """Values the server fills into customer-request.html. Everything is HTML-escaped."""
    phone = os.environ.get('OFFICE_PHONE', '').strip()
    return {
        '{{APP_VERSION}}': html.escape(APP_VERSION),
        '{{TURNSTILE_SITE_KEY}}': html.escape(turnstile.settings()['site_key']),
        '{{OFFICE_PHONE}}': html.escape(phone or 'the office'),
        '{{OFFICE_PHONE_TEL}}': html.escape(('+1' + matching.phone_digits(phone)) if matching.phone_digits(phone)
                                            else re.sub(r'[^\d+]', '', phone)),
        '{{HONEYPOT_FIELD}}': HONEYPOT_FIELD,
        '{{BRAND_OPTIONS}}': _options_html((b, b) for b in REQUEST_BRANDS),
        '{{NEED_OPTIONS}}': _options_html(REQUEST_NEEDS.items()),
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

    def _send_html_file(self, path, replacements=None, csp=HTML_CSP):
        self._send_static(path, 'text/html; charset=utf-8', replacements or {'{{APP_VERSION}}': APP_VERSION}, csp)

    def _send_static(self, path, content_type, replacements=None, csp=None):
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError:
            raise ApiError(404, 'Not found.')
        for placeholder, value in (replacements or {}).items():
            data = data.replace(placeholder.encode(), value.encode())
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache')
        if csp:
            self.send_header('Content-Security-Policy', csp)
        self.end_headers()
        self.wfile.write(data)

    def _content_length(self):
        try:
            return int(self.headers.get('Content-Length') or 0)
        except ValueError:
            raise ApiError(400, 'Invalid Content-Length.')

    def _read_body(self):
        length = self._content_length()
        if length == 0:
            return {}
        require(length <= MAX_JSON_BYTES, 413, 'Request body is too large.')
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, 'Malformed JSON body.')
        require(isinstance(body, dict), 400, 'Request body must be a JSON object.')
        return body

    def _client(self):
        """(ip, source) of the visitor; see client_ip() for which headers are trusted and why."""
        return client_ip(self.headers, self.client_address[0])

    def _client_key(self):
        return rate_limit_key(self._client()[0])

    def _session_cookie(self, token, max_age):
        secure = '; Secure' if self.headers.get('X-Forwarded-Proto') == 'https' else ''
        return ('Set-Cookie', f'{SESSION_COOKIE}={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age={max_age}{secure}')

    def _dispatch(self, method):
        self._response_started = False
        conn = None
        try:
            path = self.path.split('?', 1)[0]
            if method == 'GET' and path in ('/', '/generator-manager.html'):
                self._send_html_file(STATIC_FILE)
                return
            if method == 'GET' and path in ('/customer', '/customer/'):
                self._send_html_file(PORTAL_FILE, portal_replacements(), PORTAL_CSP)
                return
            if method == 'GET' and path == '/customer/thanks':
                # Static confirmation the form goes to after a successful submit. It shows no data,
                # so it's fine that anyone can open it directly.
                self._send_html_file(PORTAL_THANKS_FILE, portal_replacements(), PORTAL_CSP)
                return
            if method == 'GET' and path == '/customer-request.js':
                self._send_static(PORTAL_SCRIPT_FILE, 'text/javascript; charset=utf-8')
                return
            if method == 'POST' and path == '/api/public/appointment-requests':
                # The only write that needs no sign-in. It creates one request and returns {"ok": true}.
                ip, source = self._client()
                if self._content_length() > PORTAL_MAX_BODY_BYTES:
                    self.close_connection = True  # the body is left unread, so this connection can't be reused
                    raise ApiError(413, 'Request is too large.')
                body = self._read_body()
                check_portal_allowed(rate_limit_key(ip))
                conn = get_conn()
                self._send_json(200, submit_appointment_request(conn, body, ip, source))
                return
            conn = get_conn()
            if method == 'POST' and path == '/api/login':
                payload, token = login(conn, self._read_body(), self._client_key())
                self._send_json(200, payload, [self._session_cookie(token, SESSION_TTL_DAYS * 86400)])
                return
            if method == 'POST' and path == '/api/logout':
                self._send_json(200, logout(conn, self.headers), [self._session_cookie('', 0)])
                return
            if method == 'GET' and path == '/api/me':
                self._send_json(200, whoami(conn, get_session_user(conn, self.headers)))
                return

            upload_match = UPLOAD_ROUTE.match(path) if method == 'POST' else None
            file_match = FILE_ROUTE.match(path) if method == 'GET' else None
            route = None
            if not (upload_match or file_match):
                route = next(((pattern.match(path), fn, permission) for m, pattern, fn, permission in ROUTES
                              if m == method and pattern.match(path)), None)
                require(route, 404, 'Not found.')

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

    def log_message(self, fmt, *args):
        print('[%s] %s' % (self.log_date_time_string(), fmt % args))


def main():
    init_db()
    start_background_threads()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f'Watt Watch running at http://localhost:{PORT}/ (listening on {HOST})')
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
