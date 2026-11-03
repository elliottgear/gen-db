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
