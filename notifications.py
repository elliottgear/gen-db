"""
Email notifications: the SMTP2GO client, the message builders, and the queries behind them.

Standard library only and independent of server.py: callers pass in a database connection
and the dates to use, so everything here is testable with fixed data and a frozen clock.

Sending uses SMTP2GO's HTTPS API rather than SMTP, because Railway blocks outbound SMTP.
Configuration (environment variables):
  SMTP2GO_API_KEY   API key with the /email/send permission
  EMAIL_FROM        a sender verified in SMTP2GO, e.g. "Watt Watch <alerts@example.com>"
  APP_URL           the app's public URL, used for links in emails (optional)
  SMTP2GO_API_URL   override the endpoint (regional hosts, tests)
"""
import datetime
import html
import json
import os
import urllib.error
import urllib.request

SMTP2GO_DEFAULT_URL = 'https://api.smtp2go.com/v3/email/send'
SEND_TIMEOUT_SECONDS = 15
HIT_LIST_DAYS = 30

# kind -> label shown in Account settings. Add new kinds here.
NOTIFICATION_KINDS = {
    'service_record': 'New service record logged',
    'weekly_digest': 'Weekly digest (Fridays, 4pm)',
    'hit_list': 'Monday hit list: service due in the next 30 days (7am)',
}


class EmailError(Exception):
    pass


class EmailNotConfigured(EmailError):
    pass


# ----------------------------------------------------------------- sending --

def email_settings():
    return {
        'api_key': os.environ.get('SMTP2GO_API_KEY', '').strip(),
        'sender': os.environ.get('EMAIL_FROM', '').strip(),
        'url': os.environ.get('SMTP2GO_API_URL', '').strip() or SMTP2GO_DEFAULT_URL,
        'app_url': os.environ.get('APP_URL', '').strip().rstrip('/'),
    }


def email_configured():
    settings = email_settings()
    return bool(settings['api_key'] and settings['sender'])


def _smtp2go_error(body):
    try:
        data = json.loads(body or b'{}').get('data') or {}
    except (ValueError, AttributeError):
        return ''
    if data.get('error'):
        return str(data['error'])
    failures = data.get('failures') or []
    return '; '.join(str(f) for f in failures)


def send_email(to, subject, text, html_body):
    """Sends one email through SMTP2GO; returns its email_id. Raises EmailError on any failure."""
    settings = email_settings()
    if not (settings['api_key'] and settings['sender']):
        raise EmailNotConfigured('Email is not set up on the server (SMTP2GO_API_KEY and EMAIL_FROM).')
    payload = json.dumps({'sender': settings['sender'], 'to': [to], 'subject': subject,
                          'text_body': text, 'html_body': html_body}).encode('utf-8')
    request = urllib.request.Request(settings['url'], data=payload, method='POST', headers={
        'Content-Type': 'application/json', 'Accept': 'application/json', 'X-Smtp2go-Api-Key': settings['api_key'],
    })
    try:
        with urllib.request.urlopen(request, timeout=SEND_TIMEOUT_SECONDS) as response:
            body = response.read()
    except urllib.error.HTTPError as e:
        detail = _smtp2go_error(e.read())
        raise EmailError(f'SMTP2GO returned HTTP {e.code}' + (f': {detail}' if detail else ''))
    except (urllib.error.URLError, OSError) as e:  # DNS, refused, timeout
        raise EmailError(f'Could not reach SMTP2GO: {getattr(e, "reason", e)}')
    try:
        data = json.loads(body or b'{}').get('data') or {}
    except (ValueError, AttributeError):
        raise EmailError('SMTP2GO sent an unreadable response.')
    if not data.get('succeeded'):
        raise EmailError('SMTP2GO did not accept the email' + (f': {_smtp2go_error(body)}' if _smtp2go_error(body) else '.'))
    return data.get('email_id')


# ------------------------------------------------------------------ dates --

def fmt_date(iso):
    if not iso:
        return '—'
    try:
        d = datetime.date.fromisoformat(iso[:10])
    except ValueError:
        return iso
    return f'{d:%a} {d:%b} {d.day}, {d.year}'


def short_date(d):
    return f'{d:%b} {d.day}'


def add_one_year(d):
    try:
        return d.replace(year=d.year + 1)
    except ValueError:  # Feb 29 -> Mar 1, matching the frontend's Date.setFullYear
        return datetime.date(d.year + 1, 3, 1)


def service_due_date(last_service, next_service):
    """When a unit is due: its next service date, else a year after its last. None if never serviced.
    Same rule as serviceStatus() in the frontend."""
    if not last_service:
        return None
    if next_service:
        return datetime.date.fromisoformat(next_service)
    return add_one_year(datetime.date.fromisoformat(last_service))


# ---------------------------------------------------------------- queries --

def _unit(row):
    return f"{row['make']} {row['model']} (S/N {row['serial']})"


def weekly_activity(conn, start, end):
    """Everything logged in [start, end). start/end are timestamps in the same format as history created_at."""
    rows = conn.execute(
        '''SELECT h.*, g.make, g.model, g.serial, c.name AS customer
           FROM generator_history h JOIN generators g ON g.id = h.generator_id JOIN customers c ON c.id = g.customer_id
           WHERE h.created_at >= ? AND h.created_at < ? ORDER BY h.created_at, h.id''', (start, end)).fetchall()
    activity = {'serviced': [], 'added': [], 'transferred': [], 'dates_edited': [], 'deleted': [],
                'invoiced_count': 0}
    buckets = {'Serviced': 'serviced', 'Added to registry': 'added', 'Transferred': 'transferred',
               'Service dates edited': 'dates_edited'}
    for r in rows:
        bucket = buckets.get(r['event'])
        if bucket:
            activity[bucket].append({'customer': r['customer'], 'unit': _unit(r), 'date': r['date'] or '',
                                     'detail': r['detail'] or '', 'by': r['created_by'] or 'unknown',
                                     'invoiced': bool(r['invoiced'])})
    for a in conn.execute("SELECT * FROM audit_log WHERE at >= ? AND at < ? ORDER BY at, id", (start, end)):
        if a['action'] == 'delete':
            try:
                snapshot = json.loads(a['detail'] or '{}')
            except ValueError:
                snapshot = {}
            label = snapshot.get('name') or snapshot.get('filename') or (
                f"{snapshot.get('make', '')} {snapshot.get('model', '')} (S/N {snapshot.get('serial', '')})"
                if snapshot.get('serial') else f"#{a['entity_id']}")
            activity['deleted'].append({'entity': a['entity'], 'label': label, 'by': a['username'] or 'unknown'})
        elif a['action'] == 'invoice':
            activity['invoiced_count'] += 1
    activity['not_invoiced_total'] = conn.execute(
        "SELECT COUNT(*) FROM generator_history WHERE event='Serviced' AND invoiced=0").fetchone()[0]
    return activity


def hit_list(conn, today, days=HIT_LIST_DAYS):
    """(overdue, due_soon): lists of customers, each with the units that are due, soonest first."""
    horizon = today + datetime.timedelta(days=days)
    groups = {'overdue': {}, 'due_soon': {}}
    rows = conn.execute(
        '''SELECT g.*, c.name AS customer, c.phone AS phone FROM generators g JOIN customers c ON c.id = g.customer_id''')
    for r in rows:
        try:
            due = service_due_date(r['last_service_date'], r['next_service_date'])
        except ValueError:
            continue  # unparseable legacy date
        if due is None or due > horizon:
            continue
        section = 'overdue' if due < today else 'due_soon'
        group = groups[section].setdefault(r['customer_id'], {
            'customer': r['customer'], 'phone': r['phone'] or '', 'units': []})
        group['units'].append({'unit': _unit(r), 'site': (r['physical_address'] or '').replace('\n', ', '),
                               'due': due.isoformat(), 'last': r['last_service_date'] or '',
                               'days': (due - today).days})

    def ordered(section):
        out = list(groups[section].values())
        for g in out:
            g['units'].sort(key=lambda u: (u['due'], u['unit']))
        return sorted(out, key=lambda g: (g['units'][0]['due'], g['customer'].lower()))
    return ordered('overdue'), ordered('due_soon')


# --------------------------------------------------------------- messages --

def _layout(title, body_html, app_url):
    link = (f'<p style="margin:24px 0 0;font-size:12px;color:#5B6672;"><a href="{html.escape(app_url)}" '
            f'style="color:#B9741C;">Open Watt Watch</a> · Change which emails you get under Account settings.</p>'
            if app_url else '<p style="margin:24px 0 0;font-size:12px;color:#5B6672;">Change which emails you get '
                            'under Account settings in Watt Watch.</p>')
    return (f'<!DOCTYPE html><html><body style="margin:0;background:#EEF0F2;font-family:Arial,Helvetica,sans-serif;'
            f'color:#1B2430;"><div style="max-width:640px;margin:0 auto;padding:24px;">'
            f'<div style="background:#1E2C3B;color:#fff;padding:14px 18px;border-radius:8px 8px 0 0;font-size:16px;'
            f'font-weight:bold;">⚡ Watt Watch</div><div style="background:#fff;padding:20px 22px;border-radius:0 0 8px 8px;">'
            f'<h1 style="font-size:19px;margin:0 0 14px;">{html.escape(title)}</h1>{body_html}{link}</div></div>'
            f'</body></html>')


def _text_footer(app_url):
    return ('\n\n--\n' + (f'Open Watt Watch: {app_url}\n' if app_url else '') +
            'Change which emails you get under Account settings.')


def _table(headers, rows):
    head = ''.join(f'<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #D7DBE0;font-size:12px;'
                   f'color:#5B6672;">{html.escape(h)}</th>' for h in headers)
    body = ''.join('<tr>' + ''.join(f'<td style="padding:6px 8px;border-bottom:1px solid #EEF0F2;font-size:13px;'
                                    f'vertical-align:top;">{html.escape(str(c))}</td>' for c in r) + '</tr>' for r in rows)
    return f'<table style="width:100%;border-collapse:collapse;margin:0 0 18px;"><tr>{head}</tr>{body}</table>'


def _section(title):
    return f'<h2 style="font-size:15px;margin:18px 0 8px;">{html.escape(title)}</h2>'


def _empty(message):
    return f'<p style="font-size:13px;color:#5B6672;margin:0 0 18px;">{html.escape(message)}</p>'


def service_record_email(rec, app_url=''):
    """rec: customer, phone, site, unit, service_date, next_service_date, detail, logged_by."""
    subject = f"Service logged: {rec['customer']} — {rec['unit']}"
    fields = [('Customer', rec['customer']), ('Phone', rec.get('phone') or '—'), ('Unit', rec['unit']),
              ('Site', rec.get('site') or '—'), ('Service date', fmt_date(rec['service_date'])),
              ('Next service', fmt_date(rec.get('next_service_date'))), ('Notes', rec['detail']),
              ('Logged by', rec['logged_by'])]
    text = 'A service visit was logged in Watt Watch.\n\n' + '\n'.join(f'{k}: {v}' for k, v in fields)
    rows = ''.join(f'<tr><td style="padding:6px 10px 6px 0;color:#5B6672;font-size:13px;width:110px;vertical-align:top;">'
                   f'{html.escape(k)}</td><td style="padding:6px 0;font-size:13px;white-space:pre-wrap;">'
                   f'{html.escape(str(v))}</td></tr>' for k, v in fields)
    body = f'<table style="border-collapse:collapse;">{rows}</table>'
    return subject, text + _text_footer(app_url), _layout('Service visit logged', body, app_url)


def weekly_digest_email(activity, start, end, app_url=''):
    """start/end: dates bounding the week (end inclusive, for display)."""
    span = f'{short_date(start)} – {short_date(end)}'
    n = len(activity['serviced'])
    subject = f"Weekly digest: {n} service visit{'' if n == 1 else 's'} ({span})"
    summary = (f"{n} service visit{'' if n == 1 else 's'} logged · {len(activity['added'])} generator(s) added · "
               f"{len(activity['transferred'])} transfer(s) · {activity['invoiced_count']} marked invoiced · "
               f"{activity['not_invoiced_total']} service record(s) not yet invoiced")
    sections = [
        ('Service visits', ['Visit date', 'Customer', 'Unit', 'Logged by', 'Invoiced', 'Notes'],
         [(fmt_date(s['date']), s['customer'], s['unit'], s['by'], 'Yes' if s['invoiced'] else 'No', s['detail'])
          for s in activity['serviced']], 'No service visits were logged this week.'),
        ('Generators added', ['Customer', 'Unit', 'Added by'],
         [(a['customer'], a['unit'], a['by']) for a in activity['added']], None),
        ('Transfers', ['Unit', 'Detail', 'By'],
         [(t['unit'], t['detail'], t['by']) for t in activity['transferred']], None),
        ('Service dates edited', ['Customer', 'Unit', 'Change', 'By'],
         [(e['customer'], e['unit'], e['detail'], e['by']) for e in activity['dates_edited']], None),
        ('Deleted', ['Type', 'What', 'By'],
         [(d['entity'].replace('_', ' '), d['label'], d['by']) for d in activity['deleted']], None),
    ]
    html_parts = [f'<p style="font-size:13px;margin:0 0 8px;">{html.escape(summary)}</p>']
    text_parts = [f'Watt Watch weekly digest, {span}', '', summary]
    for title, headers, rows, empty in sections:
        if not rows and empty is None:
            continue
        html_parts.append(_section(title))
        html_parts.append(_table(headers, rows) if rows else _empty(empty))
        text_parts += ['', title.upper()]
        text_parts += [' | '.join(str(c) for c in r) for r in rows] if rows else [empty]
    return (subject, '\n'.join(text_parts) + _text_footer(app_url),
            _layout(f'Weekly digest · {span}', ''.join(html_parts), app_url))


def hit_list_email(overdue, due_soon, today, days=HIT_LIST_DAYS, app_url=''):
    count = lambda groups: sum(len(g['units']) for g in groups)  # noqa: E731
    subject = f'Hit list: {count(overdue)} overdue, {count(due_soon)} due in the next {days} days'
    html_parts = [f'<p style="font-size:13px;margin:0 0 8px;">Week of {html.escape(fmt_date(today.isoformat()))}.</p>']
    text_parts = [f'Watt Watch hit list, week of {fmt_date(today.isoformat())}']
    for title, groups, empty in (('Overdue', overdue, 'Nothing overdue.'),
                                 (f'Due in the next {days} days', due_soon, f'Nothing due in the next {days} days.')):
        html_parts.append(_section(f'{title} ({count(groups)})'))
        text_parts += ['', f'{title.upper()} ({count(groups)})']
        if not groups:
            html_parts.append(_empty(empty))
            text_parts.append(empty)
            continue
        rows = []
        for g in groups:
            for u in g['units']:
                when = f"{-u['days']} days overdue" if u['days'] < 0 else ('today' if u['days'] == 0 else f"in {u['days']} days")
                rows.append((g['customer'], g['phone'] or '—', u['unit'], u['site'] or '—', f"{fmt_date(u['due'])} ({when})"))
                text_parts.append(f"{g['customer']} | {g['phone'] or '—'} | {u['unit']} | {u['site'] or '—'} | "
                                  f"due {fmt_date(u['due'])} ({when})")
        html_parts.append(_table(['Customer', 'Phone', 'Unit', 'Site', 'Due'], rows))
    return subject, '\n'.join(text_parts) + _text_footer(app_url), _layout('Monday hit list', ''.join(html_parts), app_url)


def sample_email(username, app_url=''):
    subject = 'Watt Watch test email'
    text = f'This is a test email for {username}. If you can read it, email notifications are working.'
    return subject, text + _text_footer(app_url), _layout('Test email', f'<p style="font-size:13px;">{html.escape(text)}</p>', app_url)
