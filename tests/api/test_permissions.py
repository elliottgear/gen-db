"""Role enforcement across every write route."""
import pytest

import server


def route_path(pattern, ids):
    path = pattern.pattern.strip('^$')
    for i in ids:
        path = path.replace(r'(\d+)', str(i), 1)
    return path


@pytest.fixture
def viewer(make_user, client, monkeypatch):
    """A role with no write permissions, to prove every write route checks one."""
    monkeypatch.setitem(server.ROLES, 'viewer', set())
    make_user('viewer', 'viewer')
    return client('viewer')


def test_every_write_route_is_refused_without_its_permission(viewer, serviced_generator):
    gen, hist_id = serviced_generator
    ids = {'customers': gen['customerId'], 'generators': gen['id'], 'service-records': hist_id, 'attachments': 1}
    for method, pattern, _fn, permission in server.ROUTES:
        if permission is None:
            continue
        resource = pattern.pattern.strip('^$').split('/')[2]
        status, body = viewer.json(method, route_path(pattern, [ids[resource]]), {})
        assert status == 403, (method, pattern.pattern, status)
        assert server.PERMISSIONS[permission] in body['error']


def test_uploads_need_the_upload_permission(viewer, make_generator):
    gen = make_generator()
    assert viewer.upload(gen['id'], 'a.png', b'\x89PNG\r\n\x1a\n')[0] == 403


def test_reads_are_open_to_every_signed_in_role(viewer, make_generator):
    gen = make_generator()
    for path in ('/api/customers', '/api/generators', f"/api/generators/{gen['id']}",
                 f"/api/customers/{gen['customerId']}", '/api/me'):
        assert viewer.json('GET', path)[0] == 200, path


def test_technician_cannot_mark_or_unmark_invoiced(tech, admin, serviced_generator):
    _, hist_id = serviced_generator
    for invoiced in (True, False):
        status, body = tech.json('POST', f'/api/service-records/{hist_id}/invoice', {'invoiced': invoiced})
        assert status == 403 and 'invoiced' in body['error']
        assert admin.json('POST', f'/api/service-records/{hist_id}/invoice', {'invoiced': invoiced})[0] == 200


def test_technician_can_do_everything_else(tech, serviced_generator, make_customer):
    from tests.helpers import PNG
    gen, _ = serviced_generator
    edit = {k: gen[k] for k in ('make', 'model', 'serial')}
    assert tech.json('PUT', f"/api/generators/{gen['id']}", dict(edit, notes='x'))[0] == 200
    status, gen = tech.upload(gen['id'], 'pic.png', PNG)
    assert status == 200
    assert tech.json('DELETE', f"/api/attachments/{gen['attachments'][-1]['id']}")[0] == 200
    other = make_customer(name='New Owner')
    assert tech.json('POST', f"/api/generators/{gen['id']}/transfer", {'newCustomerId': other['id']})[0] == 200
    assert tech.json('DELETE', f"/api/generators/{gen['id']}")[0] == 200
    assert tech.json('DELETE', f"/api/customers/{other['id']}")[0] == 200


def test_me_reports_permissions_for_the_ui(tech, admin):
    assert 'service.invoice' not in tech.json('GET', '/api/me')[1]['permissions']
    assert set(admin.json('GET', '/api/me')[1]['permissions']) == set(server.PERMISSIONS)


def test_promotion_applies_without_signing_in_again(tech, db, serviced_generator):
    _, hist_id = serviced_generator
    db.execute("UPDATE users SET role='admin' WHERE username='tech'")
    db.commit()
    assert tech.json('POST', f'/api/service-records/{hist_id}/invoice', {'invoiced': True})[0] == 200


def test_unknown_role_in_the_database_fails_closed(tech, db):
    db.execute("UPDATE users SET role='intern' WHERE username='tech'")
    db.commit()
    assert tech.json('POST', '/api/customers', {'name': 'x'})[0] == 403
    assert tech.json('GET', '/api/customers')[0] == 200
