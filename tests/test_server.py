"""
End-to-end tests for server.py: each test class gets a fresh temp database and
uploads folder, and talks to a real server over HTTP.

Run:  python3 -m unittest discover tests
"""
import datetime
import http.client
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid

TMP = tempfile.mkdtemp(prefix='wattwatch-test-')
os.environ['DB_PATH'] = os.path.join(TMP, 'test.sqlite3')
os.environ['UPLOADS_DIR'] = os.path.join(TMP, 'uploads')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

PASSWORD = 'correct horse battery'


def setUpModule():
    server.init_db()
    conn = server.get_conn()
    salt, digest = server.hash_password(PASSWORD)
    conn.execute('INSERT INTO users (username,password_hash,password_salt,created_at) VALUES (?,?,?,?)',
                 ('tester', digest, salt, '2026-01-01'))
    conn.commit()
    conn.close()
    global HTTPD
    HTTPD = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    threading.Thread(target=HTTPD.serve_forever, daemon=True).start()


def tearDownModule():
    HTTPD.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)


class Client:
    def __init__(self):
        self.cookie = None

    def request(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection('127.0.0.1', HTTPD.server_address[1], timeout=10)
        headers = dict(headers or {})
        if self.cookie:
            headers['Cookie'] = self.cookie
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None and 'Content-Type' not in headers:
            headers['Content-Type'] = 'application/json'
        conn.request(method, path, body=data, headers=headers)
        res = conn.getresponse()
        payload = res.read()
        conn.close()
        set_cookie = res.getheader('Set-Cookie')
        if set_cookie:
            self.cookie = set_cookie.split(';', 1)[0]
        return res, payload

    def json(self, method, path, body=None, **kw):
        res, payload = self.request(method, path, body, **kw)
        return res.status, (json.loads(payload) if payload else None)

    def login(self):
        status, _ = self.json('POST', '/api/login', {'username': 'tester', 'password': PASSWORD})
        assert status == 200, status
        return self

    def upload(self, gen_id, filename, data, content_type='application/octet-stream'):
        boundary = uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f'Content-Type: {content_type}\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
        return self.json('POST', f'/api/generators/{gen_id}/attachments', raw=body,
                         headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})


PNG = b'\x89PNG\r\n\x1a\n' + bytes(range(256)) * 4


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        server._login_failures.clear()
        self.c = Client().login()

    def new_generator(self, **overrides):
        status, cust = self.c.json('POST', '/api/customers', {'name': 'Test Customer'})
        self.assertEqual(status, 200)
        body = {'customerId': cust['id'], 'make': 'Generac', 'model': 'G22', 'serial': uuid.uuid4().hex}
        body.update(overrides)
        status, gen = self.c.json('POST', '/api/generators', body)
        self.assertEqual(status, 200, gen)
        return gen


class AuthTests(ApiTestCase):
    def test_requires_login(self):
        status, _ = Client().json('GET', '/api/customers')
        self.assertEqual(status, 401)

    def test_expired_session_rejected(self):
        conn = server.get_conn()
        conn.execute("UPDATE sessions SET created_at = datetime('now', '-31 days') WHERE token=?",
                     (self.c.cookie.split('=', 1)[1],))
        conn.commit()
        conn.close()
        status, _ = self.c.json('GET', '/api/me')
        self.assertEqual(status, 401)

    def test_login_rate_limited(self):
        anon = Client()
        for _ in range(server.LOGIN_MAX_FAILURES):
            status, _ = anon.json('POST', '/api/login', {'username': 'tester', 'password': 'wrong'})
            self.assertEqual(status, 401)
        status, _ = anon.json('POST', '/api/login', {'username': 'tester', 'password': PASSWORD})
        self.assertEqual(status, 429)

    def test_malformed_cookie_is_just_unauthenticated(self):
        status, _ = Client().json('GET', '/api/me', headers={'Cookie': 'a="\\'})
        self.assertEqual(status, 401)


class ValidationTests(ApiTestCase):
    def test_rejects_non_date(self):
        gen = self.new_generator()
        status, body = self.c.json('POST', f"/api/generators/{gen['id']}/service-records",
                                   {'serviceDate': '<img src=x onerror=alert(1)>'})
        self.assertEqual(status, 400)
        self.assertIn('valid date', body['error'])
        status, _ = self.c.json('POST', f"/api/generators/{gen['id']}/service-records", {'serviceDate': '2026-02-30'})
        self.assertEqual(status, 400)

    def test_null_field_and_non_object_body_are_400(self):
        status, _ = self.c.json('POST', '/api/customers', {'name': 'x', 'email': None})
        self.assertEqual(status, 200)
        status, _ = self.c.json('POST', '/api/customers', {'name': 'x', 'email': 5})
        self.assertEqual(status, 400)
        status, body = self.c.json('POST', '/api/customers', [1])
        self.assertEqual(status, 400)
        self.assertNotIn('object has no attribute', body['error'])

    def test_text_length_limit(self):
        status, _ = self.c.json('POST', '/api/customers', {'name': 'x' * (server.SHORT_TEXT + 1)})
        self.assertEqual(status, 400)

    def test_transfer_to_same_customer_with_string_id(self):
        gen = self.new_generator()
        status, _ = self.c.json('POST', f"/api/generators/{gen['id']}/transfer", {'newCustomerId': str(gen['customerId'])})
        self.assertEqual(status, 400)

    def test_duplicate_serial_rejected(self):
        gen = self.new_generator()
        status, _ = self.c.json('POST', '/api/generators', {
            'customerId': gen['customerId'], 'make': 'generac', 'model': 'X', 'serial': gen['serial'].upper()})
        self.assertEqual(status, 409)


class ServiceTests(ApiTestCase):
    def test_backdated_service_keeps_schedule(self):
        gen = self.new_generator()
        url = f"/api/generators/{gen['id']}/service-records"
        self.c.json('POST', url, {'serviceDate': '2026-06-01', 'nextServiceDate': '2027-06-01'})
        status, gen = self.c.json('POST', url, {'serviceDate': '2025-06-01', 'nextServiceDate': ''})
        self.assertEqual(status, 200)
        self.assertEqual((gen['lastServiceDate'], gen['nextServiceDate']), ('2026-06-01', '2027-06-01'))
        self.assertEqual(sum(h['event'] == 'Serviced' for h in gen['history']), 2)

    def test_stale_edit_conflicts(self):
        gen = self.new_generator()
        edit = {k: gen[k] for k in ('make', 'model', 'serial')}
        status, updated = self.c.json('PUT', f"/api/generators/{gen['id']}", dict(edit, expectedUpdatedAt=gen['updatedAt']))
        self.assertEqual(status, 200)
        status, body = self.c.json('PUT', f"/api/generators/{gen['id']}", dict(edit, expectedUpdatedAt=gen['updatedAt']))
        self.assertEqual((status, body['code']), (409, 'stale'))

    def test_editing_service_dates_is_logged(self):
        gen = self.new_generator()
        edit = {k: gen[k] for k in ('make', 'model', 'serial')}
        status, gen = self.c.json('PUT', f"/api/generators/{gen['id']}", dict(edit, nextServiceDate='2027-01-15'))
        self.assertEqual(gen['nextServiceDate'], '2027-01-15')
        self.assertEqual(gen['history'][-1]['event'], 'Service dates edited')

    def test_uninvoice_is_audited(self):
        gen = self.new_generator()
        _, gen = self.c.json('POST', f"/api/generators/{gen['id']}/service-records", {'serviceDate': '2026-06-01'})
        hist_id = gen['history'][-1]['id']
        self.c.json('POST', f'/api/service-records/{hist_id}/invoice', {'invoiced': True})
        self.c.json('POST', f'/api/service-records/{hist_id}/invoice', {'invoiced': False})
        conn = server.get_conn()
        actions = [r['action'] for r in conn.execute(
            "SELECT action FROM audit_log WHERE entity='service_record' AND entity_id=? ORDER BY id", (hist_id,))]
        conn.close()
        self.assertEqual(actions, ['invoice', 'uninvoice'])


class AttachmentTests(ApiTestCase):
    def test_round_trip_is_byte_exact(self):
        gen = self.new_generator()
        for tail in (b'\n', b'\r\n', b'\r\n\r\n', b'\r'):
            data = b'%PDF-1.4\n' + os.urandom(200_000) + b'%%EOF' + tail
            status, gen = self.c.upload(gen['id'], 'report.pdf', data, 'application/pdf')
            self.assertEqual(status, 200, gen)
            att = gen['attachments'][-1]
            self.assertEqual(att['sizeBytes'], len(data))
            res, payload = self.c.request('GET', att['url'])
            self.assertEqual(payload, data, f'tail {tail!r}')

    def test_unicode_filename_downloads(self):
        gen = self.new_generator()
        name = 'Screenshot 2026-07-21 at 5.48.12 PM – ünits 照片.png'
        status, gen = self.c.upload(gen['id'], name, PNG, 'image/png')
        self.assertEqual(status, 200)
        att = gen['attachments'][-1]
        self.assertEqual(att['filename'], name)
        res, payload = self.c.request('GET', att['url'])
        self.assertEqual(res.status, 200)
        self.assertEqual(payload, PNG)
        self.assertIn("filename*=UTF-8''", res.getheader('Content-Disposition'))

    def test_filename_control_characters_stripped(self):
        gen = self.new_generator()
        status, gen = self.c.upload(gen['id'], 'a\x07b\x1bc.png', PNG, 'image/png')
        self.assertEqual(status, 200)
        self.assertEqual(gen['attachments'][-1]['filename'], 'abc.png')

    def test_declared_type_ignored_magic_bytes_enforced(self):
        gen = self.new_generator()
        heic = b'\x00\x00\x00\x18ftypheic' + b'\x00' * 100
        status, gen2 = self.c.upload(gen['id'], 'IMG_0001.HEIC', heic, 'application/octet-stream')
        self.assertEqual(status, 200, gen2)
        self.assertEqual(gen2['attachments'][-1]['contentType'], 'image/heic')
        status, _ = self.c.upload(gen['id'], 'fake.png', b'<html>not a png</html>', 'image/png')
        self.assertEqual(status, 400)

    def test_range_requests(self):
        gen = self.new_generator()
        _, gen = self.c.upload(gen['id'], 'pic.png', PNG, 'image/png')
        url = gen['attachments'][-1]['url']
        res, payload = self.c.request('GET', url, headers={'Range': 'bytes=8-15'})
        self.assertEqual(res.status, 206)
        self.assertEqual(payload, PNG[8:16])
        self.assertEqual(res.getheader('Content-Range'), f'bytes 8-15/{len(PNG)}')
        res, payload = self.c.request('GET', url, headers={'Range': 'bytes=-4'})
        self.assertEqual(payload, PNG[-4:])
        res, _ = self.c.request('GET', url, headers={'Range': f'bytes={len(PNG)}-'})
        self.assertEqual(res.status, 416)

    def test_deleting_generator_removes_files(self):
        gen = self.new_generator()
        _, gen = self.c.upload(gen['id'], 'pic.png', PNG, 'image/png')
        before = set(os.listdir(server.UPLOADS_DIR))
        status, _ = self.c.json('DELETE', f"/api/generators/{gen['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(len(before - set(os.listdir(server.UPLOADS_DIR))), 1)

    def test_no_temp_files_left_after_rejected_upload(self):
        gen = self.new_generator()
        self.c.upload(gen['id'], 'fake.png', b'nope', 'image/png')
        self.assertFalse([n for n in os.listdir(server.UPLOADS_DIR) if n.startswith('.incoming-')])


class TimeTests(unittest.TestCase):
    def test_us_eastern_fallback_matches_zoneinfo(self):
        if not server.LOCAL_TZ:
            self.skipTest('no tz database here')
        start = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        for hours in range(0, 24 * 366 * 2, 7):
            t = start + datetime.timedelta(hours=hours)
            self.assertEqual(t.astimezone(server._us_eastern_tz(t)).utcoffset(),
                             t.astimezone(server.LOCAL_TZ).utcoffset(), t)


class MigrationTests(unittest.TestCase):
    def test_migrations_bring_old_db_current(self):
        path = os.path.join(TMP, 'old.sqlite3')
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.executescript('''
            CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE generators (id INTEGER PRIMARY KEY, customer_id INTEGER, make TEXT, model TEXT, serial TEXT);
            CREATE TABLE generator_history (id INTEGER PRIMARY KEY, generator_id INTEGER, date TEXT, event TEXT, detail TEXT);
            CREATE TABLE sessions (token TEXT PRIMARY KEY, user_id INTEGER, created_at TEXT);
            INSERT INTO sessions VALUES ('leaked', 1, '2026-07-18');
        ''')
        server._migrate(conn)
        cols = {r['name'] for r in conn.execute('PRAGMA table_info(generators)')}
        self.assertTrue({'next_service_date', 'physical_address', 'updated_by', 'updated_at'} <= cols)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)
        self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], len(server.MIGRATIONS))
        conn.close()


if __name__ == '__main__':
    unittest.main()
