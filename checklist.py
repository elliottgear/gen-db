"""
The service checklist: its definition (immutable versions in `checklist_versions`), answer validation,
and the "Needs follow-up" flags.

A saved service record points at the exact version it was answered against and is always judged by
that version's labels and ranges, so changing the list later never changes old records. Imports only
`core` (never `server`).
"""
import json

from core import ApiError, bool_field, choice_field, number_field, require, text_field

NOTE_MAX = 500
KINDS = ('number', 'yesno', 'pfna')
VALUES = {'yesno': ('yes', 'no'), 'pfna': ('pass', 'fail')}
VALUE_LABELS = {'yes': 'Yes', 'no': 'No', 'pass': 'Pass', 'fail': 'Fail'}
SECTIONS = ('Readings', 'Maintenance', 'Inspection')


def _number(key, label, unit, valid, normal=(None, None)):
    return {'key': key, 'label': label, 'section': 'Readings', 'kind': 'number', 'unit': unit,
            'validMin': valid[0], 'validMax': valid[1], 'normalMin': normal[0], 'normalMax': normal[1],
            'flagOn': None, 'noteRequiredWhenFlagged': False, 'required': True}


def _choice(key, label, section, kind, flag_on=None, note_required=False):
    return {'key': key, 'label': label, 'section': section, 'kind': kind, 'unit': '',
            'flagOn': flag_on, 'noteRequiredWhenFlagged': note_required, 'required': True}


# Version 1, seeded by migration 12. The ranges are provisional until the team confirms them
# (todo.md); change them with a new version row, never by editing this one.
VERSION_1 = [
    _number('voltage_l1', 'Voltage out L1', 'V', (0, 700), (114, 126)),
    _number('voltage_l2', 'Voltage out L2', 'V', (0, 700), (114, 126)),
    _number('frequency', 'Frequency', 'Hz', (0, 100), (59, 61)),
    _number('run_hours', 'Engine run hours', 'h', (0, 100000)),
    _number('battery_voltage', 'Battery voltage', 'V', (0, 30), (12.4, 14.6)),
    _choice('oil_changed', 'Oil changed', 'Maintenance', 'yesno'),
    _choice('oil_filter_changed', 'Oil filter changed', 'Maintenance', 'yesno'),
    _choice('air_filter_changed', 'Air filter changed', 'Maintenance', 'yesno'),
    _choice('spark_plugs_changed', 'Spark plugs changed', 'Maintenance', 'yesno'),
    _choice('battery_terminals', 'Battery terminals clean and tight', 'Inspection', 'pfna', 'fail'),
    _choice('oil_leaks', 'Signs of oil leaks', 'Inspection', 'yesno', 'yes'),
    _choice('coolant_leaks', 'Signs of coolant leaks', 'Inspection', 'yesno', 'yes'),
    _choice('transfer_switch', 'Transfer switch test', 'Inspection', 'pfna', 'fail'),
    _choice('exercise_schedule', 'Exercise schedule set correctly', 'Inspection', 'pfna', 'fail'),
    _choice('enclosure_clear', 'Enclosure clear of debris/pests/snow', 'Inspection', 'pfna', 'fail'),
    _choice('fault_codes', 'Controller fault codes present', 'Inspection', 'yesno', 'yes', note_required=True),
]


# ------------------------------------------------------------------ versions --

def _version_json(row):
    return {'id': row['id'], 'items': json.loads(row['items']), 'createdAt': row['created_at'] or '',
            'createdBy': row['created_by'] or '', 'note': row['note'] or ''}


def list_versions(conn):
    return [_version_json(r) for r in conn.execute('SELECT * FROM checklist_versions ORDER BY id')]


def get_version(conn, version_id):
    row = conn.execute('SELECT * FROM checklist_versions WHERE id=?', (version_id,)).fetchone()
    return _version_json(row) if row else None


def current_version(conn):
    row = conn.execute('SELECT * FROM checklist_versions ORDER BY id DESC LIMIT 1').fetchone()
    return _version_json(row) if row else None


# ---------------------------------------------------------------- answers --

def fmt_number(value):
    return f'{value:g}' if value is not None else ''


def answer_text(item, answer):
    """'118.5 V', 'Yes', 'Fail', 'N/A' (the answer alone, without the note)."""
    if not answer:
        return '—'
    if answer.get('na'):
        return 'N/A'
    if item['kind'] == 'number':
        return f"{fmt_number(answer.get('value'))} {item.get('unit') or ''}".strip()
    return VALUE_LABELS.get(answer.get('value'), '—')


def _range_text(item):
    lo, hi = item.get('normalMin'), item.get('normalMax')
    unit = f" {item['unit']}" if item.get('unit') else ''
    if lo is not None and hi is not None:
        return f'{fmt_number(lo)}–{fmt_number(hi)}{unit}'
    if lo is not None:
        return f'at least {fmt_number(lo)}{unit}'
    if hi is not None:
        return f'at most {fmt_number(hi)}{unit}'
    return ''


def is_flagged(item, answer):
    if not answer or answer.get('na'):
        return False
    value = answer.get('value')
    if item['kind'] == 'number':
        lo, hi = item.get('normalMin'), item.get('normalMax')
        return value is not None and ((lo is not None and value < lo) or (hi is not None and value > hi))
    return item.get('flagOn') is not None and value == item['flagOn']


def flag_reason(item, answer):
    if item['kind'] == 'number':
        return f"{item['label']} {answer_text(item, answer)} (normal {_range_text(item)})"
    text = f"{item['label']}: {answer_text(item, answer)}"
    return text + (f" ({answer['note']})" if answer.get('note') else '')


def validate_answers(version, raw):
    """Checks a submitted {itemKey: {value, na, note}} against a checklist version. Every item of the
    version must be answered (N/A counts), unknown keys are refused. Returns the clean answers, each
    with its computed `flagged`."""
    require(isinstance(raw, dict), 400, 'Checklist answers must be an object.')
    items = {i['key']: i for i in version['items']}
    unknown = [k for k in raw if k not in items]
    require(not unknown, 400, f'Unknown checklist item: {", ".join(sorted(unknown))[:200]}.')
    clean, missing = {}, []
    for key, item in items.items():
        entry = raw.get(key)
        if entry is None:
            if item.get('required'):
                missing.append(item['label'])
            continue
        require(isinstance(entry, dict), 400, f"Checklist answer for {item['label']} must be an object.")
        label = item['label']
        na = bool_field(entry, 'na', f'{label} N/A')
        note = text_field(entry, 'note', f'{label} note', NOTE_MAX)
        if na:
            require(entry.get('value') in (None, ''), 400, f'{label} is marked N/A, so it can\'t have a value too.')
            value = None
        elif item['kind'] == 'number':
            value = number_field(entry, 'value', label, item['validMin'], item['validMax'])
        else:
            value = choice_field(entry, 'value', label, VALUES[item['kind']]) or None
        if not na and value is None:
            if item.get('required'):
                missing.append(label)
            continue
        answer = {'value': value, 'na': na, 'note': note}
        answer['flagged'] = is_flagged(item, answer)
        if answer['flagged'] and item.get('noteRequiredWhenFlagged'):
            require(note, 400, f'{label}: add a note (for example, the codes shown).')
        clean[key] = answer
    if missing:
        raise ApiError(400, f'Answer every checklist item (N/A is fine). Missing: {", ".join(missing)}.')
    return clean


def evaluate(version, answers):
    """(needs_follow_up, reasons) for a record's answers, judged by its own version."""
    reasons = [flag_reason(i, answers[i['key']]) for i in version['items']
               if is_flagged(i, answers.get(i['key']))]
    return bool(reasons), reasons


def rows(version, answers):
    """[(section, label, answer text, note, flagged)] in checklist order, for emails."""
    return [(i['section'], i['label'], answer_text(i, answers.get(i['key'])),
             (answers.get(i['key']) or {}).get('note') or '', is_flagged(i, answers.get(i['key'])))
            for i in version['items']]
