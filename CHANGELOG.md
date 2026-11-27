# Changelog

Newest first. Versioning rules are in `README.md` (Versioning).

## 0.9.0 — 2026-11-27

- **Customer email addresses are hidden from technicians.** Technicians now see them as
  `j•••@gmail.com` (first letter and domain) in the customer list and on customer pages. Admins
  see them in full, as before. The address is masked by the server, so the full address never
  reaches a technician's browser, and searching by email only matches what they can see.
- **On the customer form, technicians can't change an existing customer's email:** the field is
  locked, with "Only admins can see or change email addresses.", and saving other changes keeps the
  stored email. They can still type an email when adding a new customer; once saved, it shows
  masked to them.
- New permission `customers.view_email` (admins only).

## 0.8.2 — 2026-11-26

- **Fix: a quick triple-click could invoice the wrong record.** Found in the second prod test. A
  service record was saved, and the next click on the same spot opened whichever record was listed
  underneath. The click after that landed on that record's "Mark as invoiced". Now mouse and tap
  clicks are ignored for half a second after any window (record, form or confirm dialog) opens or
  closes. A fast double or triple click can't carry over into what appears under the pointer; this
  also stops a double-click from confirming a Delete. Keyboard use (Enter/Space) isn't affected.

## 0.8.1 — 2026-11-26

The rest of the fixes from the Nov 26 prod QA:

- **Huge ids no longer crash a request.** A 30-digit number in an address (for example
  `/api/generators/999…`) gave "Internal server error" on every route that takes an id, because the
  number didn't fit in the database. It's now "Not found". An id that big in a request body (such as
  `customerId`) is refused like any other invalid id.
- **Long words wrap.** Text with no spaces (a pasted URL or part number, or a long string of
  characters) ran out of the record views and could stretch tables. It now wraps everywhere,
  including in emails.
- **Text boxes stop at the limit the server enforces:** 5,000 characters for notes and addresses,
  200 for names, make, model, serial and phone. Before, you only found out on saving.
- **Unsupported requests answer like the rest of the app:** unknown methods (PATCH and so on) and
  garbled requests get a short JSON error with the usual security headers, not Python's HTML error
  page.
- **HEAD requests work on the pages**, for uptime monitors (the same headers as GET, without the
  body). The API answers HEAD with 405.

## 0.8.0 — 2026-11-26

Fixes from testing v0.7.0 on prod (dummy data), plus one feature.

- **Two saves at the same moment no longer overwrite each other.** When two people (or two tabs)
  saved the same service record, generator, customer or appointment request at almost the same
  moment, both were accepted and the second silently replaced the first. Now the second waits for
  the first, sees that the record changed, and gets the usual "was changed by … after you opened
  it" message. This was there before v0.7.0 too.
- **Saving an edited service record closes it.** It used to reopen the record, whose "Mark as
  invoiced" button sat exactly where "Save changes" had been, so a quick second click could
  invoice (and lock) the record by accident.
- **Date limits** on service visits (logging and editing) and on a generator's last/next service
  dates:
  - A service date can't be in the future (today in Maine is fine) or before 2000.
  - The next service date must be after it and within 5 years of it, or of today when there's no
    service date.
  - These catch typos like 2207 for 2027, and visits dated ahead of time, which used to sort to the
    top of "Needs review".
  - The date pickers stop at today.
  - Existing records with odd dates can still have their other fields edited.
- **New: delete a service record** logged by mistake, until it's invoiced. It follows the same
  rules as editing: the logger can delete their own, and admins anyone's. There's a confirm step,
  and the audit log keeps a snapshot of what was deleted. If it was the unit's latest visit, the
  unit's last/next service dates go back to the previous visit, or are cleared if there's none (a
  newer date set by hand is kept).

## 0.7.0 — 2026-11-26

- **Service records can be edited until they're invoiced.** Open a record (Service records, or a
  unit's history) and choose **Edit** to change the service date, next service date or notes.
  - Technicians can edit the records they logged; admins can edit anyone's.
  - Once a record is marked invoiced it's locked: the Edit button goes away and the server refuses
    changes. An admin can undo invoiced to reopen it.
  - The record shows who edited it last, and every edit is written to the audit log with the
    before and after values.
  - No email is sent again.
- **The schedule follows the latest visit.** If the edited record is the unit's most recent
  visit, or becomes it, the generator's Last service and Next service dates (and so its status
  and map color) follow, with a "Service dates edited" history entry. Editing an older visit never
  changes the schedule, and a last-service date set by hand to something newer is left alone.
- **Database:** migration 11 adds `notes`, `next_service_date`, `updated_by` and `updated_at` to
  `generator_history`, and fills in the notes and next-service date of existing service records
  from their text. The text itself isn't changed.

## 0.6.0 — 2026-11-26

- **The request form asks whether the billing address is the same.** A "Billing address is the
  same" box sits under Town and ZIP, ticked by default, so most people type nothing extra.
  Unticking it shows Billing street (or PO Box), Town, State and ZIP. State is a dropdown starting
  on Maine, for out-of-state billing (snowbirds, landlords).
- **Admins see it:** the request details have a "Billing address" line ("Same as service address"
  or the address), and so does the new-request email.
- **"Create new customer" fills the billing address in** when it differs, with the customer form's
  "same" box unticked.
- **Database:** migration 10 adds the billing columns to `appointment_requests`. Older requests
  count as "same", as they always did. A browser still running a cached copy of the old form script
  sends no billing answer, and that is also taken as "same".

## 0.5.0 — 2026-11-26

- **Generator map.** The Generators page has **List** and **Map** tabs. The map uses the same
  search, brand and status filters as the list.
  - Each pin is colored by status: green for up to date, amber for due soon, red for overdue and
    grey for no service on file.
  - Units at the same address share one pin, colored by the most urgent one.
  - Clicking a pin lists its units, and clicking a unit opens it.
- **Under the map:** a legend with counts, and a line saying how many generators couldn't be
  placed and why: no address, address needs a town or ZIP, address not found, or still being
  looked up. "Show them" lists those units so their addresses can be fixed.
- **Where a pin goes:** the generator's site address, else its customer's address.
  - Addresses are turned into map positions by the US Census Bureau's free geocoder. Only the
    street, town, state and ZIP are sent: no names, phones or emails.
  - Each answer is saved, so an address is looked up once, and again only after it's edited. New
    and edited addresses appear on the map within a few minutes.
- **Database:** migration 9 adds the `geocodes` table. Existing tables are unchanged.
- The map library (Leaflet 1.9.4) is served from the app itself. Map tiles come from
  OpenStreetMap.
## 0.4.1 — 2026-11-26

- **Fix: the request form could say "Please enter a 5-digit ZIP code" when the ZIP was fine.**
  Cloudflare tells browsers to keep `.js` and `.css` files for 4 hours, overriding the app's own
  "don't cache" header. A browser that had opened the form before v0.4.0 got the new page (with
  the ZIP box) but kept the old script, which didn't send the ZIP, so the server refused it.
  The form pages now link their script and stylesheet with the version in the URL
  (`customer-request.js?v=0.4.1`), so every release gets fresh copies.

## 0.4.0 — 2026-11-26

- **The request form asks for a ZIP code**, in a short box next to Town. Phones show a number pad,
  and browsers fill it in from saved addresses. 5 digits, or ZIP+4.
- **Customer cards made from a request get a complete address:** "12 Main St" / "Saco, ME 04072".
  The state comes from the ZIP: ME for 039–049, NH for 030–038. Anywhere else, no state is
  guessed and the office completes it. The billing address still copies the service address, as
  before. The requests list, request details and the request emails show the same line.
- **Matching uses the ZIP:** the same street in a different ZIP is no longer suggested as an
  address match. Customer cards without a ZIP still match on the street alone, and phone/email
  matches are unaffected.
- **Database:** migration 8 adds `service_zip` to `appointment_requests`. Older requests have no
  ZIP and show just the town.

## 0.3.0 — 2026-11-26

- **Appointment-request emails for admins.** Two new choices under Account settings. Take
  either one, or both:
  - **Each new one, as it comes in:** one email per request from the website. It has everything
    on the form (the phone is tap-to-call, the email address opens a new message), when it came
    in, and the suggested match, such as "Likely match: Elliott Gear · phone matches, name and
    address differ". Requests caught by the spam filter don't send one.
  - **Daily summary (6pm):** every day at 6pm Maine time. It lists the requests received since
    6pm the day before, then everything still in New. It's skipped on days when nothing came in
    and nothing is waiting.
- Only admins see or get these, because they contain customers' contact details. Technicians
  can't subscribe, and the server checks again when it sends.
- `send_notifications.py request_digest --preview` shows what tonight's summary would say.
- No database changes and no new settings.

## 0.2.8 — 2026-11-25

- **Request form works with Turnstile in Invisible mode.** With the widget set to Invisible in
  Cloudflare, the check runs in the background with nothing on screen, and its token arrives a
  few seconds after the page loads. Pressing Send before then gave "Please complete the check
  above the button", with no check to complete. Now Send waits up to 15 seconds for the token
  while showing "Sending…". If none comes, the form says so and gives the office number. The
  empty space reserved for the widget is gone when nothing is shown.

## 0.2.7 — 2026-11-25

Fixes from the Nov 25 request-form test. Ships together with 0.2.6, which hadn't been deployed.

- **Matching: a shared phone or email is no longer "Strong" on its own when the name and the
  address both differ.** The candidate is still shown, as **Likely**, with a "name and address
  differ" chip. Example: "Mike Jones" at 65 Oak St shared a phone with Elliott Gear at 33 Beach St.
  A property manager writing from a company email about another site (Pinecrest) is the same
  pattern and still appears. Phone or email plus a matching name *or* address stays Strong. If
  the customer has no address on file, or the request has none, nothing counts as differing.
  Strong suggestions are listed first. The "phone and email point to different customers"
  warning works as before.
- **Phone numbers in the admin app** show as (207) 555-0000 when they're 10-digit US numbers
  (an extra leading 1 is dropped). This covers the Appointment requests list, request details,
  match cards, the "match to another customer" list, and the customer list and details. Display
  only: what was typed is what's stored, and anything else shows as typed.
- **The request form's "too many requests" message gives the office number** (from
  `OFFICE_PHONE`) as a tap-to-call link, like the outage banner. So does "online requests
  aren't available", along with the page's own connection and error messages.
- **Turnstile can be switched off for testing:** set `TURNSTILE_DISABLED=1`. The server skips
  the check, and the page doesn't load the widget or ask for it, so nobody is held up by a check
  that isn't running. Rate limits and the honeypot still apply. The server logs a warning at
  startup and marks each stored request "turnstile off". Before this there was no off switch.
  Cloudflare's widget script is now loaded by the page script, and only when the check is on.
  If it's blocked (an extension or the network), the form says the check didn't load and gives
  the office number, instead of asking for a check that isn't there.

## 0.2.6 — 2026-11-23

- **Sign-in limit per account.** On top of the existing limit (10 failed sign-ins from one
  connection in 15 minutes), 20 failed sign-ins for one username within an hour now lock that
  account, from whatever connections they come. Guessing a password from many addresses no longer
  gets around the limit. Unknown usernames are counted the same way, so the answer never reveals
  which accounts exist. A successful sign-in clears the count; restarting the service clears all.
  No Turnstile on sign-in, so staff have no extra step and Cloudflare outages don't block sign-in.

## 0.2.5 — 2026-11-21

- **The tab icon has a transparent background.** It was a white square behind the dog, which showed
  on grey or dark browser tabs. All three pages (the app, the request form, and the "Request
  received" page) now link the same icon, served at `/favicon.png`. Before, the form pages had none.
  `/favicon.ico` serves it too, for browsers that ask for it on their own.
- **Request form wording:** "What do you need?" is now "Service needed", and the "Maintenance"
  choice is now "Routine maintenance". Display text only: stored values are unchanged
  (`maintenance` / `repair` / `not_running`), so existing requests show the new labels. The admin
  Appointment requests list uses the same wording.
- **Request form outage banner** now reads "Power out and your generator isn't running? Call us
  now at (office phone). This form is for scheduling routine visits."
  The "Request received" page uses the same first sentence, without the line about the form.
- **Request form dropdowns** (Generator brand, Service needed) are now the same height, padding and
  font size as the text boxes. Safari had drawn them at about half the height.

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
