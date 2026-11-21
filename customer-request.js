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

  // No site key configured on the server: the form can't be verified, so don't offer it.
  if (!form.dataset.sitekey) {
    form.hidden = true;
    unavailable.classList.add('show');
    return;
  }

  function showError(message) {
    errorBox.textContent = message;
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

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    clearError();
    var problem = firstProblem();
    if (problem) {
      showError(problem.message);
      problem.el.focus();
      return;
    }
    var tokenField = form.querySelector('[name="cf-turnstile-response"]');
    var token = tokenField ? tokenField.value : '';
    if (!token) {
      showError('Please complete the check above the button.');
      return;
    }
    var payload = { turnstileToken: token };
    FIELDS.forEach(function (name) { payload[name] = form.elements[name].value.trim(); });
    payload[honeypot.name] = honeypot.value;

    submitBtn.disabled = true;
    submitBtn.textContent = 'Sending…';
    fetch('/api/public/appointment-requests', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (!res.ok) throw new Error(body.error || 'Something went wrong. Please try again, or call the office.');
      });
    }).then(function () {
      // A page of its own, so nobody leaves thinking it didn't go through. replace(): Back won't
      // return to the filled-in form.
      sent = true;
      window.location.replace(THANKS_URL);
    }).catch(function (err) {
      var offline = err instanceof TypeError;  // fetch itself failed: no connection
      showError(offline ? "Couldn't send your request. Check your connection and try again, or call the office." : err.message);
    }).then(function () {
      if (sent) return;  // leaving for the thank-you page; keep the button disabled
      submitBtn.disabled = false;
      submitBtn.textContent = 'Send request';
      resetCheck();  // tokens are single-use
    });
  });
})();
