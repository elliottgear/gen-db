import json


def test_create_returns_the_stored_customer_with_audit_fields(tech):
    status, c = tech.json('POST', '/api/customers', {
        'name': '  Dennis & Karen Roy ', 'email': 'droy@example.com', 'phone': '(207) 555-0198',
        'physicalAddress': '402 Tidewater Ln\nFalmouth, ME 04105', 'billingAddress': 'PO Box 220', 'notes': 'Dog'})
    assert status == 200
    assert c['name'] == 'Dennis & Karen Roy'
    assert c['physicalAddress'] == '402 Tidewater Ln\nFalmouth, ME 04105'
    assert (c['createdBy'], c['updatedBy']) == ('tech', 'tech')
    assert c['updatedAt']


def test_optional_fields_default_to_empty_strings(tech):
    _, c = tech.json('POST', '/api/customers', {'name': 'Only Name'})
    assert all(c[k] == '' for k in ('email', 'phone', 'physicalAddress', 'billingAddress', 'notes'))


def test_name_is_required(tech):
    for body in ({}, {'name': ''}, {'name': '   '}, {'name': None}):
        assert tech.json('POST', '/api/customers', body) == (400, {'error': 'Name is required.'})


def test_list_is_sorted_by_name_ignoring_case(tech, make_customer):
    for name in ('zeta', 'Alpha', 'beta'):
        make_customer(name=name)
    names = [c['name'] for c in tech.json('GET', '/api/customers')[1]]
    assert names == sorted(names, key=str.lower)


def test_get_one_and_404(tech, make_customer):
    c = make_customer(name='Findable')
    assert tech.json('GET', f"/api/customers/{c['id']}")[1]['name'] == 'Findable'
    assert tech.json('GET', '/api/customers/999999') == (404, {'error': 'Customer not found.'})


def test_update_replaces_fields_and_restamps(tech, make_customer, client, make_user):
    c = make_customer(name='Before', email='a@example.com')
    make_user('other')
    status, updated = client('other').json('PUT', f"/api/customers/{c['id']}", {'name': 'After'})
    assert status == 200
    assert (updated['name'], updated['email'], updated['updatedBy'], updated['createdBy']) == ('After', '', 'other', 'tech')
    assert updated['updatedAt'] != c['updatedAt']


def test_update_with_a_stale_version_is_refused(tech, make_customer):
    c = make_customer()
    url = f"/api/customers/{c['id']}"
    assert tech.json('PUT', url, {'name': 'One', 'expectedUpdatedAt': c['updatedAt']})[0] == 200
    status, body = tech.json('PUT', url, {'name': 'Two', 'expectedUpdatedAt': c['updatedAt']})
    assert (status, body['code']) == (409, 'stale')
    assert tech.json('GET', url)[1]['name'] == 'One'


def test_update_missing_customer(tech):
    assert tech.json('PUT', '/api/customers/999999', {'name': 'x'})[0] == 404


def test_delete_is_audited_with_a_snapshot(tech, make_customer, db):
    c = make_customer(name='Gone Soon')
    assert tech.json('DELETE', f"/api/customers/{c['id']}") == (200, {'ok': True})
    assert tech.json('GET', f"/api/customers/{c['id']}")[0] == 404
    row = db.execute("SELECT * FROM audit_log WHERE entity='customer' AND entity_id=?", (c['id'],)).fetchone()
    assert (row['username'], row['action']) == ('tech', 'delete')
    assert json.loads(row['detail'])['name'] == 'Gone Soon'


def test_cannot_delete_a_customer_who_still_has_generators(tech, make_generator):
    gen = make_generator()
    status, body = tech.json('DELETE', f"/api/customers/{gen['customerId']}")
    assert status == 409 and '1 generator(s)' in body['error']


def test_unicode_and_special_characters_round_trip(tech):
    name = 'Gagné & Côté <b>"Ltd"</b> — 照片 🔌'
    _, c = tech.json('POST', '/api/customers', {'name': name, 'notes': "O'Brien; DROP TABLE customers;--"})
    assert tech.json('GET', f"/api/customers/{c['id']}")[1]['name'] == name
    assert tech.json('GET', '/api/customers')[0] == 200


def test_field_length_limits(tech):
    assert tech.json('POST', '/api/customers', {'name': 'x' * 201})[0] == 400
    assert tech.json('POST', '/api/customers', {'name': 'x', 'notes': 'n' * 5000})[0] == 200
    assert tech.json('POST', '/api/customers', {'name': 'x', 'notes': 'n' * 5001})[0] == 400


def test_wrong_field_types_are_400_not_500(tech):
    for body in ({'name': 5}, {'name': 'x', 'email': ['a']}, {'name': 'x', 'notes': {'a': 1}}):
        status, err = tech.json('POST', '/api/customers', body)
        assert status == 400 and 'must be text' in err['error']
