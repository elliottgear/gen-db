import pytest

import server
from server import ApiError


def error_of(fn, *args):
    with pytest.raises(ApiError) as exc:
        fn(*args)
    return exc.value


class TestTextField:
    def test_missing_or_null_becomes_empty_string(self):
        assert server.text_field({}, 'email', 'Email') == ''
        assert server.text_field({'email': None}, 'email', 'Email') == ''

    def test_strips_surrounding_whitespace(self):
        assert server.text_field({'name': '  Marlene  \n'}, 'name', 'Name') == 'Marlene'

    @pytest.mark.parametrize('value', [5, 1.5, True, ['a'], {'a': 1}])
    def test_rejects_non_strings_with_the_field_label(self, value):
        err = error_of(server.text_field, {'phone': value}, 'phone', 'Phone')
        assert (err.status, err.message) == (400, 'Phone must be text.')

    def test_enforces_max_length_after_stripping(self):
        assert server.text_field({'n': ' ' + 'x' * 10 + ' '}, 'n', 'N', 10) == 'x' * 10
        err = error_of(server.text_field, {'n': 'x' * 11}, 'n', 'N', 10)
        assert err.status == 400 and 'max 10' in err.message

    def test_keeps_unicode_intact(self):
        assert server.text_field({'n': 'Gagné & Côté — 照片 🔌'}, 'n', 'N') == 'Gagné & Côté — 照片 🔌'


class TestDateField:
    def test_accepts_iso_dates_and_blank(self):
        assert server.date_field({'d': '2026-02-28'}, 'd', 'D') == '2026-02-28'
        assert server.date_field({'d': '2028-02-29'}, 'd', 'D') == '2028-02-29'
        assert server.date_field({'d': ''}, 'd', 'D') == ''
        assert server.date_field({}, 'd', 'D') == ''

    @pytest.mark.parametrize('value', [
        '2026-02-30', '2027-02-29', '2026-13-01', '26-01-01', '2026-1-1', '2026/01/01',
        '20260101', 'July 4, 2026', '<img src=x onerror=alert(1)>', '2026-01-01T00:00:00', '２０２６-01-01',
    ])
    def test_rejects_anything_but_a_real_yyyy_mm_dd_date(self, value):
        err = error_of(server.date_field, {'d': value}, 'd', 'Service date')
        assert (err.status, err.message) == (400, 'Service date must be a valid date.')

    def test_rejects_non_string(self):
        assert error_of(server.date_field, {'d': 20260101}, 'd', 'D').status == 400


class TestIdField:
    @pytest.mark.parametrize('value, expected', [(3, 3), ('3', 3), ('0042', 42)])
    def test_accepts_positive_ints_and_digit_strings(self, value, expected):
        assert server.id_field({'id': value}, 'id', 'bad') == expected

    @pytest.mark.parametrize('value', [None, 0, -1, '-1', 'abc', '3.0', 3.0, True, False, [], {}])
    def test_rejects_everything_else_with_the_given_message(self, value):
        err = error_of(server.id_field, {'id': value}, 'id', 'A customer is required.')
        assert (err.status, err.message) == (400, 'A customer is required.')


class TestCheckNotStale:
    row = {'updated_at': '2026-07-04T10:00:00.000000-04:00', 'updated_by': 'tech1'}

    def test_passes_when_client_did_not_send_a_version(self):
        server.check_not_stale(self.row, {}, 'generator')

    def test_passes_when_versions_match(self):
        server.check_not_stale(self.row, {'expectedUpdatedAt': self.row['updated_at']}, 'generator')

    def test_conflict_names_who_changed_it(self):
        err = error_of(server.check_not_stale, self.row, {'expectedUpdatedAt': 'older'}, 'generator')
        assert (err.status, err.code) == (409, 'stale')
        assert 'changed by tech1' in err.message

    def test_records_never_stamped_match_an_empty_version(self):
        server.check_not_stale({'updated_at': None, 'updated_by': None}, {'expectedUpdatedAt': ''}, 'customer')
        err = error_of(server.check_not_stale, {'updated_at': None, 'updated_by': None},
                       {'expectedUpdatedAt': 'x'}, 'customer')
        assert 'another user' in err.message


def test_require_raises_only_when_condition_is_falsy():
    server.require(1, 400, 'nope')
    err = error_of(server.require, '', 418, 'teapot')
    assert (err.status, err.message, err.code) == (418, 'teapot', None)
