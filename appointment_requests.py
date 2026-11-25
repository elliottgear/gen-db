"""
Appointment requests: the public /customer form and the admin side that handles what it sends in.

The flow: a request comes in (new) -> an admin links it to a customer (matched) -> the office books
the visit (scheduled; booking happens outside the app for now) -> the service is logged on the
generator as usual. Dismissed is for spam and duplicates.

The public side may only create a request and never reads or returns existing data. Everything
else here is admin-only (`requests.manage` on every route in server.ROUTES, reads included).
Creating a customer from a request lives in server.py, which owns customers; it uses
row_for_linking() and link() from here so both writes land in one transaction.
"""
import html
import os
import re

import matching
import turnstile
from core import (EMAIL_RE, ApiError, _timestamp, audit, check_not_stale, id_field, require,
                  text_field)
from ratelimit import SlidingWindow, rate_limit_key

# Fixed on purpose: the public page must never read existing data. brands_missing_from_form()
# flags brands on file that aren't offered here.
BRANDS = ('Generac', 'Kohler', 'Cummins', 'Briggs & Stratton', 'Champion', 'Honeywell', 'Other', 'Not sure')
# Stored value -> label shown on the form and to admins. Relabel freely; never change the keys
# (they're what's in the database).
NEEDS = {'maintenance': 'Routine maintenance', 'repair': 'Repair', 'not_running': 'Not running'}
STATUSES = ('new', 'matched', 'scheduled', 'dismissed')
# Status changes an admin can make directly. new -> matched only happens by linking a customer
# (match() or creating one), never by setting the status alone.
MANUAL_TRANSITIONS = {('new', 'dismissed'), ('dismissed', 'new'), ('matched', 'scheduled'), ('matched', 'new'),
                      ('scheduled', 'matched')}

# A field real people never see or fill. A nonsense name, so browsers have nothing to autofill.
HONEYPOT_FIELD = 'qz7_vk2'
HONEYPOT_REASON = 'spam filter (honeypot)'

MAX_BODY_BYTES = 16 * 1024
# Several people often share one address (an office, a household, a mobile carrier), and typos or a
# failed robot check shouldn't use up their allowance, so the tight limit counts stored requests.
MAX_PER_VISITOR = 5        # stored requests per visitor per hour
MAX_ATTEMPTS_PER_VISITOR = 20  # submissions per visitor per hour, stored or not (stops hammering)
MAX_PER_DAY = 100          # stored requests per 24 hours, from everyone together
NOTES_MAX = 2000
STREET_MAX = 300
TOWN_MAX = 100

ATTEMPTS = SlidingWindow(60 * 60)              # key: visitor
STORED_BY_VISITOR = SlidingWindow(60 * 60)     # key: visitor
STORED = SlidingWindow(24 * 60 * 60)           # key: EVERYONE
EVERYONE = 'all'


# ---------------------------------------------------------------- public side --

def _form_values(body):
    """The public form's fields, validated, keyed by column name."""
    values = {
        'name': text_field(body, 'name', 'Name'),
        'phone': text_field(body, 'phone', 'Phone'),
        'email': text_field(body, 'email', 'Email'),
        'service_street': text_field(body, 'serviceStreet', 'Street address', STREET_MAX),
        'service_town': text_field(body, 'serviceTown', 'Town', TOWN_MAX),
        'brand': text_field(body, 'brand', 'Generator brand'),
        'need': text_field(body, 'need', 'Service needed'),
        'model': text_field(body, 'model', 'Model'),
        'notes': text_field(body, 'notes', 'Notes', NOTES_MAX),
    }
    values['phone_digits'] = matching.phone_digits(values['phone'])
    require(values['name'], 400, 'Please enter your name.')
    require(values['phone_digits'], 400, 'Please enter a 10-digit phone number.')
    require(values['email'] and EMAIL_RE.match(values['email']), 400, 'Please enter a valid email address.')
    require(values['service_street'], 400, 'Please enter the street address.')
    require(values['service_town'], 400, 'Please enter the town.')
    require(values['brand'] in BRANDS, 400, 'Please choose a generator brand.')
    require(values['need'] in NEEDS, 400, 'Please choose the service needed.')
    return values


def office_phone():
    return os.environ.get('OFFICE_PHONE', '').strip()


def _call_office():
    """'call the office at 207-363-7177', or just 'call the office' when OFFICE_PHONE isn't set.
    The page turns the number into a tap-to-call link."""
    phone = office_phone()
    return f'call the office at {phone}' if phone else 'call the office'


def _check_limits(visitor):
    """Counts this attempt; 429 past any limit. Runs before Cloudflare is asked, so a blocked
    attempt costs nothing. (Two at the exact same moment could both get the last slot; harmless.)"""
    too_many = f'Too many requests from your connection. Please try again later, or {_call_office()}.'
    require(ATTEMPTS.hit_if_under(visitor, MAX_ATTEMPTS_PER_VISITOR), 429, too_many)
    require(STORED_BY_VISITOR.count(visitor) < MAX_PER_VISITOR, 429, too_many)
    require(STORED.count(EVERYONE) < MAX_PER_DAY, 429, f"We've had a lot of requests today. Please {_call_office()}.")


def submit(conn, body, ip, source):
    """The public form. Creates one request and returns {'ok': True}; never reads or returns other data.

    ip/source come from ratelimit.client_ip(). The visitor's IP is only passed on to Cloudflare when
    Cloudflare itself reported it.
    """
    visitor = rate_limit_key(ip)
    _check_limits(visitor)
    values = _form_values(body)
    try:
        turnstile.verify(body.get('turnstileToken'), str(ip) if source == 'cloudflare' and ip else None)
    except turnstile.TurnstileUnavailable as e:
        print(f'turnstile unavailable: {e}', flush=True)
        raise ApiError(503, f"Online requests aren't available right now. Please {_call_office()}.")
    except turnstile.TurnstileRejected:
        raise ApiError(400, "We couldn't confirm you're not a robot. Please try the check again.")
    spam = body.get(HONEYPOT_FIELD) not in (None, '')
    now = _timestamp()
    values.update(created_at=now, updated_at=now, status='dismissed' if spam else 'new',
                  dismiss_reason=HONEYPOT_REASON if spam else '')
    conn.execute(f'INSERT INTO appointment_requests ({", ".join(values)}) VALUES ({", ".join("?" * len(values))})',
                 tuple(values.values()))
    conn.commit()
    STORED_BY_VISITOR.hit(visitor)
    STORED.hit(EVERYONE)
    print(f'appointment request stored (client ip via {source}{", honeypot" if spam else ""}'
          f'{", turnstile off" if turnstile.disabled() else ""})', flush=True)
    return {'ok': True}


def _options_html(options):
    return ''.join(f'<option value="{html.escape(value)}">{html.escape(label)}</option>' for value, label in options)


def page_replacements(app_version):
    """Values the server fills into the portal pages. Everything is HTML-escaped."""
    phone = office_phone()
    digits = matching.phone_digits(phone)
    check = turnstile.settings()
    return {
        '{{APP_VERSION}}': html.escape(app_version),
        '{{TURNSTILE_SITE_KEY}}': html.escape(check['site_key']),
        '{{TURNSTILE_CHECK}}': 'off' if check['disabled'] else 'on',
        '{{OFFICE_PHONE}}': html.escape(phone or 'the office'),
        '{{OFFICE_PHONE_TEL}}': html.escape('+1' + digits if digits else re.sub(r'[^\d+]', '', phone)),
        '{{HONEYPOT_FIELD}}': HONEYPOT_FIELD,
        '{{BRAND_OPTIONS}}': _options_html((b, b) for b in BRANDS),
        '{{NEED_OPTIONS}}': _options_html(NEEDS.items()),
    }


# ----------------------------------------------------------------- admin side --

def to_json(row):
    return {
        'id': row['id'], 'createdAt': row['created_at'], 'status': row['status'],
        'name': row['name'], 'phone': row['phone'], 'email': row['email'],
        'serviceStreet': row['service_street'], 'serviceTown': row['service_town'],
        'brand': row['brand'], 'model': row['model'],
        'need': row['need'], 'needLabel': NEEDS.get(row['need'], row['need']), 'notes': row['notes'],
        'customerId': row['customer_id'], 'generatorId': row['generator_id'],
        'dismissReason': row['dismiss_reason'], 'handledBy': row['handled_by'], 'handledAt': row['handled_at'],
        'updatedBy': row['updated_by'], 'updatedAt': row['updated_at'] or '',
    }


def _match_data(conn):
    customers = [dict(r) for r in conn.execute(
        'SELECT id, name, phone, email, physical_address, billing_address FROM customers')]
    generators = [dict(r) for r in conn.execute(
        'SELECT id, customer_id, make, model, serial, physical_address FROM generators')]
    return customers, generators


def _match(row, data):
    # The matcher compares the street line (house number + street); the town is shown to the admin.
    return matching.match_request(dict(row, service_address=row['service_street']), *data)


def brands_missing_from_form(conn):
    """Brands on file that the public form doesn't offer (so customers would have to pick "Other")."""
    offered = {b.lower() for b in BRANDS}
    makes = {r[0].strip() for r in conn.execute('SELECT DISTINCT make FROM generators') if r[0] and r[0].strip()}
    return sorted((m for m in makes if m.lower() not in offered), key=str.lower)


def list_all(conn):
    rows = conn.execute('SELECT * FROM appointment_requests ORDER BY created_at DESC, id DESC').fetchall()
    data = _match_data(conn) if any(r['status'] == 'new' for r in rows) else None
    out = []
    for row in rows:
        item = to_json(row)
        if row['status'] == 'new':
            result = _match(row, data)
            item['suggestion'] = result['candidates'][0] if result['candidates'] else None
            item['conflict'] = result['conflict']
        out.append(item)
    return {'requests': out, 'formBrandGaps': brands_missing_from_form(conn)}


def _row(conn, req_id):
    row = conn.execute('SELECT * FROM appointment_requests WHERE id=?', (req_id,)).fetchone()
    require(row, 404, 'Request not found.')
    return row


def get(conn, req_id):
    row = _row(conn, req_id)
    result = _match(row, _match_data(conn))
    return {'request': to_json(row), 'candidates': result['candidates'], 'conflict': result['conflict']}


def _transition(conn, row, user, status, action, detail='', **changes):
    """The one way a request changes: sets status and `changes`, stamps who and when, audits, commits.
    Callers check that the change is allowed first."""
    now = _timestamp()
    columns = dict(changes, status=status, handled_by=user['username'], handled_at=now,
                   updated_by=user['username'], updated_at=now)
    conn.execute(f'UPDATE appointment_requests SET {", ".join(f"{c}=?" for c in columns)} WHERE id=?',
                 (*columns.values(), row['id']))
    audit(conn, user, action, 'appointment_request', row['id'], detail)
    conn.commit()
    return to_json(_row(conn, row['id']))


def row_for_linking(conn, req_id, body):
    """The request, checked as fresh and still new, ready to be linked to a customer."""
    row = _row(conn, req_id)
    check_not_stale(row, body, 'request')
    require(row['status'] == 'new', 409, f"This request is already {row['status']}. Reload to see the latest.")
    return row


def link(conn, row, user, customer_id, generator_id, action, detail):
    """new -> matched, pointing at a customer (and maybe one of their generators)."""
    return _transition(conn, row, user, 'matched', action, detail, customer_id=customer_id, generator_id=generator_id)


def _generator_for(conn, body, customer_id):
    if body.get('generatorId') in (None, ''):
        return None
    gen_id = id_field(body, 'generatorId', 'Invalid generator.')
    gen = conn.execute('SELECT customer_id FROM generators WHERE id=?', (gen_id,)).fetchone()
    require(gen and gen['customer_id'] == customer_id, 400, "That generator doesn't belong to this customer.")
    return gen_id


def match(conn, req_id, body, user):
    row = row_for_linking(conn, req_id, body)
    customer_id = id_field(body, 'customerId', 'Choose a customer.')
    require(conn.execute('SELECT 1 FROM customers WHERE id=?', (customer_id,)).fetchone(), 400, 'Customer not found.')
    gen_id = _generator_for(conn, body, customer_id)
    return link(conn, row, user, customer_id, gen_id, 'match', f'customer {customer_id}, generator {gen_id or "-"}')


def set_status(conn, req_id, body, user):
    row = _row(conn, req_id)
    check_not_stale(row, body, 'request')
    status = text_field(body, 'status', 'Status')
    require(status in STATUSES, 400, 'Unknown status.')
    require((row['status'], status) in MANUAL_TRANSITIONS, 400, f"A {row['status']} request can't be marked {status}.")
    if status == 'dismissed':
        reason = text_field(body, 'reason', 'Reason') or 'dismissed by admin'
        return _transition(conn, row, user, status, status, reason, dismiss_reason=reason)
    if status == 'new':  # back to the start: forget the dismissal and any link
        return _transition(conn, row, user, status, status, dismiss_reason='', customer_id=None, generator_id=None)
    return _transition(conn, row, user, status, status)
