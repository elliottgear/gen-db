"""Retries from a phone on a weak signal never save twice: each save carries a client-generated clientId."""
import threading
import uuid

import pytest

import server
from tests.conftest import FROZEN_MAINE_DATE
from tests.helpers import answers


@pytest.fixture(autouse=True)
def _clock(frozen_clock):
    return frozen_clock


def new_body(client_id, **extra):
    body = {'serviceDate': FROZEN_MAINE_DATE, 'nextServiceDate': '2027-07-01', 'checklistVersion': 1,
            'answers': answers(), 'clientId': client_id}
    body.update(extra)
    return body


def post(c, gen_id, body):
    return c.json('POST', f'/api/generators/{gen_id}/service-records', body)


def visits(db):
    return db.execute("SELECT COUNT(*) FROM generator_history WHERE event='Serviced'").fetchone()[0]


@pytest.fixture
def subscribed_admin(make_user, client):
    make_user('betty', 'admin', email='betty@example.com', notify=['service_record'])
    return client('betty')


def test_a_repeated_create_returns_the_same_record_and_emails_once(tech, subscribed_admin, make_generator, db, smtp2go):
    gen = make_generator()
    key = str(uuid.uuid4())
    first = post(tech, gen['id'], new_body(key))
    again = post(tech, gen['id'], new_body(key, notes='a retry may even differ; the first save wins'))
    assert first[0] == again[0] == 200
    assert first[1]['serviceRecordId'] == again[1]['serviceRecordId'] and again[1]['replayed'] is True
    assert visits(db) == 1
    server.run_email_jobs()
    assert len(smtp2go.requests) == 1


def test_concurrent_repeats_still_save_once(tech, make_generator, db):
    gen = make_generator()
    key = str(uuid.uuid4())
    results = []
    threads = [threading.Thread(target=lambda: results.append(post(tech, gen['id'], new_body(key)))) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [s for s, _ in results] == [200] * 5
    assert len({b['serviceRecordId'] for _, b in results}) == 1 and visits(db) == 1


def test_different_keys_are_different_records(tech, make_generator, db):
    gen = make_generator()
    post(tech, gen['id'], new_body(str(uuid.uuid4())))
    post(tech, gen['id'], new_body(str(uuid.uuid4())))
    assert visits(db) == 2


def test_another_users_key_is_refused(tech, admin, make_generator, db):
    gen = make_generator()
    key = str(uuid.uuid4())
    post(tech, gen['id'], new_body(key))
    status, body = post(admin, gen['id'], new_body(key))
    assert status == 409 and visits(db) == 1


def test_a_replay_after_the_record_was_deleted_does_not_bring_it_back(tech, make_generator, db):
    gen = make_generator()
    key = str(uuid.uuid4())
    _, saved = post(tech, gen['id'], new_body(key))
    tech.json('DELETE', f"/api/service-records/{saved['serviceRecordId']}")
    status, body = post(tech, gen['id'], new_body(key))
    assert status == 404 and 'deleted' in body['error'] and visits(db) == 0


def test_a_refused_save_does_not_use_up_its_key(tech, make_generator, db):
    gen = make_generator()
    key = str(uuid.uuid4())
    assert post(tech, gen['id'], new_body(key, answers=answers(oil_leaks=None)))[0] == 400
    assert post(tech, gen['id'], new_body(key))[0] == 200 and visits(db) == 1


def test_the_key_must_be_a_uuid(tech, make_generator):
    status, body = post(tech, make_generator()['id'], new_body('abc'))
    assert status == 400 and 'UUID' in body['error']


def test_an_edit_replayed_after_it_went_through_is_not_a_conflict(tech, make_generator):
    gen = make_generator()
    _, saved = post(tech, gen['id'], new_body(str(uuid.uuid4())))
    hist_id = saved['serviceRecordId']
    before = next(h for h in saved['history'] if h['id'] == hist_id)
    edit = {'serviceDate': FROZEN_MAINE_DATE, 'nextServiceDate': '2027-07-01', 'notes': 'Edited offline',
            'expectedUpdatedAt': before['updatedAt'], 'clientId': str(uuid.uuid4())}
    first = tech.json('PUT', f'/api/service-records/{hist_id}', edit)
    again = tech.json('PUT', f'/api/service-records/{hist_id}', edit)  # the phone never saw the first answer
    assert first[0] == again[0] == 200 and again[1]['replayed'] is True
    # Without the key, the same stale expectedUpdatedAt is a real conflict.
    del edit['clientId']
    status, body = tech.json('PUT', f'/api/service-records/{hist_id}', edit)
    assert status == 409 and body['code'] == 'stale'
