"""The service checklist over HTTP: saving answers, flags, frozen history, editing, follow-ups, email."""
import json

import pytest

import server
from tests.conftest import FROZEN_MAINE_DATE
from tests.helpers import answers

TODAY = FROZEN_MAINE_DATE  # 2026-07-04


@pytest.fixture(autouse=True)
def _clock(frozen_clock):
    return frozen_clock


def log(c, gen_id, service_date=TODAY, **body):
    payload = {'serviceDate': service_date, 'nextServiceDate': '2027-07-01', 'checklistVersion': 1,
               'answers': answers()}
    payload.update(body)
    return c.json('POST', f'/api/generators/{gen_id}/service-records', payload)


def record(gen, hist_id):
    return next(h for h in gen['history'] if h['id'] == hist_id)


def logged(c, gen_id, **body):
    status, gen = log(c, gen_id, **body)
    assert status == 200, gen
    return gen, record(gen, gen['serviceRecordId'])


def edit(c, hist_id, **body):
    payload = {'serviceDate': TODAY, 'nextServiceDate': '2027-07-01'}
    payload.update(body)
    return c.json('PUT', f'/api/service-records/{hist_id}', payload)


def add_version(db, items):
    db.execute("INSERT INTO checklist_versions (id, items, created_at) VALUES (2, ?, 'now')", (json.dumps(items),))
    db.commit()


# ------------------------------------------------------------------- saving --

def test_answers_are_saved_and_returned(tech, make_generator):
    _, r = logged(tech, make_generator()['id'])
    assert r['checklistVersion'] == 1 and r['needsFollowUp'] is False and r['followUpReasons'] == []
    assert r['answers']['voltage_l1'] == {'value': 120.0, 'na': False, 'note': '', 'flagged': False}
    assert r['answers']['coolant_leaks']['na'] is True


def test_a_visit_today_needs_the_checklist(tech, make_generator):
    status, body = log(tech, make_generator()['id'], answers=None)
    assert status == 400 and 'Fill in the checklist' in body['error']


def test_an_older_visit_may_be_saved_from_paper_without_one(tech, make_generator):
    _, r = logged(tech, make_generator()['id'], service_date='2026-07-03', answers=None)
    assert r['checklistVersion'] is None and r['answers'] is None and r['needsFollowUp'] is False


def test_partial_checklists_are_refused(tech, make_generator):
    status, body = log(tech, make_generator()['id'], answers=answers(oil_leaks=None))
    assert status == 400 and 'Missing: Signs of oil leaks' in body['error']


@pytest.mark.parametrize('version, message', [(None, 'Checklist version is required'), (99, 'Unknown checklist version')])
def test_the_version_must_exist(tech, make_generator, version, message):
    status, body = log(tech, make_generator()['id'], checklistVersion=version)
    assert status == 400 and message in body['error']


def test_nothing_is_saved_when_the_checklist_is_refused(tech, make_generator, db):
    gen = make_generator()
    log(tech, gen['id'], answers=answers(voltage_l1={'value': 'high'}))
    assert db.execute("SELECT COUNT(*) FROM generator_history WHERE event='Serviced'").fetchone()[0] == 0


def test_checklist_versions_are_readable_by_anyone_signed_in(tech, client):
    status, versions = tech.json('GET', '/api/checklist-versions')
    assert status == 200 and [v['id'] for v in versions] == [1] and len(versions[0]['items']) == 16
    assert client().json('GET', '/api/checklist-versions')[0] == 401


# ----------------------------------------------------------------- flagging --

def test_flags_mark_the_record_needs_follow_up(tech, make_generator):
    _, r = logged(tech, make_generator()['id'], answers=answers(
        battery_voltage={'value': 11.9}, coolant_leaks={'value': 'yes'},
        fault_codes={'value': 'yes', 'note': 'E-1501 low oil'}))
    assert r['needsFollowUp'] is True
    assert r['followUpReasons'] == ['Battery voltage 11.9 V (normal 12.4–14.6 V)', 'Signs of coolant leaks: Yes',
                                    'Controller fault codes present: Yes (E-1501 low oil)']
    assert r['answers']['battery_voltage']['flagged'] and not r['answers']['voltage_l1']['flagged']


# ------------------------------------------------------------ frozen history --

def test_old_records_keep_their_version_when_the_list_changes(tech, make_generator, db):
    gen = make_generator()
    _, r = logged(tech, gen['id'], answers=answers(voltage_l1={'value': 130}))
    v2 = [dict(i) for i in server.checklist.VERSION_1]
    v2[0].update(label='L1 volts', normalMin=100, normalMax=140)
    add_version(db, v2)

    r = record(tech.json('GET', f"/api/generators/{gen['id']}")[1], r['id'])
    assert r['checklistVersion'] == 1 and r['needsFollowUp'] is True
    assert r['followUpReasons'] == ['Voltage out L1 130 V (normal 114–126 V)']
    # Editing it is judged by version 1 still, not by the new ranges.
    status, after = edit(tech, r['id'], answers=answers(voltage_l1={'value': 131}))
    r = record(after, r['id'])
    assert status == 200 and r['checklistVersion'] == 1 and r['followUpReasons'] == ['Voltage out L1 131 V (normal 114–126 V)']
    # New records use what the page sent: here the new version, where 130 is normal.
    _, new = logged(tech, gen['id'], checklistVersion=2, answers=answers(voltage_l1={'value': 130}))
    assert new['checklistVersion'] == 2 and new['needsFollowUp'] is False


def test_a_draft_started_on_the_old_version_still_saves_against_it(tech, make_generator, db):
    add_version(db, server.checklist.VERSION_1[:3])
    _, r = logged(tech, make_generator()['id'], checklistVersion=1)
    assert r['checklistVersion'] == 1 and len(r['answers']) == 16


def test_versions_are_immutable(db):
    for sql in ("UPDATE checklist_versions SET items='[]'", 'DELETE FROM checklist_versions'):
        with pytest.raises(server.sqlite3.DatabaseError, match='immutable'):
            db.execute(sql)


# ------------------------------------------------------------------ editing --

def test_editing_answers_re_evaluates_the_flags(tech, make_generator):
    _, r = logged(tech, make_generator()['id'])
    status, gen = edit(tech, r['id'], answers=answers(oil_leaks={'value': 'yes'}))
    assert status == 200 and record(gen, r['id'])['needsFollowUp'] is True
    status, gen = edit(tech, r['id'], answers=answers())
    assert status == 200 and record(gen, r['id'])['needsFollowUp'] is False and record(gen, r['id'])['followUpReasons'] == []


def test_leaving_answers_out_keeps_them(tech, make_generator):
    _, r = logged(tech, make_generator()['id'], answers=answers(oil_leaks={'value': 'yes'}))
    status, gen = edit(tech, r['id'], notes='Dates only')
    assert status == 200 and record(gen, r['id'])['answers'] == r['answers'] and record(gen, r['id'])['needsFollowUp']


def test_edits_are_checked_like_new_records(tech, make_generator):
    _, r = logged(tech, make_generator()['id'])
    status, body = edit(tech, r['id'], answers=answers(frequency=None))
    assert status == 400 and 'Missing: Frequency' in body['error']


def test_a_record_saved_without_a_checklist_has_none_to_edit(tech, make_generator):
    _, r = logged(tech, make_generator()['id'], service_date='2026-07-01', answers=None)
    assert edit(tech, r['id'], serviceDate='2026-07-01', notes='ok')[0] == 200
    status, body = edit(tech, r['id'], serviceDate='2026-07-01', answers=answers())
    assert status == 400 and 'without a checklist' in body['error']


def test_records_from_before_checklists_still_display_and_edit(tech, serviced_generator):
    gen, hist_id = serviced_generator
    r = record(gen, hist_id)
    assert r['checklistVersion'] is None and r['answers'] is None and r['needsFollowUp'] is False
    assert edit(tech, hist_id, serviceDate='2026-06-01', notes='Fixed a typo')[0] == 200


def test_invoiced_records_cant_change_their_answers(tech, admin, make_generator):
    _, r = logged(tech, make_generator()['id'])
    admin.json('POST', f"/api/service-records/{r['id']}/invoice", {'invoiced': True})
    assert edit(tech, r['id'], answers=answers(oil_leaks={'value': 'yes'}))[0] == 409


def test_edits_audit_the_answers(tech, make_generator, db):
    _, r = logged(tech, make_generator()['id'])
    edit(tech, r['id'], answers=answers(oil_leaks={'value': 'yes'}))
    detail = json.loads(db.execute("SELECT detail FROM audit_log WHERE action='edit'").fetchone()[0])
    assert detail['before']['answers']['oil_leaks']['value'] == 'no' and detail['before']['needsFollowUp'] is False
    assert detail['after']['answers']['oil_leaks']['value'] == 'yes' and detail['after']['needsFollowUp'] is True


# ---------------------------------------------------------------- follow-up --

def follow_up(c, hist_id, **body):
    return c.json('POST', f'/api/service-records/{hist_id}/follow-up', body)


def test_admins_mark_a_follow_up_handled_and_it_is_audited(tech, admin, make_generator, db):
    _, r = logged(tech, make_generator()['id'], answers=answers(oil_leaks={'value': 'yes'}))
    admin.json('POST', f"/api/service-records/{r['id']}/invoice", {'invoiced': True})  # works after invoicing too
    status, gen = follow_up(admin, r['id'], handled=True, note='Booked a repair')
    h = record(gen, r['id'])
    assert status == 200 and (h['followUpHandledBy'], h['followUpHandledAt'], h['followUpNote']) == (
        'boss', TODAY, 'Booked a repair')
    assert h['needsFollowUp'] is True and h['updatedAt'] == r['updatedAt']  # the tech's record is untouched
    status, gen = follow_up(admin, r['id'], handled=False)
    assert status == 200 and record(gen, r['id'])['followUpHandledBy'] == ''
    assert [a['action'] for a in db.execute("SELECT action FROM audit_log WHERE action LIKE 'follow_up%' ORDER BY id")] == [
        'follow_up_handled', 'follow_up_reopened']


def test_technicians_cant_mark_follow_ups(tech, make_generator):
    _, r = logged(tech, make_generator()['id'], answers=answers(oil_leaks={'value': 'yes'}))
    assert follow_up(tech, r['id'], handled=True)[0] == 403


def test_only_flagged_records_have_a_follow_up(admin, make_generator):
    _, r = logged(admin, make_generator(admin)['id'])
    status, body = follow_up(admin, r['id'], handled=True)
    assert status == 400 and 'nothing to follow up' in body['error']
    assert follow_up(admin, 999, handled=True)[0] == 404
    assert follow_up(admin, r['id'], handled='yes')[0] == 400


def test_new_flags_from_an_edit_reopen_a_handled_follow_up(tech, admin, make_generator):
    _, r = logged(tech, make_generator()['id'], answers=answers(oil_leaks={'value': 'yes'}))
    follow_up(admin, r['id'], handled=True)
    _, gen = edit(tech, r['id'], notes='same answers')
    assert record(gen, r['id'])['followUpHandledBy'] == 'boss'  # nothing changed in the flags
    _, gen = edit(tech, r['id'], answers=answers(oil_leaks={'value': 'yes'}, transfer_switch={'value': 'fail'}))
    assert record(gen, r['id'])['followUpHandledBy'] == ''


# -------------------------------------------------------------------- email --

@pytest.fixture
def subscribed_admin(make_user, client):
    make_user('betty', 'admin', email='betty@example.com', notify=['service_record'])
    return client('betty')


def test_the_email_shows_the_checklist_and_follow_up_escaped(tech, subscribed_admin, make_generator, smtp2go):
    logged(tech, make_generator()['id'], answers=answers(
        oil_leaks={'value': 'yes', 'note': '<script>alert(1)</script>'}))
    server.run_email_jobs()
    body = smtp2go.requests[0]['body']
    assert body['subject'].startswith('Needs follow-up: Test Customer')
    assert 'NEEDS FOLLOW-UP' in body['text_body'] and '[!] Signs of oil leaks: Yes' in body['text_body']
    assert 'Voltage out L1: 120 V' in body['text_body']
    assert '<script>' not in body['html_body'] and '&lt;script&gt;alert(1)&lt;/script&gt;' in body['html_body']
    assert 'Checklist' in body['html_body']


def test_an_unflagged_visit_has_the_usual_subject(tech, subscribed_admin, make_generator, smtp2go):
    logged(tech, make_generator()['id'])
    server.run_email_jobs()
    assert smtp2go.requests[0]['body']['subject'].startswith('Service logged:')


def test_a_paper_entry_says_it_has_no_checklist(tech, subscribed_admin, make_generator, smtp2go):
    logged(tech, make_generator()['id'], service_date='2026-07-01', answers=None)
    server.run_email_jobs()
    assert 'Checklist: none (entered from paper).' in smtp2go.requests[0]['body']['text_body']
