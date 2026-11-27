"""Fixes from the Nov 26 prod QA of v0.7.0: racing saves, date limits, deleting a service record."""
import json
import threading
import time

import pytest

import core
import server
from tests.helpers import answers


def log(c, gen_id, service_date, next_date='', notes=''):
    status, gen = c.json('POST', f'/api/generators/{gen_id}/service-records',
                         {'serviceDate': service_date, 'nextServiceDate': next_date, 'notes': notes,
                          'checklistVersion': 1, 'answers': answers()})
    assert status == 200, gen
    return gen, gen['history'][-1]['id']


# ------------------------------------------------------- 1. saves from the same version --

@pytest.fixture
def slow_stale_check(monkeypatch):
    """Widens the gap between reading a row and writing it, so two saves overlap every time."""
    real = core.check_not_stale

    def slow(*args, **kwargs):
        time.sleep(0.3)
        return real(*args, **kwargs)
    monkeypatch.setattr(server, 'check_not_stale', slow)


def race(c, method, path, bodies):
    results = [None] * len(bodies)

    def go(i):
        results[i] = c.json(method, path, bodies[i])[0]
    threads = [threading.Thread(target=go, args=(i,)) for i in range(len(bodies))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sorted(results)


def test_only_one_of_two_racing_service_record_edits_wins(tech, serviced_generator, slow_stale_check):
    gen, hist_id = serviced_generator
    bodies = [{'serviceDate': '2026-06-01', 'notes': f'racer {i}', 'expectedUpdatedAt': ''} for i in (1, 2)]
    assert race(tech, 'PUT', f'/api/service-records/{hist_id}', bodies) == [200, 409]


def test_only_one_of_two_racing_generator_edits_wins(tech, make_generator, slow_stale_check):
    gen = make_generator()
    base = {k: gen[k] for k in ('make', 'model', 'serial')}
    bodies = [dict(base, notes=f'racer {i}', expectedUpdatedAt=gen['updatedAt']) for i in (1, 2)]
    assert race(tech, 'PUT', f"/api/generators/{gen['id']}", bodies) == [200, 409]


def test_only_one_of_two_racing_customer_edits_wins(tech, make_customer, slow_stale_check):
    cust = make_customer()
    bodies = [{'name': f'Racer {i}', 'expectedUpdatedAt': cust['updatedAt']} for i in (1, 2)]
    assert race(tech, 'PUT', f"/api/customers/{cust['id']}", bodies) == [200, 409]


def test_write_lock_is_a_no_op_inside_a_transaction(db):
    db.execute("UPDATE users SET email=email")  # opens an implicit transaction
    core.write_lock(db)                         # must not raise "cannot start a transaction within a transaction"
    db.commit()


# ------------------------------------------------------------------ 4. date limits --

@pytest.mark.parametrize('service_date, next_date, message', [
    ('2099-01-01', '', "can't be in the future"),
    ('1999-12-31', '', "can't be before 2000"),
    ('0001-01-01', '', "can't be before 2000"),
    ('2026-06-01', '9999-12-31', 'within 5 years'),
    ('2026-06-01', '2031-06-02', 'within 5 years'),
])
def test_service_date_limits_on_log_and_edit(tech, make_generator, serviced_generator, service_date, next_date, message):
    status, body = tech.json('POST', f"/api/generators/{make_generator()['id']}/service-records",
                             {'serviceDate': service_date, 'nextServiceDate': next_date})
    assert status == 400 and message in body['error']
    _, hist_id = serviced_generator
    status, body = tech.json('PUT', f'/api/service-records/{hist_id}',
                             {'serviceDate': service_date, 'nextServiceDate': next_date})
    assert status == 400 and message in body['error']


def test_limits_are_inclusive(tech, make_generator):
    gen = make_generator()
    log(tech, gen['id'], '2000-01-01', '2004-12-31')
    log(tech, gen['id'], server._today(), server._years_after(server._today(), 5))  # today, and exactly 5 years


def test_leap_day_plus_five_years(tech, make_generator):
    assert server._years_after('2024-02-29', 5) == '2029-02-28'
    log(tech, make_generator()['id'], '2024-02-29', '2029-02-28')


@pytest.mark.parametrize('dates, message', [
    ({'lastServiceDate': '2099-01-01'}, "last service date can't be in the future"),
    ({'lastServiceDate': '1980-01-01'}, "can't be before 2000"),
    ({'lastServiceDate': '2026-06-01', 'nextServiceDate': '2040-01-01'}, 'within 5 years of the last service date'),
    ({'nextServiceDate': '2099-01-01'}, 'within 5 years of the current date'),
])
def test_generator_dates_have_the_same_limits(tech, make_generator, make_customer, dates, message):
    gen = make_generator()
    base = {k: gen[k] for k in ('make', 'model', 'serial')}
    status, body = tech.json('PUT', f"/api/generators/{gen['id']}", dict(base, **dates))
    assert status == 400 and message in body['error']
    status, body = tech.json('POST', '/api/generators', dict(dates, customerId=make_customer()['id'], make='Kohler',
                                                            model='20RESA', serial='LIMIT-1'))
    assert status == 400 and message in body['error']


def test_old_out_of_range_dates_dont_block_other_edits(tech, make_generator, db):
    gen = make_generator()
    db.execute("UPDATE generators SET last_service_date='1900-01-01', next_service_date='9999-12-31' WHERE id=?",
               (gen['id'],))
    db.commit()
    body = {k: gen[k] for k in ('make', 'model', 'serial')}
    body.update(lastServiceDate='1900-01-01', nextServiceDate='9999-12-31', notes='only notes changed')
    assert tech.json('PUT', f"/api/generators/{gen['id']}", body)[0] == 200


# ------------------------------------------------------------- deleting a service record --

@pytest.fixture
def other_tech(make_user, client):
    make_user('tech2', 'technician')
    return client('tech2')


def dates(c, gen_id):
    gen = c.json('GET', f'/api/generators/{gen_id}')[1]
    return gen['lastServiceDate'], gen['nextServiceDate']


def test_the_logger_can_delete_and_the_unit_falls_back(tech, make_generator):
    gen = make_generator()
    log(tech, gen['id'], '2025-06-01', '2026-06-01')
    _, latest = log(tech, gen['id'], '2026-06-01', '2027-06-01')
    status, out = tech.json('DELETE', f'/api/service-records/{latest}')
    assert status == 200 and latest not in [h['id'] for h in out['history']]
    assert dates(tech, gen['id']) == ('2025-06-01', '2026-06-01')
    assert out['history'][-1]['detail'] == ('Last service: 2026-06-01 → 2025-06-01. Next service: 2027-06-01 → '
                                            '2026-06-01. From a deleted service record.')


def test_deleting_the_only_visit_clears_the_dates(tech, serviced_generator):
    gen, hist_id = serviced_generator
    assert tech.json('DELETE', f'/api/service-records/{hist_id}')[0] == 200
    assert dates(tech, gen['id']) == ('', '')


def test_deleting_an_older_visit_leaves_the_schedule(tech, make_generator):
    gen = make_generator()
    _, old = log(tech, gen['id'], '2025-06-01', '2026-06-01')
    log(tech, gen['id'], '2026-06-01', '2027-06-01')
    tech.json('DELETE', f'/api/service-records/{old}')
    assert dates(tech, gen['id']) == ('2026-06-01', '2027-06-01')


def test_a_newer_hand_set_date_survives_a_delete(tech, serviced_generator):
    gen, hist_id = serviced_generator
    body = {k: gen[k] for k in ('make', 'model', 'serial')}
    body.update(lastServiceDate='2026-09-01', nextServiceDate='2027-09-01')
    assert tech.json('PUT', f"/api/generators/{gen['id']}", body)[0] == 200
    tech.json('DELETE', f'/api/service-records/{hist_id}')
    assert dates(tech, gen['id']) == ('2026-09-01', '2027-09-01')


def test_who_can_delete(tech, other_tech, admin, make_generator):
    gen = make_generator()
    _, a = log(tech, gen['id'], '2025-06-01')
    _, b = log(tech, gen['id'], '2025-07-01')
    status, body = other_tech.json('DELETE', f'/api/service-records/{a}')
    assert status == 403 and body['error'] == 'You can only delete service records you logged.'
    assert admin.json('DELETE', f'/api/service-records/{b}')[0] == 200


def test_invoiced_records_cant_be_deleted(tech, admin, serviced_generator):
    _, hist_id = serviced_generator
    admin.json('POST', f'/api/service-records/{hist_id}/invoice', {'invoiced': True})
    for c in (tech, admin):
        status, body = c.json('DELETE', f'/api/service-records/{hist_id}')
        assert status == 409 and 'invoiced' in body['error']


def test_delete_edge_cases(tech, admin, client, serviced_generator):
    gen, hist_id = serviced_generator
    added = next(h for h in gen['history'] if h['event'] == 'Added to registry')
    assert admin.json('DELETE', f"/api/service-records/{added['id']}")[0] == 400
    assert admin.json('DELETE', '/api/service-records/999999')[0] == 404
    assert client().json('DELETE', f'/api/service-records/{hist_id}')[0] == 401
    assert tech.json('DELETE', f'/api/service-records/{hist_id}')[0] == 200
    assert tech.json('DELETE', f'/api/service-records/{hist_id}')[0] == 404  # already gone


def test_deletes_are_audited_with_a_snapshot(tech, db, make_generator):
    gen = make_generator()
    _, hist_id = log(tech, gen['id'], '2026-06-01', '2027-06-01', 'Oops, wrong unit.')
    tech.json('DELETE', f'/api/service-records/{hist_id}')
    row = db.execute("SELECT * FROM audit_log WHERE action='delete' AND entity='service_record'").fetchone()
    snap = json.loads(row['detail'])
    assert row['entity_id'] == hist_id and row['username'] == 'tech'
    assert (snap['generatorId'], snap['date'], snap['nextServiceDate'], snap['notes'], snap['loggedBy']) == \
        (gen['id'], '2026-06-01', '2027-06-01', 'Oops, wrong unit.', 'tech')


# ------------------------------------------------- click-through guard (v0.8.2) --

def test_clicks_right_after_a_modal_opens_or_closes_are_dropped():
    """Save, click, click used to invoice a different record: the save closed the form, the 2nd click
    opened the record underneath, the 3rd hit its "Mark as invoiced". The page drops pointer clicks for a
    moment after any modal opens or closes (keyboard activation, detail 0, still works)."""
    page = open(server.STATIC_FILE).read()
    assert 'const CLICK_GUARD_MS = 500;' in page
    assert "if(e.detail > 0 && Date.now() - modalChangedAt < CLICK_GUARD_MS){" in page
    assert '}, true);' in page.split('const CLICK_GUARD_MS')[1].split('/*')[0]  # capture phase: runs first
    open_fn = page.split('function openModal(m){')[1].split('\n}')[0]
    close_fn = page.split('function closeModal(){')[1].split('\n}')[0]
    assert 'modalChangedAt = Date.now()' in open_fn and 'modalChangedAt = Date.now()' in close_fn
