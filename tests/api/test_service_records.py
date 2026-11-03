import pytest


def log(c, gen_id, **body):
    return c.json('POST', f'/api/generators/{gen_id}/service-records', body)


def test_logging_service_updates_the_schedule_and_history(tech, make_generator):
    gen = make_generator()
    status, gen = log(tech, gen['id'], serviceDate='2026-06-01', nextServiceDate='2027-06-01', notes='Oil + filter')
    assert status == 200
    assert (gen['lastServiceDate'], gen['nextServiceDate']) == ('2026-06-01', '2027-06-01')
    last = gen['history'][-1]
    assert (last['event'], last['date'], last['by'], last['invoiced']) == ('Serviced', '2026-06-01', 'tech', False)
    assert last['detail'] == 'Oil + filter Next service scheduled for 2027-06-01.'


def test_default_detail_when_no_notes(tech, make_generator):
    _, gen = log(tech, make_generator()['id'], serviceDate='2026-06-01')
    assert gen['history'][-1]['detail'] == 'Routine service.'
    assert gen['nextServiceDate'] == ''


@pytest.mark.parametrize('date', [None, '', '2026-02-30', 'tomorrow', '<img src=x onerror=alert(1)>'])
def test_service_date_must_be_a_real_date(tech, make_generator, date):
    gen = make_generator()
    status, body = log(tech, gen['id'], serviceDate=date)
    assert status == 400
    assert tech.json('GET', f"/api/generators/{gen['id']}")[1]['history'] == gen['history']  # nothing stored


def test_future_service_dates_are_allowed(tech, make_generator):
    assert log(tech, make_generator()['id'], serviceDate='2030-01-01')[0] == 200


def test_backdated_visit_is_recorded_without_rolling_the_schedule_back(tech, make_generator):
    gen = make_generator()
    log(tech, gen['id'], serviceDate='2026-06-01', nextServiceDate='2027-06-01')
    status, gen = log(tech, gen['id'], serviceDate='2025-06-01', nextServiceDate='')
    assert status == 200
    assert (gen['lastServiceDate'], gen['nextServiceDate']) == ('2026-06-01', '2027-06-01')
    assert [h['date'] for h in gen['history'] if h['event'] == 'Serviced'] == ['2026-06-01', '2025-06-01']


def test_same_day_visit_does_update_the_schedule(tech, make_generator):
    gen = make_generator()
    log(tech, gen['id'], serviceDate='2026-06-01', nextServiceDate='2027-06-01')
    _, gen = log(tech, gen['id'], serviceDate='2026-06-01', nextServiceDate='2026-12-01')
    assert gen['nextServiceDate'] == '2026-12-01'


@pytest.mark.xfail(strict=True, reason='BUG: the next service date must be after the service date, '
                                       'but the API accepts one on or before it.')
def test_next_service_on_or_before_the_service_date_is_refused(tech, make_generator):
    gen = make_generator()
    assert log(tech, gen['id'], serviceDate='2026-06-01', nextServiceDate='2026-05-01')[0] == 400


def test_missing_generator(tech):
    assert log(tech, 999999, serviceDate='2026-06-01')[0] == 404


class TestInvoicing:
    def url(self, hist_id):
        return f'/api/service-records/{hist_id}/invoice'

    def test_mark_and_undo(self, admin, serviced_generator, frozen_clock):
        _, hist_id = serviced_generator
        status, gen = admin.json('POST', self.url(hist_id), {'invoiced': True})
        rec = next(h for h in gen['history'] if h['id'] == hist_id)
        assert (status, rec['invoiced'], rec['invoicedBy'], rec['invoicedAt']) == (200, True, 'boss', '2026-07-04')
        _, gen = admin.json('POST', self.url(hist_id), {'invoiced': False})
        rec = next(h for h in gen['history'] if h['id'] == hist_id)
        assert (rec['invoiced'], rec['invoicedBy'], rec['invoicedAt']) == (False, '', '')

    def test_undo_is_audited_with_who_had_invoiced_it(self, admin, serviced_generator, db):
        _, hist_id = serviced_generator
        admin.json('POST', self.url(hist_id), {'invoiced': True})
        admin.json('POST', self.url(hist_id), {'invoiced': False})
        rows = db.execute("SELECT action, username, detail FROM audit_log WHERE entity='service_record' "
                          "AND entity_id=? ORDER BY id", (hist_id,)).fetchall()
        assert [(r['action'], r['username']) for r in rows] == [('invoice', 'boss'), ('uninvoice', 'boss')]
        assert 'Was invoiced by boss' in rows[1]['detail']

    def test_only_service_entries_can_be_invoiced(self, admin, make_generator):
        hist_id = make_generator()['history'][0]['id']  # 'Added to registry'
        assert admin.json('POST', self.url(hist_id), {'invoiced': True}) == \
            (400, {'error': 'Only service records can be invoiced.'})

    @pytest.mark.parametrize('value', [None, 1, 'true', 'yes'])
    def test_invoiced_must_be_a_boolean(self, admin, serviced_generator, value):
        _, hist_id = serviced_generator
        assert admin.json('POST', self.url(hist_id), {'invoiced': value})[0] == 400

    def test_missing_record(self, admin):
        assert admin.json('POST', self.url(999999), {'invoiced': True})[0] == 404
