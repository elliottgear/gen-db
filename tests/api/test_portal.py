"""The public customer request page (/customer) and its one endpoint: create a request, reveal nothing."""
import json
import re

import pytest

import appointment_requests as requests_mod
import server
from tests.conftest import Siteverify

SUBMIT = '/api/public/appointment-requests'
CF_EDGE = '172.70.1.2'


def form(**overrides):
    body = {'name': 'Pat Doe', 'phone': '(207) 555-0142', 'email': 'pat@example.com',
            'serviceStreet': '12 Main St', 'serviceTown': 'Saco', 'serviceZip': '04072', 'brand': 'Generac', 'need': 'maintenance',
            'model': '', 'notes': '', 'turnstileToken': 'XXXX.DUMMY.TOKEN.XXXX', requests_mod.HONEYPOT_FIELD: ''}
    body.update(overrides)
    return body


def submit(anon, body=None, ip='198.51.100.7', **headers):
    return anon.json('POST', SUBMIT, form() if body is None else body, headers={'X-Real-IP': ip, **headers})


def stored(db):
    return db.execute('SELECT * FROM appointment_requests ORDER BY id').fetchall()


# ------------------------------------------------------------------ submit --

def test_a_valid_request_is_stored_as_new(client, db, turnstile_stub):
    assert submit(client()) == (200, {'ok': True})
    [row] = stored(db)
    assert row['status'] == 'new' and row['name'] == 'Pat Doe' and row['phone_digits'] == '2075550142'
    assert (row['service_street'], row['service_town']) == ('12 Main St', 'Saco')
    assert row['brand'] == 'Generac' and row['need'] == 'maintenance' and row['dismiss_reason'] == ''


def test_the_answer_is_identical_whether_or_not_it_matches_a_customer(client, turnstile_stub, make_customer):
    make_customer(name='Pat Doe', phone='207-555-0142', email='pat@example.com', physicalAddress='12 Main St')
    anon = client()
    matching_res, matching_body = anon.request('POST', SUBMIT, form(), headers={'X-Real-IP': '198.51.100.1'})
    other_res, other_body = anon.request('POST', SUBMIT, form(name='Nobody', phone='2075559999', email='n@x.com',
                                                               serviceStreet='1 Nowhere Ln'),
                                         headers={'X-Real-IP': '198.51.100.2'})
    assert matching_body == other_body == b'{"ok": true}'
    assert matching_res.status == other_res.status == 200


def test_signed_in_users_get_the_same_bare_answer(client, admin, turnstile_stub):
    status, body = admin.json('POST', SUBMIT, form())
    assert (status, body) == (200, {'ok': True})


@pytest.mark.parametrize('field, value, message', [
    ('name', '', 'your name'),
    ('phone', '555-0142', '10-digit phone'),
    ('email', 'not-an-email', 'valid email'),
    ('serviceStreet', '', 'street address'),
    ('serviceTown', '', 'town'),
    ('serviceTown', 'x' * 101, 'too long'),
    ('serviceZip', '', '5-digit zip'),
    ('serviceZip', '4072', '5-digit zip'),
    ('serviceZip', 'abcde', '5-digit zip'),
    ('serviceZip', '040721', '5-digit zip'),
    ('serviceZip', '04072-12', '5-digit zip'),
    ('brand', 'Deere', 'generator brand'),
    ('brand', '', 'generator brand'),
    ('need', 'oil change', 'service needed'),
    ('name', 'x' * 201, 'too long'),
    ('notes', 'x' * 2001, 'too long'),
    ('name', 42, 'must be text'),
])
def test_validation(client, db, turnstile_stub, field, value, message):
    status, body = submit(client(), form(**{field: value}))
    assert status == 400 and message in body['error'].lower()
    assert stored(db) == [] and turnstile_stub.requests == []  # rejected before asking Cloudflare


def test_unknown_fields_are_ignored(client, db, turnstile_stub):
    assert submit(client(), form(status='scheduled', customer_id=1, id=99, serviceAddress='ignored'))[0] == 200
    [row] = stored(db)
    assert row['status'] == 'new' and row['customer_id'] is None and row['id'] == 1


def test_every_brand_and_need_on_the_form_is_accepted(client, db, turnstile_stub):
    anon = client()
    for i, brand in enumerate(requests_mod.BRANDS):
        need = list(requests_mod.NEEDS)[i % len(requests_mod.NEEDS)]
        assert submit(anon, form(brand=brand, need=need), ip=f'198.51.100.{i}')[0] == 200
    assert [r['brand'] for r in stored(db)] == list(requests_mod.BRANDS)


def test_large_bodies_are_refused(client, db, turnstile_stub):
    body = form(notes='x' * (requests_mod.MAX_BODY_BYTES + 1))
    assert submit(client(), body)[0] == 413
    assert stored(db) == []


def test_only_post_creates_and_nothing_can_be_read_without_signing_in(client, turnstile_stub):
    anon = client()
    assert anon.json('GET', SUBMIT)[0] in (401, 404)
    assert anon.json('GET', '/api/appointment-requests')[0] == 401
    assert anon.json('GET', '/api/appointment-requests/1')[0] == 401


# --------------------------------------------------------------- turnstile --

def test_the_token_and_visitor_ip_are_sent_to_cloudflare(client, turnstile_stub):
    submit(client(), ip=CF_EDGE, **{'CF-Connecting-IP': '203.0.113.5'})
    [sent] = turnstile_stub.requests
    assert sent['secret'] == 'secret-key-test' and sent['response'] == 'XXXX.DUMMY.TOKEN.XXXX'
    assert sent['remoteip'] == '203.0.113.5' and sent['idempotency_key']


def test_an_unverified_address_is_not_sent_as_remoteip(client, turnstile_stub):
    submit(client(), ip='198.51.100.7')
    assert 'remoteip' not in turnstile_stub.requests[0]


@pytest.mark.parametrize('reply', [
    Siteverify.REJECTED,
    (200, {'success': False, 'error-codes': ['timeout-or-duplicate']}),
    (200, {'success': True, 'hostname': 'evil.example.com'}),  # token minted on another site
    (200, {'success': 'true', 'hostname': Siteverify.HOSTNAME}),  # only a real JSON true counts
])
def test_rejected_tokens_store_nothing(client, db, turnstile_stub, reply):
    turnstile_stub.reply = reply
    status, body = submit(client())
    assert status == 400 and 'robot' in body['error']
    assert stored(db) == []


def test_missing_token_is_rejected_without_calling_cloudflare(client, db, turnstile_stub):
    status, _ = submit(client(), form(turnstileToken=''))
    assert status == 400 and turnstile_stub.requests == [] and stored(db) == []


@pytest.mark.parametrize('reply', [
    (500, {}),
    (200, {'success': False, 'error-codes': ['invalid-input-secret']}),
    (200, {'success': False, 'error-codes': ['internal-error']}),
])
def test_cloudflare_errors_fail_closed(client, db, turnstile_stub, reply):
    turnstile_stub.reply = reply
    status, body = submit(client())
    assert status == 503 and 'call the office' in body['error']
    assert stored(db) == []


def test_unreachable_cloudflare_fails_closed(client, db, turnstile_stub, monkeypatch):
    monkeypatch.setenv('TURNSTILE_VERIFY_URL', 'http://127.0.0.1:9/siteverify')  # nothing listens on port 9
    assert submit(client())[0] == 503
    assert stored(db) == []


def test_slow_cloudflare_fails_closed(client, db, turnstile_stub, monkeypatch):
    import turnstile
    monkeypatch.setattr(turnstile, 'VERIFY_TIMEOUT_SECONDS', 0.2)
    turnstile_stub.delay = 1
    assert submit(client())[0] == 503
    assert stored(db) == []


def test_without_a_secret_key_nothing_is_accepted(client, db):
    status, body = submit(client())
    assert status == 503 and stored(db) == []


def test_hostname_check_defaults_to_app_url(monkeypatch):
    import turnstile
    monkeypatch.setenv('APP_URL', 'https://gen.bridgeselec.com/')
    assert turnstile.settings()['hostname'] == 'gen.bridgeselec.com'
    monkeypatch.setenv('TURNSTILE_HOSTNAME', '')
    assert turnstile.settings()['hostname'] == ''


# ---------------------------------------------------------------- honeypot --

def test_honeypot_submissions_are_kept_as_dismissed_and_get_the_same_answer(client, db, turnstile_stub):
    res, body = client().request('POST', SUBMIT, form(**{requests_mod.HONEYPOT_FIELD: 'http://spam.example'}),
                                 headers={'X-Real-IP': '198.51.100.7'})
    assert res.status == 200 and body == b'{"ok": true}'
    [row] = stored(db)
    assert row['status'] == 'dismissed' and row['dismiss_reason'] == 'spam filter (honeypot)'


def test_an_admin_can_restore_a_honeypot_false_positive(client, db, admin, turnstile_stub):
    submit(client(), form(**{requests_mod.HONEYPOT_FIELD: 'oops'}))
    req = admin.json('GET', '/api/appointment-requests')[1]['requests'][0]
    status, restored = admin.json('POST', f"/api/appointment-requests/{req['id']}/status",
                                  {'status': 'new', 'expectedUpdatedAt': req['updatedAt']})
    assert status == 200 and restored['status'] == 'new' and restored['dismissReason'] == ''


# -------------------------------------------------------------- rate limits --

def test_five_stored_requests_per_hour_per_visitor(client, db, turnstile_stub):
    anon = client()
    for _ in range(requests_mod.MAX_PER_VISITOR):
        assert submit(anon)[0] == 200
    status, body = submit(anon)
    assert status == 429 and 'call the office' in body['error']
    assert len(stored(db)) == requests_mod.MAX_PER_VISITOR
    assert len(turnstile_stub.requests) == requests_mod.MAX_PER_VISITOR  # the blocked one never reached Cloudflare
    assert submit(anon, ip='198.51.100.8')[0] == 200  # someone else is fine


def test_failed_attempts_dont_use_up_the_allowance(client, db, turnstile_stub):
    """A shared connection where people mistype or fail the robot check can still send requests."""
    anon = client()
    turnstile_stub.reply = Siteverify.REJECTED
    for _ in range(requests_mod.MAX_PER_VISITOR):
        assert submit(anon)[0] == 400
    for _ in range(3):
        assert submit(anon, form(email='nope'))[0] == 400
    turnstile_stub.reply = Siteverify.OK
    for _ in range(requests_mod.MAX_PER_VISITOR):
        assert submit(anon)[0] == 200
    assert len(stored(db)) == requests_mod.MAX_PER_VISITOR


def test_honeypot_hits_count_as_stored(client, turnstile_stub):
    anon = client()
    for _ in range(requests_mod.MAX_PER_VISITOR):
        assert submit(anon, form(**{requests_mod.HONEYPOT_FIELD: 'x'}))[0] == 200
    assert submit(anon)[0] == 429


def test_attempts_are_capped_too(client, db, turnstile_stub):
    turnstile_stub.reply = Siteverify.REJECTED
    anon = client()
    for _ in range(requests_mod.MAX_ATTEMPTS_PER_VISITOR):
        assert submit(anon)[0] == 400
    turnstile_stub.reply = Siteverify.OK
    status, body = submit(anon)
    assert status == 429 and 'call the office' in body['error']
    assert len(turnstile_stub.requests) == requests_mod.MAX_ATTEMPTS_PER_VISITOR
    assert stored(db) == []


def test_the_attempt_cap_is_looser_than_the_stored_cap():
    assert requests_mod.MAX_ATTEMPTS_PER_VISITOR > requests_mod.MAX_PER_VISITOR


@pytest.mark.parametrize('spoof', [
    lambda i: {'X-Forwarded-For': f'10.0.0.{i}'},
    lambda i: {'CF-Connecting-IP': f'203.0.113.{i}'},               # not from a Cloudflare address: ignored
    lambda i: {'X-Forwarded-For': f'10.0.0.{i}', 'CF-Connecting-IP': f'203.0.113.{i}', 'True-Client-IP': f'10.1.1.{i}'},
])
def test_spoofed_headers_cannot_dodge_the_per_visitor_limit(client, turnstile_stub, spoof):
    anon = client()
    for i in range(requests_mod.MAX_PER_VISITOR):
        assert submit(anon, **spoof(i))[0] == 200
    assert submit(anon, **spoof(99))[0] == 429


def test_through_cloudflare_visitors_are_counted_separately(client, turnstile_stub):
    anon = client()
    for _ in range(requests_mod.MAX_PER_VISITOR):
        submit(anon, ip=CF_EDGE, **{'CF-Connecting-IP': '203.0.113.5'})
    assert submit(anon, ip=CF_EDGE, **{'CF-Connecting-IP': '203.0.113.5'})[0] == 429
    assert submit(anon, ip=CF_EDGE, **{'CF-Connecting-IP': '203.0.113.6'})[0] == 200


def test_overall_daily_cap(client, db, turnstile_stub, monkeypatch):
    monkeypatch.setattr(requests_mod, 'MAX_PER_DAY', 3)
    anon = client()
    for i in range(3):
        assert submit(anon, ip=f'198.51.100.{i}')[0] == 200
    status, body = submit(anon, ip='198.51.100.50')
    assert status == 429 and 'call the office' in body['error']
    assert len(stored(db)) == 3 and len(turnstile_stub.requests) == 3


# -------------------------------------------------------------------- page --

def page(anon):
    res, body = anon.request('GET', '/customer')
    return res, body.decode()


def test_page_is_public_and_filled_in(client, turnstile_stub, monkeypatch):
    monkeypatch.setenv('OFFICE_PHONE', '207-363-7177')
    res, html = page(client())
    assert res.status == 200 and res.getheader('Content-Type') == 'text/html; charset=utf-8'
    assert ("<strong>Power out and your generator isn't running?</strong> Call us now at "
            '<a href="tel:+12073637177">207-363-7177</a>. This form is for scheduling routine visits.') in html
    assert 'data-sitekey="site-key-test"' in html
    assert f'name="{requests_mod.HONEYPOT_FIELD}"' in html
    assert f'v{server.APP_VERSION}' in html
    assert not re.search(r'\{\{[A-Z_]+\}\}', html), 'unfilled placeholder'
    for brand in requests_mod.BRANDS:
        assert f'>{brand.replace("&", "&amp;")}</option>' in html
    for value, label in requests_mod.NEEDS.items():
        assert f'<option value="{value}">{label}</option>' in html


def test_page_shows_no_existing_data(client, turnstile_stub, make_generator):
    make_generator(make='Deere', model='SecretModel', serial='SN-SECRET')
    _, html = page(client())
    assert 'Deere' not in html and 'SecretModel' not in html and 'Test Customer' not in html


def test_page_escapes_configured_values(client, monkeypatch):
    monkeypatch.setenv('OFFICE_PHONE', '<b>207</b>')
    monkeypatch.setenv('TURNSTILE_SITE_KEY', '"><script>x</script>')
    _, html = page(client())
    assert '<b>207</b>' not in html and '<script>x</script>' not in html


def test_honeypot_field_is_hidden_from_people_and_browsers():
    html = open(server.PORTAL_FILE).read()
    field = re.search(r'<input[^>]*name="\{\{HONEYPOT_FIELD\}\}"[^>]*>', html).group(0)
    assert 'tabindex="-1"' in field and 'autocomplete="off"' in field
    wrapper = html[:html.index(field)].rsplit('<div', 1)[1]
    assert 'aria-hidden="true"' in wrapper and 'class="hp"' in wrapper
    assert requests_mod.HONEYPOT_FIELD not in ('website', 'company', 'url', 'fax', 'address2')
    assert not re.search(r'name|mail|phone|tel|addr|url|site|company|org|zip|city', requests_mod.HONEYPOT_FIELD)


@pytest.mark.parametrize('path', [server.PORTAL_FILE, server.PORTAL_THANKS_FILE])
def test_page_has_no_inline_script(path):
    """The portal CSP has no 'unsafe-inline' for scripts, so its pages must not need it."""
    html = open(path).read()
    for tag in re.findall(r'<script\b[^>]*>(.*?)</script>', html, re.S | re.I):
        assert tag.strip() == '', 'inline <script> content'
    assert all(' src=' in t for t in re.findall(r'<script\b[^>]*>', html, re.I))
    assert not re.search(r'\son[a-z]+\s*=', html, re.I), 'inline event handler attribute'
    assert 'javascript:' not in html.lower()


def test_portal_page_script_has_no_inline_handlers():
    script = open(server.PORTAL_SCRIPT_FILE).read()
    assert not re.search(r'\son[a-z]+\s*=\s*["\']', script), 'script builds inline handlers'
    assert 'innerHTML' not in script and 'eval(' not in script


def test_portal_csp_allows_no_inline_script_and_only_turnstile_from_cloudflare(client):
    res, _ = page(client())
    csp = dict(part.strip().split(' ', 1) for part in res.getheader('Content-Security-Policy').split(';'))
    assert csp['script-src'] == "'self' https://challenges.cloudflare.com"
    assert 'unsafe-inline' not in csp['script-src'] and 'unsafe-eval' not in csp['script-src']
    assert csp['frame-src'] == 'https://challenges.cloudflare.com'
    assert csp['connect-src'] == "'self'" and csp['frame-ancestors'] == "'none'"
    assert res.getheader('X-Frame-Options') == 'DENY'


def test_main_app_csp_is_unchanged_and_never_mentions_cloudflare(client):
    res, _ = client().request('GET', '/')
    assert res.getheader('Content-Security-Policy') == server.HTML_CSP
    assert 'cloudflare' not in server.HTML_CSP


def test_page_script_is_served(client):
    res, body = client().request('GET', '/customer-request.js')
    assert res.status == 200 and res.getheader('Content-Type') == 'text/javascript; charset=utf-8'
    assert b'/api/public/appointment-requests' in body


def test_without_a_site_key_the_page_says_to_call(client, monkeypatch):
    monkeypatch.setenv('OFFICE_PHONE', '207-363-7177')
    _, html = page(client())
    assert 'data-sitekey=""' in html  # customer-request.js hides the form and shows the call-the-office notice
    assert "Online requests aren't available right now" in html


def test_response_never_echoes_submitted_data(client, turnstile_stub):
    res, body = client().request('POST', SUBMIT, form(name='<script>alert(1)</script>'),
                                 headers={'X-Real-IP': '198.51.100.7'})
    assert json.loads(body) == {'ok': True}


# ------------------------------------------------------------ thank-you page --

def test_after_a_successful_submit_the_form_goes_to_the_thank_you_page():
    script = open(server.PORTAL_SCRIPT_FILE).read()
    assert "THANKS_URL = '/customer/thanks'" in script
    assert 'window.location.replace(THANKS_URL)' in script
    assert 'dialog' not in open(server.PORTAL_FILE).read()  # no more in-page confirmation


def test_thank_you_page_is_public_and_filled_in(client, monkeypatch):
    monkeypatch.setenv('OFFICE_PHONE', '207-363-7177')
    res, body = client().request('GET', '/customer/thanks')
    html = body.decode()
    assert res.status == 200 and res.getheader('Content-Type') == 'text/html; charset=utf-8'
    assert 'Request received' in html and 'someone from the office will reach out' in html
    assert ("<strong>Power out and your generator isn't running?</strong> Call us now at "
            '<a href="tel:+12073637177">207-363-7177</a>.') in html
    assert 'This form is for scheduling' not in html
    assert '<a class="btn btn-secondary" href="/customer">Send another request</a>' in html
    assert f'v{server.APP_VERSION}' in html
    assert not re.search(r'\{\{[A-Z_]+\}\}', html), 'unfilled placeholder'
    assert res.getheader('Content-Security-Policy') == server.PORTAL_CSP
    assert '<script' not in html  # nothing to run at all


def test_thank_you_page_shows_no_request_data(client, turnstile_stub):
    anon = client()
    submit(anon, form(name='Secret Name', email='secret@example.com'))
    _, body = anon.request('GET', '/customer/thanks')
    assert b'Secret Name' not in body and b'secret@example.com' not in body


# -------------------------------------------------------------- shared styles --

def test_portal_pages_share_one_stylesheet(client):
    res, body = client().request('GET', '/portal.css')
    assert res.status == 200 and res.getheader('Content-Type') == 'text/css; charset=utf-8'
    assert b'.dog-mark{' in body and b'.emergency{' in body
    for path in (server.PORTAL_FILE, server.PORTAL_THANKS_FILE):
        html = open(path).read()
        assert '<link rel="stylesheet" href="/portal.css?v={{APP_VERSION}}">' in html
        assert '<style' not in html and 'data:image' not in html  # nothing copied inline


def test_need_labels_change_but_stored_values_dont(client, db, admin, turnstile_stub):
    assert list(requests_mod.NEEDS) == ['maintenance', 'repair', 'not_running']
    html = page(client())[1]
    assert 'Service needed' in html and 'What do you need' not in html
    assert '<option value="maintenance">Routine maintenance</option>' in html
    assert submit(client())[0] == 200
    assert stored(db)[0]['need'] == 'maintenance'
    [item] = admin.json('GET', '/api/appointment-requests')[1]['requests']
    assert item['need'] == 'maintenance' and item['needLabel'] == 'Routine maintenance'


# ------------------------------------------------- office number in messages --

def test_rate_limit_message_gives_the_office_number(client, turnstile_stub, monkeypatch):
    monkeypatch.setenv('OFFICE_PHONE', '207-363-7177')
    anon = client()
    for _ in range(requests_mod.MAX_PER_VISITOR):
        submit(anon)
    status, body = submit(anon)
    assert status == 429
    assert body['error'] == ('Too many requests from your connection. Please try again later, '
                             'or call the office at 207-363-7177.')


def test_messages_without_an_office_number_still_say_call_the_office(client, db, monkeypatch):
    status, body = submit(client())  # no Turnstile secret: 503
    assert status == 503 and body['error'].endswith('Please call the office.')


def test_page_script_links_the_office_number_in_messages():
    script = open(server.PORTAL_SCRIPT_FILE).read()
    assert ".emergency a[href^=\"tel:\"]" in script and 'link.href = officeLink.getAttribute' in script


# ----------------------------------------------------- Turnstile switched off --

def test_switched_off_the_server_skips_the_check(client, db, turnstile_stub, monkeypatch):
    monkeypatch.setenv('TURNSTILE_DISABLED', '1')
    status, body = submit(client(), form(turnstileToken=''))
    assert (status, body) == (200, {'ok': True})
    assert len(stored(db)) == 1 and turnstile_stub.requests == []


def test_switched_off_works_without_any_keys(client, db, monkeypatch):
    monkeypatch.setenv('TURNSTILE_DISABLED', 'true')
    assert submit(client(), form(turnstileToken=''))[0] == 200
    _, html = page(client())
    assert 'data-check="off"' in html  # customer-request.js shows the form and loads no widget


def test_switched_off_rate_limits_and_honeypot_still_apply(client, db, monkeypatch):
    monkeypatch.setenv('TURNSTILE_DISABLED', '1')
    anon = client()
    assert submit(anon, form(**{requests_mod.HONEYPOT_FIELD: 'bot'}))[0] == 200
    assert stored(db)[0]['status'] == 'dismissed'
    for _ in range(requests_mod.MAX_PER_VISITOR - 1):
        submit(anon)
    assert submit(anon)[0] == 429


@pytest.mark.parametrize('value', ['', '0', 'false', 'no'])
def test_anything_else_leaves_the_check_on(client, db, monkeypatch, value):
    monkeypatch.setenv('TURNSTILE_DISABLED', value)
    assert submit(client(), form(turnstileToken=''))[0] == 503  # no secret: fails closed
    _, html = page(client())
    assert 'data-check="on"' in html


def test_the_widget_script_is_only_loaded_when_the_check_is_on():
    assert 'turnstile/v0/api.js' not in open(server.PORTAL_FILE).read()
    script = open(server.PORTAL_SCRIPT_FILE).read()
    assert "form.dataset.check !== 'off'" in script and 'widget.remove()' in script
    assert "The robot check didn't load" in script  # blocked script: say so, give the number


def test_send_waits_for_a_background_token_instead_of_asking_for_a_hidden_check():
    """Invisible mode: the token arrives a few seconds after load, with nothing on screen."""
    script = open(server.PORTAL_SCRIPT_FILE).read()
    assert 'waitForToken().then(' in script and 'TOKEN_WAIT_MS' in script
    assert "'Please complete the check above the button.'" not in script


@pytest.mark.parametrize('zip_code', ['04072', '04072-1234', '03820'])
def test_zip_codes_are_stored(client, db, turnstile_stub, zip_code):
    assert submit(client(), form(serviceZip=f' {zip_code} '))[0] == 200
    assert stored(db)[0]['service_zip'] == zip_code


def test_the_form_asks_for_a_zip(client):
    _, html = page(client())
    assert 'name="serviceZip"' in html and 'autocomplete="postal-code"' in html and 'inputmode="numeric"' in html
    assert "'serviceZip'" in open(server.PORTAL_SCRIPT_FILE).read()


@pytest.mark.parametrize('path', ['/customer', '/customer/thanks'])
def test_page_assets_carry_the_app_version(client, path):
    """Cloudflare tells browsers to keep .js/.css for hours, overriding our no-cache. A versioned URL
    means a release never pairs a new page with an old cached script (the Nov 26 ZIP bug)."""
    _, body = client().request('GET', path)
    html = body.decode()
    assert f'href="/portal.css?v={server.APP_VERSION}"' in html
    if path == '/customer':
        assert f'src="/customer-request.js?v={server.APP_VERSION}"' in html
    assert 'href="/portal.css"' not in html and 'src="/customer-request.js"' not in html


def test_versioned_asset_urls_are_served(client):
    res, body = client().request('GET', f'/customer-request.js?v={server.APP_VERSION}')
    assert res.status == 200 and b'/api/public/appointment-requests' in body
