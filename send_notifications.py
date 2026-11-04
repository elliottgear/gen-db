#!/usr/bin/env python3
"""
Preview or send a scheduled email by hand. The server sends these on its own schedule
(weekly digest Fridays 4pm, hit list Mondays 7am, Maine time); this is for checking content
or re-sending.

Usage:  python3 send_notifications.py <weekly_digest|hit_list> --preview      print it, send nothing
        python3 send_notifications.py <weekly_digest|hit_list> --to ADDRESS  send to one address
        python3 send_notifications.py <weekly_digest|hit_list> --subscribers send to everyone opted in

The digest covers the 7 days up to now; the hit list is as of today.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

USAGE = __doc__[__doc__.index('Usage:'):__doc__.index('\n\nThe digest')].strip()


def fail(message):
    print(message)
    sys.exit(1)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or args[0] not in server.SCHEDULED_EMAILS:
        fail(USAGE)
    kind, mode, rest = args[0], args[1], args[2:]
    if mode == '--to':
        if len(rest) != 1 or not server.EMAIL_RE.match(rest[0]):
            fail('--to needs one email address.')
    elif mode not in ('--preview', '--subscribers') or rest:
        fail(USAGE)

    server.init_db()
    conn = server.get_conn()
    try:
        subject, text, html_body = server.build_scheduled_email(conn, kind, server._local_now())
        if mode == '--preview':
            print(f'Subject: {subject}\n\n{text}')
            return
        if mode == '--to':
            error = server.deliver(conn, kind, rest[0], subject, text, html_body)
            if error:
                fail(f'Not sent: {error}')
            print(f'Sent "{subject}" to {rest[0]}.')
            return
        recipients = server.notification_recipients(conn, kind)
        if not recipients:
            fail(f'Nobody is subscribed to {kind}.')
        failures = [r['email'] for r in recipients
                    if server.deliver(conn, kind, r['email'], subject, text, html_body)]
        print(f'Sent "{subject}" to {len(recipients) - len(failures)} of {len(recipients)} subscriber(s).')
        if failures:
            fail('Failed: ' + ', '.join(failures) + ' (see email_log).')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
