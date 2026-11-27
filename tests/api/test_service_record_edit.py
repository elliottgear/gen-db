"""PUT /api/service-records/:id: a service visit can be edited until it's invoiced."""
import json

import pytest

import server


def record(gen, hist_id):
    return next(h for h in gen['history'] if h['id'] == hist_id)


def edit(c, hist_id, **body):
    base = {'serviceDate': '2026-06-02', 'nextServiceDate': '2027-06-02', 'notes': 'Changed oil and filter.'}
    base.update(body)
    return c.json('PUT', f'/api/service-records/{hist_id}', base)


def log(c, gen_id, service_date, next_date='', notes=''):
    status, gen = c.json('POST', f'/api/generators/{gen_id}/service-records',
                         {'serviceDate': service_date, 'nextServiceDate': next_date, 'notes': notes})
    assert status == 200, gen
    return gen, gen['history'][-1]['id']


@pytest.fixture
def other_tech(make_user, client):
    make_user('tech2', 'technician')
    return client('tech2')


# ------------------------------------------------------------------ who and when --

def test_the_logger_can_edit_their_own_record(tech, serviced_generator):
    _, hist_id = serviced_generator
    status, gen = edit(tech, hist_id)
    assert status == 200
    r = record(gen, hist_id)
    assert (r['date'], r['nextServiceDate'], r['notes']) == ('2026-06-02', '2027-06-02', 'Changed oil and filter.')
    assert r['detail'] == 'Changed oil and filter. Next service scheduled for 2027-06-02.'
    assert r['by'] == 'tech' and r['updatedBy'] == 'tech' and r['updatedAt']


def test_another_technician_cannot(other_tech, serviced_generator):
    _, hist_id = serviced_generator
    status, body = edit(other_tech, hist_id)
    assert status == 403 and 'records you logged' in body['error']


def test_admins_can_edit_anyones(admin, serviced_generator):
    _, hist_id = serviced_generator
    status, gen = edit(admin, hist_id, notes='')
    assert status == 200 and record(gen, hist_id)['detail'] == 'Routine service. Next service scheduled for 2027-06-02.'


def test_invoiced_records_are_locked_until_undone(tech, admin, serviced_generator):
    _, hist_id = serviced_generator
    assert admin.json('POST', f'/api/service-records/{hist_id}/invoice', {'invoiced': True})[0] == 200
    for c in (tech, admin):
        status, body = edit(c, hist_id)
        assert status == 409 and 'invoiced' in body['error']
    assert admin.json('POST', f'/api/service-records/{hist_id}/invoice', {'invoiced': False})[0] == 200
    assert edit(tech, hist_id)[0] == 200


def test_stale_edits_are_refused(tech, admin, serviced_generator):
    _, hist_id = serviced_generator
    status, gen = edit(admin, hist_id, expectedUpdatedAt='')
    assert status == 200
    status, body = edit(tech, hist_id, expectedUpdatedAt='', notes='mine')  # opened before the admin's edit
    assert status == 409 and body.get('code') == 'stale'
    assert edit(tech, hist_id, expectedUpdatedAt=record(gen, hist_id)['updatedAt'])[0] == 200


@pytest.mark.parametrize('body, message', [
    ({'serviceDate': ''}, 'service date is required'),
    ({'serviceDate': '2026-13-01'}, 'service date'),
    ({'nextServiceDate': '2026-06-02'}, 'after the service date'),
    ({'nextServiceDate': '2026-01-01'}, 'after the service date'),
    ({'notes': 'x' * (server.LONG_TEXT + 1)}, 'too long'),
])
def test_validation(tech, serviced_generator, body, message):
    _, hist_id = serviced_generator
    status, out = edit(tech, hist_id, **body)
    assert status == 400 and message in out['error'].lower()


def test_only_service_records_and_real_ids(tech, admin, serviced_generator):
    gen, _ = serviced_generator
    added = next(h for h in gen['history'] if h['event'] == 'Added to registry')
    assert edit(admin, added['id'])[0] == 400
    assert edit(admin, 999999)[0] == 404


def test_signed_out_gets_401(client, serviced_generator):
    _, hist_id = serviced_generator
    assert edit(client(), hist_id)[0] == 401


def test_edits_are_audited(admin, db, serviced_generator):
    _, hist_id = serviced_generator
    edit(admin, hist_id)
    row = db.execute("SELECT * FROM audit_log WHERE action='edit' AND entity='service_record'").fetchone()
    detail = json.loads(row['detail'])
    assert row['entity_id'] == hist_id and row['username'] == 'boss'
    assert detail['before'] == {'date': '2026-06-01', 'nextServiceDate': '2027-06-01', 'notes': ''}
    assert detail['after']['notes'] == 'Changed oil and filter.'


# ----------------------------------------------------------------- generator dates --

def dates(c, gen_id):
    gen = c.json('GET', f'/api/generators/{gen_id}')[1]
    return gen['lastServiceDate'], gen['nextServiceDate']


def test_editing_the_latest_visit_moves_the_generators_dates(tech, serviced_generator):
    gen, hist_id = serviced_generator
    status, out = edit(tech, hist_id, serviceDate='2026-06-03', nextServiceDate='2026-12-03')
    assert dates(tech, gen['id']) == ('2026-06-03', '2026-12-03')
    note = [h for h in out['history'] if h['event'] == 'Service dates edited'][-1]['detail']
    assert note == ('Last service: 2026-06-01 → 2026-06-03. Next service: 2027-06-01 → 2026-12-03. '
                    'From an edited service record.')


def test_editing_an_older_visit_leaves_the_generator_alone(tech, make_generator):
    gen = make_generator()
    _, old = log(tech, gen['id'], '2025-06-01', '2026-06-01')
    log(tech, gen['id'], '2026-06-01', '2027-06-01')
    edit(tech, old, serviceDate='2025-05-01', nextServiceDate='2025-11-01')
    assert dates(tech, gen['id']) == ('2026-06-01', '2027-06-01')


def test_an_older_visit_moved_past_the_latest_becomes_the_latest(tech, make_generator):
    gen = make_generator()
    _, old = log(tech, gen['id'], '2025-06-01', '2026-06-01')
    log(tech, gen['id'], '2026-06-01', '2027-06-01')
    edit(tech, old, serviceDate='2026-07-01', nextServiceDate='2027-07-01')
    assert dates(tech, gen['id']) == ('2026-07-01', '2027-07-01')


def test_moving_the_latest_back_falls_back_to_the_previous_visit(tech, make_generator):
    gen = make_generator()
    log(tech, gen['id'], '2026-03-01', '2027-03-01')
    _, latest = log(tech, gen['id'], '2026-06-01', '2027-06-01')  # say, typed in the wrong year
    edit(tech, latest, serviceDate='2025-06-01', nextServiceDate='2026-06-01')
    assert dates(tech, gen['id']) == ('2026-03-01', '2027-03-01')


def test_a_newer_hand_set_date_is_never_overwritten(tech, serviced_generator):
    gen, hist_id = serviced_generator
    body = {k: gen[k] for k in ('make', 'model', 'serial')}
    body.update(lastServiceDate='2026-09-01', nextServiceDate='2027-09-01')
    assert tech.json('PUT', f"/api/generators/{gen['id']}", body)[0] == 200
    edit(tech, hist_id, serviceDate='2026-06-05', nextServiceDate='2027-06-05')
    assert dates(tech, gen['id']) == ('2026-09-01', '2027-09-01')


def test_notes_only_edits_dont_touch_the_generator(tech, serviced_generator):
    gen, hist_id = serviced_generator
    _, out = edit(tech, hist_id, serviceDate='2026-06-01', nextServiceDate='2027-06-01', notes='Belt replaced.')
    assert dates(tech, gen['id']) == ('2026-06-01', '2027-06-01')
    assert not [h for h in out['history'] if h['event'] == 'Service dates edited']


def test_logging_stores_notes_and_next_date_as_fields(tech, make_generator):
    gen = make_generator()
    gen, hist_id = log(tech, gen['id'], '2026-06-01', '2027-06-01', 'Valve adjusted.')
    r = record(gen, hist_id)
    assert (r['notes'], r['nextServiceDate']) == ('Valve adjusted.', '2027-06-01')
    assert r['detail'] == 'Valve adjusted. Next service scheduled for 2027-06-01.'
