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
| `notifications.py` | Email: SMTP2GO client, message builders, digest/hit-list queries |
| `send_notifications.py` | Preview or hand-send the weekly digest / hit list |
| `tests/` | pytest suite (unit, integration, API, CLI); see Running tests |
| `Procfile` | Railway start command |
| `todo.md` | Open issues and review findings |

## Running locally

```sh
python3 server.py              # http://localhost:8787/
python3 create_user.py alice   # technician; prompts for a password (min 8 chars)
python3 create_user.py bob --role admin
python3 create_user.py --set-role alice admin   # takes effect on her next page load
python3 create_user.py --reset alice   # new password + signs alice out everywhere
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
| `SMTP2GO_API_KEY` | unset | SMTP2GO API key with the `/email/send` permission. Unset = email off |
| `EMAIL_FROM` | unset | Verified SMTP2GO sender, e.g. `Watt Watch <alerts@yourdomain>` |
| `APP_URL` | unset | Public URL of the app, for links in emails |
| `SMTP2GO_API_URL` | global endpoint | Override for SMTP2GO's regional hosts |

## Running tests

```sh
make test        # creates .venv with requirements-dev.txt on first run, then runs pytest
make coverage    # same, with a per-file coverage report
.venv/bin/python -m pytest tests/api/test_attachments.py -k range   # a subset
```

The app has no runtime dependencies; pytest and coverage are dev-only (`requirements-dev.txt`).
Every test gets its own temporary database and uploads folder (`tests/conftest.py`), so the suite
never touches `gen_db.sqlite3` or `uploads/`. API tests run a real server on a random localhost port.
Time-dependent tests use the `frozen_clock` fixture and generated data uses `seeded_random`.

| Folder | What's in it |
| --- | --- |
| `tests/unit/` | Pure logic: validation, multipart parsing, file types, Range, time zone, roles |
| `tests/integration/` | Real SQLite: schema, constraints, migrations, sessions, demo data |
| `tests/api/` | HTTP endpoints: status codes, payloads, permissions, attachments, malformed requests |
| `tests/cli/` | `create_user.py`, `backup.py`, `seed_batch.py` |

Known bugs are pinned as `xfail(strict=True)` tests with the bug described in `reason`. When one is
fixed, its test starts passing, and pytest will flag it until the marker is removed. GitHub Actions
(`.github/workflows/tests.yml`) runs the suite on Python 3.9 and 3.12 for every push and pull request.

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
- The next service date must be after the service date (or the last service date when editing a
  generator). Editing a generator only checks this when its dates change.
- Editing a generator's service dates directly appends a `Service dates edited` entry.
- Make + serial must be unique (case-insensitive).
- Dates must be `YYYY-MM-DD`; text fields are length-limited. "Today" is always
  the date in Maine (US Eastern).
- Edits send back the record's `updatedAt`; if someone else saved in between,
  the server returns 409 and the page reloads the latest version.
- 10 failed logins from one IP within 15 minutes → locked out for 15 minutes.
- A connection that stalls for 30 seconds mid-request gets 408 and is closed.

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
| Save an email address on their own account (`account.edit`) | ✓ | ✓ |
| Receive email notifications (`notifications.receive`) | | ✓ |

To restrict technicians further, remove permissions from `ROLES['technician']`. New accounts
default to technician. Roles are read on every request, so changes apply immediately. An unknown
role gets no write access.

## Email notifications

Admins choose which emails they get under **Account settings** (sidebar). Anyone can save an
email address there; for technicians it's kept for future features like password reset.

| Email | When (Maine time) | Contents |
| --- | --- | --- |
| New service record | Right after a visit is logged | Customer, unit, site, dates, notes, who logged it |
| Weekly digest | Fridays 4:00pm | The 7 days up to send time: service visits (with invoiced status), generators added, transfers, service-date edits, deletions, invoicing, and the count not yet invoiced |
| Monday hit list | Mondays 7:00am | Overdue units, then units due in the next 30 days, by customer with phone and site. "Due" = next service date, else a year after the last service; never-serviced units are left out |

How it works:
- **SMTP2GO's HTTPS API, not SMTP**, because Railway blocks outbound SMTP.
- **Sending never blocks or fails a request.** New-record emails go through a background worker
  thread. Every attempt (sent / failed / skipped because email isn't configured) is recorded in
  the `email_log` table.
- **Scheduled emails run on a scheduler thread inside the web process.** The SQLite volume can
  only be mounted by one Railway service, so a separate cron service can't read it.
- **Each run is claimed in `scheduled_runs`**, so a digest goes out once per week even if two
  instances overlap during a deploy.
- **Catch-up:** if the app was down at send time, it sends within 12 hours of the scheduled time.
  After that, that week's run is skipped.
- **Recipients are checked at send time.** Someone must be an admin, have an email address, and
  have opted in.
- **Manual sends:**
  - `python3 send_notifications.py hit_list --preview` prints the email.
  - `--to you@example.com` sends it to one address.
  - `--subscribers` sends it to everyone opted in.

## API

Every endpoint except login/logout/me requires a valid session cookie.

| Method | Path | Description |
| --- | --- | --- |
| POST | `/api/login` | `{username, password}` → sets `session_token` cookie |
| POST | `/api/logout` | Deletes the current session |
| GET | `/api/me` | Current user `{username, role, permissions, email, notifications, notificationKinds}` (+ `emailConfigured` for admins), or 401 |
| PUT | `/api/account` | `{email, notifications?: [kind]}`: your own account; notifications need `notifications.receive` |
| POST | `/api/account/test-email` | Send a test email to your saved address (admins) |
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
- Python is pinned in `.python-version`. In the container it's at
  `/mise/installs/python/<version>/bin/python3.12` (there's no `python3` on PATH).
- Email: set `SMTP2GO_API_KEY`, `EMAIL_FROM` (verified in SMTP2GO) and `APP_URL`, then each
  admin uses **Send test email** in Account settings.
- Backups: `railway run python3 backup.py`, or schedule it as a Railway cron
  service. The archives land on the same volume, so copy them off-site too.

See `todo.md` for known issues.
