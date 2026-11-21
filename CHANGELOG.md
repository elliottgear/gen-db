# Changelog

Newest first. Versioning rules are in `README.md` (Versioning).

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
