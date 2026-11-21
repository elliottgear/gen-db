# Changelog

Newest first. Versioning rules are in `README.md` (Versioning).

## 0.2.4 — 2026-11-21

- **Request form limits are fairer to shared connections.** The per-connection limit now counts
  requests that were actually sent in (5 per hour), not attempts, so typos and a failed robot check
  no longer use it up. A looser cap of 20 attempts per hour per connection still stops anyone
  hammering the form, and the 100-per-day overall cap is unchanged.
- **The sidebar's connection line is real.** It used to say "Connected" no matter what. It now
  switches to "Can't reach the server — changes won't be saved" when a request gets no answer from
  the app (network down, or the server restarting), re-checks every 30 seconds, and goes back to
  "Connected" on the next answer. Changing it never redraws the page, so an open form is kept.

## 0.2.3 — 2026-11-21

Internal restructure of the request portal code; no change in what users see or how the app
behaves.

- The portal now lives in its own module, `appointment_requests.py`. Shared helpers (errors, field
  validation, Maine time, audit log) moved to `core.py`, and the client IP rule plus one
  `SlidingWindow` counter (now behind the login limit and both form limits) moved to `ratelimit.py`.
  `server.py` went from 1,960 to about 1,560 lines.
- Every request status change goes through one function that stamps, audits and commits.
  Creating a customer from a request is still a single transaction.
- Served files (app, portal pages, script, styles) come from one table, and routes that need no
  sign-in from another, instead of a ladder of special cases.
- The two portal pages share `portal.css` instead of carrying copies of the same styles and logo.
- Smaller cleanups: simpler address scoring in `matching.py`, and the customer form no longer looks
  at the open dialog to decide what saving means.

## 0.2.2 — 2026-11-21

- After a successful request, the public form now goes to a separate confirmation page
  (`/customer/thanks`, "Request received") instead of showing a dialog over the form. Someone who
  clicks away quickly can't mistake it for a request that didn't go through, and Back doesn't
  return to the filled-in form.

## 0.2.1 — 2026-11-20

Fast follows to the request portal.

- The request flow is now New → Matched → **Scheduled**, with Dismissed for spam and duplicates. The
  office books the visit outside the app for now, and the service is then logged on the generator
  as usual. On a matched request the button is **Mark scheduled** (it was "Mark done"), and
  **Not scheduled yet** moves a request back to Matched.
- The public form asks for **street address** and **town** as separate fields. Matching uses the
  street, and creating a customer from a request puts the street and town on two lines, like other
  customer addresses.
- Migration 7 rebuilds `appointment_requests`: `done` becomes `scheduled`, and earlier addresses
  are kept as the street with the town left blank.

## 0.2.0 — 2026-11-20

Customer request portal.

- **Public page at `/customer`** (no sign-in, phone-friendly) where customers request service:
  name, phone, email, service address, brand and need, plus optional model and notes. It shows the
  office number for outages and confirms that the office will reach out. No emails are sent.
- **Spam protection:**
  - Cloudflare Turnstile, verified on the server. It refuses submissions when Turnstile isn't set up
    or can't be reached.
  - Rate limits: 5 attempts per hour per visitor, 100 stored requests per day overall.
  - A hidden honeypot field. Hits are kept as Dismissed so a false positive can be restored.
  - The public endpoint only ever answers `{"ok": true}` and never reveals existing data.
- **Appointment requests** section for admins only (technicians get 403 everywhere, reads
  included):
  - Statuses: New, Matched, Done, Dismissed.
  - Each request has a suggested match with reasons such as "phone matches" or "address similar".
  - Admins confirm a match, create a customer from the request, or dismiss it.
  - Every action is audited.
- **Matching without AI:** the same phone number or email decides a match, a similar address is a
  likely match, and a similar name only supports one.
- **Rate limits now find the visitor safely:**
  - They use Railway's `X-Real-IP`, then Cloudflare's `CF-Connecting-IP`, but the second only for
    requests that really came from Cloudflare.
  - `X-Forwarded-For` is no longer trusted. This also applies to the login limit.
- **Database:** migration 6 adds the `appointment_requests` table and changes no existing data.
- **New environment variables:** `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET_KEY` and `OFFICE_PHONE`.

## 0.1.1 — 2026-11-20

- The `Server` response header now reports the app version (`WattWatch/0.1.1`) instead of a
  stale `WattWatch/1.0`, and no longer includes the Python version.
- `create_user.py --list` prints every user with their role, email, created date and number of
  signed-in sessions (internal tooling).

## 0.1.0 — 2026-11-20

First versioned release. Everything built so far:

- Customers, generators and service records, with attachments (photos, video, PDFs).
- Technician and admin roles; admins handle invoicing and get email.
- Email via SMTP2GO: a new-service-record email, the Friday 4pm weekly digest and the
  Monday 7am hit list.
- Bridges Electric styling, phone layout and light sidebar.
- Version number in the page footer.
