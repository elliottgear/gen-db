"""Transport-level behaviour: the page, headers, malformed requests, and failure handling."""
import json
import os
import re
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
                                          ('GET', '/robots.txt'), ('GET', '/api/customers/abc')])
def test_unknown_routes_are_404(client, method, path):
    assert client().json(method, path)[0] == 404


def test_unsupported_methods_are_501(client):
    assert client().request('PATCH', '/api/customers')[0].status == 501


def test_missing_page_file_is_404(client, monkeypatch, tmp_path):
    monkeypatch.setitem(server.PAGES, '/', (str(tmp_path / 'gone.html'), server.HTML, server.HTML_CSP, None))
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


def read_until_closed(sock):
    # A response can arrive in several TCP segments; one recv() may see only the headers.
    chunks = []
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            return b''.join(chunks)
        chunks.append(chunk)


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


def test_production_request_timeout_is_set():
    assert server.Handler.timeout == server.REQUEST_TIMEOUT_SECONDS == 30


def test_stalled_request_body_is_timed_out(app, monkeypatch):
    monkeypatch.setattr(server.Handler, 'timeout', 0.3)
    with socket.create_connection(('127.0.0.1', app), timeout=5) as s:
        s.sendall(b'POST /api/login HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n{"user')
        response = read_until_closed(s)
        assert b' 408 ' in response.split(b'\r\n', 1)[0]
        assert b'Request timed out.' in response


def test_stalled_upload_is_timed_out_and_leaves_no_partial_file(tech, app, make_generator, uploads_dir, monkeypatch):
    import os
    monkeypatch.setattr(server.Handler, 'timeout', 0.3)
    gen = make_generator()
    head = (f"POST /api/generators/{gen['id']}/attachments HTTP/1.1\r\nHost: x\r\nCookie: {tech.cookie}\r\n"
            f"Content-Type: multipart/form-data; boundary=b\r\nContent-Length: 100000\r\n\r\n"
            f'--b\r\nContent-Disposition: form-data; name="file"; filename="a.png"\r\n\r\n\x89PNG')
    with socket.create_connection(('127.0.0.1', app), timeout=5) as s:
        s.sendall(head.encode('latin-1'))
        assert b' 408 ' in s.recv(4096).split(b'\r\n', 1)[0]
    assert os.listdir(uploads_dir) == []


def test_idle_connection_that_never_sends_a_request_is_closed(app, monkeypatch):
    monkeypatch.setattr(server.Handler, 'timeout', 0.3)
    with socket.create_connection(('127.0.0.1', app), timeout=5) as s:
        assert s.recv(4096) == b''


def test_page_carries_the_app_version(client):
    res, body = client().request('GET', '/')
    page = body.decode()
    assert f'<meta name="app-version" content="{server.APP_VERSION}">' in page
    assert '{{APP_VERSION}}' not in page
    assert int(res.getheader('Content-Length')) == len(body)


def test_server_header_is_the_app_version_only(client):
    for path in ('/', '/api/me', '/nope'):
        res, _ = client().request('GET', path)
        assert res.getheader('Server') == f'WattWatch/{server.APP_VERSION}'


@pytest.mark.parametrize('path', ['/favicon.png', '/favicon.ico'])
def test_favicon_is_served_without_signing_in(client, path):
    res, body = client().request('GET', path)
    assert res.status == 200 and res.getheader('Content-Type') == 'image/png'
    assert body == open(server.FAVICON_FILE, 'rb').read()


def test_favicon_has_a_transparent_background():
    """It sits on the browser's tab colour, so no white square behind the dog."""
    import struct
    import zlib
    data = open(server.FAVICON_FILE, 'rb').read()
    width, height, depth, colour = struct.unpack('>IIBB', data[16:26])
    assert (depth, colour) == (8, 6)  # 8-bit RGBA
    raw, pos = b'', 8
    while pos < len(data):
        length, kind = struct.unpack('>I4s', data[pos:pos + 8])
        if kind == b'IDAT':
            raw += data[pos + 8:pos + 8 + length]
        pos += 12 + length
    first_row = zlib.decompress(raw)[:1 + width * 4]
    assert first_row[0] in (0, 1)  # no or "sub" filter: the alpha of the corner pixel is literal
    assert first_row[1 + 3] == 0


@pytest.mark.parametrize('page', ['generator-manager.html', 'customer-request.html', 'customer-thanks.html'])
def test_every_page_links_the_one_favicon(page):
    html = open(os.path.join(server.BASE_DIR, page)).read()
    assert '<link rel="icon" type="image/png" href="/favicon.png">' in html


# --------------------------------------------------------------- generator map --

def test_app_csp_allows_map_tiles_but_no_outside_scripts(client):
    res, _ = client().request('GET', '/')
    csp = dict(part.strip().split(' ', 1) for part in res.getheader('Content-Security-Policy').split(';'))
    assert 'https://tile.openstreetmap.org' in csp['img-src']
    assert 'openstreetmap' not in csp['script-src'] and csp['connect-src'] == "'self'"


@pytest.mark.parametrize('path, content_type, marker', [
    ('/vendor/leaflet.js', 'text/javascript; charset=utf-8', b'Leaflet 1.9.4'),
    ('/vendor/leaflet.css', 'text/css; charset=utf-8', b'.leaflet-pane'),
])
def test_leaflet_is_served_from_the_app(client, path, content_type, marker):
    res, body = client().request('GET', path)
    assert res.status == 200 and res.getheader('Content-Type') == content_type and marker in body


# ---------------------------------------------------------- QA leftovers (v0.8.1) --

HUGE = '9' * 30


@pytest.mark.parametrize('method, path', [
    ('GET', f'/api/generators/{HUGE}'), ('GET', f'/api/customers/{HUGE}'),
    ('PUT', f'/api/service-records/{HUGE}'), ('DELETE', f'/api/service-records/{HUGE}'),
    ('POST', f'/api/service-records/{HUGE}/invoice'), ('GET', f'/api/attachments/{HUGE}/file'),
    ('POST', f'/api/generators/{HUGE}/attachments'), ('GET', f'/api/appointment-requests/{HUGE}'),
    ('GET', f'/api/generators/{2 ** 63}'),
])
def test_ids_too_big_for_the_database_are_404_not_500(admin, method, path):
    assert admin.json(method, path, {})[0] == 404


def test_the_largest_possible_id_is_just_not_found(admin):
    assert admin.json('GET', f'/api/generators/{2 ** 63 - 1}')[0] == 404


@pytest.mark.parametrize('value', [10 ** 30, str(10 ** 30), '١٢', 0, -1, True, 1.5])
def test_ids_in_bodies_are_checked(tech, value):
    status, body = tech.json('POST', '/api/generators', {'customerId': value, 'make': 'Kohler', 'model': 'M',
                                                         'serial': 'S-ID'})
    assert status == 400 and 'required' in body['error']


@pytest.mark.parametrize('method', ['PATCH', 'OPTIONS', 'TRACE'])
def test_unsupported_methods_answer_in_json(client, method):
    res, body = client().request(method, '/api/customers')
    assert res.status == 501 and res.getheader('Content-Type') == 'application/json'
    assert json.loads(body) == {'error': 'Method not supported.'}
    assert res.getheader('X-Content-Type-Options') == 'nosniff'


def test_a_garbled_request_line_is_a_json_400(app):
    with socket.create_connection(('127.0.0.1', app), timeout=5) as s:
        s.sendall(b'GET / HTTP/x.y\r\n\r\n')  # a 1-word line is treated as HTTP/0.9, which has no status line
        reply = read_until_closed(s)
    assert reply.startswith(b'HTTP/1.0 400') and reply.endswith(b'{"error": "Bad request."}')


@pytest.mark.parametrize('path', ['/', '/customer', '/portal.css', '/vendor/leaflet.js'])
def test_head_on_pages_is_get_without_the_body(client, path):
    get, get_body = client().request('GET', path)
    head, head_body = client().request('HEAD', path)
    assert head.status == 200 and head_body == b''
    assert head.getheader('Content-Length') == str(len(get_body))
    assert head.getheader('Content-Type') == get.getheader('Content-Type')


def test_head_on_the_api_is_405(client):
    res, body = client().request('HEAD', '/api/customers')
    assert res.status == 405 and body == b'' and res.getheader('Allow') == 'GET, POST, PUT, DELETE'


def test_admin_text_fields_carry_the_servers_limits():
    page = open(server.STATIC_FILE).read()
    for name in ('notes', 'physicalAddress', 'billingAddress', 'note'):
        for tag in re.findall(rf'<textarea name="{name}"[^>]*>', page):
            assert f'maxlength="{server.LONG_TEXT}"' in tag, tag
    for name in ('name', 'email', 'phone', 'make', 'model', 'serial', 'installerName'):
        for tag in re.findall(rf'<input type="(?:text|email|tel)" name="{name}"[^>]*>', page):
            assert 'maxlength=' in tag, tag


def test_long_words_wrap():
    page = open(server.STATIC_FILE).read()
    assert '.field-value{white-space:pre-wrap;overflow-wrap:anywhere;' in page
    assert 'td.wrap-any{overflow-wrap:anywhere;}' in page
    assert page.count('class="wrap-any"') + page.count('wrap-any${') >= 4  # detail, email, sender, address cells
    assert 'vertical-align:middle;overflow-wrap:anywhere' not in page  # never on every cell: words broke mid-word
