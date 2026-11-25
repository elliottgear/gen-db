"""
Cloudflare Turnstile: server-side check of the token the customer request page sends.

Configuration (environment variables):
  TURNSTILE_SITE_KEY     public key the page uses to show the widget
  TURNSTILE_SECRET_KEY   secret used here to verify tokens. Unset = the portal refuses submissions
  TURNSTILE_HOSTNAME     hostname a token must have been issued on (default: the host of APP_URL;
                         empty = don't check, e.g. with Cloudflare's test keys)
  TURNSTILE_VERIFY_URL   override the siteverify endpoint (tests)
  TURNSTILE_DISABLED     1/true/yes/on = switch the check off, for testing: the server skips it AND
                         the page doesn't load the widget, so nobody waits on a check that isn't
                         running. Rate limits and the honeypot still apply. Logged at startup.

Fails closed while switched on: if the secret is missing or Cloudflare can't be reached, nothing
is accepted.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

VERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'
VERIFY_TIMEOUT_SECONDS = 10
# Our misconfiguration or Cloudflare's fault, not the visitor's: report "unavailable", not "rejected".
SERVER_SIDE_ERRORS = {'missing-input-secret', 'invalid-input-secret', 'internal-error'}


class TurnstileUnavailable(Exception):
    """Can't verify right now (not configured, or Cloudflare unreachable). Refuse, but it's not the user's fault."""


class TurnstileRejected(Exception):
    """Cloudflare says the token isn't valid (bot, expired, reused, or wrong site)."""


def disabled():
    return os.environ.get('TURNSTILE_DISABLED', '').strip().lower() in {'1', 'true', 'yes', 'on'}


def settings():
    hostname = os.environ.get('TURNSTILE_HOSTNAME')
    if hostname is None:
        hostname = urllib.parse.urlsplit(os.environ.get('APP_URL', '').strip()).hostname or ''
    return {
        'site_key': os.environ.get('TURNSTILE_SITE_KEY', '').strip(),
        'secret_key': os.environ.get('TURNSTILE_SECRET_KEY', '').strip(),
        'hostname': hostname.strip().lower(),
        'url': os.environ.get('TURNSTILE_VERIFY_URL', '').strip() or VERIFY_URL,
        'disabled': disabled(),
    }


def verify(token, remote_ip=None):
    """Returns None if Cloudflare accepts `token` (or the check is switched off);
    raises TurnstileRejected or TurnstileUnavailable."""
    config = settings()
    if config['disabled']:
        return
    if not config['secret_key']:
        raise TurnstileUnavailable('TURNSTILE_SECRET_KEY is not set.')
    if not isinstance(token, str) or not token.strip() or len(token) > 4096:
        raise TurnstileRejected('missing-input-response')
    fields = {'secret': config['secret_key'], 'response': token.strip(), 'idempotency_key': str(uuid.uuid4())}
    if remote_ip:
        fields['remoteip'] = remote_ip
    request = urllib.request.Request(config['url'], data=urllib.parse.urlencode(fields).encode(), method='POST',
                                     headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(request, timeout=VERIFY_TIMEOUT_SECONDS) as response:
            body = response.read()
    except urllib.error.HTTPError as e:
        raise TurnstileUnavailable(f'siteverify returned HTTP {e.code}')
    except (urllib.error.URLError, OSError) as e:  # DNS, refused, timeout
        raise TurnstileUnavailable(f'Could not reach siteverify: {getattr(e, "reason", e)}')
    try:
        result = json.loads(body or b'{}')
    except ValueError:
        raise TurnstileUnavailable('siteverify sent an unreadable response.')
    if not isinstance(result, dict) or result.get('success') is not True:
        codes = (result.get('error-codes') if isinstance(result, dict) else None) or []
        detail = ', '.join(str(c) for c in codes) or 'rejected'
        if set(codes) & SERVER_SIDE_ERRORS:
            raise TurnstileUnavailable(detail)
        raise TurnstileRejected(detail)
    if config['hostname'] and (result.get('hostname') or '').lower() != config['hostname']:
        raise TurnstileRejected(f"token was issued for {result.get('hostname')!r}")
