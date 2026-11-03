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
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import traceback
import unicodedata
import uuid
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('DB_PATH') or os.path.join(BASE_DIR, 'gen_db.sqlite3')
STATIC_FILE = os.path.join(BASE_DIR, 'generator-manager.html')
PORT = int(os.environ.get('PORT', 8787))
# Railway sets PORT; locally, stay off the LAN unless HOST=0.0.0.0 is given.
HOST = os.environ.get('HOST') or ('0.0.0.0' if 'PORT' in os.environ else '127.0.0.1')

# Uploads live next to the database by default so they land on the same persistent volume.
UPLOADS_DIR = os.environ.get('UPLOADS_DIR') or os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), 'uploads')
os.makedirs(UPLOADS_DIR, exist_ok=True)
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
STREAM_CHUNK = 64 * 1024

SHORT_TEXT = 200
LONG_TEXT = 5000


# -------------------------------------------------------------------- time --
# The business only operates in Maine, so "today" always means today in US Eastern.

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo('America/New_York')
except Exception:  # no tz database on the host
    LOCAL_TZ = None


def _nth_sunday(year, month, n):
    first = datetime.date(year, month, 1)
    first_sunday = first + datetime.timedelta(days=(6 - first.weekday()) % 7)
    return first_sunday + datetime.timedelta(weeks=n - 1)


def _us_eastern_tz(utc_now):
    # DST runs from 2am EST on the 2nd Sunday of March to 2am EDT on the 1st Sunday of November.
    year = utc_now.year
    dst_start = datetime.datetime.combine(_nth_sunday(year, 3, 2), datetime.time(7), datetime.timezone.utc)
    dst_end = datetime.datetime.combine(_nth_sunday(year, 11, 1), datetime.time(6), datetime.timezone.utc)
    hours = -4 if dst_start <= utc_now < dst_end else -5
    return datetime.timezone(datetime.timedelta(hours=hours))


def _local_now():
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    return utc_now.astimezone(LOCAL_TZ or _us_eastern_tz(utc_now))


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


MIGRATIONS = [_migration_1, _migration_2, _migration_3, _migration_4]


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
}
ROLES = {
    'admin': set(PERMISSIONS),
    'technician': set(PERMISSIONS) - {'service.invoice'},
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
        while self.remaining > 0:
            chunk = self.rfile.read(min(STREAM_CHUNK, self.remaining))
            if not chunk:
                break
            self.remaining -= len(chunk)


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
    conn.execute(
        'INSERT INTO generator_history (generator_id,date,event,detail,created_by) VALUES (?,?,?,?,?)',
        (gen_id, v['install_date'] or _today(), 'Added to registry', f"Linked to {cust['name']}.", user['username'])
    )
    conn.commit()
    return get_generator(conn, gen_id)


def update_generator(conn, gen_id, body, user):
    row = conn.execute('SELECT * FROM generators WHERE id=?', (gen_id,)).fetchone()
    require(row, 404, 'Generator not found.')
    check_not_stale(row, body, 'generator')
    v = _generator_values(body)
    if (v['make'].lower(), v['serial'].lower()) != (row['make'].lower(), row['serial'].lower()):
        check_serial_unique(conn, v['make'], v['serial'], exclude_id=gen_id)
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
        conn.execute(
            'INSERT INTO generator_history (generator_id,date,event,detail,created_by) VALUES (?,?,?,?,?)',
            (gen_id, _today(), 'Service dates edited', ' '.join(changes), user['username'])
        )
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
    conn.execute(
        'INSERT INTO generator_history (generator_id,date,event,detail,created_by) VALUES (?,?,?,?,?)',
        (gen_id, _today(), 'Transferred', detail, user['username'])
    )
    conn.commit()
    return get_generator(conn, gen_id)


def add_service_record(conn, gen_id, body, user):
    row = conn.execute('SELECT * FROM generators WHERE id=?', (gen_id,)).fetchone()
    require(row, 404, 'Generator not found.')
    service_date = date_field(body, 'serviceDate', 'Service date')
    next_service_date = date_field(body, 'nextServiceDate', 'Next service date')
    notes = text_field(body, 'notes', 'Notes', LONG_TEXT)
    require(service_date, 400, 'Service date is required.')
    detail = notes if notes else 'Routine service.'
    if next_service_date:
        detail += f' Next service scheduled for {next_service_date}.'
    # A backdated visit (catch-up data entry) is recorded in history but mustn't roll the schedule backwards.
    if not row['last_service_date'] or service_date >= row['last_service_date']:
        conn.execute(
            'UPDATE generators SET last_service_date=?, next_service_date=?, updated_by=?, updated_at=? WHERE id=?',
            (service_date, next_service_date, user['username'], _timestamp(), gen_id)
        )
    conn.execute(
        'INSERT INTO generator_history (generator_id,date,event,detail,created_by) VALUES (?,?,?,?,?)',
        (gen_id, service_date, 'Serviced', detail, user['username'])
    )
    conn.commit()
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


def whoami(user):
    require(user, 401, 'Not authenticated.')
    return user_json(user)


# ---------------------------------------------------------------- dispatch --

# (method, path, handler, permission required — None for reads)
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


class Handler(BaseHTTPRequestHandler):
    server_version = 'WattWatch/1.0'

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

    def _send_html_file(self, path):
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError:
            raise ApiError(404, 'Not found.')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Security-Policy', HTML_CSP)
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

    def _client_key(self):
        # Behind Railway's proxy the real client is the last X-Forwarded-For hop.
        forwarded = self.headers.get('X-Forwarded-For')
        if forwarded:
            return forwarded.split(',')[-1].strip()
        return self.client_address[0]

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
            conn = get_conn()
            if method == 'POST' and path == '/api/login':
                payload, token = login(conn, self._read_body(), self._client_key())
                self._send_json(200, payload, [self._session_cookie(token, SESSION_TTL_DAYS * 86400)])
                return
            if method == 'POST' and path == '/api/logout':
                self._send_json(200, logout(conn, self.headers), [self._session_cookie('', 0)])
                return
            if method == 'GET' and path == '/api/me':
                self._send_json(200, whoami(get_session_user(conn, self.headers)))
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
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f'Watt Watch running at http://localhost:{PORT}/ (listening on {HOST})')
    print(f'Database file: {DB_PATH}')
    print(f'Uploads folder: {UPLOADS_DIR}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nShutting down.')
        server.shutdown()


if __name__ == '__main__':
    main()
