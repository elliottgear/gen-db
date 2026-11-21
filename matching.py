"""
Fuzzy matching of appointment requests to existing customers and generators. No AI.

match_request() is pure: it takes plain dicts (database rows work) and returns ranked candidates,
each with the reasons it was suggested. Nothing here writes or merges anything; an admin confirms.
An AI tie-breaker can be added later as a separate step that re-ranks the same candidate list.

Signals:
  phone digits or email  decide a match outright   -> tier "strong"
  similar service address is a likely match         -> tier "likely"
  name                    only supports a match (never suggests one on its own)
  brand                   only helps pick which of the customer's generators is meant
"""
import difflib
import re

STRONG = 'strong'
LIKELY = 'likely'

SCORES = {
    'phone': 100,
    'email': 100,
    'address_exact': 60,
    'address_similar': 50,
    'name_exact': 20,
    'name_similar': 10,
    'brand': 5,
}
REASONS = {
    'phone': 'phone matches',
    'email': 'email matches',
    'address_exact': 'address matches',
    'address_similar': 'address similar',
    'name_exact': 'name matches',
    'name_similar': 'name similar',
    'brand': 'brand matches',
}
ADDRESS_SIMILARITY = 0.85  # difflib ratio between normalized street names (same house number required)
NAME_SIMILARITY = 0.8      # difflib ratio between two name words (one letter off in five)
MAX_CANDIDATES = 3
NO_BRAND = {'', 'other', 'not sure'}

# Street words reduced to one spelling, so "12 Main Street" and "12 Main St." compare equal.
STREET_WORDS = {
    'street': 'st', 'str': 'st', 'road': 'rd', 'avenue': 'ave', 'av': 'ave', 'drive': 'dr', 'drv': 'dr',
    'lane': 'ln', 'way': 'wy', 'court': 'ct', 'circle': 'cir', 'place': 'pl', 'boulevard': 'blvd',
    'highway': 'hwy', 'route': 'rte', 'rt': 'rte', 'terrace': 'ter', 'parkway': 'pkwy', 'point': 'pt',
    'extension': 'ext', 'heights': 'hts', 'hill': 'hl', 'mount': 'mt', 'square': 'sq', 'trail': 'trl',
    'north': 'n', 'south': 's', 'east': 'e', 'west': 'w',
}
UNIT_WORDS = {'apt', 'apartment', 'unit', 'suite', 'ste', 'lot', 'po'}
NAME_SKIP = {'and', 'mr', 'mrs', 'ms', 'dr', 'jr', 'sr', 'ii', 'iii', 'the', 'family'}


# ------------------------------------------------------------- normalizers --

def phone_digits(value):
    """The 10-digit US number in `value`, or '' if there isn't exactly one."""
    digits = re.sub(r'\D', '', value or '')
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    return digits if len(digits) == 10 else ''


def normalize_email(value):
    value = (value or '').strip().lower()
    return value if '@' in value else ''


def parse_address(value):
    """(house number, normalized street) from the first line of an address, or None.

    Only the part before the first comma or newline is used, so a town or ZIP doesn't affect it.
    An address without a leading house number gives None: too vague to match on.
    """
    first = re.split(r'[\n,]', value or '', maxsplit=1)[0].lower()
    tokens = re.findall(r'[a-z0-9]+', first)
    if len(tokens) < 2 or not re.fullmatch(r'\d+[a-z]?', tokens[0]):
        return None
    street = []
    for token in tokens[1:]:
        if token in UNIT_WORDS:
            break
        street.append(STREET_WORDS.get(token, token))
    return (tokens[0], ' '.join(street)) if street else None


def name_words(value):
    return [w for w in re.findall(r'[a-z]+', (value or '').lower()) if w not in NAME_SKIP and len(w) > 1]


def _similar(a, b, threshold):
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= threshold


# ----------------------------------------------------------------- signals --

def address_signal(a, b):
    """'address_exact', 'address_similar' or None for two parse_address() results."""
    if not a or not b or a[0] != b[0]:  # different house numbers never match
        return None
    if a[1] == b[1]:
        return 'address_exact'
    if _similar(a[1], b[1], ADDRESS_SIMILARITY):
        return 'address_similar'
    return None


def name_signal(request_name, customer_name):
    """'name_exact', 'name_similar' or None. Supporting evidence only."""
    a, b = name_words(request_name), name_words(customer_name)
    if not a or not b:
        return None
    if sorted(a) == sorted(b):
        return 'name_exact'
    surname = a[-1]  # "Amy & Dennis Roy" -> roy
    if any(_similar(surname, w, NAME_SIMILARITY) for w in b):
        return 'name_similar'
    return None


def _better(signal, best):
    order = (None, 'address_similar', 'address_exact')
    return order.index(signal) > order.index(best)


def _pick_generator(generators, at_address, brand):
    """The generator the request most likely means, or None to let the admin pick."""
    same_brand = [g for g in generators if brand and (g['make'] or '').strip().lower() == brand]
    if len(at_address) == 1:
        return at_address[0]
    if at_address:
        narrowed = [g for g in at_address if g in same_brand]
        return narrowed[0] if len(narrowed) == 1 else None
    if len(same_brand) == 1:
        return same_brand[0]
    if len(generators) == 1:
        return generators[0]
    return None


def _first_line(value):
    return (value or '').split('\n')[0].strip()


def generator_label(g):
    return f"{g['make']} {g['model']} (S/N {g['serial']})"


# ------------------------------------------------------------------ public --

def match_request(request, customers, generators):
    """Ranked match candidates for one appointment request.

    request:    dict with name, phone, email, service_address, brand
    customers:  dicts with id, name, phone, email, physical_address, billing_address
    generators: dicts with id, customer_id, make, model, serial, physical_address

    Returns {'candidates': [...], 'conflict': bool}. Each candidate has customerId, customerName,
    customerPhone, customerAddress, tier ('strong' or 'likely'), score, reasons, generatorId and
    generatorLabel. `conflict` is true when strong signals point at more than one customer
    (e.g. the phone matches one customer and the email another).
    """
    req_phone = phone_digits(request.get('phone'))
    req_email = normalize_email(request.get('email'))
    req_address = parse_address(request.get('service_address'))
    brand = (request.get('brand') or '').strip().lower()
    brand = '' if brand in NO_BRAND else brand

    by_customer = {}
    for g in generators:
        by_customer.setdefault(g['customer_id'], []).append(g)

    candidates = []
    for c in customers:
        gens = by_customer.get(c['id'], [])
        signals = []
        if req_phone and req_phone == phone_digits(c['phone']):
            signals.append('phone')
        if req_email and req_email == normalize_email(c['email']):
            signals.append('email')

        best_address, at_address = None, []
        for addr in (c['physical_address'], c['billing_address']):
            sig = address_signal(req_address, parse_address(addr))
            if _better(sig, best_address):
                best_address = sig
        for g in gens:
            sig = address_signal(req_address, parse_address(g['physical_address']))
            if sig and _better(sig, best_address):
                best_address, at_address = sig, [g]
            elif sig and sig == best_address:
                at_address.append(g)
        if best_address:
            signals.append(best_address)

        strong = 'phone' in signals or 'email' in signals
        if not strong and not best_address:
            continue  # a name alone never makes a suggestion

        name = name_signal(request.get('name'), c['name'])
        if name:
            signals.append(name)
        generator = _pick_generator(gens, at_address, brand)
        if generator and brand and (generator['make'] or '').strip().lower() == brand:
            signals.append('brand')

        candidates.append({
            'customerId': c['id'],
            'customerName': c['name'],
            'customerPhone': c['phone'] or '',
            'customerAddress': _first_line(c['physical_address']),
            'tier': STRONG if strong else LIKELY,
            'score': sum(SCORES[s] for s in signals),
            'reasons': [REASONS[s] for s in signals],
            'generatorId': generator['id'] if generator else None,
            'generatorLabel': generator_label(generator) if generator else '',
        })

    candidates.sort(key=lambda m: (-m['score'], (m['customerName'] or '').lower(), m['customerId']))
    conflict = len({m['customerId'] for m in candidates if m['tier'] == STRONG}) > 1
    return {'candidates': candidates[:MAX_CANDIDATES], 'conflict': conflict}
