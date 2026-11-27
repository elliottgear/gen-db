"""The appointment-request matcher: phone/email decide, address is likely, name only supports."""
import pytest

import matching


def customer(id, name, phone='', email='', physical='', billing=''):
    return {'id': id, 'name': name, 'phone': phone, 'email': email,
            'physical_address': physical, 'billing_address': billing}


def generator(id, customer_id, make='Generac', model='G22', serial=None, address=''):
    return {'id': id, 'customer_id': customer_id, 'make': make, 'model': model,
            'serial': serial or f'S{id}', 'physical_address': address}


def request(**fields):
    base = {'name': 'Pat Doe', 'phone': '', 'email': '', 'service_address': '', 'brand': 'Not sure'}
    base.update(fields)
    return base


def match(req, customers, generators=()):
    return matching.match_request(req, customers, list(generators))


# ------------------------------------------------------------ normalizers --

@pytest.mark.parametrize('raw', ['(207) 555-0142', '207.555.0142', '+1 207 555 0142', '1-207-555-0142', '2075550142'])
def test_phone_formats_normalize_to_ten_digits(raw):
    assert matching.phone_digits(raw) == '2075550142'


@pytest.mark.parametrize('raw', ['', '555-0142', '12075550142999', 'call me'])
def test_phones_that_arent_ten_digits_never_match(raw):
    assert matching.phone_digits(raw) == ''


@pytest.mark.parametrize('a, b', [
    ('12 Main Street', '12 Main St.'),
    ('880 Stonehenge Way, Kennebunk ME', '880 stonehenge wy'),
    ('5 North Road\nSaco, ME 04072', '5 N Rd'),
    ('14 Ocean Ave Apt 3', '14 Ocean Avenue'),
])
def test_address_spellings_compare_equal(a, b):
    assert matching.address_signal(matching.parse_address(a), matching.parse_address(b)) == 'address_exact'


def test_small_typos_are_similar():
    a, b = matching.parse_address('880 Stonehenge Way'), matching.parse_address('880 Stonehedge Way')
    assert matching.address_signal(a, b) == 'address_similar'


@pytest.mark.parametrize('a, b', [
    ('12 Main St', '21 Main St'),        # different house number never matches
    ('12 Main St', '12 Maple Rd'),       # different street
    ('Main St', 'Main St'),              # no house number: too vague
    ('', '12 Main St'),
])
def test_addresses_that_must_not_match(a, b):
    assert matching.address_signal(matching.parse_address(a), matching.parse_address(b)) is None


@pytest.mark.parametrize('a, b, expected', [
    ('Dennis Roy', 'Amy & Dennis Roy', 'name_similar'),
    ('Amy & Dennis Roy', 'Dennis and Amy Roy', 'name_exact'),
    ('Mrs. Jane Smith', 'Jane Smith', 'name_exact'),
    ('Jane Smyth', 'Jane Smith', 'name_similar'),
    ('Jane Doe', 'John Smith', None),
])
def test_name_signals(a, b, expected):
    assert matching.name_signal(a, b) == expected


# ------------------------------------------------------------------ tiers --

def test_phone_alone_is_a_strong_match():
    result = match(request(phone='207.555.0142'), [customer(1, 'Marlene Ouellette', phone='(207) 555-0142')])
    [c] = result['candidates']
    assert c['tier'] == 'strong' and c['reasons'] == ['phone matches'] and c['customerId'] == 1


def test_email_alone_is_a_strong_match_ignoring_case():
    result = match(request(email=' Pat@Example.COM '), [customer(1, 'X', email='pat@example.com')])
    assert result['candidates'][0]['tier'] == 'strong'
    assert result['candidates'][0]['reasons'] == ['email matches']


def test_similar_address_alone_is_a_likely_match():
    result = match(request(service_address='880 Stonehedge Way'), [customer(1, 'X', physical='880 Stonehenge Way\nKennebunk')])
    [c] = result['candidates']
    assert c['tier'] == 'likely' and c['reasons'] == ['address similar']


def test_billing_address_counts_too():
    result = match(request(service_address='3 Elm St'), [customer(1, 'X', physical='PO Box 9', billing='3 Elm Street')])
    assert result['candidates'][0]['reasons'] == ['address matches']


def test_generator_site_address_counts_and_picks_that_generator():
    customers = [customer(1, 'Landlord', physical='1 Office Park')]
    generators = [generator(10, 1, address='77 Lake Rd'), generator(11, 1, address='9 Hill St')]
    [c] = match(request(service_address='77 Lake Road'), customers, generators)['candidates']
    assert c['reasons'] == ['address matches'] and c['generatorId'] == 10


def test_name_alone_never_suggests_a_match():
    assert match(request(name='Marlene Ouellette'), [customer(1, 'Marlene Ouellette')])['candidates'] == []


def test_name_supports_a_match_found_another_way():
    customers = [customer(1, 'Amy Roy', physical='5 Pine St'), customer(2, 'Bob Stone', physical='5 Pine St')]
    result = match(request(name='Amy Roy', service_address='5 Pine Street'), customers)
    assert [c['customerId'] for c in result['candidates']] == [1, 2]
    assert result['candidates'][0]['reasons'] == ['address matches', 'name matches']


def test_strong_outranks_likely():
    customers = [customer(1, 'Address Match', physical='5 Pine St'), customer(2, 'Phone Match', phone='2075550142')]
    result = match(request(phone='207-555-0142', service_address='5 Pine St'), customers)
    assert [c['customerId'] for c in result['candidates']] == [2, 1]
    assert [c['tier'] for c in result['candidates']] == ['strong', 'likely']


def test_phone_and_email_pointing_at_different_customers_is_a_conflict():
    customers = [customer(1, 'A', phone='2075550142'), customer(2, 'B', email='pat@example.com')]
    result = match(request(phone='2075550142', email='pat@example.com'), customers)
    assert result['conflict'] is True
    assert {c['customerId'] for c in result['candidates']} == {1, 2}


def test_one_customer_matching_several_ways_is_not_a_conflict():
    customers = [customer(1, 'A', phone='2075550142', email='pat@example.com')]
    result = match(request(phone='2075550142', email='pat@example.com'), customers)
    assert result['conflict'] is False
    assert result['candidates'][0]['reasons'] == ['phone matches', 'email matches']


def test_nothing_matches():
    result = match(request(phone='2075550000', email='x@y.com', service_address='1 Nowhere Ln'),
                   [customer(1, 'A', phone='2075551111', email='a@b.com', physical='2 Somewhere St')])
    assert result == {'candidates': [], 'conflict': False}


def test_at_most_three_candidates():
    customers = [customer(i, f'C{i}', physical='5 Pine St') for i in range(1, 8)]
    assert len(match(request(service_address='5 Pine St'), customers)['candidates']) == matching.MAX_CANDIDATES


def test_empty_and_missing_fields_dont_crash():
    customers = [customer(1, '', phone=None, email=None, physical=None, billing=None)]
    generators = [generator(1, 1, make=None, address=None)]
    assert match(request(name='', phone=None, email=None, service_address=None, brand=None), customers, generators) == \
        {'candidates': [], 'conflict': False}


# -------------------------------------------------------- generator choice --

def test_brand_picks_the_generator_when_the_customer_has_several():
    customers = [customer(1, 'A', phone='2075550142')]
    generators = [generator(10, 1, make='Generac'), generator(11, 1, make='Kohler')]
    [c] = match(request(phone='2075550142', brand='Kohler'), customers, generators)['candidates']
    assert c['generatorId'] == 11 and 'brand matches' in c['reasons']
    assert c['generatorLabel'] == 'Kohler G22 (S/N S11)'


def test_only_generator_is_suggested():
    [c] = match(request(phone='2075550142'), [customer(1, 'A', phone='2075550142')], [generator(10, 1)])['candidates']
    assert c['generatorId'] == 10


def test_ambiguous_generators_are_left_for_the_admin():
    generators = [generator(10, 1, make='Generac'), generator(11, 1, make='Generac')]
    [c] = match(request(phone='2075550142', brand='Not sure'), [customer(1, 'A', phone='2075550142')], generators)['candidates']
    assert c['generatorId'] is None and c['generatorLabel'] == ''


def test_other_and_not_sure_are_not_brands():
    generators = [generator(10, 1, make='Other'), generator(11, 1, make='Kohler')]
    [c] = match(request(phone='2075550142', brand='Other'), [customer(1, 'A', phone='2075550142')], generators)['candidates']
    assert c['generatorId'] is None


# ------------------------------------------- contact match, name and address differ --

def test_shared_phone_with_a_different_name_and_address_is_only_likely():
    # Nov 25 portal test: "Mike Jones" at 65 Oak St shared a phone with Elliott Gear (33 Beach St).
    customers = [customer(1, 'Elliott Gear', phone='207-475-5657', physical='33 Beach St\nSaco, ME')]
    result = match(request(name='Mike Jones', phone='2074755657', service_address='65 Oak St'), customers)
    [c] = result['candidates']  # still shown, not hidden
    assert c['tier'] == 'likely' and c['reasons'] == ['phone matches', 'name and address differ']
    assert result['conflict'] is False


def test_company_email_from_another_site_is_kept_as_likely():
    # Nov 25 test #9: a property manager writing from the company email about a different property.
    customers = [customer(1, 'Pinecrest Housing Partners', email='pinecrest.partners76@example.com',
                          physical='10 Commerce Dr\nPortland')]
    req = request(name='Dana Morin (Pinecrest property manager)', email='Pinecrest.Partners76@example.com',
                  service_address='400 Payne Rd')
    [c] = match(req, customers)['candidates']
    assert c['customerId'] == 1 and c['tier'] == 'likely'
    assert c['reasons'] == ['email matches', 'name and address differ']


@pytest.mark.parametrize('name, address, reasons', [
    ('Elliott Gear', '65 Oak St', ['phone matches', 'name matches']),           # name matches, address differs
    ('Mike Jones', '33 Beach Street', ['phone matches', 'address matches']),     # address matches, name differs
    ('Mike Jones', '', ['phone matches']),                                       # no address given: can't differ
])
def test_phone_with_a_matching_name_or_address_stays_strong(name, address, reasons):
    customers = [customer(1, 'Elliott Gear', phone='2074755657', physical='33 Beach St')]
    [c] = match(request(name=name, phone='2074755657', service_address=address), customers)['candidates']
    assert c['tier'] == 'strong' and c['reasons'] == reasons


def test_customer_without_an_address_on_file_stays_strong():
    [c] = match(request(name='Mike Jones', phone='2074755657', service_address='65 Oak St'),
                [customer(1, 'Elliott Gear', phone='2074755657')])['candidates']
    assert c['tier'] == 'strong'


def test_a_downgraded_contact_match_ranks_below_strong_but_above_likely():
    customers = [customer(1, 'Elliott Gear', phone='2074755657', physical='33 Beach St'),
                 customer(2, 'Mike Jones', email='mike@example.com', physical='1 Elm St'),
                 customer(3, 'Someone Else', physical='65 Oak St')]
    req = request(name='Mike Jones', phone='2074755657', email='mike@example.com', service_address='65 Oak St')
    result = match(req, customers)
    assert [(c['customerId'], c['tier']) for c in result['candidates']] == [(2, 'strong'), (1, 'likely'), (3, 'likely')]
    assert result['conflict'] is True  # phone and email still point at different customers


# ------------------------------------------------------------------- ZIP codes --

@pytest.mark.parametrize('address, expected', [
    ('33 Beach St\nSaco, ME 04072', '04072'),
    ('33 Beach St\nSaco, ME 04072-1234', '04072'),
    ('04072', '04072'),
    ('12345 Main St', ''),                 # a 5-digit house number isn't a ZIP
    ('12345 Main St\nSaco 04072', '04072'),
    ('12 Main St\nSaco', ''),
    (None, ''),
])
def test_zip_in(address, expected):
    assert matching.zip_in(address) == expected


def test_same_street_in_a_different_zip_is_not_an_address_match():
    customers = [customer(1, 'A', physical='12 Main St\nSaco, ME 04072')]
    assert match(request(service_address='12 Main St', service_zip='04101'), customers)['candidates'] == []


def test_same_street_and_zip_matches():
    customers = [customer(1, 'A', physical='12 Main St\nSaco, ME 04072')]
    [c] = match(request(service_address='12 Main St', service_zip='04072-1234'), customers)['candidates']
    assert c['reasons'] == ['address matches']


def test_a_customer_card_without_a_zip_still_matches_on_street():
    [c] = match(request(service_address='12 Main St', service_zip='04101'),
                [customer(1, 'A', physical='12 Main St\nSaco')])['candidates']
    assert c['reasons'] == ['address matches']


def test_a_generator_site_in_another_zip_doesnt_count():
    customers = [customer(1, 'Landlord', physical='1 Office Park')]
    generators = [generator(10, 1, address='77 Lake Rd\nWindham, ME 04062')]
    assert match(request(service_address='77 Lake Road', service_zip='04071'), customers, generators)['candidates'] == []


def test_zip_doesnt_affect_a_phone_match():
    customers = [customer(1, 'Pat Doe', phone='2075550142', physical='12 Main St\nSaco, ME 04072')]
    [c] = match(request(phone='2075550142', service_address='12 Main St', service_zip='04101'), customers)['candidates']
    assert c['tier'] == 'strong' and c['reasons'] == ['phone matches', 'name matches']
