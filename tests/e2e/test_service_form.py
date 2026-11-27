"""The field-proof service form in a real browser at 390px.

Drafts survive reloads and closed tabs, a dirty form can't be closed by accident, a save with no signal
is kept as "Not sent yet" and sent once when the connection is back, retries never duplicate, and the
server's 400/409 answers are never retried automatically."""
import json
import re
import time

import pytest
from playwright.sync_api import expect

from tests.helpers import answers

READINGS = {'voltage_l1': '120', 'voltage_l2': '121', 'frequency': '60', 'run_hours': '412.5', 'battery_voltage': '13.2'}
CHOICES = {'oil_changed': 'yes', 'oil_filter_changed': 'yes', 'air_filter_changed': 'no', 'spark_plugs_changed': 'no',
           'battery_terminals': 'pass', 'oil_leaks': 'no', 'coolant_leaks': 'na', 'transfer_switch': 'pass',
           'exercise_schedule': 'pass', 'enclosure_clear': 'pass', 'fault_codes': 'no'}


def visits(db):
    return db.execute("SELECT COUNT(*) FROM generator_history WHERE event='Serviced'").fetchone()[0]


def open_app(page, base):
    page.goto(base + '/')
    expect(page.locator('.nav-item', has_text='Service records')).to_be_visible()
    wait_js(page, 'state.currentUser && !state.loading')


def wait_js(page, expression, timeout=10):
    """Polls a JS expression until it's truthy (wait_for_function needs eval, which the page's CSP forbids)."""
    end = time.monotonic() + timeout
    while not page.evaluate(expression):
        assert time.monotonic() < end, f'timed out waiting for {expression}'
        page.wait_for_timeout(100)


def settle(page):
    page.wait_for_timeout(600)  # past the click guard after a modal or dialog opens


def choose(page, name):
    """Clicks a button in the in-app dialog, after the 500ms click guard that drops taps right after a
    dialog or modal opens (so a double-tap can't land on whatever appears under the finger)."""
    page.locator('#dialog-slot .modal').wait_for()
    page.wait_for_timeout(600)
    page.locator('#dialog-slot').get_by_role('button', name=name).click()


def reload(page):
    page.reload()
    wait_js(page, 'state.currentUser && !state.loading')


def open_form(page, gen_id):
    page.evaluate(f'openServiceForm({{presetGenId: {gen_id}}})')
    settle(page)


def fill_checklist(page, **override):
    for key, value in {**READINGS, **{k: v for k, v in override.items() if k in READINGS}}.items():
        page.fill(f'#ck-{key}-v', value)
    for key, value in {**CHOICES, **{k: v for k, v in override.items() if k in CHOICES}}.items():
        page.locator(f'label:has(#ck-{key}-{value})').click()


def save(page):
    page.locator('#sr-form button[type=submit]').click()


def toast(page, text):
    expect(page.locator('.toast')).to_contain_text(text)


@pytest.fixture
def gen(make_generator):
    return make_generator()


# --------------------------------------------------------------- drafts --

def test_a_new_record_draft_survives_a_reload_and_resumes(page, signed_in, base, gen, native_dialogs):
    open_app(page, base)
    open_form(page, gen['id'])
    page.fill('#sr-notes', 'Belt looks worn')
    page.fill('#ck-voltage_l1-v', '118.5')
    page.locator('label:has(#ck-oil_leaks-yes)').click()
    expect(page.locator('#sr-saved')).to_contain_text('Draft saved on this phone')
    reload(page)
    open_form(page, gen['id'])
    expect(page.locator('#dialog-slot')).to_contain_text('Resume draft?')
    choose(page, 'Resume draft')
    settle(page)
    expect(page.locator('#sr-notes')).to_have_value('Belt looks worn')
    expect(page.locator('#ck-voltage_l1-v')).to_have_value('118.5')
    expect(page.locator('#ck-oil_leaks-yes')).to_be_checked()
    expect(page.locator('#ck-oil_leaks-hint')).to_contain_text('Needs follow-up')
    assert native_dialogs == []


def test_an_edit_draft_survives_a_reload_and_resumes(page, signed_in, base, tech, gen, db):
    _, saved = tech.json('POST', f"/api/generators/{gen['id']}/service-records",
                         {'serviceDate': '2026-06-01', 'nextServiceDate': '2027-06-01', 'notes': 'Original'})
    hist_id = saved['history'][-1]['id']
    open_app(page, base)
    page.evaluate(f'openServiceEdit({hist_id})')
    settle(page)
    page.fill('#sr-notes', 'Original, plus a fix')
    expect(page.locator('#sr-saved')).to_contain_text('Draft saved')
    reload(page)
    page.evaluate(f'openServiceEdit({hist_id})')
    settle(page)
    choose(page, 'Resume draft')
    settle(page)
    expect(page.locator('#sr-notes')).to_have_value('Original, plus a fix')
    save(page)
    toast(page, 'Service record updated.')
    assert db.execute('SELECT notes FROM generator_history WHERE id=?', (hist_id,)).fetchone()[0] == 'Original, plus a fix'


def test_a_draft_survives_a_closed_tab(page, signed_in, base, gen):
    open_app(page, base)
    open_form(page, gen['id'])
    page.fill('#sr-notes', 'Typed before the phone died')
    page.close()  # no wait for the debounce: pagehide saves it
    again = signed_in.new_page()
    open_app(again, base)
    wait_js(again, 'state.currentUser && !state.loading')
    open_form(again, gen['id'])
    choose(again, 'Resume draft')
    settle(again)
    expect(again.locator('#sr-notes')).to_have_value('Typed before the phone died')


def test_a_dirty_form_ignores_the_backdrop_and_escape_and_cancel_asks_in_app(page, signed_in, base, gen, native_dialogs):
    open_app(page, base)
    open_form(page, gen['id'])
    page.fill('#sr-notes', 'Something')
    page.wait_for_timeout(600)  # past the click guard
    page.mouse.click(5, 5)  # wherever the backdrop shows (on a phone the sheet is full height)
    page.keyboard.press('Escape')
    expect(page.locator('#sr-form')).to_be_visible()
    page.get_by_role('button', name='Cancel').click()
    expect(page.locator('#dialog-slot')).to_contain_text('Discard changes?')
    choose(page, 'Keep editing')
    settle(page)
    expect(page.locator('#sr-notes')).to_have_value('Something')
    page.get_by_role('button', name='Cancel').click()
    choose(page, 'Discard')
    expect(page.locator('#sr-form')).to_have_count(0)
    open_form(page, gen['id'])
    expect(page.locator('#dialog-slot')).not_to_contain_text('Resume')  # the draft is gone
    assert native_dialogs == []


def test_a_clean_form_closes_normally(page, signed_in, base, gen):
    open_app(page, base)
    open_form(page, gen['id'])
    page.keyboard.press('Escape')
    expect(page.locator('#sr-form')).to_have_count(0)


def test_the_form_fits_a_phone_with_big_targets(page, signed_in, base, gen):
    open_app(page, base)
    open_form(page, gen['id'])
    assert page.evaluate('document.documentElement.scrollWidth') <= 390
    box = page.locator('label:has(#ck-oil_leaks-yes) span').bounding_box()
    assert box['height'] >= 48
    assert page.get_attribute('#ck-voltage_l1-v', 'inputmode') == 'decimal'


def test_save_points_to_unanswered_items(page, signed_in, base, gen, db):
    open_app(page, base)
    open_form(page, gen['id'])
    page.fill('#ck-voltage_l1-v', '120')
    save(page)
    expect(page.locator('#sr-error')).to_contain_text('15 checklist items still need an answer')
    expect(page.locator('#ck-voltage_l2')).to_have_class(re.compile('missing'))
    assert visits(db) == 0


# ------------------------------------------------------------- offline --

def test_offline_save_is_kept_and_sent_once_when_back_online(page, signed_in, base, gen, db):
    open_app(page, base)
    open_form(page, gen['id'])
    fill_checklist(page, battery_voltage='11.8')
    signed_in.set_offline(True)
    save(page)
    toast(page, 'Saved on this phone, not sent yet')
    expect(page.locator('#outbox-slot')).to_contain_text('1 service record not sent yet')
    assert visits(db) == 0
    signed_in.set_offline(False)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    expect(page.locator('#outbox-slot')).not_to_contain_text('not sent yet', timeout=10000)
    assert visits(db) == 1
    assert db.execute("SELECT needs_follow_up FROM generator_history WHERE event='Serviced'").fetchone()[0] == 1


def test_a_lost_answer_is_retried_without_a_duplicate(page, signed_in, base, gen, db):
    calls = []

    def first_answer_lost(route):
        calls.append(route.request.post_data)
        if len(calls) == 1:
            route.fetch()   # the server saves it...
            route.abort()   # ...but the phone never hears back
        else:
            route.continue_()
    page.route('**/service-records', first_answer_lost)
    open_app(page, base)
    open_form(page, gen['id'])
    fill_checklist(page)
    save(page)
    toast(page, 'not sent yet')
    assert visits(db) == 1
    settle(page)
    page.get_by_role('button', name='Send now').first.click()
    expect(page.locator('#outbox-slot')).not_to_contain_text('not sent yet')
    assert len(calls) == 2 and json.loads(calls[0])['clientId'] == json.loads(calls[1])['clientId']
    assert visits(db) == 1


def test_a_refused_record_is_not_retried(page, signed_in, base, gen, db):
    calls = []

    def refuse(route):
        calls.append(1)
        route.fulfill(status=400, content_type='application/json', body=json.dumps({'error': 'Service date is wrong.'}))
    open_app(page, base)
    open_form(page, gen['id'])
    fill_checklist(page)
    signed_in.set_offline(True)
    save(page)
    toast(page, 'not sent yet')
    page.route('**/service-records', refuse)
    signed_in.set_offline(False)
    page.evaluate('flushOutbox(true)')
    expect(page.locator('#outbox-slot')).to_contain_text('1 needs attention')
    page.evaluate('flushOutbox(true)')
    page.evaluate("window.dispatchEvent(new Event('online'))")
    page.wait_for_timeout(500)
    assert len(calls) == 1 and visits(db) == 0
    page.evaluate("goTo('service-records')")
    expect(page.locator('.phone-row')).to_contain_text('Needs fixing')
    expect(page.locator('.phone-row')).to_contain_text('Service date is wrong.')


def test_a_conflicting_edit_keeps_both_versions_and_is_not_retried(page, signed_in, base, tech, admin, gen, db):
    _, saved = tech.json('POST', f"/api/generators/{gen['id']}/service-records",
                         {'serviceDate': '2026-06-01', 'nextServiceDate': '2027-06-01', 'notes': 'Original'})
    hist_id = saved['history'][-1]['id']
    calls = []
    page.on('response', lambda r: calls.append(r) if r.request.method == 'PUT' else None)  # answered ones only
    open_app(page, base)
    page.evaluate(f'openServiceEdit({hist_id})')
    settle(page)
    page.fill('#sr-notes', 'Mine, from the field')
    signed_in.set_offline(True)
    save(page)
    toast(page, 'not sent yet')
    current = next(h for h in saved['history'] if h['id'] == hist_id)
    status, _ = admin.json('PUT', f'/api/service-records/{hist_id}', {
        'serviceDate': '2026-06-01', 'nextServiceDate': '2027-06-01', 'notes': 'The office changed it',
        'expectedUpdatedAt': current['updatedAt']})
    assert status == 200
    signed_in.set_offline(False)
    page.evaluate('flushOutbox(true)')
    expect(page.locator('#outbox-slot')).to_contain_text('1 needs attention')
    page.evaluate('flushOutbox(true)')
    assert len(calls) == 1
    page.evaluate("goTo('service-records')")
    settle(page)  # the form closed moments ago: past the click guard
    page.get_by_role('button', name='Compare').click()
    settle(page)
    table = page.locator('.compare-table')
    expect(table).to_contain_text('Mine, from the field')
    expect(table).to_contain_text('The office changed it')
    assert db.execute('SELECT notes FROM generator_history WHERE id=?', (hist_id,)).fetchone()[0] == 'The office changed it'
    page.get_by_role('button', name='Save mine over it').click()
    toast(page, 'Your version was saved.')
    assert db.execute('SELECT notes FROM generator_history WHERE id=?', (hist_id,)).fetchone()[0] == 'Mine, from the field'


# ------------------------------------------------------- offline shell --

def load_with_worker(page, base):
    """Loads the app, then reloads once the service worker is in control (so it has kept the data)."""
    open_app(page, base)
    page.evaluate('navigator.serviceWorker.ready')
    reload(page)
    wait_js(page, 'navigator.serviceWorker.controller !== null')
    wait_js(page, "caches.open('ww-data').then(c => c.keys()).then(k => k.length >= 4)")


def test_the_app_opens_offline_from_its_last_load_and_queues_a_record(page, switchable, gen, db):
    load_with_worker(page, switchable.base)
    switchable.stop()  # no signal
    reload(page)
    expect(page.locator('#outbox-slot')).to_contain_text('Offline: showing data from')
    expect(page.locator('#connection-status')).to_contain_text('Can’t reach the server')
    open_form(page, gen['id'])
    fill_checklist(page)
    save(page)
    toast(page, 'not sent yet')
    assert visits(db) == 0
    switchable.start()  # signal again
    page.evaluate("window.dispatchEvent(new Event('online'))")
    expect(page.locator('#outbox-slot')).not_to_contain_text('not sent yet', timeout=10000)
    assert visits(db) == 1


def test_signing_out_deletes_the_offline_data(page, switchable):
    load_with_worker(page, switchable.base)
    page.evaluate('logout()')
    expect(page.get_by_role('button', name='Sign in')).to_be_visible()
    wait_js(page, "caches.has('ww-data').then(x => !x)")
    switchable.stop()
    page.reload()  # the page itself still opens from the cache, but there's no one's data in it
    expect(page.get_by_role('button', name='Sign in')).to_be_visible()
    assert page.evaluate('state.customers.length') == 0


def test_the_sign_out_warning_lists_unsent_records(page, signed_in, base, gen, native_dialogs):
    open_app(page, base)
    open_form(page, gen['id'])
    page.fill('#sr-notes', 'draft')
    page.get_by_role('button', name='Cancel').click()
    choose(page, 'Keep editing')
    page.evaluate('srPersist(); closeModal()')
    page.evaluate('logout()')
    expect(page.locator('#dialog-slot')).to_contain_text('1 service record is kept on this phone')
    assert native_dialogs == []
