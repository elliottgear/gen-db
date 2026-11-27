"""GET /api/generator-map: where each generator goes on the map, or why it can't."""
import pytest

import geocoding as geo


@pytest.fixture
def setup(db, make_customer, make_generator, admin):
    db.execute('DELETE FROM generators')
    db.commit()

    def gen(customer_address='', site_address=''):
        cust = make_customer(admin, physicalAddress=customer_address, phone='2075559999', email='who@example.com')
        return make_generator(admin, customer_id=cust['id'], physicalAddress=site_address)['id']
    return gen


def cache(db, address, status, lat=None, lng=None):
    db.execute("INSERT INTO geocodes (address_key, lat, lng, status, updated_at) VALUES (?,?,?,?, 't')",
               (geo.address_key(address), lat, lng, status))
    db.commit()


def test_points_and_reasons(admin, db, setup):
    site = setup('2 Home St\nSaco, ME', site_address='1 Site Rd\nWindham, ME')
    home = setup('3 Home St\nSaco, ME')
    missing = setup('4 Nowhere Ln\nSaco, ME')
    vague = setup('5 Vague St')
    nothing = setup()
    pending = setup('6 New St\nSaco, ME')
    failed = setup('7 Flaky St\nSaco, ME')
    cache(db, '1 Site Rd\nWindham, ME', 'found', 43.8, -70.4)
    cache(db, '3 Home St\nSaco, ME', 'found', 43.5, -70.44)
    cache(db, '4 Nowhere Ln\nSaco, ME', 'not_found')
    cache(db, '5 Vague St', 'incomplete')
    cache(db, '7 Flaky St\nSaco, ME', 'failed')
    status, body = admin.json('GET', '/api/generator-map')
    assert status == 200
    assert body['points'] == [{'generatorId': site, 'lat': 43.8, 'lng': -70.4, 'from': 'site'},
                              {'generatorId': home, 'lat': 43.5, 'lng': -70.44, 'from': 'customer'}]
    assert body['unplaced'] == [{'generatorId': missing, 'reason': 'not_found'},
                                {'generatorId': vague, 'reason': 'incomplete'},
                                {'generatorId': nothing, 'reason': 'no_address'},
                                {'generatorId': pending, 'reason': 'pending'},
                                {'generatorId': failed, 'reason': 'pending'}]


def test_no_contact_details_in_the_answer(admin, setup):
    setup('3 Home St\nSaco, ME')
    raw = admin.request('GET', '/api/generator-map')[1].decode()
    assert '2075559999' not in raw and 'who@example.com' not in raw and 'Home St' not in raw


def test_technicians_can_use_the_map(tech):
    assert tech.json('GET', '/api/generator-map')[0] == 200


def test_signed_out_gets_401(client):
    assert client().json('GET', '/api/generator-map')[0] == 401
