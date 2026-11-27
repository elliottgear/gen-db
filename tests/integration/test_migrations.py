"""Upgrading older databases. Migrations are forward-only by design (no down migrations)."""
import sqlite3

import pytest

import server

# The schema as it existed before versioned migrations (roughly the July 2026 database).
PRE_VERSIONING_SCHEMA = '''
    CREATE TABLE customers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT, phone TEXT,
                            physical_address TEXT, billing_address TEXT, notes TEXT);
    CREATE TABLE generators (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL REFERENCES customers(id),
                             make TEXT NOT NULL, model TEXT NOT NULL, serial TEXT NOT NULL, install_date TEXT,
                             last_service_date TEXT, installer_name TEXT, notes TEXT);
    CREATE TABLE generator_history (id INTEGER PRIMARY KEY AUTOINCREMENT, generator_id INTEGER NOT NULL,
                                    date TEXT, event TEXT, detail TEXT);
    CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                        password_hash TEXT NOT NULL, password_salt TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE sessions (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at TEXT NOT NULL);
    INSERT INTO customers (name) VALUES ('Marlene Ouellette');
    INSERT INTO generators (customer_id, make, model, serial, last_service_date) VALUES (1, 'Generac', 'G22', 'S1', '2025-04-02');
    INSERT INTO generator_history (generator_id, date, event, detail) VALUES (1, '2021-05-14', 'Installed', 'x');
    INSERT INTO users (username, password_hash, password_salt, created_at) VALUES ('tech1', 'h', 's', '2026-07-18');
    INSERT INTO sessions VALUES ('leaked-token', 1, '2026-07-18');
'''


@pytest.fixture
def old_db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / 'old.sqlite3'))
    conn.row_factory = sqlite3.Row
    conn.executescript(PRE_VERSIONING_SCHEMA)
    yield conn
    conn.close()


def cols(conn, table):
    return {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}


def test_brings_a_pre_versioning_database_to_the_latest_version(old_db):
    server._migrate(old_db)
    assert old_db.execute('PRAGMA user_version').fetchone()[0] == len(server.MIGRATIONS)
    assert {'next_service_date', 'physical_address', 'created_by', 'updated_by', 'updated_at'} <= cols(old_db, 'generators')
    assert {'created_by', 'updated_by', 'updated_at'} <= cols(old_db, 'customers')
    assert {'created_by', 'invoiced', 'invoiced_by', 'invoiced_at'} <= cols(old_db, 'generator_history')
    assert 'role' in cols(old_db, 'users')


def test_existing_data_survives(old_db):
    server._migrate(old_db)
    gen = old_db.execute('SELECT * FROM generators').fetchone()
    assert (gen['serial'], gen['last_service_date'], gen['next_service_date']) == ('S1', '2025-04-02', None)
    assert old_db.execute('SELECT invoiced FROM generator_history').fetchone()[0] == 0


def test_existing_users_become_technicians(old_db):
    server._migrate(old_db)
    assert old_db.execute("SELECT role FROM users WHERE username='tech1'").fetchone()[0] == 'technician'


def test_every_pre_existing_session_is_revoked(old_db):
    server._migrate(old_db)
    assert old_db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0


def test_running_again_is_a_no_op(old_db):
    server._migrate(old_db)
    old_db.execute("INSERT INTO sessions VALUES ('new-token', 1, datetime('now'))")
    old_db.commit()
    server._migrate(old_db)
    assert old_db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 1  # migration 3 didn't re-run


def test_only_pending_migrations_run(old_db):
    server._migrate(old_db)
    old_db.execute('PRAGMA user_version = 3')
    old_db.execute("INSERT INTO sessions VALUES ('kept', 1, datetime('now'))")
    old_db.commit()
    server._migrate(old_db)
    assert old_db.execute('PRAGMA user_version').fetchone()[0] == len(server.MIGRATIONS)
    assert old_db.execute("SELECT COUNT(*) FROM sessions WHERE token='kept'").fetchone()[0] == 1


def test_column_migrations_tolerate_columns_that_already_exist(old_db):
    old_db.execute('ALTER TABLE generators ADD COLUMN next_service_date TEXT')  # added by hand, pre-versioning
    server._migrate(old_db)
    assert 'next_service_date' in cols(old_db, 'generators')


def test_migration_versions_are_contiguous():
    assert server.MIGRATIONS == [getattr(server, f'_migration_{i}') for i in range(1, len(server.MIGRATIONS) + 1)]


def test_migration_6_adds_appointment_requests_to_a_version_5_database(tmp_path):
    conn = sqlite3.connect(str(tmp_path / 'v5.sqlite3'))
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE generators (id INTEGER PRIMARY KEY, customer_id INTEGER);
        INSERT INTO customers VALUES (1, 'Marlene');
        PRAGMA user_version = 5;
    ''')
    server._migrate(conn)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == len(server.MIGRATIONS)
    assert {'status', 'phone_digits', 'dismiss_reason', 'customer_id', 'generator_id', 'updated_at'} <= cols(
        conn, 'appointment_requests')
    assert conn.execute('SELECT name FROM customers').fetchone()[0] == 'Marlene'
    with pytest.raises(sqlite3.IntegrityError):  # statuses and needs are constrained in the schema
        conn.execute("INSERT INTO appointment_requests (created_at, status, name, phone, phone_digits, email, "
                     "service_street, brand, need) VALUES ('x', 'archived', 'n', 'p', 'd', 'e', 'a', 'b', 'repair')")
    conn.close()


V6_REQUESTS = '''
    CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT);
    CREATE TABLE generators (id INTEGER PRIMARY KEY, customer_id INTEGER);
    INSERT INTO customers VALUES (7, 'Marlene');
    PRAGMA user_version = 5;
'''


def test_migration_7_turns_done_into_scheduled_and_keeps_every_request(tmp_path):
    conn = sqlite3.connect(str(tmp_path / 'v6.sqlite3'))
    conn.row_factory = sqlite3.Row
    conn.executescript(V6_REQUESTS)
    server._migration_6(conn)
    conn.execute('PRAGMA user_version = 6')
    rows = [(3, 'new', None), (5, 'done', 7), (9, 'dismissed', None), (11, 'matched', 7)]
    for id_, status, cust in rows:
        conn.execute("INSERT INTO appointment_requests (id, created_at, status, name, phone, phone_digits, email, "
                     "service_address, brand, need, customer_id, dismiss_reason) "
                     "VALUES (?, '2026-11-20', ?, 'Pat', 'p', '2075550100', 'e', ?, 'Generac', 'repair', ?, ?)",
                     (id_, status, f'{id_} Main St, Saco', cust, 'spam filter (honeypot)' if status == 'dismissed' else ''))
    conn.commit()

    server._migrate(conn)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == len(server.MIGRATIONS)
    got = [tuple(r) for r in conn.execute(
        'SELECT id, status, customer_id, service_street, service_town, dismiss_reason FROM appointment_requests ORDER BY id')]
    assert got == [
        (3, 'new', None, '3 Main St, Saco', '', ''),
        (5, 'scheduled', 7, '5 Main St, Saco', '', ''),
        (9, 'dismissed', None, '9 Main St, Saco', '', 'spam filter (honeypot)'),
        (11, 'matched', 7, '11 Main St, Saco', '', ''),
    ]
    assert 'service_address' not in cols(conn, 'appointment_requests')
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='idx_appointment_requests_status'").fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE appointment_requests SET status='done' WHERE id=3")
    # New ids continue after the old ones.
    conn.execute("INSERT INTO appointment_requests (created_at, name, phone, phone_digits, email, service_street, "
                 "brand, need) VALUES ('x', 'n', 'p', 'd', 'e', 's', 'b', 'repair')")
    assert conn.execute('SELECT MAX(id) FROM appointment_requests').fetchone()[0] == 12
    conn.close()


def test_migration_7_is_safe_to_run_twice(tmp_path):
    conn = sqlite3.connect(str(tmp_path / 'twice.sqlite3'))
    conn.row_factory = sqlite3.Row
    conn.executescript(V6_REQUESTS)
    server._migrate(conn)
    server._migration_7(conn)  # e.g. a crash after the rebuild, before user_version was saved
    assert 'service_street' in cols(conn, 'appointment_requests')
    conn.close()


def test_migration_8_adds_the_zip_and_keeps_existing_requests(tmp_path):
    conn = sqlite3.connect(str(tmp_path / 'v7.sqlite3'))
    conn.row_factory = sqlite3.Row
    conn.executescript(V6_REQUESTS)
    server._migration_6(conn)
    server._migration_7(conn)
    conn.execute("INSERT INTO appointment_requests (created_at, name, phone, phone_digits, email, service_street, "
                 "service_town, brand, need) VALUES ('t', 'Pat', 'p', 'd', 'e', '12 Main St', 'Saco', 'Kohler', 'repair')")
    conn.execute('PRAGMA user_version = 7')
    server._migrate(conn)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == len(server.MIGRATIONS)
    assert 'service_zip' in cols(conn, 'appointment_requests')
    assert tuple(conn.execute('SELECT name, service_town, service_zip FROM appointment_requests').fetchone()) == \
        ('Pat', 'Saco', '')
    conn.close()
