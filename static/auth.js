'use strict';
const authForm = document.querySelector('#auth-form');
const authError = document.querySelector('#auth-error');
const authSubmit = authForm.querySelector('[type="submit"]');
const originalLabel = authSubmit.textContent;

document.querySelectorAll('[data-password-toggle]').forEach(toggle => {
  toggle.addEventListener('click', () => {
    const input = document.getElementById(toggle.dataset.passwordToggle);
    const reveal = input.type === 'password';
    input.type = reveal ? 'text' : 'password';
    toggle.textContent = reveal ? 'Hide' : 'Show';
    toggle.setAttribute('aria-label', reveal ? 'Hide password' : 'Show password');
    toggle.setAttribute('aria-pressed', String(reveal));
  });
});

function showAuthError(message) {
  authError.textContent = message;
  authError.hidden = false;
  authError.focus();
}

authSubmit.disabled = false;
authForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (authSubmit.disabled) return;
  authError.hidden = true;
  const data = Object.fromEntries(new FormData(authForm));
  if (authForm.dataset.mode === 'register' && data.password !== data.password_confirmation) {
    showAuthError('Passwords do not match. Please check both fields.');
    return;
  }
  authSubmit.disabled = true;
  authSubmit.textContent = authForm.dataset.mode === 'register' ? 'Creating account…' : 'Logging in…';
  authForm.setAttribute('aria-busy', 'true');
  try {
    const response = await fetch(authForm.action, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    const body = await response.json();
    if (!response.ok) {
      const message = Array.isArray(body.detail)
        ? body.detail.map(error => error.msg.replace(/^Value error, /, '')).join(' ')
        : body.detail;
      throw new Error(message || 'Unable to continue. Please try again.');
    }
    // Never store credentials or session tokens in localStorage or browser URLs.
    authForm.reset();
    window.location.assign('/account');
  } catch (error) {
    showAuthError(error instanceof TypeError ? 'Could not reach the server. Please try again.' : error.message);
    authSubmit.disabled = false;
    authSubmit.textContent = originalLabel;
  } finally {
    authForm.removeAttribute('aria-busy');
  }
});
