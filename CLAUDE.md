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
  Internal-only tooling (CLI scripts like `create_user.py`) is an `x.x.1` bump, just for tracking.
  Docs/tests/CI-only changes don't bump and can be pushed as-is. Add each release to the top of
  `CHANGELOG.md` and tag the commit `vX.Y.Z`.
- The whole frontend is `generator-manager.html`; every interpolated value must go through `esc()`
  (and `fmtDate()`, which escapes its fallback).
- Schema changes go in a new `_migration_N` appended to `MIGRATIONS` in `server.py`; never edit an
  existing migration.
- Where code lives: `server.py` is HTTP, auth, customers/generators/service records, email
  scheduling and migrations. Shared helpers (errors, validators, Maine time, `audit`) are in
  `core.py`, which must never import `server`. Feature modules import from `core`, not `server`:
  `appointment_requests.py` (the request portal), `ratelimit.py`, `matching.py`, `turnstile.py`,
  `notifications.py`, `checklist.py` (service checklist and flags). `server.py` re-exports the `core` names.
- The service checklist definition is immutable: change it by adding a `checklist_versions` row (in a
  migration), never by editing one. Service record saves go through the outbox in the page
  (`sendEntry()`); keep `clientId` on every save so retries stay idempotent.
- "Today" means today in Maine: use `_today()` server-side and `today()` client-side, never UTC.
- Every new write route in `ROUTES` must name a permission from `PERMISSIONS` (and the UI should
  gate its control with `can()`). Exceptions, on purpose: `/api/appointment-requests` reads need
  `requests.manage` too (admins only), and `POST /api/public/appointment-requests` (handled in
  `PUBLIC_ROUTES`, not `ROUTES`) is the one write that needs no sign-in. It may only create a request
  and must never return existing data.
- Client IP for rate limits comes from `ratelimit.client_ip()` only; never read `X-Forwarded-For`.
  Count things with `ratelimit.SlidingWindow` rather than a hand-rolled dict.
- Appointment requests change status only through `appointment_requests._transition()` (it stamps,
  audits and commits); files served as-is or with placeholders go in `PAGES` in `server.py`.
- `customer-request.html` and `customer-thanks.html` must stay free of inline scripts and `on*=` handlers
  (their CSP forbids them), and take their styles from `portal.css`, not inline copies.
- New email kinds go in `NOTIFICATION_KINDS` (notifications.py); scheduled ones also in
  `SCHEDULED_EMAILS` (server.py). Tests use the `smtp2go` stub fixture and must never hit the real API.
- Validate request fields with `text_field` / `date_field` / `id_field` (in `core.py`).
- Run tests: `make test` (or `make coverage`). Tests must use the fixtures in `tests/conftest.py`
  (temp DB per test, `frozen_clock`, `seeded_random`); never point a test at the real DB.
- Known bugs are `xfail(strict=True)` tests; remove the marker when fixing one.
