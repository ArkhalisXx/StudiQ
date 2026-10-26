'use strict';
document.querySelectorAll('[data-logout]').forEach(button => {
  button.addEventListener('click', async () => {
    button.disabled = true;
    const message = document.querySelector('#site-message');
    message.hidden = true;
    try {
      const response = await fetch('/api/auth/logout', {method: 'POST', credentials: 'same-origin'});
      if (!response.ok) throw new Error('Could not log out. Please try again.');
      window.location.assign('/login');
    } catch (error) {
      message.textContent = 'Could not log out. Please check your connection and try again.';
      message.hidden = false;
      button.disabled = false;
    }
  });
});
