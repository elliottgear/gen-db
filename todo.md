# TODO

Severity: **P0** fix now · **P1** fix before relying on the feature · **P2** should fix · **P3** nice to have.

## Open

### Needs you (can't be done from code)
- [ ] **Clear Cloudflare's "Siteverify isn't being called" notice.** Prod has probably never
  reached siteverify (every test so far stopped at validation or the old rate limit). Send one real
  request on gen.bridgeselec.com/customer, then dismiss it. If it says "couldn't confirm you're not
  a robot", the Railway secret belongs to another widget: compare keys with the "gen.bridgeselec.com
  (Spin)" widget. The 176 challenges/day were likely one Firefox tab left open (the check renews
  every ~5 min). Update 2026-11-25: 12 test requests were stored on prod, so siteverify has now
  been called and accepted tokens. Check whether the notice is gone. If prod's
  `TURNSTILE_SECRET_KEY` was swapped for Cloudflare's test secret during that test, put the real
  one back and use `TURNSTILE_DISABLED=1` for testing instead.
- [ ] **Check the v0.2.5 form in Safari:** dropdowns should match the text boxes' height (only
  previewed in Chrome), and the tab icon should have no white square.
- [ ] **P0 — Reset the `tech1` and `tech2` passwords** locally: `python3 create_user.py --reset tech1`.
  Their hashes were in public git history. Prod doesn't have them (checked 2026-11-20: prod users are
  `betty` admin and `calvin` technician). Also change any other account that reuses those passwords.
- [ ] **P0 — Optional: remove the DB from git history.** The repo is private now, but any existing
  clone/fork still has it. `git filter-repo --invert-paths --path gen_db.sqlite3 --path gen_db.sqlite3.bak-20260718175615`
  then force-push. This rewrites every commit hash.
- [ ] **Before launch: settle `BRANDS` (appointment_requests.py) with the team.** Kept as-is for now (2026-11-21). The
  admin Appointment requests page lists brands on file that the form doesn't offer (locally: Deere).
- [ ] **Off-site backups.** `backup.py` writes to the volume; pick a destination (S3/B2/Drive) and
  schedule it as a Railway cron job.

### Bugs found by the test suite — fixed 2026-11-03
- [x] Next service date must be after the service date (logging service; creating/editing a
  generator, checked on edit only when the dates change, so older bad records stay editable).
- [x] `seed_batch.py` "overdue" units now have a next-service date after their last service.
- [x] 30-second socket timeout; stalled requests get 408 and leave no partial upload.
- [x] Backups taken in the same second no longer overwrite each other.
- [ ] Existing data (local and prod) may still contain generators with next ≤ last service from older
  `seed_batch` runs: `SELECT id FROM generators WHERE next_service_date != '' AND next_service_date <= last_service_date`.

### Email follow-ups
- [ ] Password reset by email (uses `users.email`). Verify email addresses before relying on them for that.
- [ ] Unsubscribe link in emails (currently: change choices in Account settings).
- [ ] `email_log` and `scheduled_runs` grow forever; prune old rows.
- [ ] Admin view of `email_log` (today: query it with `sqlite3`).

### Before go-live: data cleanup
- [ ] Demo data still seeds into an empty DB (`seed()` in `init_db`). Kept for now while building;
  gate behind an env flag (e.g. `SEED_DEMO=1`) before real use.
- [ ] Remove the generator with make **"TEST TRANSFER"** (prod, seen 2026-11-25; it shows in the
  Appointment requests "brands on file" notice). Left as-is on purpose: no prod data changes
  until the cleanup.
- [ ] Unset `TURNSTILE_DISABLED` on Railway if it was set for testing (the server logs a warning at
  startup while it's on).

### Deferred on purpose
- [ ] Frontend JS has no automated tests (would need Node + Playwright or similar).
- [ ] HEIC files show a file link instead of a thumbnail (only Safari can render HEIC). Fix: convert
  to JPEG on upload with Pillow + pillow-heif (add both to `requirements.txt` and the README table).
- [ ] Replace the hand-written multipart parser with `python-multipart` (fewer parsing edge cases
  to own). Keep the streaming-to-disk, size cap and magic-byte checks.
- [ ] Prune old dismissed appointment requests (they hold the public's contact details), e.g.
  after 90 days.
- [ ] Optional email alert to admins for new appointment requests (v0.2.0 sends none on purpose).
- [ ] Refresh `CLOUDFLARE_NETWORKS` in `server.py` if Cloudflare's published ranges change.
- [ ] Book appointments in the app. Today the flow is request → Scheduled (booked outside the app) →
  service logged on the generator, and nothing ties the request to the service record it led to.
- [x] Use the town in request matching: done with the ZIP in v0.4.0. Customer cards without a ZIP
  still match on street alone.
- [ ] README says to create users with `railway run python3 create_user.py`. `railway run` likely
  runs on your own machine with Railway's env vars, not in the container; once confirmed, switch the
  docs to `railway ssh` + `/app/.venv/bin/python create_user.py`.
- [ ] `HEAD` requests get 501 (`Handler` has no `do_HEAD`). Harmless for browsers, but some uptime
  monitors use `HEAD`; add it if one gets set up.
- [ ] Evaluate a production WSGI server (`waitress`) in place of `ThreadingHTTPServer`. This is a
  server swap, not a framework move; check timeouts, Range responses and upload streaming still hold.
- [ ] CSP still allows `'unsafe-inline'` scripts because the page uses inline `onclick=` handlers.
  Moving to delegated event listeners would let us drop it.
- [ ] Audit log (`audit_log` table) has no UI; read it with `sqlite3`. Deletes are hard deletes
  with a JSON snapshot in the audit log rather than soft deletes.
- [ ] In-app user management (add users, change roles, reset passwords). CLI only for now.
- [ ] Frontend loads every record at startup. Fine for hundreds of generators; paginate / search
  server-side for thousands.
- [ ] No in-app password change or "sign out everywhere"; use `create_user.py --reset`.

## Done (2026-11-04 → 2026-11-21)

### Checks
- [x] 2026-11-21: Railway overwrites a client-sent `X-Real-IP`. Six portal POSTs straight to
  `gen-db-production.up.railway.app`, each claiming a different `X-Real-IP`, got 400, 400, 400, 400,
  400, then 429: all six counted as one visitor, so the client-IP rule holds without edge blocking.

### Decisions
- [x] 2026-11-21: internal-only tooling is an `x.x.1` bump for tracking; docs/tests/CI changes ship
  with no bump (recorded in `CLAUDE.md` and README Versioning).

### Releases
- [x] **v0.2.7** (2026-11-25): fixes from the Nov 25 form test (matcher tiers, phone display,
  office number in the rate-limit message, `TURNSTILE_DISABLED`). Shipped together with **v0.2.6**
  (per-account sign-in limit), which had been held on its branch.
- [x] **v0.2.2** (2026-11-21): after a request, the form goes to a separate "Request received" page
  (`/customer/thanks`) instead of a dialog, so nobody leaves thinking it didn't go through.
- [x] **v0.2.1** (`9e8b239`): request statuses are New → Matched → **Scheduled** (was Done) →
  Dismissed, with a "Mark scheduled" button; the public form has separate street and town fields.
  Migration 7 rebuilt `appointment_requests`.
- [x] **v0.2.0** (`e593e67`): customer request portal. Public `/customer` page (Turnstile, rate
  limits, honeypot kept as Dismissed), admin-only Appointment requests with fuzzy matching
  (`matching.py`), migration 6. Rate limits now use `X-Real-IP` / Cloudflare's `CF-Connecting-IP`
  (only from Cloudflare ranges) instead of `X-Forwarded-For`, for login too. Prod env vars
  (`TURNSTILE_*`, `OFFICE_PHONE`) set and confirmed live.
- [x] **v0.1.1** (`2e78224`): `Server` header reports `APP_VERSION` without the Python version;
  `create_user.py --list`. Prod users checked: `betty` (admin), `calvin` (technician).
- [x] **v0.1.0** (`1f4ca2e`): version number in the page footer; versioning rules in `CLAUDE.md`;
  `CHANGELOG.md`.
- [x] **Runtime dependencies allowed** (`9a9dc0e`): `requirements.txt` with `tzdata`; the
  hand-written DST fallback is gone. Railway's Railpack venv at `/app/.venv` confirmed in prod.

### Deploys
- [x] Migration 3 (revoke every session once, killing the leaked tokens) shipped with the 2026-11-03 review fixes.

### Look and feel
- [x] **2026-11-04 — Bridges Electric restyle** (`223fb09`): van-black + gold palette with gold
  only on dark grounds and minimal orange, Gill Sans body, Copperplate wordmark, square corners,
  dog mark + favicon inlined as data URIs. Emails use the same colors (text and logic unchanged).
- [x] **2026-11-04 — Phone layout** (`5c5e2bd`): below 760px, list rows become stacked cards, with
  a sticky top bar, full-width actions, 16px inputs (no iOS zoom), bigger tap targets, and modals
  as bottom sheets.
- [x] **2026-11-20 — Light sidebar** (`35e11aa`): the white dog mark on the dark sidebar didn't
  read well, so the sidebar is now light and the dog stays black. The active item gets a steel bar.
  `assets/dog-mark-white.png` removed.

### CI
- [x] **2026-11-20 — CI tests Python 3.12 + 3.15** (`f982667`): `ubuntu-latest` (Ubuntu 26.04) no
  longer offers 3.9, so the 3.9 job failed in setup. 3.15 is the new local dev interpreter and 3.12
  is what Railway runs. Email notifications were confirmed working in prod the same day.

## Done (2026-11-03 review)

### P0 — Security
- [x] Sessions expire after 30 days server-side; expired rows purged on startup.
- [x] All existing sessions revoked once (migration 3); the leaked July tokens are dead.
- [x] `create_user.py --reset <user>` sets a new password and signs that user out everywhere.
- [x] Stored XSS: dates validated server-side (`YYYY-MM-DD`); `fmtDate()` fallback and date
  `value=` attributes escaped client-side.

### P1 — Attachments
- [x] Multipart parser no longer strips the file's trailing `\r`/`\n` bytes (round-trips byte-exact).
- [x] Uploads stream to disk (no 100 MB in memory); downloads stream in chunks.
- [x] Non-Latin-1 filenames download fine (`filename*=UTF-8''…`); control characters stripped.
- [x] HTTP Range support (206/416) for iOS video; `preload="metadata"`; immutable caching headers.
- [x] Deleting a generator deletes its files; failed uploads leave no orphan/temp files.
- [x] Browser-declared Content-Type ignored; magic bytes decide (fixes HEIC/MOV rejections).
- [x] `UPLOADS_DIR` defaults to `uploads/` next to `DB_PATH`, so it lands on the Railway volume.
- [x] HEIC: no broken `<img>` thumbnail.

### P1 — Normal-use bugs
- [x] "Today" is the Maine date client-side and server-side (was UTC, i.e. tomorrow after ~8pm).
- [x] Backdated service visits are logged without rolling last/next service dates backwards.
- [x] Double-submit guarded on every form and confirm/invoice button.
- [x] Next service date is editable; manual service-date edits write a history entry.
- [x] Invoice / un-invoice and all deletes recorded in `audit_log`.
- [x] Error toasts no longer re-render the app (they used to wipe whatever was typed into an open form).

### P2 — Robustness / API
- [x] Bad input → 400 with a clear message (null/non-string fields, non-object bodies, bad IDs,
  over-length text); 500s no longer leak Python internals.
- [x] All handler branches catch errors; malformed cookies are treated as signed-out.
- [x] IDs cast to int (string `"3"` can't "transfer" to the current owner).
- [x] 1 MB cap on JSON bodies.
- [x] Login rate limit (10 failures / 15 min per IP); constant-time response for unknown usernames.
- [x] `Secure` cookie behind HTTPS; `nosniff`, `X-Frame-Options`, `Referrer-Policy` on all responses,
  CSP on the page.
- [x] Make + serial unique (409 on duplicates).
- [x] Optimistic concurrency: edits send `expectedUpdatedAt`; conflicting saves get 409 and the page
  reloads the latest version.
- [x] SQLite WAL mode + busy timeout.
- [x] Local server binds `127.0.0.1` unless `HOST`/`PORT` is set.

### P2 — Frontend
- [x] Address columns show the first line (`split('\\n')` → `split('\n')`).
- [x] 401 → back to the sign-in screen with "session expired".
- [x] Data refreshes when you return to the tab (if no form is open).
- [x] "Details" button on customer-page generator cards (attachments reachable from there).
- [x] Cancelling "Delete attachment?" returns to the generator details.
- [x] Phone search matches on digits (`2075550142` finds `(207) 555-0142`).
- [x] Accessibility: nav/crumbs/logout are real buttons, rows keyboard-focusable (Enter opens),
  Escape closes modals/sidebar, first field focused on open, labels tied to inputs.

### P3
- [x] `list_generators` N+1 queries → 3 queries total.
- [x] Versioned migrations via `PRAGMA user_version`.
- [x] `tests/test_server.py` (22 end-to-end tests).
- [x] `backup.py` (online SQLite backup + uploads, keeps newest 14).
- [x] `.python-version` pins 3.12.
- [x] Untracked `.DS_Store`, `server.log`, `.claude/settings.local.json`; `.gitignore` covers
  `*.sqlite3.bak-*`, `backups/`, logs, `__pycache__`.
- [x] `CLAUDE.md` filled in.
