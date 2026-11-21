# Changelog

Newest first. Versioning rules are in `README.md` (Versioning).

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
