import json

import pytest

from tests.conftest import FROZEN_MAINE_DATE


def test_create_links_to_a_customer_and_logs_history(tech, make_customer):
    cust = make_customer(name='Marlene')
    status, gen = tech.json('POST', '/api/generators', {
        'customerId': cust['id'], 'make': 'Kohler', 'model': '14RESAL', 'serial': 'KOH-1',
        'installDate': '2019-09-30', 'lastServiceDate': '2024-02-11', 'nextServiceDate': '2025-02-11'})
    assert status == 200
    assert (gen['customerId'], gen['nextServiceDate'], gen['createdBy']) == (cust['id'], '2025-02-11', 'tech')
    assert [(h['date'], h['event'], h['detail']) for h in gen['history']] == \
        [('2019-09-30', 'Added to registry', 'Linked to Marlene.')]
    assert gen['attachments'] == []


def test_create_refuses_next_service_on_or_before_last_service(tech, make_customer):
    body = {'customerId': make_customer()['id'], 'make': 'M', 'model': 'X', 'serial': 'S',
            'lastServiceDate': '2026-06-01', 'nextServiceDate': '2026-06-01'}
    assert tech.json('POST', '/api/generators', body)[0] == 400


def test_history_date_defaults_to_today_in_maine(tech, make_customer, frozen_clock):
    _, gen = tech.json('POST', '/api/generators', {'customerId': make_customer()['id'], 'make': 'M', 'model': 'X', 'serial': 'S'})
    assert gen['history'][0]['date'] == FROZEN_MAINE_DATE


@pytest.mark.parametrize('missing', ['make', 'model', 'serial'])
def test_make_model_serial_are_required(tech, make_customer, missing):
    body = {'customerId': make_customer()['id'], 'make': 'M', 'model': 'X', 'serial': 'S'}
    body[missing] = '  '
    assert tech.json('POST', '/api/generators', body)[0] == 400


@pytest.mark.parametrize('customer_id', [None, 0, 'abc', 999999])
def test_customer_must_exist(tech, customer_id):
    assert tech.json('POST', '/api/generators', {'customerId': customer_id, 'make': 'M', 'model': 'X', 'serial': 'S'})[0] == 400


def test_duplicate_serial_for_the_same_make_is_refused_ignoring_case(tech, make_generator):
    gen = make_generator(make='Generac', serial='GEN-1')
    status, body = tech.json('POST', '/api/generators', {
        'customerId': gen['customerId'], 'make': 'GENERAC', 'model': 'X', 'serial': 'gen-1'})
    assert status == 409 and 'already on file' in body['error']


def test_same_serial_under_a_different_make_is_allowed(make_generator):
    make_generator(make='Generac', serial='SAME-1')
    make_generator(make='Kohler', serial='SAME-1')


def test_editing_into_a_duplicate_serial_is_refused(tech, make_generator):
    a = make_generator(serial='A-1')
    b = make_generator(serial='B-1')
    assert tech.json('PUT', f"/api/generators/{b['id']}", {'make': b['make'], 'model': 'X', 'serial': 'a-1'})[0] == 409


def test_editing_without_changing_the_serial_is_fine(tech, make_generator):
    gen = make_generator()
    assert tech.json('PUT', f"/api/generators/{gen['id']}", {'make': gen['make'], 'model': 'New', 'serial': gen['serial']})[0] == 200


def test_list_includes_history_and_attachments_for_every_unit(tech, make_generator):
    ids = {make_generator()['id'] for _ in range(3)}
    gens = {g['id']: g for g in tech.json('GET', '/api/generators')[1]}
    assert ids <= set(gens)
    assert all(gens[i]['history'] and gens[i]['attachments'] == [] for i in ids)


def test_get_missing_generator(tech):
    assert tech.json('GET', '/api/generators/999999') == (404, {'error': 'Generator not found.'})


class TestUpdate:
    def test_changing_service_dates_by_hand_is_logged(self, tech, make_generator, frozen_clock):
        gen = make_generator(lastServiceDate='2025-01-01')
        _, gen = tech.json('PUT', f"/api/generators/{gen['id']}", {
            'make': gen['make'], 'model': gen['model'], 'serial': gen['serial'],
            'lastServiceDate': '2025-01-01', 'nextServiceDate': '2026-01-15'})
        last = gen['history'][-1]
        assert (last['event'], last['date'], last['by']) == ('Service dates edited', FROZEN_MAINE_DATE, 'tech')
        assert last['detail'] == 'Next service: none → 2026-01-15.'

    def test_unchanged_service_dates_add_no_history(self, tech, make_generator):
        gen = make_generator()
        _, gen = tech.json('PUT', f"/api/generators/{gen['id']}", {'make': gen['make'], 'model': 'X', 'serial': gen['serial']})
        assert len(gen['history']) == 1

    def test_stale_version_is_refused(self, tech, make_generator):
        gen = make_generator()
        body = {'make': gen['make'], 'model': 'X', 'serial': gen['serial'], 'expectedUpdatedAt': gen['updatedAt']}
        assert tech.json('PUT', f"/api/generators/{gen['id']}", body)[0] == 200
        status, err = tech.json('PUT', f"/api/generators/{gen['id']}", body)
        assert (status, err['code']) == (409, 'stale')

    def test_invalid_date(self, tech, make_generator):
        gen = make_generator()
        assert tech.json('PUT', f"/api/generators/{gen['id']}", {
            'make': 'M', 'model': 'X', 'serial': 'S', 'installDate': '"><script>'})[0] == 400

    def test_missing_generator(self, tech):
        assert tech.json('PUT', '/api/generators/999999', {'make': 'M', 'model': 'X', 'serial': 'S'})[0] == 404

    @pytest.mark.parametrize('next_date', ['2026-05-01', '2026-06-01'])
    def test_next_service_on_or_before_last_service_is_refused(self, tech, make_generator, next_date):
        gen = make_generator()
        status, body = tech.json('PUT', f"/api/generators/{gen['id']}", {
            'make': gen['make'], 'model': 'X', 'serial': gen['serial'],
            'lastServiceDate': '2026-06-01', 'nextServiceDate': next_date})
        assert (status, body['error']) == (400, 'Next service date must be after the last service date.')

    def test_either_date_alone_is_fine(self, tech, make_generator):
        gen = make_generator()
        base = {'make': gen['make'], 'model': 'X', 'serial': gen['serial']}
        assert tech.json('PUT', f"/api/generators/{gen['id']}", dict(base, nextServiceDate='2020-01-01'))[0] == 200
        assert tech.json('PUT', f"/api/generators/{gen['id']}", dict(base, lastServiceDate='2030-01-01'))[0] == 200

    def test_records_with_old_bad_dates_can_still_be_edited_if_dates_are_untouched(self, tech, make_generator, db):
        gen = make_generator()
        db.execute("UPDATE generators SET last_service_date='2026-06-01', next_service_date='2026-01-01' WHERE id=?",
                   (gen['id'],))
        db.commit()
        body = {'make': gen['make'], 'model': 'X', 'serial': gen['serial'], 'notes': 'gate code 1234',
                'lastServiceDate': '2026-06-01', 'nextServiceDate': '2026-01-01'}
        assert tech.json('PUT', f"/api/generators/{gen['id']}", body)[0] == 200
        assert tech.json('PUT', f"/api/generators/{gen['id']}", dict(body, nextServiceDate='2026-02-01'))[0] == 400


class TestTransfer:
    def test_moves_the_unit_and_records_who_and_why(self, tech, make_generator, make_customer, frozen_clock):
        gen = make_generator()
        new_owner = make_customer(name='New Owner')
        status, gen2 = tech.json('POST', f"/api/generators/{gen['id']}/transfer",
                                 {'newCustomerId': new_owner['id'], 'note': 'House sold'})
        assert status == 200 and gen2['customerId'] == new_owner['id']
        last = gen2['history'][-1]
        assert (last['event'], last['date'], last['by']) == ('Transferred', FROZEN_MAINE_DATE, 'tech')
        assert last['detail'] == 'Transferred from Test Customer to New Owner. Note: House sold'
        assert len(gen2['history']) == len(gen['history']) + 1  # earlier history kept

    @pytest.mark.parametrize('target', ['same', 'same-as-string'])
    def test_to_the_current_owner_is_refused(self, tech, make_generator, target):
        gen = make_generator()
        cid = gen['customerId'] if target == 'same' else str(gen['customerId'])
        assert tech.json('POST', f"/api/generators/{gen['id']}/transfer", {'newCustomerId': cid})[0] == 400

    @pytest.mark.parametrize('cid', [None, 999999, 'x'])
    def test_needs_an_existing_destination(self, tech, make_generator, cid):
        assert tech.json('POST', f"/api/generators/{make_generator()['id']}/transfer", {'newCustomerId': cid})[0] == 400

    def test_missing_generator(self, tech, make_customer):
        assert tech.json('POST', '/api/generators/999999/transfer', {'newCustomerId': make_customer()['id']})[0] == 404


class TestDelete:
    def test_removes_the_unit_and_its_history_and_audits_a_snapshot(self, tech, serviced_generator, db):
        gen, _ = serviced_generator
        assert tech.json('DELETE', f"/api/generators/{gen['id']}") == (200, {'ok': True})
        assert tech.json('GET', f"/api/generators/{gen['id']}")[0] == 404
        assert db.execute('SELECT COUNT(*) FROM generator_history WHERE generator_id=?', (gen['id'],)).fetchone()[0] == 0
        row = db.execute("SELECT * FROM audit_log WHERE entity='generator' AND entity_id=?", (gen['id'],)).fetchone()
        snapshot = json.loads(row['detail'])
        assert snapshot['serial'] == gen['serial'] and any(h['event'] == 'Serviced' for h in snapshot['history'])

    def test_missing_generator(self, tech):
        assert tech.json('DELETE', '/api/generators/999999')[0] == 404
