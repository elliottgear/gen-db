"""
Addresses -> map coordinates, for the generator map. Uses the US Census Bureau's free batch
geocoder (no account or key) and caches every answer in the `geocodes` table, keyed by the
address text, so each address is looked up once and again only after it's edited.

Only addresses are sent (street, town, state, ZIP and a row number): no names, phones or emails.

A generator is placed at its site address, or at its customer's physical address when it has none.

Configuration (environment variables):
  GEOCODER_URL   override the Census batch endpoint (tests)
"""
import csv
import datetime
import io
import os
import re
import urllib.error
import urllib.request
import uuid

from core import _local_now, _timestamp

CENSUS_BATCH_URL = 'https://geocoding.geo.census.gov/geocoder/locations/addressbatch'
BENCHMARK = 'Public_AR_Current'
BATCH_SIZE = 1000          # the API takes up to 10,000; smaller batches fail smaller
TIMEOUT_SECONDS = 60
RETRY_FAILED_AFTER = datetime.timedelta(hours=1)

FOUND, NOT_FOUND, INCOMPLETE, FAILED = 'found', 'not_found', 'incomplete', 'failed'
STATE_NAMES = {'maine': 'ME', 'new hampshire': 'NH', 'massachusetts': 'MA', 'vermont': 'VT'}
DEFAULT_STATE = 'ME'


class GeocodeUnavailable(Exception):
    """The geocoder couldn't be reached or answered with an error. Try again later."""


# ------------------------------------------------------------- addresses --

def address_key(text):
    """The cache key: the address with case and spacing normalized. '' for no address."""
    return ' '.join(re.split(r'\s+', (text or '').strip().lower())).strip(' ,')


def location_address(generator_address, customer_address):
    """(address, 'site' or 'customer') where a generator is: its site, else its customer's address."""
    if (generator_address or '').strip():
        return generator_address, 'site'
    if (customer_address or '').strip():
        return customer_address, 'customer'
    return '', None


def split_address(text):
    """(street, city, state, zip) from an address as typed, or None if it's too incomplete to look up.

    Takes "12 Main St\\nSaco, ME 04072" (the app's usual layout) or "12 Main St, Saco, ME 04072".
    Needs a street and a town or ZIP. The state defaults to ME. PO boxes can't be placed on a map.
    """
    lines = [line.strip(' ,') for line in (text or '').splitlines() if line.strip(' ,')]
    if len(lines) >= 2:
        street, tail = lines[0], lines[-1]
    elif lines:
        parts = [p.strip() for p in lines[0].split(',') if p.strip()]
        street, tail = parts[0], ', '.join(parts[1:])
    else:
        return None
    if not re.search(r'[a-z]', street, re.I) or re.match(r'^p\.?\s*o\.?\s*box\b', street, re.I):
        return None
    zip_match = re.search(r'\b(\d{5})(?:-\d{4})?\s*$', tail)
    zip_code = zip_match.group(1) if zip_match else ''
    rest = tail[:zip_match.start()] if zip_match else tail
    rest = rest.strip(' ,')
    state = ''
    for name, abbr in STATE_NAMES.items():
        if rest.lower().endswith(name):
            state, rest = abbr, rest[:-len(name)]
            break
    else:
        state_match = re.search(r'(?:^|[\s,])([A-Za-z]{2})$', rest)
        if state_match and (',' in rest or zip_code or ' ' in rest):
            state, rest = state_match.group(1).upper(), rest[:state_match.start()]
    city = rest.strip(' ,')
    if not city and not zip_code:
        return None
    return street, city, state or DEFAULT_STATE, zip_code


# ----------------------------------------------------------- census API --

def batch_csv(items):
    """items: [(row_id, (street, city, state, zip))] -> the CSV the batch API expects (no header)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator='\n')
    for row_id, (street, city, state, zip_code) in items:
        writer.writerow([row_id, street, city, state, zip_code])
    return out.getvalue()


def parse_batch_response(body):
    """{row_id: (lat, lng, matched address) or None} from the batch API's CSV answer.
    Rows: id, input, Match/No_Match/Tie, Exact/Non_Exact, matched address, "lon,lat", tiger id, side."""
    results = {}
    for row in csv.reader(io.StringIO(body)):
        if not row or not row[0].strip():
            continue
        row_id = row[0].strip()
        results[row_id] = None
        if len(row) >= 6 and row[2] == 'Match':
            try:
                lng, lat = (float(x) for x in row[5].split(','))
            except ValueError:
                continue
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                results[row_id] = (lat, lng, row[4])
    return results


def _multipart(fields, file_field, filename, file_text):
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
             for name, value in fields.items()]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                 f'Content-Type: text/csv\r\n\r\n{file_text}\r\n--{boundary}--\r\n')
    return ''.join(parts).encode('utf-8'), f'multipart/form-data; boundary={boundary}'


def lookup(items):
    """items: [(row_id, parts)] -> parse_batch_response() of the answer. Raises GeocodeUnavailable."""
    body, content_type = _multipart({'benchmark': BENCHMARK}, 'addressFile', 'addresses.csv', batch_csv(items))
    url = os.environ.get('GEOCODER_URL', '').strip() or CENSUS_BATCH_URL
    request = urllib.request.Request(url, data=body, method='POST', headers={'Content-Type': content_type})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            text = response.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        raise GeocodeUnavailable(f'geocoder returned HTTP {e.code}')
    except (urllib.error.URLError, OSError) as e:
        raise GeocodeUnavailable(f'could not reach the geocoder: {getattr(e, "reason", e)}')
    return parse_batch_response(text)


# ---------------------------------------------------------------- cache --

def addresses_in_use(conn):
    """{address key: address text} for every generator's map location."""
    rows = conn.execute('SELECT g.physical_address AS site, c.physical_address AS home FROM generators g '
                        'LEFT JOIN customers c ON c.id = g.customer_id')
    out = {}
    for r in rows:
        text, _ = location_address(r['site'], r['home'])
        key = address_key(text)
        if key:
            out.setdefault(key, text)
    return out


def _due(row, now):
    if row is None:
        return True
    if row['status'] != FAILED:
        return False
    try:
        return now - datetime.datetime.fromisoformat(row['updated_at']) >= RETRY_FAILED_AFTER
    except ValueError:
        return True


def _store(conn, key, status, lat=None, lng=None, matched=None):
    conn.execute('INSERT OR REPLACE INTO geocodes (address_key, lat, lng, status, matched_address, updated_at) '
                 'VALUES (?,?,?,?,?,?)', (key, lat, lng, status, matched, _timestamp()))


def geocode_pending(conn):
    """Looks up every address in use that isn't cached yet (and retries failures after an hour).
    Returns {status: count} for what it stored this time. Never raises for a geocoder problem."""
    now = _local_now()
    cached = {r['address_key']: r for r in conn.execute('SELECT address_key, status, updated_at FROM geocodes')}
    todo = [(key, text) for key, text in addresses_in_use(conn).items() if _due(cached.get(key), now)]
    counts = {}

    def stored(status, n=1):
        counts[status] = counts.get(status, 0) + n

    to_send = []
    for key, text in todo:
        parts = split_address(text)
        if parts is None:
            _store(conn, key, INCOMPLETE)
            stored(INCOMPLETE)
        else:
            to_send.append((key, parts))
    conn.commit()
    for start in range(0, len(to_send), BATCH_SIZE):
        chunk = to_send[start:start + BATCH_SIZE]
        items = [(str(i), parts) for i, (_, parts) in enumerate(chunk, 1)]
        try:
            results = lookup(items)
        except GeocodeUnavailable as e:
            print(f'[geocode] {len(chunk)} address(es) not looked up: {e}', flush=True)
            for key, _ in chunk:
                _store(conn, key, FAILED)
            stored(FAILED, len(chunk))
            conn.commit()
            continue
        for i, (key, _) in enumerate(chunk, 1):
            hit = results.get(str(i))
            if hit:
                _store(conn, key, FOUND, *hit)
                stored(FOUND)
            else:
                _store(conn, key, NOT_FOUND)
                stored(NOT_FOUND)
        conn.commit()
    return counts


# ------------------------------------------------------------------- map --

def generator_map(conn):
    """Where each generator goes on the map, or why it can't go there. No names or contact details."""
    cache = {r['address_key']: r for r in conn.execute('SELECT * FROM geocodes')}
    points, unplaced = [], []
    rows = conn.execute('SELECT g.id, g.physical_address AS site, c.physical_address AS home FROM generators g '
                        'LEFT JOIN customers c ON c.id = g.customer_id ORDER BY g.id')
    for r in rows:
        text, source = location_address(r['site'], r['home'])
        hit = cache.get(address_key(text)) if text else None
        if not text:
            unplaced.append({'generatorId': r['id'], 'reason': 'no_address'})
        elif hit is None or hit['status'] == FAILED:
            unplaced.append({'generatorId': r['id'], 'reason': 'pending'})
        elif hit['status'] == FOUND:
            points.append({'generatorId': r['id'], 'lat': hit['lat'], 'lng': hit['lng'], 'from': source})
        else:
            unplaced.append({'generatorId': r['id'], 'reason': hit['status']})  # not_found / incomplete
    return {'points': points, 'unplaced': unplaced}
