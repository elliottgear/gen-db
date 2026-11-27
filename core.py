"""
Shared building blocks for server.py and its feature modules: API errors, request-field
validation, Maine time, and the audit log.

Nothing here imports server.py, so feature modules (appointment_requests.py, ...) can use these
without a circular import. server.py re-exports them, so `server.text_field` etc. still work.
"""
import datetime
import re
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SHORT_TEXT = 200
LONG_TEXT = 5000
MAX_ID = 2 ** 63 - 1  # SQLite's largest integer; anything bigger can't be a row id (and crashed the query)


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
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


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
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    require(isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_ID, 400, message)
    return value


def write_lock(conn):
    """Takes SQLite's write lock now, before reading a row that a check_not_stale() / status check will
    judge. Without it, two saves of the same version can both read the old row, both pass, and the
    second silently overwrites the first. The lock is released by the handler's commit (or rollback
    when the connection closes on an error). Call it first, before any read the decision depends on,
    and never around slow network calls."""
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')


def check_not_stale(row, body, what):
    expected = body.get('expectedUpdatedAt')
    if expected is None:
        return
    if (row['updated_at'] or '') != expected:
        who = row['updated_by'] or 'another user'
        raise ApiError(409, f'This {what} was changed by {who} after you opened it. '
                            f'The latest version has been loaded — please make your edit again.', code='stale')


# --------------------------------------------------------------- audit log --

def audit(conn, user, action, entity, entity_id, detail=''):
    conn.execute(
        'INSERT INTO audit_log (at,username,action,entity,entity_id,detail) VALUES (?,?,?,?,?,?)',
        (_timestamp(), user['username'] if user else None, action, entity, entity_id, detail)
    )
