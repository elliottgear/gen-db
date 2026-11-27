"""geocode_pending(): looks addresses up once (against a local stub), caches every answer, retries failures."""
import datetime

import pytest

import geocoding as geo
import server


@pytest.fixture
def empty(db):
    db.execute('DELETE FROM generators')
    db.execute('DELETE FROM customers')
    db.commit()
    return db


def add(db, customer_address, site_address='', name='Pat Doe', phone='2075550142'):
    cust = db.execute('INSERT INTO customers (name, phone, email, physical_address) VALUES (?,?,?,?)',
                      (name, phone, 'pat@example.com', customer_address)).lastrowid
    gen = db.execute("INSERT INTO generators (customer_id, make, model, serial, physical_address) VALUES (?,?,?,?,?)",
                     (cust, 'Generac', 'G22', f'S{cust}-{site_address[:3]}', site_address)).lastrowid
    db.commit()
    return gen


def statuses(db):
    return {r['address_key']: r['status'] for r in db.execute('SELECT * FROM geocodes')}


def test_each_answer_is_cached(empty, geocoder_stub):
    geocoder_stub.known = {'1 Found St': (43.5, -70.4)}
    add(empty, '1 Found St\nSaco, ME 04072')
    add(empty, '2 Missing St\nSaco, ME 04072')
    add(empty, '3 Vague St')           # no town or ZIP: never sent
    add(empty, '')                     # no address at all: nothing to cache
    assert geo.geocode_pending(empty) == {'incomplete': 1, 'found': 1, 'not_found': 1}
    assert statuses(empty) == {'1 found st saco, me 04072': 'found', '2 missing st saco, me 04072': 'not_found',
                               '3 vague st': 'incomplete'}
    row = empty.execute("SELECT lat, lng FROM geocodes WHERE status='found'").fetchone()
    assert (row['lat'], row['lng']) == (43.5, -70.4)


def test_only_addresses_are_sent(empty, geocoder_stub):
    add(empty, '1 Found St\nSaco, ME 04072', name='Marlene Ouellette', phone='2075559999')
    geo.geocode_pending(empty)
    [batch] = geocoder_stub.requests
    assert batch['rows'] == [['1', '1 Found St', 'Saco', 'ME', '04072']]
    assert 'Marlene' not in batch['raw'] and '2075559999' not in batch['raw'] and 'example.com' not in batch['raw']


def test_cached_addresses_arent_looked_up_again(empty, geocoder_stub):
    add(empty, '1 Found St\nSaco, ME 04072')
    add(empty, '1 found st\n  Saco, ME 04072')  # the same address typed differently
    geo.geocode_pending(empty)
    assert geo.geocode_pending(empty) == {}
    assert len(geocoder_stub.requests) == 1 and len(geocoder_stub.requests[0]['rows']) == 1


def test_an_edited_address_is_looked_up_again(empty, geocoder_stub):
    gen = add(empty, '', site_address='1 Old Rd\nSaco, ME')
    geo.geocode_pending(empty)
    empty.execute('UPDATE generators SET physical_address=? WHERE id=?', ('9 New Rd\nSaco, ME', gen))
    empty.commit()
    geo.geocode_pending(empty)
    assert [r[1] for b in geocoder_stub.requests for r in b['rows']] == ['1 Old Rd', '9 New Rd']


def test_the_site_address_is_used_before_the_customers(empty, geocoder_stub):
    add(empty, '2 Home St\nSaco, ME', site_address='1 Site Rd\nWindham, ME')
    geo.geocode_pending(empty)
    assert [r[1] for r in geocoder_stub.requests[0]['rows']] == ['1 Site Rd']


def test_geocoder_errors_are_failed_and_retried_after_an_hour(empty, geocoder_stub, frozen_clock):
    geocoder_stub.status = 500
    add(empty, '1 Found St\nSaco, ME 04072')
    assert geo.geocode_pending(empty) == {'failed': 1}
    geocoder_stub.status = 200
    geocoder_stub.known = {'1 Found St': (43.5, -70.4)}
    frozen_clock.frozen += datetime.timedelta(minutes=59)
    assert geo.geocode_pending(empty) == {}               # too soon
    frozen_clock.frozen += datetime.timedelta(minutes=2)
    assert geo.geocode_pending(empty) == {'found': 1}


def test_unreachable_geocoder_doesnt_raise(empty):
    add(empty, '1 Found St\nSaco, ME 04072')  # GEOCODER_URL points at a closed port in tests
    assert geo.geocode_pending(empty) == {'failed': 1}


def test_large_sets_go_in_batches(empty, geocoder_stub, monkeypatch):
    monkeypatch.setattr(geo, 'BATCH_SIZE', 2)
    for i in range(5):
        add(empty, f'{i + 1} Main St\nSaco, ME')
    assert geo.geocode_pending(empty) == {'not_found': 5}
    assert [len(b['rows']) for b in geocoder_stub.requests] == [2, 2, 1]


def test_the_background_loop_survives_errors(monkeypatch):
    calls = []

    def boom(conn):
        calls.append(1)
        raise RuntimeError('boom')

    def stop(seconds):
        raise SystemExit
    monkeypatch.setattr(geo, 'geocode_pending', boom)
    monkeypatch.setattr(server.time, 'sleep', stop)
    with pytest.raises(SystemExit):
        server._geocode_loop()  # the error is printed, then it goes to sleep rather than dying
    assert calls == [1]
