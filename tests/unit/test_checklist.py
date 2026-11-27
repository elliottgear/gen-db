"""checklist.py: answer validation and the "Needs follow-up" rules, without a database."""
import pytest

import checklist
import core
from core import ApiError
from tests.helpers import answers

V1 = {'id': 1, 'items': checklist.VERSION_1}


def check(**overrides):
    return checklist.validate_answers(V1, answers(**overrides))


def error(**overrides):
    with pytest.raises(ApiError) as e:
        check(**overrides)
    assert e.value.status == 400
    return e.value.message


def test_a_complete_good_checklist_has_no_flags():
    clean = check()
    assert set(clean) == {i['key'] for i in checklist.VERSION_1}
    assert not any(a['flagged'] for a in clean.values())
    assert checklist.evaluate(V1, clean) == (False, [])


def test_version_1_has_the_agreed_items_each_answerable_na():
    assert len(checklist.VERSION_1) == 16
    assert all(i['required'] for i in checklist.VERSION_1)
    assert len({i['key'] for i in checklist.VERSION_1}) == 16
    for item in checklist.VERSION_1:
        assert item['kind'] in checklist.KINDS and item['section'] in checklist.SECTIONS
        assert check(**{item['key']: {'na': True}})[item['key']] == {'value': None, 'na': True, 'note': '',
                                                                     'flagged': False}


# ------------------------------------------------------------- what is valid --

def test_every_item_must_be_answered():
    message = error(oil_leaks=None, frequency={'value': None})
    assert 'Missing: Frequency, Signs of oil leaks' in message


def test_unknown_items_are_refused():
    assert 'Unknown checklist item: bogus' in error(bogus={'value': 'yes'})


@pytest.mark.parametrize('entry, message', [
    ({'value': '120'}, 'must be a number'),
    ({'value': True}, 'must be a number'),
    ({'value': 701}, 'between 0 and 700'),
    ({'value': -1}, 'between 0 and 700'),
    ({'value': 120.123}, 'too many decimal places'),
    ({'value': 120, 'na': True}, "can't have a value too"),
    ({'value': 120, 'na': 'yes'}, 'true or false'),
    ({'value': 120, 'note': 'x' * 501}, 'too long'),
    ('120', 'must be an object'),
])
def test_bad_readings(entry, message):
    assert message in error(voltage_l1=entry)


@pytest.mark.parametrize('key, value', [('oil_changed', 'pass'), ('battery_terminals', 'yes'), ('oil_leaks', 'maybe')])
def test_choices_must_fit_the_kind(key, value):
    assert 'must be one of' in error(**{key: {'value': value}})


def test_answers_must_be_an_object():
    with pytest.raises(ApiError):
        checklist.validate_answers(V1, ['yes'])


def test_notes_are_trimmed_and_kept():
    assert check(oil_changed={'value': 'yes', 'note': '  5W-30  '})['oil_changed']['note'] == '5W-30'


# ------------------------------------------------------------------ flagging --

@pytest.mark.parametrize('key, value, flagged', [
    ('voltage_l1', 114, False), ('voltage_l1', 126, False),  # range ends are normal
    ('voltage_l1', 113.9, True), ('voltage_l1', 126.1, True),
    ('voltage_l2', 0, True), ('frequency', 58.9, True), ('frequency', 61, False),
    ('battery_voltage', 12.3, True), ('battery_voltage', 14.6, False),
    ('run_hours', 99999, False),  # no normal range
])
def test_readings_outside_their_normal_range(key, value, flagged):
    assert check(**{key: {'value': value}})[key]['flagged'] is flagged


@pytest.mark.parametrize('key, value, flagged', [
    ('oil_leaks', 'yes', True), ('oil_leaks', 'no', False), ('coolant_leaks', 'yes', True),
    ('battery_terminals', 'fail', True), ('transfer_switch', 'fail', True), ('exercise_schedule', 'fail', True),
    ('enclosure_clear', 'fail', True), ('enclosure_clear', 'pass', False),
    ('oil_changed', 'no', False), ('spark_plugs_changed', 'no', False),  # maintenance never flags
])
def test_choice_flags(key, value, flagged):
    assert check(**{key: {'value': value}})[key]['flagged'] is flagged


def test_na_never_flags():
    assert not check(oil_leaks={'na': True}, voltage_l1={'na': True})['oil_leaks']['flagged']


def test_fault_codes_need_a_note_saying_which():
    assert 'add a note' in error(fault_codes={'value': 'yes'})
    assert check(fault_codes={'value': 'yes', 'note': 'E-1501'})['fault_codes']['flagged']


def test_reasons_read_well():
    clean = check(battery_voltage={'value': 11.8}, oil_leaks={'value': 'yes', 'note': 'drip under the engine'},
                  transfer_switch={'value': 'fail'})
    flagged, reasons = checklist.evaluate(V1, clean)
    assert flagged and reasons == [
        'Battery voltage 11.8 V (normal 12.4–14.6 V)',
        'Signs of oil leaks: Yes (drip under the engine)',
        'Transfer switch test: Fail',
    ]


def test_rows_for_emails_follow_the_checklist_order():
    rows = checklist.rows(V1, check(voltage_l1={'value': 130}))
    assert rows[0] == ('Readings', 'Voltage out L1', '130 V', '', True)
    assert rows[3][2] == '412.5 h' and rows[11][2] == 'N/A'


# ------------------------------------------------------------- core helpers --

@pytest.mark.parametrize('value, ok', [
    ('6f1c7f4e-2b1a-4c1e-9a3e-1d2c3b4a5f60', True), ('6F1C7F4E-2B1A-4C1E-9A3E-1D2C3B4A5F60', True),
    ('not-a-uuid', False), ('6f1c7f4e2b1a4c1e9a3e1d2c3b4a5f60', False), (123, False),
])
def test_uuid_field(value, ok):
    if ok:
        assert core.uuid_field({'k': value}, 'k', 'Id') == value.lower()
    else:
        with pytest.raises(ApiError):
            core.uuid_field({'k': value}, 'k', 'Id')


def test_number_field_rejects_nan_and_infinity():
    for value in (float('nan'), float('inf')):
        with pytest.raises(ApiError):
            core.number_field({'k': value}, 'k', 'N', 0, 10)
    assert core.number_field({}, 'k', 'N', 0, 10) is None
