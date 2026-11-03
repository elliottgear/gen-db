import json
import os

import pytest

import server
from tests import helpers
from tests.helpers import PNG, multipart_body


@pytest.fixture
def gen(make_generator):
    return make_generator()


def test_upload_round_trips_byte_exact_and_records_metadata(tech, gen, frozen_clock):
    data = helpers.PDF + os.urandom(300_000) + b'\r\n'
    status, g = tech.upload(gen['id'], 'report.pdf', data, 'application/pdf')
    assert status == 200
    att = g['attachments'][-1]
    assert (att['filename'], att['kind'], att['contentType'], att['sizeBytes'], att['uploadedBy'], att['uploadedAt']) == \
        ('report.pdf', 'pdf', 'application/pdf', len(data), 'tech', '2026-07-04')
    res, payload = tech.request('GET', att['url'])
    assert (res.status, payload) == (200, data)


def test_stored_file_name_is_random_not_the_users_name(tech, gen, uploads_dir):
    tech.upload(gen['id'], '../../evil.png', PNG)
    names = os.listdir(uploads_dir)
    assert len(names) == 1 and names[0].endswith('.png') and 'evil' not in names[0]


def test_served_with_safe_headers_and_long_caching(tech, gen):
    _, g = tech.upload(gen['id'], 'pic.png', PNG)
    res, _ = tech.request('GET', g['attachments'][-1]['url'])
    assert res.getheader('Content-Type') == 'image/png'
    assert res.getheader('X-Content-Type-Options') == 'nosniff'
    assert res.getheader('Accept-Ranges') == 'bytes'
    assert 'immutable' in res.getheader('Cache-Control')
    assert res.getheader('Content-Security-Policy') is None  # would break Chrome's PDF viewer


def test_unicode_filename_downloads(tech, gen):
    name = 'Screenshot 2026-07-21 at 5.48.12 PM – ünits 照片.png'
    _, g = tech.upload(gen['id'], name, PNG)
    att = g['attachments'][-1]
    assert att['filename'] == name
    res, payload = tech.request('GET', att['url'])
    assert (res.status, payload) == (200, PNG)
    assert "filename*=UTF-8''" in res.getheader('Content-Disposition')


def test_declared_content_type_is_ignored_in_favour_of_the_file_itself(tech, gen):
    status, g = tech.upload(gen['id'], 'IMG_0001.HEIC', helpers.HEIC, 'application/octet-stream')
    assert status == 200 and g['attachments'][-1]['contentType'] == 'image/heic'


@pytest.mark.parametrize('filename, data, message', [
    ('fake.png', b'<html><script>alert(1)</script>', 'does not match'),
    ('page.html', b'<html>', 'Unsupported file type'),
    ('icon.svg', b'<svg/>', 'Unsupported file type'),
])
def test_rejects_disallowed_or_disguised_files_and_leaves_nothing_on_disk(tech, gen, uploads_dir, filename, data, message):
    status, body = tech.upload(gen['id'], filename, data)
    assert status == 400 and message in body['error']
    assert os.listdir(uploads_dir) == []


def test_upload_to_missing_generator(tech, uploads_dir):
    assert tech.upload(999999, 'a.png', PNG)[0] == 404
    assert os.listdir(uploads_dir) == []


def test_size_limit_is_checked_before_reading_the_body(tech, gen, monkeypatch):
    monkeypatch.setattr(server, 'MAX_UPLOAD_BYTES', 1000)
    body, ctype = multipart_body([('file', 'a.png', 'image/png', PNG)])
    res, payload = tech.request('POST', f"/api/generators/{gen['id']}/attachments", raw=body,
                                headers={'Content-Type': ctype})
    assert res.status == 413


def test_non_multipart_upload_is_400(tech, gen):
    assert tech.json('POST', f"/api/generators/{gen['id']}/attachments", {'file': 'x'})[0] == 400


class TestRange:
    @pytest.fixture
    def url(self, tech, gen):
        return tech.upload(gen['id'], 'pic.png', PNG)[1]['attachments'][-1]['url']

    def test_partial_content(self, tech, url):
        res, payload = tech.request('GET', url, headers={'Range': 'bytes=8-15'})
        assert (res.status, payload) == (206, PNG[8:16])
        assert res.getheader('Content-Range') == f'bytes 8-15/{len(PNG)}'
        assert res.getheader('Content-Length') == '8'

    def test_suffix_range(self, tech, url):
        assert tech.request('GET', url, headers={'Range': 'bytes=-4'})[1] == PNG[-4:]

    def test_unsatisfiable(self, tech, url):
        res, _ = tech.request('GET', url, headers={'Range': f'bytes={len(PNG)}-'})
        assert res.status == 416 and res.getheader('Content-Range') == f'bytes */{len(PNG)}'

    def test_multi_range_falls_back_to_the_whole_file(self, tech, url):
        res, payload = tech.request('GET', url, headers={'Range': 'bytes=0-1,4-5'})
        assert (res.status, payload) == (200, PNG)


def test_missing_file_on_disk_is_404(tech, gen, uploads_dir):
    _, g = tech.upload(gen['id'], 'pic.png', PNG)
    for name in os.listdir(uploads_dir):
        os.remove(os.path.join(uploads_dir, name))
    assert tech.json('GET', g['attachments'][-1]['url']) == (404, {'error': 'File not found on disk.'})


def test_unknown_attachment_is_404(tech):
    assert tech.json('GET', '/api/attachments/999999/file')[0] == 404
    assert tech.json('DELETE', '/api/attachments/999999')[0] == 404


def test_delete_removes_row_and_file_and_is_audited(tech, gen, uploads_dir, db):
    _, g = tech.upload(gen['id'], 'pic.png', PNG)
    att = g['attachments'][-1]
    status, g = tech.json('DELETE', f"/api/attachments/{att['id']}")
    assert status == 200 and g['attachments'] == []
    assert os.listdir(uploads_dir) == []
    row = db.execute("SELECT detail FROM audit_log WHERE entity='attachment' AND entity_id=?", (att['id'],)).fetchone()
    assert json.loads(row['detail'])['filename'] == 'pic.png'


def test_deleting_the_generator_removes_its_files(tech, gen, uploads_dir):
    tech.upload(gen['id'], 'a.png', PNG)
    tech.upload(gen['id'], 'b.png', PNG)
    assert len(os.listdir(uploads_dir)) == 2
    tech.json('DELETE', f"/api/generators/{gen['id']}")
    assert os.listdir(uploads_dir) == []


def test_large_upload_streams_correctly(tech, gen):
    data = helpers.MP4 + os.urandom(5 * 1024 * 1024)
    status, g = tech.upload(gen['id'], 'clip.mp4', data, 'video/mp4')
    assert status == 200
    assert tech.request('GET', g['attachments'][-1]['url'])[1] == data


def test_failed_database_write_removes_the_stored_file(tech, gen, uploads_dir, db):
    db.execute('DROP TABLE generator_attachments')
    db.commit()
    assert tech.upload(gen['id'], 'pic.png', PNG) == (500, {'error': 'Internal server error.'})
    assert os.listdir(uploads_dir) == []
