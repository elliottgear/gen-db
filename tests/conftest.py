"""
Shared fixtures. Every test gets its own temp database and uploads folder; nothing
touches gen_db.sqlite3 or ./uploads.
"""
import datetime
import os
import random
import sys
import tempfile
import threading

import pytest

# Point the defaults somewhere harmless *before* server is imported, so even an accidental
# get_conn() outside the fixtures can't reach the real database.
_SAFETY_DIR = tempfile.mkdtemp(prefix='wattwatch-safety-')
os.environ['DB_PATH'] = os.path.join(_SAFETY_DIR, 'never-use.sqlite3')
os.environ['UPLOADS_DIR'] = os.path.join(_SAFETY_DIR, 'uploads')
os.environ.pop('PORT', None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
from tests.helpers import Client  # noqa: E402

REAL_PBKDF2_ITERATIONS = server.PBKDF2_ITERATIONS
PASSWORD = 'correct horse battery'

# A fixed instant: 8:30pm EDT on July 4th 2026, i.e. already July 5th in UTC. Catches UTC-vs-Maine mixups.
FROZEN_UTC = datetime.datetime(2026, 7, 5, 0, 30, tzinfo=datetime.timezone.utc)
FROZEN_MAINE_DATE = '2026-07-04'


@pytest.fixture(autouse=True)
def isolated_app_state(tmp_path, monkeypatch):
    uploads = tmp_path / 'uploads'
    uploads.mkdir()
    monkeypatch.setattr(server, 'DB_PATH', str(tmp_path / 'test.sqlite3'))
    monkeypatch.setattr(server, 'UPLOADS_DIR', str(uploads))
    monkeypatch.setattr(server, 'PBKDF2_ITERATIONS', 1000)  # real value is slow by design
    server._login_failures.clear()
    yield
    server._login_failures.clear()


@pytest.fixture
def uploads_dir():
    return server.UPLOADS_DIR


@pytest.fixture
def db():
    """An initialised (migrated + demo-seeded) database connection."""
    server.init_db()
    conn = server.get_conn()
    yield conn
    conn.close()


@pytest.fixture
def make_user(db):
    def make(username='tech', role='technician', password=PASSWORD):
        salt, digest = server.hash_password(password)
        cur = db.execute('INSERT INTO users (username,password_hash,password_salt,created_at,role) VALUES (?,?,?,?,?)',
                         (username, digest, salt, '2026-01-01', role))
        db.commit()
        return cur.lastrowid
    return make


@pytest.fixture
def app(db):
    """A real server on a random localhost port, backed by this test's database."""
    httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()
    thread.join()


@pytest.fixture
def client(app):
    """Factory for HTTP clients; pass a username to get one that's already signed in."""
    def make(username=None, password=PASSWORD):
        c = Client(app)
        if username:
            c.login(username, password)
        return c
    return make


@pytest.fixture
def tech(make_user, client):
    make_user('tech', 'technician')
    return client('tech')


@pytest.fixture
def admin(make_user, client):
    make_user('boss', 'admin')
    return client('boss')


@pytest.fixture
def make_customer(tech):
    def make(c=None, **fields):
        body = {'name': 'Test Customer'}
        body.update(fields)
        status, cust = (c or tech).json('POST', '/api/customers', body)
        assert status == 200, cust
        return cust
    return make


_serials = iter(range(10**6))


@pytest.fixture
def make_generator(tech, make_customer):
    def make(c=None, customer_id=None, **fields):
        body = {'customerId': customer_id or make_customer(c)['id'], 'make': 'Generac', 'model': 'Guardian 22kW',
                'serial': f'TEST-{next(_serials):06d}'}
        body.update(fields)
        status, gen = (c or tech).json('POST', '/api/generators', body)
        assert status == 200, gen
        return gen
    return make


@pytest.fixture
def serviced_generator(tech, make_generator):
    """A generator with one 'Serviced' history entry; returns (generator, history_id)."""
    gen = make_generator()
    status, gen = tech.json('POST', f"/api/generators/{gen['id']}/service-records",
                            {'serviceDate': '2026-06-01', 'nextServiceDate': '2027-06-01'})
    assert status == 200, gen
    return gen, gen['history'][-1]['id']


class _FrozenDatetime(datetime.datetime):
    frozen = FROZEN_UTC

    @classmethod
    def now(cls, tz=None):
        return cls.frozen.astimezone(tz) if tz else cls.frozen.astimezone().replace(tzinfo=None)


@pytest.fixture
def frozen_clock(monkeypatch):
    """Freezes datetime.datetime.now() (which server._local_now uses). Set .frozen to move the clock."""
    monkeypatch.setattr(datetime, 'datetime', _FrozenDatetime)
    _FrozenDatetime.frozen = FROZEN_UTC
    yield _FrozenDatetime
    _FrozenDatetime.frozen = FROZEN_UTC


@pytest.fixture
def seeded_random():
    state = random.getstate()
    random.seed(20260704)
    yield random
    random.setstate(state)
