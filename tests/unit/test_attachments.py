"""Pure attachment logic: multipart parsing, type sniffing, filenames, Range headers."""
import io

import pytest

import server
from server import ApiError
from tests import helpers
from tests.helpers import multipart_body


def receive(body, content_type, dest):
    return server.receive_upload(io.BytesIO(body), len(body), content_type, str(dest))


class TestReceiveUpload:
    @pytest.mark.parametrize('tail', [b'', b'\n', b'\r\n', b'\r\n\r\n', b'\r', b'--'])
    def test_file_bytes_round_trip_exactly(self, tmp_path, tail):
        data = b'%PDF-1.4\n' + bytes(range(256)) * 50 + tail
        body, ctype = multipart_body([('file', 'a.pdf', 'application/pdf', data)])
        info = receive(body, ctype, tmp_path / 'out')
        assert (tmp_path / 'out').read_bytes() == data
        assert info['size'] == len(data)
        assert info['filename'] == 'a.pdf'
        assert info['head'] == data[:server.MAGIC_BYTES_NEEDED]

    def test_delimiter_split_across_read_chunks_is_still_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server, 'STREAM_CHUNK', 7)
        data = b'\x89PNG' + b'\r\n-' * 500 + b'end'
        body, ctype = multipart_body([('file', 'x.png', 'image/png', data)])
        receive(body, ctype, tmp_path / 'out')
        assert (tmp_path / 'out').read_bytes() == data

    def test_takes_the_first_file_field_and_skips_other_fields_and_preamble(self, tmp_path):
        body, ctype = multipart_body([
            ('note', None, None, b'just text'),
            ('file', 'first.png', 'image/png', b'FIRST'),
            ('file', 'second.png', 'image/png', b'SECOND'),
        ], preamble=b'ignored preamble\r\n')
        info = receive(body, ctype, tmp_path / 'out')
        assert info['filename'] == 'first.png'
        assert (tmp_path / 'out').read_bytes() == b'FIRST'

    def test_quoted_boundary_parameter(self, tmp_path):
        body, _ = multipart_body([('file', 'a.png', 'image/png', b'data')], boundary='abc123')
        receive(body, 'multipart/form-data; boundary="abc123"', tmp_path / 'out')
        assert (tmp_path / 'out').read_bytes() == b'data'

    def test_unicode_filename_is_decoded(self, tmp_path):
        body, ctype = multipart_body([('file', '照片 – 5.48\u202fPM.png', 'image/png', b'x')])
        assert receive(body, ctype, tmp_path / 'out')['filename'] == '照片 – 5.48\u202fPM.png'

    def test_empty_file_is_accepted_by_the_parser(self, tmp_path):
        body, ctype = multipart_body([('file', 'a.png', 'image/png', b'')])
        assert receive(body, ctype, tmp_path / 'out')['size'] == 0

    @pytest.mark.parametrize('content_type, message', [
        (None, 'Expected multipart/form-data.'),
        ('application/json', 'Expected multipart/form-data.'),
        ('multipart/form-data', 'Missing multipart boundary.'),
        ('multipart/form-data; boundary=', 'Missing multipart boundary.'),
    ])
    def test_rejects_bad_content_type_headers(self, tmp_path, content_type, message):
        with pytest.raises(ApiError) as exc:
            receive(b'whatever', content_type, tmp_path / 'out')
        assert (exc.value.status, exc.value.message) == (400, message)

    def test_body_with_no_file_field(self, tmp_path):
        body, ctype = multipart_body([('note', None, None, b'text only')])
        with pytest.raises(ApiError, match='No file provided'):
            receive(body, ctype, tmp_path / 'out')

    def test_truncated_body(self, tmp_path):
        body, ctype = multipart_body([('file', 'a.png', 'image/png', b'x' * 1000)])
        with pytest.raises(ApiError, match='cut off'):
            receive(body[:600], ctype, tmp_path / 'out')

    def test_oversized_part_headers(self, tmp_path):
        body = b'--b\r\nContent-Disposition: form-data; name="file"; X: ' + b'a' * (server.MAX_PART_HEADER_BYTES + 10)
        with pytest.raises(ApiError, match='Malformed'):
            receive(body, 'multipart/form-data; boundary=b', tmp_path / 'out')

    def test_never_reads_past_content_length(self, tmp_path):
        body, ctype = multipart_body([('file', 'a.png', 'image/png', b'data')])
        stream = io.BytesIO(body + b'NEXT REQUEST BYTES')
        server.receive_upload(stream, len(body), ctype, str(tmp_path / 'out'))
        assert stream.read() == b'NEXT REQUEST BYTES'


class TestValidateUpload:
    @pytest.mark.parametrize('filename, data, kind, content_type', [
        ('a.jpg', helpers.JPEG, 'image', 'image/jpeg'),
        ('a.JPEG', helpers.JPEG, 'image', 'image/jpeg'),
        ('a.png', helpers.PNG, 'image', 'image/png'),
        ('a.gif', helpers.GIF, 'image', 'image/gif'),
        ('a.webp', helpers.WEBP, 'image', 'image/webp'),
        ('IMG_0001.HEIC', helpers.HEIC, 'image', 'image/heic'),
        ('a.pdf', helpers.PDF, 'pdf', 'application/pdf'),
        ('a.mp4', helpers.MP4, 'video', 'video/mp4'),
        ('a.mov', helpers.MOV, 'video', 'video/quicktime'),
        ('a.webm', helpers.WEBM, 'video', 'video/webm'),
    ])
    def test_accepts_every_supported_type(self, filename, data, kind, content_type):
        got_kind, ext, got_type = server.validate_upload(filename, data[:server.MAGIC_BYTES_NEEDED])
        assert (got_kind, got_type) == (kind, content_type)
        assert ext == '.' + filename.rsplit('.', 1)[1].lower()

    @pytest.mark.parametrize('filename', ['a.svg', 'a.html', 'a.exe', 'noext', 'a.png.exe', '.png'])
    def test_rejects_unsupported_extensions(self, filename):
        with pytest.raises(ApiError, match='Unsupported file type'):
            server.validate_upload(filename, helpers.PNG)

    @pytest.mark.parametrize('filename, data', [
        ('a.png', helpers.JPEG), ('a.jpg', b'<html>'), ('a.pdf', helpers.PNG),
        ('a.mp4', helpers.HEIC), ('a.heic', helpers.MP4), ('a.png', b''),
    ])
    def test_rejects_content_that_does_not_match_the_extension(self, filename, data):
        with pytest.raises(ApiError, match='does not match'):
            server.validate_upload(filename, data)


class TestCleanFilename:
    @pytest.mark.parametrize('raw, expected', [
        ('photo.png', 'photo.png'),
        (r'C:\Users\me\Desktop\photo.png', 'photo.png'),
        ('../../etc/passwd.png', 'passwd.png'),
        ('a\x07b\x1bc.png', 'abc.png'),
        ('evil\r\nX-Injected: 1.png', 'evilX-Injected: 1.png'),
        ('zero\u200bwidth.png', 'zerowidth.png'),
        ('Screenshot 5.48\u202fPM.png', 'Screenshot 5.48\u202fPM.png'),
        ('  spaced.png  ', 'spaced.png'),
        ('', 'upload'),
        ('\x00\x01', 'upload'),
    ])
    def test_cleaning(self, raw, expected):
        assert server.clean_filename(raw) == expected

    def test_long_names_keep_their_extension(self):
        cleaned = server.clean_filename('x' * 500 + '.jpeg')
        assert cleaned.endswith('.jpeg') and len(cleaned) == 155


class TestContentDisposition:
    def test_ascii_name(self):
        assert server.content_disposition('report.pdf') == 'inline; filename="report.pdf"; filename*=UTF-8\'\'report.pdf'

    def test_non_ascii_name_has_ascii_fallback_and_utf8_form(self):
        header = server.content_disposition('Côté 照片.png')
        assert header.startswith('inline; filename="Ct .png"; ')
        assert "filename*=UTF-8''C%C3%B4t%C3%A9%20%E7%85%A7%E7%89%87.png" in header
        header.encode('latin-1')  # must be sendable as an HTTP header

    def test_quotes_backslashes_and_control_chars_cannot_break_the_header(self):
        header = server.content_disposition('a"b\\c\r\nd.png')
        assert '\r' not in header and '\n' not in header
        assert header.startswith('inline; filename="abcd.png"')

    def test_all_non_ascii_falls_back_to_download(self):
        assert server.content_disposition('照片').startswith('inline; filename="download"')


class TestParseRange:
    @pytest.mark.parametrize('header, expected', [
        ('bytes=0-99', (0, 99)),
        ('bytes=100-', (100, 999)),
        ('bytes=-100', (900, 999)),
        ('bytes=-5000', (0, 999)),
        ('bytes=990-5000', (990, 999)),
        ('bytes=999-999', (999, 999)),
        (' bytes=0-0 ', (0, 0)),
    ])
    def test_satisfiable_ranges(self, header, expected):
        assert server.parse_range(header, 1000) == expected

    @pytest.mark.parametrize('header', ['bytes=1000-', 'bytes=5000-6000', 'bytes=50-10'])
    def test_unsatisfiable_ranges(self, header):
        assert server.parse_range(header, 1000) is False

    @pytest.mark.parametrize('header', [None, '', 'bytes=-', 'bytes=0-1,5-6', 'items=0-1', 'bytes=a-b'])
    def test_ignored_ranges_serve_the_whole_file(self, header):
        assert server.parse_range(header, 1000) is None
