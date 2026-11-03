# Watt Watch — Customer & Generator Registry

A small internal web app for a standby-generator service business. It tracks
customers, the generators installed at their properties, service visits, and
whether each visit has been invoiced. Staff log in with pre-provisioned
accounts; there is no public signup.

## Stack

- **Backend:** `server.py` — plain Python 3 standard library (`http.server` +
  `sqlite3`). No third-party dependencies, no framework.
- **Frontend:** `generator-manager.html` — one self-contained page with inline
  CSS and vanilla JS. It renders by rebuilding `innerHTML` from a single
  `state` object and talks to the backend over JSON (`/api/*`).
- **Database:** SQLite file (`gen_db.sqlite3` by default). Schema is created
  and migrated automatically at startup (`init_db` / `_migrate`).
- **File storage:** uploaded attachments are written to `uploads/` with random
  UUID filenames; metadata lives in the `generator_attachments` table.
- **Hosting:** Railway (`Procfile`: `web: python3 server.py`).

## Repository layout

| File | Purpose |
| --- | --- |
| `server.py` | HTTP server, REST API, auth, schema/migrations, demo seed data |
| `generator-manager.html` | Entire frontend (served at `/`) |
| `create_user.py` | CLI for logins: create, reset password, change role |
| `seed_batch.py` | Inserts 50 fake customers (1–5 generators each) for testing |
| `backup.py` | Snapshots the DB + uploads into a `.tar.gz`, keeps the newest 14 |
| `tests/` | End-to-end API tests: `python3 -m unittest discover tests` |
| `Procfile` | Railway start command |
| `todo.md` | Open issues and review findings |

## Running locally

```sh
python3 server.py              # http://localhost:8787/
python3 create_user.py alice   # technician; prompts for a password (min 8 chars)
python3 create_user.py bob --role admin
python3 create_user.py --set-role alice admin   # takes effect on her next page load
python3 create_user.py --reset alice   # new password + signs alice out everywhere
python3 -m unittest discover tests
```

Locally the server listens on `127.0.0.1` only; set `HOST=0.0.0.0` to test from a phone on the LAN.

On first start with an empty `customers` table, the server seeds three demo
customers and generators (see `seed()` in `server.py`).

### Environment variables

| Variable | Default | Notes |
| --- | --- | --- |
| `PORT` | `8787` | Railway sets this automatically |
| `DB_PATH` | `./gen_db.sqlite3` | Point at a mounted volume in production |
| `UPLOADS_DIR` | `uploads/` next to `DB_PATH` | Defaults onto the same volume as the database |
| `HOST` | `127.0.0.1`, or `0.0.0.0` when `PORT` is set | Bind address |
| `BACKUP_DIR` / `BACKUP_KEEP` | `backups/` next to `DB_PATH` / `14` | Used by `backup.py` |

## Data model

- **customers** — name (required), email, phone, physical/billing address,
  notes, `created_by` / `updated_by`.
- **generators** — belongs to one customer. Make, model, serial (required),
  site address, install / last service / next service dates, installer, notes.
- **generator_history** — event log per generator (`Added to registry`,
  `Installed`, `Serviced`, `Transferred`). `Serviced` rows carry the invoicing
  fields `invoiced`, `invoiced_by`, `invoiced_at`.
- **generator_attachments** — photos, videos and PDFs attached to a generator.
- **users** / **sessions** — role (`admin` / `technician`), PBKDF2-SHA256 password hashes; opaque session
  tokens stored server-side and sent as an `HttpOnly` cookie (`Secure` behind
  HTTPS). Sessions expire after 30 days.
- **audit_log** — who deleted customers/generators/attachments (with a JSON
  snapshot of what was deleted) and who invoiced / un-invoiced service records.
  No UI yet; query it with `sqlite3`.

Schema changes are numbered migrations (`MIGRATIONS` in `server.py`, tracked
with `PRAGMA user_version`). The DB runs in WAL mode.

Business rules enforced by the backend:

- A generator must be linked to an existing customer.
- A customer with generators cannot be deleted (transfer or remove them first).
- Transferring a generator keeps its full history and appends a
  `Transferred` entry.
- Logging a service appends a `Serviced` entry and, unless it's older than the
  current last service (a backdated catch-up entry), sets the last/next service
  dates. Only `Serviced` entries can be marked invoiced.
- Editing a generator's service dates directly appends a `Service dates edited` entry.
- Make + serial must be unique (case-insensitive).
- Dates must be `YYYY-MM-DD`; text fields are length-limited. "Today" is always
  the date in Maine (US Eastern).
- Edits send back the record's `updatedAt`; if someone else saved in between,
  the server returns 409 and the page reloads the latest version.
- 10 failed logins from one IP within 15 minutes → locked out for 15 minutes.

Service status (computed in the frontend, `serviceStatus()`): overdue if past
the next service date (or last service + 1 year when no next date is set),
"due soon" within 60 days, otherwise up to date.

## Roles

Two roles, defined as permission sets in `ROLES` in `server.py`. Every write route in `ROUTES`
names the permission it needs, and the server returns 403 without it. Viewing is open to any
signed-in user. The page hides controls the user lacks (`can()` in the frontend), but the
server is what enforces it.

| Permission | Technician | Admin |
| --- | --- | --- |
| Add/edit/delete customers, add/edit/remove/transfer generators | ✓ | ✓ |
| Log service records, upload/delete files | ✓ | ✓ |
| Mark / un-mark service records as invoiced (`service.invoice`) | | ✓ |

To restrict technicians further, remove permissions from `ROLES['technician']`. New accounts
default to technician. Roles are read on every request, so changes apply immediately. An unknown
role gets no write access.

## API

Every endpoint except login/logout/me requires a valid session cookie.

| Method | Path | Description |
| --- | --- | --- |
| POST | `/api/login` | `{username, password}` → sets `session_token` cookie |
| POST | `/api/logout` | Deletes the current session |
| GET | `/api/me` | Current user `{username, role, permissions}`, or 401 |
| GET/POST | `/api/customers` | List / create |
| GET/PUT/DELETE | `/api/customers/:id` | Read / update / delete |
| GET/POST | `/api/generators` | List (with history + attachments) / create |
| GET/PUT/DELETE | `/api/generators/:id` | Read / update / delete |
| POST | `/api/generators/:id/transfer` | `{newCustomerId, note}` |
| POST | `/api/generators/:id/service-records` | `{serviceDate, nextServiceDate, notes}` |
| POST | `/api/service-records/:id/invoice` | `{invoiced: bool}` — admins only |
| POST | `/api/generators/:id/attachments` | multipart upload, field `file`, max 100 MB |
| GET | `/api/attachments/:id/file` | Download / inline view |
| DELETE | `/api/attachments/:id` | Delete attachment and its file |

Allowed uploads: JPG, PNG, GIF, WEBP, HEIC, PDF, MP4, MOV, WEBM. The extension
and the file's magic bytes must agree (the browser's declared type is ignored).
Uploads are streamed to disk, not held in memory. Downloads support HTTP Range
requests (needed for video on iOS Safari) and are cached by the browser.

## Frontend views

- **Customers** — searchable list → customer detail (contact, addresses,
  generator cards with Log service / Edit / Transfer / Upload / Remove).
- **Generators** — every unit across customers, with search plus brand and
  status filters. Clicking a row opens a detail modal with attachments and
  history.
- **Service records** — `Serviced` entries split into "Needs review" (not yet
  invoiced) and "Invoiced".

## Deployment notes (Railway)

- Mount a volume and set `DB_PATH` to a path on it (uploads follow automatically).
- Create users with `railway run python3 create_user.py <name> [--role admin]`.
- Python is pinned in `.python-version`.
- Backups: `railway run python3 backup.py`, or schedule it as a Railway cron
  service. The archives land on the same volume, so copy them off-site too.

See `todo.md` for known issues.
