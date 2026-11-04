# CLAUDE.md

See `README.md` for architecture, API, and deployment, and `todo.md` for open issues.

- Standard library only — don't add pip dependencies to `server.py`.
- The whole frontend is `generator-manager.html`; every interpolated value must go through `esc()`
  (and `fmtDate()`, which escapes its fallback).
- Schema changes go in a new `_migration_N` appended to `MIGRATIONS` in `server.py`; never edit an
  existing migration.
- "Today" means today in Maine: use `_today()` server-side and `today()` client-side, never UTC.
- Every new write route in `ROUTES` must name a permission from `PERMISSIONS` (and the UI should
  gate its control with `can()`).
- New email kinds go in `NOTIFICATION_KINDS` (notifications.py); scheduled ones also in
  `SCHEDULED_EMAILS` (server.py). Tests use the `smtp2go` stub fixture and must never hit the real API.
- Validate request fields with `text_field` / `date_field` / `id_field`.
- Run tests: `make test` (or `make coverage`). Tests must use the fixtures in `tests/conftest.py`
  (temp DB per test, `frozen_clock`, `seeded_random`); never point a test at the real DB.
- Known bugs are `xfail(strict=True)` tests; remove the marker when fixing one.
