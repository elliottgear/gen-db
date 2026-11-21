# CLAUDE.md

See `README.md` for architecture, API, and deployment, and `todo.md` for open issues.

- Dependencies: keep them few. A runtime package is OK when it replaces hand-written
  security, parsing or file-format code, or a vendor API client. It must be well known and
  maintained, install from wheels on Linux/Python 3.12, be pinned exactly in
  requirements.txt, and be listed with a one-line reason in the README's Dependencies
  table. Don't move server.py onto a web framework without asking me.
- Versioning: `APP_VERSION` in `server.py` is the only copy (the footer shows it). Every change
  that ships to the running app bumps it in the same commit: `x.x.1` for bug fixes, `x.1.x` for
  new features (patch resets to 0), `1.x.x` for major enhancements (minor and patch reset).
  Docs/tests/CI-only changes don't bump. Add the release to the top of `CHANGELOG.md` and tag the
  commit `vX.Y.Z`.
- The whole frontend is `generator-manager.html`; every interpolated value must go through `esc()`
  (and `fmtDate()`, which escapes its fallback).
- Schema changes go in a new `_migration_N` appended to `MIGRATIONS` in `server.py`; never edit an
  existing migration.
- "Today" means today in Maine: use `_today()` server-side and `today()` client-side, never UTC.
- Every new write route in `ROUTES` must name a permission from `PERMISSIONS` (and the UI should
  gate its control with `can()`). Exceptions, on purpose: `/api/appointment-requests` reads need
  `requests.manage` too (admins only), and `POST /api/public/appointment-requests` (handled in
  `_dispatch`, not `ROUTES`) is the one write that needs no sign-in. It may only create a request
  and must never return existing data.
- Client IP for rate limits comes from `client_ip()` only; never read `X-Forwarded-For`.
- `customer-request.html` must stay free of inline scripts and `on*=` handlers (its CSP forbids them).
- New email kinds go in `NOTIFICATION_KINDS` (notifications.py); scheduled ones also in
  `SCHEDULED_EMAILS` (server.py). Tests use the `smtp2go` stub fixture and must never hit the real API.
- Validate request fields with `text_field` / `date_field` / `id_field`.
- Run tests: `make test` (or `make coverage`). Tests must use the fixtures in `tests/conftest.py`
  (temp DB per test, `frozen_clock`, `seeded_random`); never point a test at the real DB.
- Known bugs are `xfail(strict=True)` tests; remove the marker when fixing one.
