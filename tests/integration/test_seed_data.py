"""The demo data (server.seed and seed_batch.py) must satisfy the schema and the app's own rules."""
import datetime

import pytest

import seed_batch
import server

TODAY = datetime.date(2026, 7, 4)


def is_iso_date(value):
    try:
        datetime.date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


@pytest.fixture
def batch_db(db, seeded_random):
    before = db.execute('SELECT COUNT(*) FROM customers').fetchone()[0]
    count = seed_batch.insert_batch(db, today=TODAY)
    return db, before, count


class TestBuiltInSeed:
    def test_runs_only_on_an_empty_database(self, db):
        assert db.execute('SELECT COUNT(*) FROM customers').fetchone()[0] == 3
        assert db.execute('SELECT COUNT(*) FROM generators').fetchone()[0] == 3

    def test_seeded_generators_have_valid_dates_and_an_install_history(self, db):
        for g in db.execute('SELECT * FROM generators'):
            assert is_iso_date(g['install_date']) and is_iso_date(g['last_service_date'])
            events = [h['event'] for h in db.execute('SELECT event FROM generator_history WHERE generator_id=?', (g['id'],))]
            assert events == ['Installed']


class TestSeedBatch:
    def test_inserts_a_batch_of_customers_each_with_1_to_5_generators(self, batch_db):
        db, before, count = batch_db
        assert db.execute('SELECT COUNT(*) FROM customers').fetchone()[0] == before + seed_batch.BATCH_SIZE
        per_customer = [r[0] for r in db.execute(
            "SELECT COUNT(g.id) FROM customers c LEFT JOIN generators g ON g.customer_id=c.id "
            "WHERE c.created_by=? GROUP BY c.id", (seed_batch.CREATED_BY,))]
        assert len(per_customer) == seed_batch.BATCH_SIZE
        assert all(1 <= n <= 5 for n in per_customer)
        assert sum(per_customer) == count

    def test_is_deterministic_for_a_given_seed_and_date(self, db, tmp_path, monkeypatch, seeded_random):
        def snapshot(path):
            monkeypatch.setattr(server, 'DB_PATH', str(path))
            server.init_db()
            conn = server.get_conn()
            seeded_random.seed(99)
            seed_batch.insert_batch(conn, today=TODAY)
            rows = [tuple(r) for r in conn.execute(
                'SELECT make, model, serial, install_date, last_service_date, next_service_date FROM generators ORDER BY id')]
            conn.close()
            return rows
        assert snapshot(tmp_path / 'a.sqlite3') == snapshot(tmp_path / 'b.sqlite3')

    def test_serials_are_unique_per_make(self, batch_db):
        db, _, _ = batch_db
        assert db.execute('SELECT COUNT(*) FROM (SELECT 1 FROM generators GROUP BY make COLLATE NOCASE, '
                          'serial COLLATE NOCASE HAVING COUNT(*) > 1)').fetchone()[0] == 0

    def test_dates_are_valid_and_in_order(self, batch_db):
        db, _, _ = batch_db
        for g in db.execute('SELECT * FROM generators WHERE created_by=?', (seed_batch.CREATED_BY,)):
            assert is_iso_date(g['install_date'])
            assert g['install_date'] <= TODAY.isoformat()
            if g['last_service_date']:
                assert is_iso_date(g['last_service_date']) and is_iso_date(g['next_service_date'])
                assert g['install_date'] < g['last_service_date'] <= TODAY.isoformat()
            else:
                assert g['next_service_date'] == ''

    def test_next_service_is_after_last_service(self, batch_db):
        db, _, _ = batch_db
        bad = db.execute("SELECT COUNT(*) FROM generators WHERE created_by=? AND last_service_date != '' "
                         "AND next_service_date <= last_service_date", (seed_batch.CREATED_BY,)).fetchone()[0]
        assert bad == 0

    def test_includes_overdue_due_soon_and_up_to_date_units(self, batch_db):
        db, _, _ = batch_db
        today = TODAY.isoformat()
        soon = (TODAY + datetime.timedelta(days=60)).isoformat()
        nexts = [r[0] for r in db.execute("SELECT next_service_date FROM generators WHERE created_by=? "
                                          "AND next_service_date != ''", (seed_batch.CREATED_BY,))]
        assert any(n < today for n in nexts)
        assert any(today <= n <= soon for n in nexts)
        assert any(n > soon for n in nexts)

    def test_text_fields_fit_the_api_limits(self, batch_db):
        db, _, _ = batch_db
        for c in db.execute('SELECT * FROM customers WHERE created_by=?', (seed_batch.CREATED_BY,)):
            assert c['name'] and len(c['name']) <= server.SHORT_TEXT
            assert len(c['email']) <= server.SHORT_TEXT and '@' in c['email']
            assert len(c['physical_address']) <= server.LONG_TEXT

    def test_every_generator_has_an_install_history_entry(self, batch_db):
        db, _, _ = batch_db
        missing = db.execute("SELECT COUNT(*) FROM generators g WHERE created_by=? AND NOT EXISTS "
                             "(SELECT 1 FROM generator_history h WHERE h.generator_id=g.id AND h.event='Installed')",
                             (seed_batch.CREATED_BY,)).fetchone()[0]
        assert missing == 0
