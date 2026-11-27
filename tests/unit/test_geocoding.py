"""geocoding.py: reading addresses as typed, the cache key, and the Census batch format."""
import pytest

import geocoding as geo


@pytest.mark.parametrize('text, expected', [
    ('793 Wolfe Neck Rd\nGray, ME 04039', ('793 Wolfe Neck Rd', 'Gray', 'ME', '04039')),
    ('12 Main St, Saco, ME 04072', ('12 Main St', 'Saco', 'ME', '04072')),
    ('12 Main St\nSaco', ('12 Main St', 'Saco', 'ME', '')),                    # no state: Maine
    ('12 Main St, Saco 04072-1234', ('12 Main St', 'Saco', 'ME', '04072')),    # ZIP+4
    ('14 Ocean Ave\nApt 3\nYork, Maine 03909', ('14 Ocean Ave', 'York', 'ME', '03909')),
    ('5 Elm St\nPortsmouth NH 03801', ('5 Elm St', 'Portsmouth', 'NH', '03801')),
    ('12 Main St\n04072', ('12 Main St', '', 'ME', '04072')),
    ('  12 Main St ,\n  Old Orchard Beach , ME  ', ('12 Main St', 'Old Orchard Beach', 'ME', '')),
])
def test_split_address(text, expected):
    assert geo.split_address(text) == expected


@pytest.mark.parametrize('text', ['', None, '12 Main St', 'PO Box 9\nSaco, ME 04072', 'P.O. Box 9, Saco', '12\nSaco'])
def test_too_incomplete_to_look_up(text):
    assert geo.split_address(text) is None


def test_address_key_ignores_case_and_spacing():
    assert geo.address_key('12 Main St\nSaco, ME') == geo.address_key('  12  main st  saco, me ') == '12 main st saco, me'
    assert geo.address_key('') == geo.address_key(None) == ''


def test_location_is_the_site_else_the_customer():
    assert geo.location_address('1 Site Rd\nSaco', '2 Home St\nSaco') == ('1 Site Rd\nSaco', 'site')
    assert geo.location_address('  ', '2 Home St\nSaco') == ('2 Home St\nSaco', 'customer')
    assert geo.location_address('', None) == ('', None)


def test_batch_csv_quotes_commas():
    assert geo.batch_csv([('1', ('12 Main St, Unit 2', 'Saco', 'ME', '04072'))]) == '1,"12 Main St, Unit 2",Saco,ME,04072\n'


def test_parse_batch_response():
    body = ('"1","793 Wolfe Neck Rd, Gray, ME, 04039","No_Match"\n'
            '"2","10 Harbor View Rd, Standish, ME, 04084","Match","Non_Exact","10 HARBOR VIEW WAY, STANDISH, ME, 04084",'
            '"-70.596143567504,43.829546724167","78067693","L"\n'
            '"3","x","Tie"\n'
            '"4","x","Match","Exact","X","not,numbers","1","L"\n'
            '"5","x","Match","Exact","X","-500,43","1","L"\n'
            '\n')
    assert geo.parse_batch_response(body) == {
        '1': None, '2': (43.829546724167, -70.596143567504, '10 HARBOR VIEW WAY, STANDISH, ME, 04084'),
        '3': None, '4': None, '5': None}
