// The public customer request page (/customer). Loaded as a file so the page's CSP can forbid
// inline scripts entirely: no inline <script> blocks and no on* attributes anywhere.
(function () {
  'use strict';

  var form = document.getElementById('request-form');
  var unavailable = document.getElementById('unavailable');
  var errorBox = document.getElementById('form-error');
  var submitBtn = document.getElementById('submit-btn');
  var THANKS_URL = '/customer/thanks';
  var sent = false;
  var FIELDS = ['name', 'phone', 'email', 'serviceStreet', 'serviceTown', 'brand', 'need', 'model', 'notes'];
  var honeypot = form.querySelector('.hp input');
  var widget = form.querySelector('.cf-turnstile');
  var TURNSTILE_SCRIPT = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
  // The server says whether it checks tokens (TURNSTILE_DISABLED). The page follows it, so a
  // customer is never held up by a check the server isn't running.
  var checkOn = form.dataset.check !== 'off';
  var checkFailedToLoad = false;
  // The office number as the page shows it (from OFFICE_PHONE), so messages can link to it.
  var officeLink = document.querySelector('.emergency a[href^="tel:"]');
  var officePhone = officeLink && /\d/.test(officeLink.textContent) ? officeLink.textContent : '';

  function callOffice() {
    return officePhone ? 'call the office at ' + officePhone : 'call the office';
  }

  if (!checkOn) {
    widget.remove();
  } else if (!form.dataset.sitekey) {
    // Check switched on but no site key configured: the form can't be verified, so don't offer it.
    form.hidden = true;
    unavailable.classList.add('show');
    return;
  } else {
    var script = document.createElement('script');
    script.src = TURNSTILE_SCRIPT;
    script.async = true;
    script.addEventListener('error', function () { checkFailedToLoad = true; });
    document.head.appendChild(script);
  }

  // Built from text nodes only; the office number, if the message has it, becomes a tel: link.
  function showError(message) {
    errorBox.textContent = '';
    var at = officePhone ? message.indexOf(officePhone) : -1;
    if (at < 0) {
      errorBox.textContent = message;
    } else {
      var link = document.createElement('a');
      link.href = officeLink.getAttribute('href');
      link.textContent = officePhone;
      errorBox.append(message.slice(0, at), link, message.slice(at + officePhone.length));
    }
    errorBox.classList.add('show');
    errorBox.scrollIntoView({ block: 'nearest' });
  }

  function clearError() {
    errorBox.textContent = '';
    errorBox.classList.remove('show');
  }

  function firstProblem() {
    var invalid = form.querySelector(':invalid');
    if (!invalid) return null;
    var label = form.querySelector('label[for="' + invalid.id + '"]');
    var name = label ? label.firstChild.textContent.trim() : 'This field';
    if (invalid.type === 'email' && invalid.value) return { el: invalid, message: 'Please enter a valid email address.' };
    return { el: invalid, message: name + ' is required.' };
  }

  function resetCheck() {
    if (window.turnstile) {
      try { window.turnstile.reset(); } catch (e) { /* widget not rendered yet */ }
    }
  }

  // The token from the check (or '' when the check is switched off). Waits up to TOKEN_WAIT_MS
  // for one, since in Invisible mode it arrives a few seconds after the page loads.
  var TOKEN_WAIT_MS = 15000;
  function currentToken() {
    var field = form.querySelector('[name="cf-turnstile-response"]');
    return field ? field.value : '';
  }
  function waitForToken() {
    if (!checkOn) return Promise.resolve('');
    var started = Date.now();
    return new Promise(function (resolve, reject) {
      (function poll() {
        var token = currentToken();
        if (token) return resolve(token);
        var waited = Date.now() - started;
        // Blocked by an extension or the network: say so rather than wait for a check that isn't there.
        if (checkFailedToLoad || (!window.turnstile && waited > 5000)) {
          return reject(new Error("The robot check didn't load. Please reload the page, or " + callOffice() + '.'));
        }
        if (waited > TOKEN_WAIT_MS) {
          return reject(new Error("We couldn't finish checking that you're not a robot. If a check shows above " +
                                  'the button, please complete it. Otherwise reload the page, or ' + callOffice() + '.'));
        }
        setTimeout(poll, 250);
      })();
    });
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    clearError();
    var problem = firstProblem();
    if (problem) {
      showError(problem.message);
      problem.el.focus();
      return;
    }
    submitBtn.disabled = true;
    submitBtn.textContent = 'Sending…';
    // In Invisible (or non-interactive) mode the check runs in the background with nothing on
    // screen, so a quick visitor can press Send before the token exists: wait for it.
    waitForToken().then(function (token) {
      var payload = { turnstileToken: token };
      FIELDS.forEach(function (name) { payload[name] = form.elements[name].value.trim(); });
      payload[honeypot.name] = honeypot.value;
      return fetch('/api/public/appointment-requests', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (!res.ok) throw new Error(body.error || 'Something went wrong. Please try again, or ' + callOffice() + '.');
      });
    }).then(function () {
      // A page of its own, so nobody leaves thinking it didn't go through. replace(): Back won't
      // return to the filled-in form.
      sent = true;
      window.location.replace(THANKS_URL);
    }).catch(function (err) {
      var offline = err instanceof TypeError;  // fetch itself failed: no connection
      showError(offline ? "Couldn't send your request. Check your connection and try again, or " + callOffice() + '.' : err.message);
    }).then(function () {
      if (sent) return;  // leaving for the thank-you page; keep the button disabled
      submitBtn.disabled = false;
      submitBtn.textContent = 'Send request';
      resetCheck();  // tokens are single-use
    });
  });
})();
