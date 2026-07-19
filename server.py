#!/usr/bin/env python3
"""
Watt Watch backend — customers & generators registry.

Zero external dependencies: serves the static frontend and a small JSON
REST API on top of a local SQLite database (gen_db.sqlite3, created
automatically next to this file on first run).

Run:  python3 server.py
Then open http://localhost:8787/
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'gen_db.sqlite3')
STATIC_FILE = os.path.join(BASE_DIR, 'generator-manager.html')
PORT = int(os.environ.get('PORT', 8787))


# ---------------------------------------------------------------- database --

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db():
    conn = get_conn()
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
            detail TEXT
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
    ''')
    conn.commit()
    _migrate(conn)
    if conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0] == 0:
        seed(conn)
    conn.close()


def _migrate(conn):
    cols = [r['name'] for r in conn.execute('PRAGMA table_info(generators)').fetchall()]
    if 'next_service_date' not in cols:
        conn.execute('ALTER TABLE generators ADD COLUMN next_service_date TEXT')
        conn.commit()
    for table in ('customers', 'generators'):
        cols = [r['name'] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()]
        for col in ('created_by', 'updated_by'):
            if col not in cols:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {col} TEXT')
    hist_cols = [r['name'] for r in conn.execute('PRAGMA table_info(generator_history)').fetchall()]
    if 'created_by' not in hist_cols:
        conn.execute('ALTER TABLE generator_history ADD COLUMN created_by TEXT')
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
             install_date='2021-05-14', last_service_date='2025-04-02', installer_name='Coastal Power Solutions',
             notes='Runs on propane. 500 gal tank buried east side of house.',
             history=[('2021-05-14', 'Installed', 'Installed for Marlene Ouellette by Coastal Power Solutions.')]),
        dict(customer_id=cust_ids[1], make='Kohler', model='14RESAL', serial='KOH-551029-C',
             install_date='2019-09-30', last_service_date='2024-02-11', installer_name='Downeast Generator Co.',
             notes='',
             history=[('2019-09-30', 'Installed', 'Installed for Dennis & Karen Roy by Downeast Generator Co.')]),
        dict(customer_id=cust_ids[2], make='Generac', model='Protector 48kW', serial='GEN-702255-B',
             install_date='2022-01-20', last_service_date='2026-06-01', installer_name='Coastal Power Solutions',
             notes='Commercial unit, natural gas fed. Access via rear alley gate.',
             history=[('2022-01-20', 'Installed', 'Installed for Harborview Property Mgmt by Coastal Power Solutions.')]),
    ]
    for g in generators:
        cur = conn.execute(
            '''INSERT INTO generators
               (customer_id,make,model,serial,install_date,last_service_date,installer_name,notes)
               VALUES (?,?,?,?,?,?,?,?)''',
            (g['customer_id'], g['make'], g['model'], g['serial'], g['install_date'],
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
        'INSERT INTO sessions (token, user_id, created_at) VALUES (?,?,?)',
        (token, user_id, _today())
    )
    conn.commit()
    return token


def _session_token_from_headers(headers):
    raw = headers.get('Cookie')
    if not raw:
        return None
    cookie = SimpleCookie()
    cookie.load(raw)
    morsel = cookie.get(SESSION_COOKIE)
    return morsel.value if morsel else None


def get_session_user(conn, headers):
    token = _session_token_from_headers(headers)
    if not token:
        return None
    row = conn.execute(
        '''SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token = ?''',
        (token,)
    ).fetchone()
    return {'id': row['id'], 'username': row['username']} if row else None


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
    }


def generator_row_to_json(row, history_rows):
    return {
        'id': row['id'],
        'customerId': row['customer_id'],
        'make': row['make'],
        'model': row['model'],
        'serial': row['serial'],
        'installDate': row['install_date'] or '',
        'lastServiceDate': row['last_service_date'] or '',
        'nextServiceDate': row['next_service_date'] or '',
        'installerName': row['installer_name'] or '',
        'notes': row['notes'] or '',
        'createdBy': row['created_by'] or '',
        'updatedBy': row['updated_by'] or '',
        'history': [
            {'date': h['date'], 'event': h['event'], 'detail': h['detail'], 'by': h['created_by'] or ''}
            for h in history_rows
        ],
    }


def fetch_generator(conn, gen_id):
    row = conn.execute('SELECT * FROM generators WHERE id=?', (gen_id,)).fetchone()
    if not row:
        return None
    hist = conn.execute(
        'SELECT * FROM generator_history WHERE generator_id=? ORDER BY id ASC', (gen_id,)
    ).fetchall()
    return generator_row_to_json(row, hist)


# --------------------------------------------------------------- API errors --

class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def require(cond, status, message):
    if not cond:
        raise ApiError(status, message)


# ------------------------------------------------------------------ routes --

CUSTOMER_FIELDS = ('name', 'email', 'phone', 'physicalAddress', 'billingAddress', 'notes')
GENERATOR_FIELDS = ('make', 'model', 'serial', 'installDate', 'lastServiceDate', 'installerName', 'notes')


def list_customers(conn, params):
    rows = conn.execute('SELECT * FROM customers ORDER BY name COLLATE NOCASE ASC').fetchall()
    return [customer_row_to_json(r) for r in rows]


def get_customer(conn, cust_id):
    row = conn.execute('SELECT * FROM customers WHERE id=?', (cust_id,)).fetchone()
    require(row, 404, 'Customer not found.')
    return customer_row_to_json(row)


def create_customer(conn, body, user):
    name = (body.get('name') or '').strip()
    require(name, 400, 'Name is required.')
    cur = conn.execute(
        '''INSERT INTO customers (name,email,phone,physical_address,billing_address,notes,created_by,updated_by)
           VALUES (?,?,?,?,?,?,?,?)''',
        (name, body.get('email', '').strip(), body.get('phone', '').strip(),
         body.get('physicalAddress', '').strip(), body.get('billingAddress', '').strip(),
         body.get('notes', '').strip(), user['username'], user['username'])
    )
    conn.commit()
    return get_customer(conn, cur.lastrowid)


def update_customer(conn, cust_id, body, user):
    row = conn.execute('SELECT * FROM customers WHERE id=?', (cust_id,)).fetchone()
    require(row, 404, 'Customer not found.')
    name = (body.get('name') or '').strip()
    require(name, 400, 'Name is required.')
    conn.execute(
        '''UPDATE customers SET name=?, email=?, phone=?, physical_address=?, billing_address=?, notes=?,
           updated_by=? WHERE id=?''',
        (name, body.get('email', '').strip(), body.get('phone', '').strip(),
         body.get('physicalAddress', '').strip(), body.get('billingAddress', '').strip(),
         body.get('notes', '').strip(), user['username'], cust_id)
    )
    conn.commit()
    return get_customer(conn, cust_id)


def delete_customer(conn, cust_id):
    row = conn.execute('SELECT * FROM customers WHERE id=?', (cust_id,)).fetchone()
    require(row, 404, 'Customer not found.')
    gen_count = conn.execute('SELECT COUNT(*) FROM generators WHERE customer_id=?', (cust_id,)).fetchone()[0]
    if gen_count > 0:
        raise ApiError(409, f'This customer has {gen_count} generator(s) on file. '
                             f'Transfer or remove them before deleting this customer.')
    conn.execute('DELETE FROM customers WHERE id=?', (cust_id,))
    conn.commit()
    return {'ok': True}


def list_generators(conn, params):
    rows = conn.execute('SELECT * FROM generators ORDER BY id ASC').fetchall()
    out = []
    for r in rows:
        hist = conn.execute(
            'SELECT * FROM generator_history WHERE generator_id=? ORDER BY id ASC', (r['id'],)
        ).fetchall()
        out.append(generator_row_to_json(r, hist))
    return out


def get_generator(conn, gen_id):
    g = fetch_generator(conn, gen_id)
    require(g, 404, 'Generator not found.')
    return g


def create_generator(conn, body, user):
    make = (body.get('make') or '').strip()
    model = (body.get('model') or '').strip()
    serial = (body.get('serial') or '').strip()
    customer_id = body.get('customerId')
    require(make and model and serial and customer_id, 400, 'Make, model, serial, and customer are required.')
    cust = conn.execute('SELECT * FROM customers WHERE id=?', (customer_id,)).fetchone()
    require(cust, 400, 'Linked customer does not exist.')
    install_date = body.get('installDate', '') or ''
    cur = conn.execute(
        '''INSERT INTO generators
           (customer_id,make,model,serial,install_date,last_service_date,installer_name,notes,created_by,updated_by)
           VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (customer_id, make, model, serial, install_date,
         body.get('lastServiceDate', '') or '', body.get('installerName', '').strip(),
         body.get('notes', '').strip(), user['username'], user['username'])
    )
    gen_id = cur.lastrowid
    conn.execute(
        'INSERT INTO generator_history (generator_id,date,event,detail,created_by) VALUES (?,?,?,?,?)',
        (gen_id, install_date or _today(), 'Added to registry', f"Linked to {cust['name']}.", user['username'])
    )
    conn.commit()
    return get_generator(conn, gen_id)


def update_generator(conn, gen_id, body, user):
    row = conn.execute('SELECT * FROM generators WHERE id=?', (gen_id,)).fetchone()
    require(row, 404, 'Generator not found.')
    make = (body.get('make') or '').strip()
    model = (body.get('model') or '').strip()
    serial = (body.get('serial') or '').strip()
    require(make and model and serial, 400, 'Make, model, and serial are required.')
    conn.execute(
        '''UPDATE generators SET make=?, model=?, serial=?, install_date=?, last_service_date=?,
           installer_name=?, notes=?, updated_by=? WHERE id=?''',
        (make, model, serial, body.get('installDate', '') or '', body.get('lastServiceDate', '') or '',
         body.get('installerName', '').strip(), body.get('notes', '').strip(), user['username'], gen_id)
    )
    conn.commit()
    return get_generator(conn, gen_id)


def delete_generator(conn, gen_id):
    row = conn.execute('SELECT * FROM generators WHERE id=?', (gen_id,)).fetchone()
    require(row, 404, 'Generator not found.')
    conn.execute('DELETE FROM generator_history WHERE generator_id=?', (gen_id,))
    conn.execute('DELETE FROM generators WHERE id=?', (gen_id,))
    conn.commit()
    return {'ok': True}


def transfer_generator(conn, gen_id, body, user):
    row = conn.execute('SELECT * FROM generators WHERE id=?', (gen_id,)).fetchone()
    require(row, 404, 'Generator not found.')
    new_customer_id = body.get('newCustomerId')
    require(new_customer_id, 400, 'A destination customer is required.')
    require(new_customer_id != row['customer_id'], 400, 'Generator is already linked to that customer.')
    from_cust = conn.execute('SELECT * FROM customers WHERE id=?', (row['customer_id'],)).fetchone()
    to_cust = conn.execute('SELECT * FROM customers WHERE id=?', (new_customer_id,)).fetchone()
    require(to_cust, 400, 'Destination customer does not exist.')
    note = (body.get('note') or '').strip()
    detail = f"Transferred from {from_cust['name'] if from_cust else 'previous owner'} to {to_cust['name']}."
    if note:
        detail += f' Note: {note}'
    conn.execute('UPDATE generators SET customer_id=?, updated_by=? WHERE id=?',
                 (new_customer_id, user['username'], gen_id))
    conn.execute(
        'INSERT INTO generator_history (generator_id,date,event,detail,created_by) VALUES (?,?,?,?,?)',
        (gen_id, _today(), 'Transferred', detail, user['username'])
    )
    conn.commit()
    return get_generator(conn, gen_id)


def add_service_record(conn, gen_id, body, user):
    row = conn.execute('SELECT * FROM generators WHERE id=?', (gen_id,)).fetchone()
    require(row, 404, 'Generator not found.')
    service_date = (body.get('serviceDate') or '').strip()
    next_service_date = (body.get('nextServiceDate') or '').strip()
    notes = (body.get('notes') or '').strip()
    require(service_date, 400, 'Service date is required.')
    detail = notes if notes else 'Routine service.'
    if next_service_date:
        detail += f' Next service scheduled for {next_service_date}.'
    conn.execute(
        'UPDATE generators SET last_service_date=?, next_service_date=?, updated_by=? WHERE id=?',
        (service_date, next_service_date, user['username'], gen_id)
    )
    conn.execute(
        'INSERT INTO generator_history (generator_id,date,event,detail,created_by) VALUES (?,?,?,?,?)',
        (gen_id, service_date, 'Serviced', detail, user['username'])
    )
    conn.commit()
    return get_generator(conn, gen_id)


def _today():
    import datetime
    return datetime.date.today().isoformat()


def login(conn, body):
    username = (body.get('username') or '').strip()
    password = body.get('password') or ''
    require(username and password, 400, 'Username and password are required.')
    row = conn.execute('SELECT * FROM users WHERE username=? COLLATE NOCASE', (username,)).fetchone()
    require(row and verify_password(password, row['password_salt'], row['password_hash']),
            401, 'Incorrect username or password.')
    token = create_session(conn, row['id'])
    return {'username': row['username']}, token


def logout(conn, headers):
    token = _session_token_from_headers(headers)
    if token:
        conn.execute('DELETE FROM sessions WHERE token=?', (token,))
        conn.commit()
    return {'ok': True}


def whoami(user):
    require(user, 401, 'Not authenticated.')
    return {'username': user['username']}


# ---------------------------------------------------------------- dispatch --

ROUTES = [
    ('GET', re.compile(r'^/api/customers$'), lambda conn, m, body, qs, user: list_customers(conn, qs)),
    ('POST', re.compile(r'^/api/customers$'), lambda conn, m, body, qs, user: create_customer(conn, body, user)),
    ('GET', re.compile(r'^/api/customers/(\d+)$'), lambda conn, m, body, qs, user: get_customer(conn, int(m.group(1)))),
    ('PUT', re.compile(r'^/api/customers/(\d+)$'), lambda conn, m, body, qs, user: update_customer(conn, int(m.group(1)), body, user)),
    ('DELETE', re.compile(r'^/api/customers/(\d+)$'), lambda conn, m, body, qs, user: delete_customer(conn, int(m.group(1)))),

    ('GET', re.compile(r'^/api/generators$'), lambda conn, m, body, qs, user: list_generators(conn, qs)),
    ('POST', re.compile(r'^/api/generators$'), lambda conn, m, body, qs, user: create_generator(conn, body, user)),
    ('GET', re.compile(r'^/api/generators/(\d+)$'), lambda conn, m, body, qs, user: get_generator(conn, int(m.group(1)))),
    ('PUT', re.compile(r'^/api/generators/(\d+)$'), lambda conn, m, body, qs, user: update_generator(conn, int(m.group(1)), body, user)),
    ('DELETE', re.compile(r'^/api/generators/(\d+)$'), lambda conn, m, body, qs, user: delete_generator(conn, int(m.group(1)))),
    ('POST', re.compile(r'^/api/generators/(\d+)/transfer$'), lambda conn, m, body, qs, user: transfer_generator(conn, int(m.group(1)), body, user)),
    ('POST', re.compile(r'^/api/generators/(\d+)/service-records$'), lambda conn, m, body, qs, user: add_service_record(conn, int(m.group(1)), body, user)),
]


class Handler(BaseHTTPRequestHandler):
    server_version = 'WattWatch/1.0'

    def _send_json(self, status, payload, extra_headers=None):
        data = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        for name, value in (extra_headers or []):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _send_html_file(self, path):
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError:
            self._send_json(404, {'error': 'Not found.'})
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode('utf-8'))
        except json.JSONDecodeError:
            raise ApiError(400, 'Malformed JSON body.')

    def _dispatch(self, method):
        path = self.path.split('?', 1)[0]
        if method == 'GET' and path in ('/', '/generator-manager.html'):
            self._send_html_file(STATIC_FILE)
            return

        if method == 'POST' and path == '/api/login':
            conn = get_conn()
            try:
                payload, token = login(conn, self._read_body())
                self._send_json(200, payload, extra_headers=[
                    ('Set-Cookie', f'{SESSION_COOKIE}={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age=2592000')
                ])
            except ApiError as e:
                self._send_json(e.status, {'error': e.message})
            except Exception as e:
                self._send_json(500, {'error': str(e)})
            finally:
                conn.close()
            return

        if method == 'POST' and path == '/api/logout':
            conn = get_conn()
            try:
                result = logout(conn, self.headers)
                self._send_json(200, result, extra_headers=[
                    ('Set-Cookie', f'{SESSION_COOKIE}=; HttpOnly; Path=/; SameSite=Lax; Max-Age=0')
                ])
            except Exception as e:
                self._send_json(500, {'error': str(e)})
            finally:
                conn.close()
            return

        if method == 'GET' and path == '/api/me':
            conn = get_conn()
            try:
                result = whoami(get_session_user(conn, self.headers))
                self._send_json(200, result)
            except ApiError as e:
                self._send_json(e.status, {'error': e.message})
            finally:
                conn.close()
            return

        for m, pattern, fn in ROUTES:
            if m != method:
                continue
            match = pattern.match(path)
            if not match:
                continue
            try:
                body = self._read_body() if method in ('POST', 'PUT') else {}
                conn = get_conn()
                try:
                    user = get_session_user(conn, self.headers)
                    require(user, 401, 'Not authenticated.')
                    result = fn(conn, match, body, None, user)
                finally:
                    conn.close()
                self._send_json(200, result)
            except ApiError as e:
                self._send_json(e.status, {'error': e.message})
            except Exception as e:
                self._send_json(500, {'error': str(e)})
            return
        self._send_json(404, {'error': 'Not found.'})

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
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f'Watt Watch running at http://localhost:{PORT}/')
    print(f'Database file: {DB_PATH}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nShutting down.')
        server.shutdown()


if __name__ == '__main__':
    main()
