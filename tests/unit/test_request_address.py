"""The mailing-address line built from a request's town and ZIP (appointment_requests)."""
import pytest

import appointment_requests as ar


@pytest.mark.parametrize('zip_code, state', [
    ('03899', 'NH'), ('03000', 'NH'), ('03901', 'ME'), ('04072', 'ME'), ('04999', 'ME'),
    ('05001', ''), ('02108', ''), ('', ''), ('abcde', ''),
])
def test_state_for_zip(zip_code, state):
    assert ar.state_for_zip(zip_code) == state


@pytest.mark.parametrize('town, zip_code, line', [
    ('Saco', '04072', 'Saco, ME 04072'),
    ('Saco', '04072-1234', 'Saco, ME 04072-1234'),
    ('Dover', '03820', 'Dover, NH 03820'),
    ('Saco', '', 'Saco'),                    # requests from before the form asked for a ZIP
    ('Boston', '02108', 'Boston 02108'),     # outside the service area: no state guessed
    ('', '04072', 'ME 04072'),
    ('', '', ''),
])
def test_town_line(town, zip_code, line):
    assert ar.town_line(town, zip_code) == line
