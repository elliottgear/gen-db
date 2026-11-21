# Watt Watch — Customer & Generator Registry

A small internal web app for a standby-generator service business. It tracks
customers, the generators installed at their properties, service visits, and
whether each visit has been invoiced. Staff log in with pre-provisioned
accounts; there is no public signup.

## Stack

- **Backend:** `server.py` — plain Python 3 (`http.server` + `sqlite3`), no framework.
  Runtime packages are few and pinned in `requirements.txt` (see Dependencies).
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
| `core.py` | Shared helpers: API errors, field validators, Maine time, audit log (never imports `server`) |
| `ratelimit.py` | Client IP rule (`client_ip()`) and the `SlidingWindow` counter behind every rate limit |
| `appointment_requests.py` | The request portal: public submit, admin list/match/status, request statuses |
| `generator-manager.html` | Entire frontend (served at `/`) |
| `customer-request.html` / `.js` | Public service request page (served at `/customer`) |
| `customer-thanks.html` | Confirmation page after a request (`/customer/thanks`), no scripts |
| `portal.css` | Styles shared by the two public pages |
| `matching.py` | Fuzzy matcher: appointment requests → existing customers/generators |
| `turnstile.py` | Cloudflare Turnstile token check for the request page |
| `create_user.py` | CLI for logins: create, reset password, change role |
| `seed_batch.py` | Inserts 50 fake customers (1–5 generators each) for testing |
| `backup.py` | Snapshots the DB + uploads into a `.tar.gz`, keeps the newest 14 |
| `notifications.py` | Email: SMTP2GO client, message builders, digest/hit-list queries |
| `send_notifications.py` | Preview or hand-send the weekly digest / hit list |
| `tests/` | pytest suite (unit, integration, API, CLI); see Running tests |
| `requirements.txt` | Runtime dependencies, pinned (Railway installs it) |
| `requirements-dev.txt` | Test dependencies; includes `requirements.txt` |
| `Procfile` | Railway start command |
| `CHANGELOG.md` | Release history, newest first (see Versioning) |
| `todo.md` | Open issues and review findings |

## Running locally

```sh
make venv                      # .venv with requirements.txt + requirements-dev.txt
.venv/bin/python server.py     # http://localhost:8787/
python3 create_user.py alice   # technician; prompts for a password (min 8 chars)
python3 create_user.py bob --role admin
python3 create_user.py --set-role alice admin   # takes effect on her next page load
python3 create_user.py --reset alice   # new password + signs alice out everywhere
python3 create_user.py --list          # every user: role, email, created, signed-in sessions
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
| `TURNSTILE_SITE_KEY` | unset | Cloudflare Turnstile site key for `/customer`. Unset = the page says to call instead |
| `TURNSTILE_SECRET_KEY` | unset | Turnstile secret. Unset = every request submission is refused (503) |
| `TURNSTILE_HOSTNAME` | host of `APP_URL` | Hostname a token must come from; empty = don't check (test keys) |
| `TURNSTILE_VERIFY_URL` | Cloudflare | Override the siteverify endpoint (tests) |
| `OFFICE_PHONE` | unset | Office number shown on `/customer`, e.g. `207-363-7177` |
| `TRUST_PROXY` | `1` when `PORT` is set | Trust Railway's `X-Real-IP` (see Client IP) |

## Running tests

```sh
make test        # creates .venv with requirements-dev.txt on first run, then runs pytest
make coverage    # same, with a per-file coverage report
.venv/bin/python -m pytest tests/api/test_attachments.py -k range   # a subset
```

pytest and coverage are dev-only (`requirements-dev.txt`, which pulls in `requirements.txt`).
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
(`.github/workflows/tests.yml`) runs the suite on Python 3.12 and 3.15 for every push and pull request.

## Versioning

The version is `APP_VERSION` in `server.py`, shown in the page footer
("© 2026 Bridges Electric Inc. | v0.1.0"). The server fills it into the page's
`<meta name="app-version">` when it serves the page, so there is only one copy.

| Bump | When | Example |
| --- | --- | --- |
| Patch `x.x.1` | Bug fixes, and internal-only tooling (CLI scripts), for tracking | 0.1.0 → 0.1.1 |
| Minor `x.1.x` | New features | 0.1.3 → 0.2.0 |
| Major `1.x.x` | Major enhancements | 0.4.2 → 1.0.0 |

Docs, tests and CI changes don't bump it and can be pushed as-is. Each release gets an entry at the
top of `CHANGELOG.md` and a git tag `vX.Y.Z`.

## Dependencies

Keep them few. A runtime package is OK when it replaces hand-written security, parsing or
file-format code, or a vendor API client. It must be well known and maintained, install from
wheels on Linux/Python 3.12, and be pinned exactly in `requirements.txt`. Add it to this table.

| Package | Why |
| --- | --- |
| `tzdata` | IANA time zone data for `zoneinfo`, so "today in Maine" works on hosts with no OS tz database; replaces a hand-written DST fallback |

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
| View and handle appointment requests (`requests.manage`) | | ✓ |

**Exception to "viewing is open":** appointment requests hold the public's contact details, so
every `/api/appointment-requests` route needs `requests.manage`, the GET routes included.
Technicians get 403 from all of them, and the sidebar section is hidden with `can()`.

To restrict technicians further, remove permissions from `ROLES['technician']`. New accounts
default to technician. Roles are read on every request, so changes apply immediately. An unknown
role gets no write access.

## Customer request portal

`/customer` is a public page (no sign-in) where customers ask for service: name, phone, email,
street address and town, generator brand and what they need (Maintenance / Repair / Not running), plus
optional model and notes. It shows the office number for outages. After a successful submit the
browser goes to `/customer/thanks`, a separate "Request received" page saying the office will reach
out (it shows no request data, so opening it directly is harmless). No emails are sent.

- **The public side can only create.** `POST /api/public/appointment-requests` stores one request
  and always answers `{"ok": true}`. It never reads or returns existing data, including whether
  anything matched. The brand list is fixed (`REQUEST_BRANDS` in `server.py`) for the same reason;
  admins see a notice listing brands on file that the form doesn't offer.
- **Spam:** Cloudflare Turnstile, verified server-side (`turnstile.py`); it fails closed when the
  secret is missing or Cloudflare can't be reached. Then a rate limit of 5 attempts per hour per
  visitor and 100 stored requests per day overall, and a hidden honeypot field. Honeypot hits are
  kept as Dismissed ("spam filter (honeypot)") so an admin can restore a false positive.
- **The flow:** request comes in (**New**) → an admin links it to a customer (**Matched**) → the
  office books the visit and marks it **Scheduled** (booking happens outside the app for now) → the
  visit is logged as a service record on the generator as usual. **Dismissed** is for spam and
  duplicates.
- **Admins** see **Appointment requests** in the sidebar, one tab per status. Each new request shows
  the best match and why. An admin confirms it (optionally choosing the generator), matches it to
  another customer, creates a new customer from it (street and town become the address), or
  dismisses it. Nothing merges automatically, and every action is written to `audit_log`.
- **Matching** (`matching.py`, no AI): the same phone number (digits only) or email decides a match
  ("strong"); a similar street address (same house number, street spelled alike) is a likely
  match; a similar name only supports a match and never suggests one alone. Brand and address pick
  which of the customer's generators is meant. `match_request()` is pure, so a tie-breaker can be
  added later without changing how candidates are found.
- **The page has no inline scripts** (`customer-request.js` does everything), so its CSP allows
  scripts only from the app and `challenges.cloudflare.com`, and frames only from Cloudflare.

### Client IP (rate limits)

Login and portal rate limits count per visitor. Behind Railway the app only sees Railway's edge
proxy, which puts the address that connected to it in `X-Real-IP`; for `gen.bridgeselec.com` that's
a Cloudflare server, and Cloudflare puts the visitor in `CF-Connecting-IP`. So `ratelimit.client_ip()` uses
`X-Real-IP` (only when `TRUST_PROXY` is on), then `CF-Connecting-IP` **only if** that address is in
Cloudflare's published ranges. `X-Forwarded-For` is never used. The app is also reachable without
Cloudflare (the `*.up.railway.app` domain), where anyone can send a `CF-Connecting-IP` header, which
is why it's only trusted from Cloudflare addresses. IPv6 visitors are grouped by /64.

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

Every endpoint except login/logout/me and the public request submission requires a valid session cookie.

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
| POST | `/api/public/appointment-requests` | **No sign-in.** Public form + `turnstileToken` → always `{ok: true}` |
| GET | `/api/appointment-requests` | `{requests (new ones with suggestion), formBrandGaps}` — admins only |
| GET | `/api/appointment-requests/:id` | `{request, candidates, conflict}` — admins only |
| POST | `/api/appointment-requests/:id/match` | `{customerId, generatorId?, expectedUpdatedAt}` — admins only |
| POST | `/api/appointment-requests/:id/create-customer` | Customer fields + `expectedUpdatedAt` — admins only |
| POST | `/api/appointment-requests/:id/status` | `{status, reason?, expectedUpdatedAt}` — admins only |

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
- **Appointment requests** (admins only) — requests from `/customer` by status, each new one with
  its suggested match; open one to confirm, match elsewhere, create a customer, or dismiss.

## Deployment notes (Railway)

- Mount a volume and set `DB_PATH` to a path on it (uploads follow automatically).
- Create users with `railway run python3 create_user.py <name> [--role admin]`.
- List users from a shell in the container (`railway ssh`):
  `/app/.venv/bin/python create_user.py --list`.
- Python is pinned in `.python-version`. Railway's builder (Railpack) creates a venv at
  `/app/.venv` and runs `pip install -r requirements.txt` into it (only that file;
  `requirements-dev.txt` is ignored). The base interpreter is at
  `/mise/installs/python/<version>/bin/python3.12`, but it doesn't have the packages: for
  one-off commands in the container use `/app/.venv/bin/python`. If the server can't load its
  time zone at startup it exits with a message naming the interpreter it ran under.
- Email: set `SMTP2GO_API_KEY`, `EMAIL_FROM` (verified in SMTP2GO) and `APP_URL`, then each
  admin uses **Send test email** in Account settings.
- Backups: `railway run python3 backup.py`, or schedule it as a Railway cron
  service. The archives land on the same volume, so copy them off-site too.
- Request portal: in Cloudflare, add a Turnstile widget (Managed) for `gen.bridgeselec.com`, then
  set `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET_KEY` and `OFFICE_PHONE` on Railway **before** deploying.
  The token hostname is checked against `APP_URL`'s host. Locally, use Cloudflare's test keys
  (`1x00000000000000000000AA` / `1x0000000000000000000000000000000AA`) with `TURNSTILE_HOSTNAME=`.
  The server logs one line per stored request saying how the client IP was found (`cloudflare`,
  `proxy` or `socket`), never the IP itself; requests through `gen.bridgeselec.com` should say
  `cloudflare`.

See `todo.md` for known issues.
