"""Appointment requests on the admin side: admins only (reads included), suggestions, and handling."""
import pytest

import appointment_requests as requests_mod
import matching
import server

REQUEST_ROUTES = [(method, pattern) for method, pattern, _fn, perm in server.ROUTES
                  if pattern.pattern.startswith('^/api/appointment-requests')]


def path_for(pattern, req_id):
    return pattern.pattern.strip('^$').replace(r'(\d+)', str(req_id))


@pytest.fixture
def new_request(db):
    """Stores a request the way the public form would, without going through Turnstile."""
    def make(status='new', **fields):
        # Contact details unlike the three demo customers the test database is seeded with.
        values = {'name': 'Pat Doe', 'phone': '(207) 555-0987', 'email': 'pat@example.com',
                  'service_street': '12 Main St', 'service_town': 'Saco', 'brand': 'Generac', 'model': '',
                  'need': 'repair', 'notes': ''}
        values.update(fields)
        values['phone_digits'] = matching.phone_digits(values['phone'])
        cols = ', '.join(values)
        cur = db.execute(f'INSERT INTO appointment_requests (created_at, status, updated_at, {cols}) '
                         f'VALUES (?, ?, ?, {", ".join("?" * len(values))})',
                         ('2026-11-20T09:00:00.000000-05:00', status, '2026-11-20T09:00:00.000000-05:00',
                          *values.values()))
        db.commit()
        return cur.lastrowid
    return make


def get(admin, req_id):
    status, body = admin.json('GET', f'/api/appointment-requests/{req_id}')
    assert status == 200, body
    return body


def act(admin, req_id, action, **body):
    req = get(admin, req_id)['request']
    return admin.json('POST', f'/api/appointment-requests/{req_id}/{action}', dict(body, expectedUpdatedAt=req['updatedAt']))


# ------------------------------------------------------------------ access --

def test_the_routes_under_test_include_the_reads():
    assert ('GET', '^/api/appointment-requests$') in [(m, p.pattern) for m, p in REQUEST_ROUTES]
    assert ('GET', r'^/api/appointment-requests/(\d+)$') in [(m, p.pattern) for m, p in REQUEST_ROUTES]
    assert len(REQUEST_ROUTES) == 5


def test_technicians_get_403_from_every_appointment_request_route(tech, new_request):
    req_id = new_request()
    for method, pattern in REQUEST_ROUTES:
        status, body = tech.json(method, path_for(pattern, req_id), {'status': 'dismissed', 'customerId': 1})
        assert status == 403, (method, pattern.pattern, status)
        assert body == {'error': "You don't have permission to view and handle appointment requests. Ask an admin."}


def test_technicians_get_403_not_404_for_requests_that_dont_exist(tech):
    for method, pattern in REQUEST_ROUTES:
        assert tech.json(method, path_for(pattern, 999), {})[0] == 403


def test_technician_403_changes_nothing(tech, db, new_request):
    req_id = new_request()
    for method, pattern in REQUEST_ROUTES:
        tech.json(method, path_for(pattern, req_id), {'status': 'dismissed', 'name': 'Hijack'})
    row = db.execute('SELECT status, customer_id FROM appointment_requests').fetchone()
    assert tuple(row) == ('new', None)
    assert db.execute("SELECT COUNT(*) FROM customers WHERE name='Hijack'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE entity='appointment_request'").fetchone()[0] == 0


def test_signed_out_gets_401_from_every_route(client, new_request):
    req_id = new_request()
    for method, pattern in REQUEST_ROUTES:
        assert client().json(method, path_for(pattern, req_id), {})[0] == 401


def test_admins_can_read(admin, new_request):
    req_id = new_request()
    assert admin.json('GET', '/api/appointment-requests')[0] == 200
    assert admin.json('GET', f'/api/appointment-requests/{req_id}')[0] == 200


def test_technicians_are_not_told_they_have_the_permission(tech, admin):
    assert 'requests.manage' not in tech.json('GET', '/api/me')[1]['permissions']
    assert 'requests.manage' in admin.json('GET', '/api/me')[1]['permissions']


# ------------------------------------------------------------- suggestions --

def test_list_shows_the_best_suggestion_with_reasons(admin, new_request, make_customer):
    cust = make_customer(admin, name='Pat Doe', phone='207-555-0987', physicalAddress='12 Main Street\nSaco')
    new_request()
    [item] = admin.json('GET', '/api/appointment-requests')[1]['requests']
    s = item['suggestion']
    assert s['customerId'] == cust['id'] and s['tier'] == 'strong'
    assert s['reasons'] == ['phone matches', 'address matches', 'name matches']
    assert item['conflict'] is False


def test_no_suggestion_when_nothing_matches(admin, new_request, make_customer):
    make_customer(admin, name='Pat Doe')  # name only: not enough
    new_request()
    assert admin.json('GET', '/api/appointment-requests')[1]['requests'][0]['suggestion'] is None


def test_detail_lists_candidates_and_flags_conflicts(admin, new_request, make_customer):
    a = make_customer(admin, name='A', phone='2075550987')
    b = make_customer(admin, name='B', email='pat@example.com')
    body = get(admin, new_request())
    assert body['conflict'] is True
    assert {c['customerId'] for c in body['candidates']} == {a['id'], b['id']}


def test_list_flags_database_brands_missing_from_the_form(admin, make_generator):
    make_generator(admin, make='Deere')
    make_generator(admin, make='kohler')  # case doesn't matter
    assert admin.json('GET', '/api/appointment-requests')[1]['formBrandGaps'] == ['Deere']


def test_brand_gap_check_ignores_blank_makes(db):
    assert requests_mod.brands_missing_from_form(db) == []


# ----------------------------------------------------------------- actions --

def test_confirming_a_match_links_customer_and_generator(admin, db, new_request, make_generator):
    gen = make_generator(admin)
    req_id = new_request()
    status, body = act(admin, req_id, 'match', customerId=gen['customerId'], generatorId=gen['id'])
    assert status == 200
    assert (body['status'], body['customerId'], body['generatorId']) == ('matched', gen['customerId'], gen['id'])
    assert body['handledBy'] == 'boss'
    audit = db.execute("SELECT action FROM audit_log WHERE entity='appointment_request'").fetchall()
    assert [r[0] for r in audit] == ['match']


def test_match_without_a_generator(admin, new_request, make_customer):
    cust = make_customer(admin)
    status, body = act(admin, new_request(), 'match', customerId=cust['id'])
    assert status == 200 and body['generatorId'] is None


def test_match_refuses_another_customers_generator(admin, new_request, make_customer, make_generator):
    other_gen = make_generator(admin)
    cust = make_customer(admin, name='Someone else')
    status, body = act(admin, new_request(), 'match', customerId=cust['id'], generatorId=other_gen['id'])
    assert status == 400 and "doesn't belong" in body['error']


@pytest.mark.parametrize('customer_id', [None, 0, 'x', 999])
def test_match_needs_a_real_customer(admin, new_request, customer_id):
    assert act(admin, new_request(), 'match', customerId=customer_id)[0] == 400


def test_only_new_requests_can_be_matched(admin, new_request, make_customer):
    cust = make_customer(admin)
    req_id = new_request(status='dismissed')
    assert act(admin, req_id, 'match', customerId=cust['id'])[0] == 409


def test_stale_edits_are_refused(admin, new_request, make_customer):
    cust = make_customer(admin)
    req_id = new_request()
    status, body = admin.json('POST', f'/api/appointment-requests/{req_id}/match',
                              {'customerId': cust['id'], 'expectedUpdatedAt': 'old'})
    assert status == 409 and body['code'] == 'stale'


def test_create_customer_from_a_request(admin, db, new_request):
    req_id = new_request()
    before = db.execute('SELECT COUNT(*) FROM customers').fetchone()[0]
    status, body = act(admin, req_id, 'create-customer', name='Pat Doe', phone='(207) 555-0987',
                       email='pat@example.com', physicalAddress='12 Main St', billingAddress='12 Main St', notes='From web')
    assert status == 200
    cust = body['customer']
    assert cust['name'] == 'Pat Doe' and cust['createdBy'] == 'boss'
    assert body['request']['status'] == 'matched' and body['request']['customerId'] == cust['id']
    assert db.execute('SELECT COUNT(*) FROM customers').fetchone()[0] == before + 1


def test_create_customer_validates_like_the_normal_form(admin, db, new_request):
    req_id = new_request()
    before = db.execute('SELECT COUNT(*) FROM customers').fetchone()[0]
    assert act(admin, req_id, 'create-customer', name='')[0] == 400
    assert db.execute('SELECT COUNT(*) FROM customers').fetchone()[0] == before
    assert get(admin, req_id)['request']['status'] == 'new'


def test_create_customer_needs_the_customer_edit_permission_too(admin, client, make_user, monkeypatch, new_request):
    monkeypatch.setitem(server.ROLES, 'dispatcher', {'requests.manage'})
    make_user('dispatcher', 'dispatcher')
    dispatcher = client('dispatcher')
    status, body = act(dispatcher, new_request(), 'create-customer', name='Pat')
    assert status == 403 and 'add or edit customers' in body['error']


@pytest.mark.parametrize('start, target', sorted(requests_mod.MANUAL_TRANSITIONS))
def test_allowed_status_changes(admin, new_request, start, target):
    status, body = act(admin, new_request(status=start), 'status', status=target)
    assert status == 200 and body['status'] == target


@pytest.mark.parametrize('start, target', [
    ('new', 'scheduled'), ('new', 'matched'), ('new', 'new'), ('dismissed', 'scheduled'),
    ('scheduled', 'new'), ('scheduled', 'dismissed'), ('matched', 'dismissed'), ('new', 'archived'), ('matched', 'done'),
])
def test_other_status_changes_are_refused(admin, new_request, start, target):
    assert act(admin, new_request(status=start), 'status', status=target)[0] == 400


def test_dismissing_records_a_reason(admin, new_request):
    req_id = new_request()
    assert act(admin, req_id, 'status', status='dismissed')[1]['dismissReason'] == 'dismissed by admin'
    act(admin, req_id, 'status', status='new')
    assert act(admin, req_id, 'status', status='dismissed', reason='duplicate')[1]['dismissReason'] == 'duplicate'


def test_unlinking_clears_the_match(admin, new_request, make_generator):
    gen = make_generator(admin)
    req_id = new_request()
    act(admin, req_id, 'match', customerId=gen['customerId'], generatorId=gen['id'])
    body = act(admin, req_id, 'status', status='new')[1]
    assert (body['status'], body['customerId'], body['generatorId']) == ('new', None, None)


def test_deleting_the_customer_keeps_the_request(admin, db, new_request, make_customer):
    cust = make_customer(admin)
    req_id = new_request()
    act(admin, req_id, 'match', customerId=cust['id'])
    assert admin.json('DELETE', f"/api/customers/{cust['id']}")[0] == 200
    body = get(admin, req_id)['request']
    assert body['status'] == 'matched' and body['customerId'] is None


def test_missing_request_is_404_for_admins(admin):
    assert admin.json('GET', '/api/appointment-requests/999')[0] == 404
    assert admin.json('POST', '/api/appointment-requests/999/status', {'status': 'dismissed'})[0] == 404


def test_matched_then_scheduled_then_back(admin, new_request, make_customer):
    cust = make_customer(admin)
    req_id = new_request()
    act(admin, req_id, 'match', customerId=cust['id'])
    body = act(admin, req_id, 'status', status='scheduled')[1]
    assert body['status'] == 'scheduled' and body['customerId'] == cust['id']
    body = act(admin, req_id, 'status', status='matched')[1]
    assert body['status'] == 'matched' and body['customerId'] == cust['id']  # still linked


def test_request_json_has_street_and_town(admin, new_request):
    body = get(admin, new_request(service_street='5 Pine St', service_town='Biddeford'))['request']
    assert (body['serviceStreet'], body['serviceTown']) == ('5 Pine St', 'Biddeford')
    assert 'serviceAddress' not in body


def test_matching_uses_the_street(admin, new_request, make_customer):
    cust = make_customer(admin, name='Someone', physicalAddress='5 Pine Street\nBiddeford, ME')
    new_request(service_street='5 Pine St', service_town='Biddeford')
    [item] = admin.json('GET', '/api/appointment-requests')[1]['requests']
    assert item['suggestion']['customerId'] == cust['id'] and item['suggestion']['reasons'] == ['address matches']


# ------------------------------------------------------------- address lines --

def test_requests_carry_the_zip_and_a_full_town_line(admin, new_request):
    req = get(admin, new_request(service_zip='04072'))['request']
    assert (req['serviceZip'], req['serviceTownLine']) == ('04072', 'Saco, ME 04072')


def test_requests_from_before_the_zip_field_have_just_the_town(admin, new_request):
    req = get(admin, new_request())['request']
    assert (req['serviceZip'], req['serviceTownLine']) == ('', 'Saco')


def test_matching_uses_the_zip(admin, new_request, make_customer):
    make_customer(admin, name='Someone Else', physicalAddress='12 Main St\nPortland, ME 04101')
    assert get(admin, new_request(service_zip='04072'))['candidates'] == []
    assert [c['customerName'] for c in get(admin, new_request(service_zip='04101'))['candidates']] == ['Someone Else']
