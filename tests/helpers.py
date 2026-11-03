"""Test helpers: a small HTTP client and multipart/file builders."""
import http.client
import json
import uuid


class Client:
    """Talks to the test server over real HTTP and keeps the session cookie."""

    def __init__(self, port):
        self.port = port
        self.cookie = None

    def request(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
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
            if self.cookie.endswith('='):
                self.cookie = None
        return res, payload

    def json(self, method, path, body=None, **kw):
        res, payload = self.request(method, path, body, **kw)
        return res.status, (json.loads(payload) if payload else None)

    def login(self, username, password):
        status, body = self.json('POST', '/api/login', {'username': username, 'password': password})
        assert status == 200, body
        return body

    def upload(self, gen_id, filename, data, content_type='application/octet-stream'):
        body, ctype = multipart_body([('file', filename, content_type, data)])
        return self.json('POST', f'/api/generators/{gen_id}/attachments', raw=body, headers={'Content-Type': ctype})


def multipart_body(parts, boundary=None, preamble=b''):
    """parts: list of (field_name, filename_or_None, content_type_or_None, bytes)."""
    boundary = boundary or uuid.uuid4().hex
    out = [preamble]
    for name, filename, content_type, data in parts:
        disposition = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename is not None else '')
        head = f'--{boundary}\r\nContent-Disposition: {disposition}\r\n'
        if content_type:
            head += f'Content-Type: {content_type}\r\n'
        out.append(head.encode('utf-8') + b'\r\n' + data + b'\r\n')
    out.append(f'--{boundary}--\r\n'.encode())
    return b''.join(out), f'multipart/form-data; boundary={boundary}'


PNG = b'\x89PNG\r\n\x1a\n' + bytes(range(256)) * 4
JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 64
GIF = b'GIF89a' + b'\x00' * 32
WEBP = b'RIFF\x00\x00\x00\x00WEBPVP8 ' + b'\x00' * 32
PDF = b'%PDF-1.4\n' + b'x' * 64 + b'\n%%EOF\n'
HEIC = b'\x00\x00\x00\x18ftypheic' + b'\x00' * 32
MP4 = b'\x00\x00\x00\x18ftypisom' + b'\x00' * 32
MOV = b'\x00\x00\x00\x14ftypqt  ' + b'\x00' * 32
WEBM = b'\x1a\x45\xdf\xa3' + b'\x00' * 32
