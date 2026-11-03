"""Transport-level behaviour: the page, headers, malformed requests, and failure handling."""
import socket

import pytest

import server


def test_serves_the_app_page_with_a_csp(client):
    for path in ('/', '/generator-manager.html', '/?x=1'):
        res, body = client().request('GET', path)
        assert res.status == 200 and b'<title>Watt Watch' in body
        assert res.getheader('Content-Type') == 'text/html; charset=utf-8'
        csp = res.getheader('Content-Security-Policy')
        assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp


def test_security_headers_on_every_response(client):
    for res, _ in (client().request('GET', '/'), client().request('GET', '/api/me'), client().request('GET', '/nope')):
        assert res.getheader('X-Content-Type-Options') == 'nosniff'
        assert res.getheader('X-Frame-Options') == 'DENY'
        assert res.getheader('Referrer-Policy') == 'same-origin'


def test_api_responses_are_not_cached(tech):
    res, _ = tech.request('GET', '/api/customers')
    assert res.getheader('Cache-Control') == 'no-store'


@pytest.mark.parametrize('method, path', [('GET', '/api/nothing'), ('POST', '/api/customers/1'),
                                          ('GET', '/favicon.ico'), ('GET', '/api/customers/abc')])
def test_unknown_routes_are_404(client, method, path):
    assert client().json(method, path)[0] == 404


def test_unsupported_methods_are_501(client):
    assert client().request('PATCH', '/api/customers')[0].status == 501


def test_missing_page_file_is_404(client, monkeypatch, tmp_path):
    monkeypatch.setattr(server, 'STATIC_FILE', str(tmp_path / 'gone.html'))
    assert client().json('GET', '/')[0] == 404


@pytest.mark.parametrize('raw, message', [
    (b'{not json', 'Malformed JSON body.'),
    (b'\xff\xfe', 'Malformed JSON body.'),
    (b'[1, 2]', 'Request body must be a JSON object.'),
    (b'"name"', 'Request body must be a JSON object.'),
    (b'null', 'Request body must be a JSON object.'),
])
def test_bad_json_bodies_are_400(tech, raw, message):
    assert tech.json('POST', '/api/customers', raw=raw) == (400, {'error': message})


def raw_request(port, head):
    with socket.create_connection(('127.0.0.1', port), timeout=5) as s:
        s.sendall(head.encode())
        return s.recv(4096).split(b'\r\n', 1)[0]


def test_oversized_json_body_is_refused_before_it_is_read(tech, app):
    status_line = raw_request(app, f'POST /api/customers HTTP/1.1\r\nHost: x\r\nCookie: {tech.cookie}\r\n'
                                   f'Content-Length: {server.MAX_JSON_BYTES + 1}\r\n\r\n')
    assert b' 413 ' in status_line


def test_invalid_content_length_is_400(tech, app):
    status_line = raw_request(app, f'POST /api/customers HTTP/1.1\r\nHost: x\r\nCookie: {tech.cookie}\r\n'
                                   f'Content-Length: abc\r\n\r\n')
    assert b' 400 ' in status_line


def test_empty_body_counts_as_empty_object(tech):
    assert tech.json('POST', '/api/customers', raw=b'') == (400, {'error': 'Name is required.'})


def test_unexpected_errors_are_500_without_internals(tech, monkeypatch):
    def boom(conn):
        raise RuntimeError('secret stack detail')
    routes = [(m, p, (lambda conn, mt, body, user: boom(conn)) if p.pattern == r'^/api/customers$' and m == 'GET' else f, perm)
              for m, p, f, perm in server.ROUTES]
    monkeypatch.setattr(server, 'ROUTES', routes)
    assert tech.json('GET', '/api/customers') == (500, {'error': 'Internal server error.'})


def test_database_that_cannot_be_opened_is_a_clean_500(tech, monkeypatch, tmp_path):
    monkeypatch.setattr(server, 'DB_PATH', str(tmp_path / 'no-such-dir' / 'db.sqlite3'))
    assert tech.json('GET', '/api/customers') == (500, {'error': 'Internal server error.'})


@pytest.mark.xfail(strict=True, raises=socket.timeout, reason=(
    'BUG: no socket timeout. A client that declares a Content-Length and then sends fewer bytes ties up a '
    'server thread indefinitely (slowloris-style). The server should time out and close the connection.'))
def test_stalled_request_body_is_timed_out(app):
    with socket.create_connection(('127.0.0.1', app), timeout=3) as s:
        s.sendall(b'POST /api/login HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n{"user')
        s.recv(4096)  # a fixed server would answer or close long before the 3s client timeout
