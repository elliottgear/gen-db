# CLAUDE.md

See `README.md` for architecture, API, and deployment, and `todo.md` for open issues.

- Standard library only — don't add pip dependencies to `server.py`.
- The whole frontend is `generator-manager.html`; every interpolated value must go through `esc()`
  (and `fmtDate()`, which escapes its fallback).
- Schema changes go in a new `_migration_N` appended to `MIGRATIONS` in `server.py`; never edit an
  existing migration.
- "Today" means today in Maine: use `_today()` server-side and `today()` client-side, never UTC.
- Validate request fields with `text_field` / `date_field` / `id_field`.
- Run tests: `python3 -m unittest discover tests`.
