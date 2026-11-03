"""Schema creation and the constraints the database itself enforces."""
import sqlite3

import pytest

import server

TABLES = {'customers', 'generators', 'generator_history', 'generator_attachments', 'users', 'sessions', 'audit_log'}


def columns(conn, table):
    return {r['name']: r for r in conn.execute(f'PRAGMA table_info({table})')}


def insert_customer(conn, name='C'):
    return conn.execute('INSERT INTO customers (name) VALUES (?)', (name,)).lastrowid


def insert_generator(conn, customer_id):
    return conn.execute('INSERT INTO generators (customer_id, make, model, serial) VALUES (?,?,?,?)',
                        (customer_id, 'M', 'X', 'S')).lastrowid


class TestFreshDatabase:
    def test_creates_every_table(self, db):
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert TABLES <= names

    def test_is_at_the_latest_migration(self, db):
        assert db.execute('PRAGMA user_version').fetchone()[0] == len(server.MIGRATIONS)

    def test_has_every_migrated_column(self, db):
        assert {'next_service_date', 'physical_address', 'created_by', 'updated_by', 'updated_at'} <= set(columns(db, 'generators'))
        assert {'created_by', 'updated_by', 'updated_at'} <= set(columns(db, 'customers'))
        assert {'created_by', 'invoiced', 'invoiced_by', 'invoiced_at'} <= set(columns(db, 'generator_history'))
        assert columns(db, 'users')['role']['dflt_value'] == "'technician'"

    def test_uses_wal_journaling(self, db):
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'

    def test_connections_enforce_foreign_keys(self, db):
        assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1

    def test_init_db_is_idempotent(self, db):
        server.init_db()
        server.init_db()
        assert db.execute('SELECT COUNT(*) FROM customers').fetchone()[0] == 3  # seeded once only

    def test_init_db_removes_leftover_partial_uploads(self, uploads_dir):
        import os
        open(os.path.join(uploads_dir, '.incoming-crashed'), 'wb').close()
        open(os.path.join(uploads_dir, 'keep.png'), 'wb').close()
        server.init_db()
        assert os.listdir(uploads_dir) == ['keep.png']


class TestConstraints:
    def test_generator_must_reference_an_existing_customer(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            insert_generator(db, 999999)

    def test_customer_with_generators_cannot_be_deleted_at_db_level(self, db):
        gen_cust = insert_customer(db)
        insert_generator(db, gen_cust)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('DELETE FROM customers WHERE id=?', (gen_cust,))

    def test_deleting_a_generator_cascades_to_history_and_attachments(self, db):
        gen_id = insert_generator(db, insert_customer(db))
        db.execute("INSERT INTO generator_history (generator_id, event) VALUES (?, 'Serviced')", (gen_id,))
        db.execute('''INSERT INTO generator_attachments (generator_id,original_filename,stored_filename,content_type,
                      kind,size_bytes,uploaded_at) VALUES (?,?,?,?,?,?,?)''', (gen_id, 'a', 'b', 'c', 'd', 1, 'e'))
        db.execute('DELETE FROM generators WHERE id=?', (gen_id,))
        for table in ('generator_history', 'generator_attachments'):
            assert db.execute(f'SELECT COUNT(*) FROM {table} WHERE generator_id=?', (gen_id,)).fetchone()[0] == 0

    def test_deleting_a_user_cascades_to_sessions(self, db, make_user):
        user_id = make_user('gone')
        server.create_session(db, user_id)
        db.execute('DELETE FROM users WHERE id=?', (user_id,))
        assert db.execute('SELECT COUNT(*) FROM sessions WHERE user_id=?', (user_id,)).fetchone()[0] == 0

    @pytest.mark.parametrize('sql', [
        'INSERT INTO customers (name) VALUES (NULL)',
        "INSERT INTO generators (customer_id, make, model, serial) VALUES (1, NULL, 'X', 'S')",
        "INSERT INTO generators (customer_id, make, model, serial) VALUES (1, 'M', 'X', NULL)",
        "INSERT INTO users (username, password_hash, password_salt, created_at) VALUES ('u', NULL, 's', 'x')",
        "INSERT INTO users (username, password_hash, password_salt, created_at, role) VALUES ('u', 'h', 's', 'x', NULL)",
        "INSERT INTO audit_log (at, action, entity) VALUES ('x', NULL, 'e')",
    ])
    def test_required_columns_are_not_null(self, db, sql):
        with pytest.raises(sqlite3.IntegrityError, match='NOT NULL'):
            db.execute(sql)

    def test_usernames_are_unique_ignoring_case(self, db, make_user):
        make_user('Tech1')
        with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
            make_user('tech1')

    def test_session_tokens_are_unique(self, db, make_user):
        user_id = make_user()
        db.execute("INSERT INTO sessions VALUES ('t', ?, '2026-01-01')", (user_id,))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO sessions VALUES ('t', ?, '2026-01-01')", (user_id,))


class TestAuditLog:
    def test_records_actor_action_and_maine_timestamp(self, db, frozen_clock):
        server.audit(db, {'username': 'boss'}, 'delete', 'customer', 7, '{"name": "x"}')
        row = db.execute('SELECT * FROM audit_log').fetchone()
        assert (row['username'], row['action'], row['entity'], row['entity_id']) == ('boss', 'delete', 'customer', 7)
        assert row['at'].startswith('2026-07-04T20:30:00')

    def test_actor_may_be_unknown(self, db):
        server.audit(db, None, 'delete', 'customer', 1)
        assert db.execute('SELECT username FROM audit_log').fetchone()[0] is None
