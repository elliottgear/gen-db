"""Email data against a real database: recipients, digest/hit-list queries, scheduled runs, the email log."""
import datetime
import sqlite3

import pytest

import notifications
import server

TODAY = datetime.date(2026, 7, 6)  # a Monday


def iso(days):
    return (TODAY + datetime.timedelta(days=days)).isoformat()


@pytest.fixture
def add_unit(db):
    def add(customer='Cust', last='', next_='', phone='(207) 555-0100', site='1 Main St\nPortland, ME', serial=None):
        cid = db.execute('INSERT INTO customers (name, phone) VALUES (?, ?)', (customer, phone)).lastrowid
        gid = db.execute('''INSERT INTO generators (customer_id, make, model, serial, physical_address,
                            last_service_date, next_service_date) VALUES (?,?,?,?,?,?,?)''',
                         (cid, 'Generac', 'G22', serial or f'S-{customer}-{last}-{next_}', site, last, next_)).lastrowid
        db.commit()
        return cid, gid
    return add


@pytest.fixture
def empty_fleet(db):
    db.execute('DELETE FROM generator_history')
    db.execute('DELETE FROM generators')
    db.commit()


class TestMigration5:
    def test_adds_email_tables_and_columns(self, db):
        assert 'email' in {r['name'] for r in db.execute('PRAGMA table_info(users)')}
        assert 'created_at' in {r['name'] for r in db.execute('PRAGMA table_info(generator_history)')}
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {'user_notifications', 'email_log', 'scheduled_runs'} <= tables

    def test_upgrades_a_version_4_database_without_touching_sessions(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / 'v4.sqlite3'))
        conn.row_factory = sqlite3.Row
        conn.executescript('''
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, role TEXT);
            CREATE TABLE generator_history (id INTEGER PRIMARY KEY, generator_id INTEGER, event TEXT);
            CREATE TABLE sessions (token TEXT PRIMARY KEY, user_id INTEGER, created_at TEXT);
            INSERT INTO users VALUES (1, 'betty', 'admin');
            INSERT INTO sessions VALUES ('live', 1, datetime('now'));
            PRAGMA user_version = 4;
        ''')
        server._migrate(conn)
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 5
        assert conn.execute('SELECT email FROM users').fetchone()[0] is None
        assert conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 1
        conn.close()

    def test_notification_choices_go_when_the_user_does(self, db, make_user):
        uid = make_user('betty', 'admin', email='b@example.com', notify=['hit_list'])
        db.execute('DELETE FROM users WHERE id=?', (uid,))
        assert db.execute('SELECT COUNT(*) FROM user_notifications').fetchone()[0] == 0


class TestRecipients:
    def test_only_opted_in_admins_with_an_email(self, db, make_user):
        make_user('betty', 'admin', email='betty@example.com', notify=['hit_list', 'weekly_digest'])
        make_user('ann', 'admin', email='ann@example.com', notify=['weekly_digest'])
        make_user('noemail', 'admin', notify=['hit_list'])
        make_user('tech', 'technician', email='tech@example.com', notify=['hit_list'])  # e.g. a demoted admin
        assert [r['email'] for r in server.notification_recipients(db, 'hit_list')] == ['betty@example.com']
        assert [r['username'] for r in server.notification_recipients(db, 'weekly_digest')] == ['ann', 'betty']

    def test_promotion_takes_effect_immediately(self, db, make_user):
        make_user('tech', 'technician', email='tech@example.com', notify=['hit_list'])
        db.execute("UPDATE users SET role='admin' WHERE username='tech'")
        assert len(server.notification_recipients(db, 'hit_list')) == 1


class TestHitList:
    def test_sections_due_rule_and_window(self, db, empty_fleet, add_unit):
        add_unit('Overdue Next', last=iso(-400), next_=iso(-5))
        add_unit('Overdue Last Only', last=iso(-366))            # due a year after last service
        add_unit('Due Today', last=iso(-300), next_=iso(0))
        add_unit('Due Day 30', last=iso(-300), next_=iso(30))
        add_unit('Due Day 31', last=iso(-300), next_=iso(31))
        add_unit('Never Serviced', next_=iso(3))
        add_unit('Fine', last=iso(-10), next_=iso(300))
        overdue, soon = notifications.hit_list(db, TODAY)
        assert [g['customer'] for g in overdue] == ['Overdue Next', 'Overdue Last Only']
        assert [g['customer'] for g in soon] == ['Due Today', 'Due Day 30']
        assert overdue[0]['units'][0]['days'] == -5

    def test_groups_units_by_customer_soonest_first(self, db, empty_fleet, add_unit):
        cid, _ = add_unit('Harborview', last=iso(-300), next_=iso(20), serial='B')
        db.execute('''INSERT INTO generators (customer_id, make, model, serial, last_service_date, next_service_date)
                      VALUES (?, 'Kohler', '14RESAL', 'A', ?, ?)''', (cid, iso(-300), iso(4)))
        add_unit('Alpha', last=iso(-300), next_=iso(10))
        _, soon = notifications.hit_list(db, TODAY)
        assert [g['customer'] for g in soon] == ['Harborview', 'Alpha']  # Harborview's earliest unit is sooner
        assert [u['due'] for u in soon[0]['units']] == [iso(4), iso(20)]
        assert soon[0]['phone'] == '(207) 555-0100' and soon[0]['units'][0]['site'] == ''
        assert soon[1]['units'][0]['site'] == '1 Main St, Portland, ME'

    def test_ignores_unparseable_legacy_dates(self, db, empty_fleet, add_unit):
        add_unit('Legacy', last='July 2024')
        assert notifications.hit_list(db, TODAY) == ([], [])


class TestWeeklyActivity:
    START, END = '2026-06-26T16:00:00.000000-04:00', '2026-07-03T16:00:00.000000-04:00'

    def history(self, db, gid, event, created_at, detail='d', invoiced=0):
        db.execute('''INSERT INTO generator_history (generator_id, date, event, detail, created_by, created_at, invoiced)
                      VALUES (?, '2026-07-01', ?, ?, 'tech', ?, ?)''', (gid, event, detail, created_at, invoiced))
        db.commit()

    def test_window_is_start_inclusive_end_exclusive(self, db, empty_fleet, add_unit):
        _, gid = add_unit('C', last=iso(-1))
        self.history(db, gid, 'Serviced', '2026-06-26T15:59:59.999999-04:00', 'before')
        self.history(db, gid, 'Serviced', self.START, 'first')
        self.history(db, gid, 'Serviced', '2026-07-03T15:59:59.000000-04:00', 'last')
        self.history(db, gid, 'Serviced', self.END, 'after')
        self.history(db, gid, 'Serviced', None, 'logged before created_at existed')
        got = notifications.weekly_activity(db, self.START, self.END)
        assert [s['detail'] for s in got['serviced']] == ['first', 'last']

    def test_buckets_events_and_reads_the_audit_log(self, db, empty_fleet, add_unit):
        _, gid = add_unit('C')
        at = '2026-07-01T10:00:00.000000-04:00'
        for event in ('Serviced', 'Added to registry', 'Transferred', 'Service dates edited', 'Installed'):
            self.history(db, gid, event, at)
        server.audit(db, {'username': 'boss'}, 'delete', 'customer', 9, '{"name": "Old Co"}')
        server.audit(db, {'username': 'boss'}, 'delete', 'generator', 8, '{"make": "Kohler", "model": "X", "serial": "K1"}')
        server.audit(db, {'username': 'boss'}, 'delete', 'attachment', 7, 'not json')
        server.audit(db, {'username': 'boss'}, 'invoice', 'service_record', 1)
        db.execute("UPDATE audit_log SET at=?", (at,))
        db.commit()
        got = notifications.weekly_activity(db, self.START, self.END)
        assert [len(got[k]) for k in ('serviced', 'added', 'transferred', 'dates_edited')] == [1, 1, 1, 1]
        assert [d['label'] for d in got['deleted']] == ['Old Co', 'Kohler X (S/N K1)', '#7']
        assert got['invoiced_count'] == 1

    def test_counts_every_service_record_not_yet_invoiced(self, db, empty_fleet, add_unit):
        _, gid = add_unit('C')
        self.history(db, gid, 'Serviced', None, invoiced=0)
        self.history(db, gid, 'Serviced', None, invoiced=1)
        assert notifications.weekly_activity(db, self.START, self.END)['not_invoiced_total'] == 1


class TestDeliver:
    def test_logs_a_sent_email(self, db, smtp2go):
        assert server.deliver(db, 'test', 'a@example.com', 'Subj', 't', 'h') is None
        row = db.execute('SELECT * FROM email_log').fetchone()
        assert (row['kind'], row['recipient'], row['subject'], row['status'], row['error']) == \
            ('test', 'a@example.com', 'Subj', 'sent', None)

    def test_logs_a_failure_instead_of_raising(self, db, smtp2go):
        smtp2go.reply = (401, {'data': {'error': 'Invalid API key'}})
        assert 'Invalid API key' in server.deliver(db, 'test', 'a@example.com', 'Subj', 't', 'h')
        assert db.execute('SELECT status FROM email_log').fetchone()[0] == 'failed'

    def test_unconfigured_is_logged_as_skipped(self, db):
        assert server.deliver(db, 'test', 'a@example.com', 'Subj', 't', 'h')
        assert db.execute('SELECT status FROM email_log').fetchone()[0] == 'skipped'


class TestRunDueJobs:
    @pytest.fixture
    def subscribers(self, make_user):
        make_user('betty', 'admin', email='betty@example.com', notify=['weekly_digest', 'hit_list'])
        make_user('ann', 'admin', email='ann@example.com', notify=['hit_list'])

    def at(self, y, m, d, hh, mm=0):
        return server._localize(datetime.datetime(y, m, d, hh, mm))

    def test_friday_digest_goes_to_subscribers_once(self, db, subscribers, smtp2go):
        friday = self.at(2026, 7, 3, 16, 1)
        assert server.run_due_jobs(friday) == ['weekly_digest']
        assert [to for to, _ in smtp2go.sent] == ['betty@example.com']
        assert smtp2go.sent[0][1].startswith('Weekly digest:')
        assert server.run_due_jobs(friday + datetime.timedelta(minutes=5)) == []
        assert len(smtp2go.requests) == 1

    def test_monday_hit_list(self, db, subscribers, smtp2go):
        assert server.run_due_jobs(self.at(2026, 7, 6, 7, 0)) == ['hit_list']
        assert sorted(to for to, _ in smtp2go.sent) == ['ann@example.com', 'betty@example.com']

    def test_nothing_due_midweek(self, db, subscribers, smtp2go):
        assert server.run_due_jobs(self.at(2026, 7, 8, 12)) == []
        assert smtp2go.requests == []

    def test_catches_up_after_a_restart_within_12_hours(self, db, subscribers, smtp2go):
        assert server.run_due_jobs(self.at(2026, 7, 4, 3, 59)) == ['weekly_digest']

    def test_skips_a_run_missed_by_more_than_12_hours(self, db, subscribers, smtp2go):
        assert server.run_due_jobs(self.at(2026, 7, 4, 4, 0)) == []
        assert smtp2go.requests == []

    def test_two_instances_racing_send_only_once(self, db, subscribers, smtp2go, tmp_path):
        friday = self.at(2026, 7, 3, 16, 1)
        other = server.get_conn()  # a second deploy instance holding its own connection
        other.execute("INSERT INTO scheduled_runs VALUES ('weekly_digest', '2026-07-03', 'x')")
        other.commit()
        other.close()
        assert server.run_due_jobs(friday) == []
        assert smtp2go.requests == []

    def test_each_week_is_its_own_run(self, db, subscribers, smtp2go):
        server.run_due_jobs(self.at(2026, 7, 3, 16, 1))
        server.run_due_jobs(self.at(2026, 7, 10, 16, 1))
        assert len(smtp2go.requests) == 2

    def test_digest_covers_the_seven_days_before_send_time(self, db, subscribers, smtp2go, tech, make_generator,
                                                           frozen_clock):
        frozen_clock.frozen = datetime.datetime(2026, 7, 3, 19, 0, tzinfo=datetime.timezone.utc)  # Fri 3pm EDT
        gen = make_generator()
        tech.json('POST', f"/api/generators/{gen['id']}/service-records", {'serviceDate': '2026-05-01'})
        server.run_due_jobs(self.at(2026, 7, 3, 16, 1))
        body = smtp2go.to('betty@example.com')[0]
        assert body['subject'] == 'Weekly digest: 1 service visit (Jun 26 – Jul 3)'
        assert 'Fri May 1, 2026' in body['text_body']  # a backdated visit still shows, by when it was logged

    def test_no_subscribers_claims_the_run_without_sending(self, db, smtp2go):
        assert server.run_due_jobs(self.at(2026, 7, 6, 7, 0)) == ['hit_list']
        assert smtp2go.requests == []
        assert db.execute('SELECT COUNT(*) FROM scheduled_runs').fetchone()[0] == 1

    def test_unknown_kind_is_rejected(self, db):
        with pytest.raises(ValueError):
            server.build_scheduled_email(db, 'monthly', self.at(2026, 7, 6, 7))
