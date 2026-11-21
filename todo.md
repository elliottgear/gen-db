# TODO

Severity: **P0** fix now · **P1** fix before relying on the feature · **P2** should fix · **P3** nice to have.

## Open

### Needs you (can't be done from code)
- [ ] **P0 — Reset the `tech1` and `tech2` passwords** locally: `python3 create_user.py --reset tech1`.
  Their hashes were in public git history. Prod doesn't have them (checked 2026-11-20: prod users are
  `betty` admin and `calvin` technician). Also change any other account that reuses those passwords.
- [ ] **P0 — Optional: remove the DB from git history.** The repo is private now, but any existing
  clone/fork still has it. `git filter-repo --invert-paths --path gen_db.sqlite3 --path gen_db.sqlite3.bak-20260718175615`
  then force-push. This rewrites every commit hash.
- [ ] **Deploy note:** migration 3 revokes every session once, so everyone has to sign in again after
  this deploys. That's intended: it kills the leaked tokens.
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

### Deferred on purpose
- [ ] Frontend JS has no automated tests (would need Node + Playwright or similar).
- [ ] Demo data still seeds into an empty DB (`seed()` in `init_db`). Kept for now while building;
  gate behind an env flag (e.g. `SEED_DEMO=1`) before real use.
- [ ] HEIC files show a file link instead of a thumbnail (only Safari can render HEIC). Fix: convert
  to JPEG on upload with Pillow + pillow-heif (add both to `requirements.txt` and the README table).
- [ ] Replace the hand-written multipart parser with `python-multipart` (fewer parsing edge cases
  to own). Keep the streaming-to-disk, size cap and magic-byte checks.
- [ ] Prune old dismissed appointment requests (they hold the public's contact details), e.g.
  after 90 days.
- [ ] Optional email alert to admins for new appointment requests (v0.2.0 sends none on purpose).
- [ ] Review `REQUEST_BRANDS` before launch: the admin page flags brands on file that it lacks
  (locally: Deere).
- [ ] Refresh `CLOUDFLARE_NETWORKS` in `server.py` if Cloudflare's published ranges change.
- [ ] Confirm on Railway that a spoofed `X-Real-IP` sent straight to the `*.up.railway.app` domain
  is overwritten (the docs don't say). If not, block non-Cloudflare traffic at the edge.
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

## Done (2026-11-04 → 2026-11-20)

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
