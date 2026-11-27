"""
Fuzzy matching of appointment requests to existing customers and generators. No AI.

match_request() is pure: it takes plain dicts (database rows work) and returns ranked candidates,
each with the reasons it was suggested. Nothing here writes or merges anything; an admin confirms.
An AI tie-breaker can be added later as a separate step that re-ranks the same candidate list.

Signals:
  phone digits or email  decide a match outright   -> tier "strong", unless the name AND the
                         address both differ from the customer's: then "likely", with the reason
                         "name and address differ" (a shared family phone, or a property manager
                         writing from a company email, still shows, just not as a sure thing)
  similar service address is a likely match         -> tier "likely" (not when both sides have a
                                                        ZIP and they differ: same street, other town)
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
    'differs': 0,
}
REASONS = {
    'phone': 'phone matches',
    'email': 'email matches',
    'address_exact': 'address matches',
    'address_similar': 'address similar',
    'name_exact': 'name matches',
    'name_similar': 'name similar',
    'brand': 'brand matches',
    'differs': 'name and address differ',
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


def zip_in(value):
    """The 5-digit ZIP ending a line of an address ("Saco, ME 04072"), or '' if there isn't one.
    Only at a line's end, so a 5-digit house number ("12345 Main St") is never taken for one."""
    found = re.findall(r'\b(\d{5})(?:-\d{4})?[ \t]*$', value or '', re.M)
    return found[-1] if found else ''


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


def _address_match(request_address, customer, generators, request_zip=''):
    """(best address signal or None, the customer's generators at that best-matching address).

    Compares against the customer's physical and billing addresses and each generator's site. When
    the request and an address both have a ZIP and they differ, it's the same street in another town.
    """
    def signal(addr):
        other_zip = zip_in(addr)
        if request_zip and other_zip and request_zip != other_zip:
            return None
        return address_signal(request_address, parse_address(addr))
    scored = [(signal(addr), None) for addr in (customer['physical_address'], customer['billing_address'])]
    scored += [(signal(g['physical_address']), g) for g in generators]
    best = max((sig for sig, _ in scored if sig), key=SCORES.get, default=None)
    return best, [g for sig, g in scored if g is not None and best and sig == best]


def _has_address(customer, generators):
    """True if anything on file for this customer has an address the matcher can compare."""
    addresses = [customer['physical_address'], customer['billing_address']] + [g['physical_address'] for g in generators]
    return any(parse_address(a) for a in addresses)


def _name_and_address_differ(request, req_address, customer, generators, best_address, name):
    """Both are known on each side and neither matches. Missing details never count as differing."""
    names_known = bool(name_words(request.get('name')) and name_words(customer['name']))
    addresses_known = bool(req_address and _has_address(customer, generators))
    return names_known and addresses_known and not name and not best_address


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

    request:    dict with name, phone, email, service_address, brand, and optionally service_zip
    customers:  dicts with id, name, phone, email, physical_address, billing_address
    generators: dicts with id, customer_id, make, model, serial, physical_address

    Returns {'candidates': [...], 'conflict': bool}. Each candidate has customerId, customerName,
    customerPhone, customerAddress, tier ('strong' or 'likely'), score, reasons, generatorId and
    generatorLabel. Strong candidates come first, then by score. `conflict` is true when the phone
    or email points at more than one customer
    (e.g. the phone matches one customer and the email another).
    """
    req_phone = phone_digits(request.get('phone'))
    req_email = normalize_email(request.get('email'))
    req_address = parse_address(request.get('service_address'))
    req_zip = zip_in(request.get('service_zip'))
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

        best_address, at_address = _address_match(req_address, c, gens, req_zip)
        if best_address:
            signals.append(best_address)

        contact = 'phone' in signals or 'email' in signals
        if not contact and not best_address:
            continue  # a name alone never makes a suggestion

        name = name_signal(request.get('name'), c['name'])
        if name:
            signals.append(name)
        strong = contact and not _name_and_address_differ(request, req_address, c, gens, best_address, name)
        if contact and not strong:
            signals.append('differs')
        generator = _pick_generator(gens, at_address, brand)
        if generator and brand and (generator['make'] or '').strip().lower() == brand:
            signals.append('brand')

        candidates.append({
            'customerId': c['id'],
            'customerName': c['name'],
            'customerPhone': c['phone'] or '',
            'customerAddress': _first_line(c['physical_address']),
            'tier': STRONG if strong else LIKELY,
            'contact': contact,
            'score': sum(SCORES[s] for s in signals),
            'reasons': [REASONS[s] for s in signals],
            'generatorId': generator['id'] if generator else None,
            'generatorLabel': generator_label(generator) if generator else '',
        })

    candidates.sort(key=lambda m: (m['tier'] != STRONG, -m['score'], (m['customerName'] or '').lower(), m['customerId']))
    # Phone/email pointing at more than one customer, whatever tier each ended up in.
    conflict = len({m['customerId'] for m in candidates if m['contact']}) > 1
    for m in candidates:
        del m['contact']
    return {'candidates': candidates[:MAX_CANDIDATES], 'conflict': conflict}
