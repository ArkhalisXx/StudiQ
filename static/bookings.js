(() => {
  'use strict';
  const list = document.querySelector('#bookings-list');
  const status = document.querySelector('#bookings-status');
  const errorBox = document.querySelector('#bookings-error');
  const refresh = document.querySelector('#bookings-refresh');
  const labels = {pending_payment: 'Awaiting staff confirmation', confirmed: 'Confirmed', checked_in: 'Checked in', completed: 'Completed', cancelled: 'Cancelled', expired: 'Expired'};
  let busy = false;
  function errorMessage(error) {
    return error instanceof TypeError || error instanceof RangeError ? 'The server returned an unexpected response. Please refresh.' : error.message;
  }
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function timestamp(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? 'Unavailable' : new Intl.DateTimeFormat('en-PH', {dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Manila'}).format(date);
  }
  async function request(url, options) {
    let response;
    try { response = await fetch(url, {credentials: 'same-origin', ...options}); }
    catch { throw new Error('Could not reach the server. Please try again.'); }
    if (response.status === 401) { window.location.assign('/login'); throw new Error('Your session has expired. Please log in.'); }
    const data = await response.json().catch(() => null);
    if (!response.ok) throw new Error(typeof data?.detail === 'string' ? data.detail : 'Unable to load your bookings. Please try again.');
    if (!data) throw new Error('The server returned an unexpected response. Please refresh.');
    return data;
  }
  function render(bookings) {
    const cards = bookings.map(booking => {
      const card = element('article', undefined, 'reservation-card');
      const top = element('div', undefined, 'reservation-heading');
      const heading = element('div');
      heading.append(element('p', booking.booking_date, 'eyebrow'), element('h2', `${booking.workspace_type} · ${booking.workspace_code}`));
      const badge = element('span', labels[booking.status] || booking.status, 'booking-badge'); badge.dataset.status = booking.status;
      top.append(heading, badge); card.append(top);
      const details = element('dl', undefined, 'review-details');
      for (const [label, value] of [
        ['Pass', booking.package_name],
        ['Time slots · Manila', booking.slots.map(slot => `${slot.label} ${slot.starts_at}–${slot.ends_at}`).join(' + ')],
        ['Pass price', new Intl.NumberFormat('en-PH', {style: 'currency', currency: booking.currency}).format(booking.price_minor / 100)],
        ['Reservation reference', booking.id]
      ]) {
        const group = element('div'); group.append(element('dt', label), element('dd', value)); details.append(group);
      }
      card.append(details);
      if (booking.status === 'pending_payment') card.append(element('p', `Hold expires ${timestamp(booking.hold_expires_at)} (Manila time). Arrange on-site payment with staff before this deadline.`, 'hold-note'));
      if (booking.status === 'expired') card.append(element('p', 'This hold expired and the space was released. Check availability to make a new reservation.', 'notice'));
      if (booking.status === 'confirmed') card.append(element('p', 'Your reservation has been confirmed by staff. This status is not an online payment receipt.', 'notice'));
      if (booking.can_cancel) {
        const button = element('button', 'Cancel reservation', 'secondary-button'); button.type = 'button';
        button.addEventListener('click', async () => {
          if (busy || !window.confirm(`Cancel your ${booking.package_name} reservation on ${booking.booking_date}?`)) return;
          busy = true; button.disabled = true; errorBox.hidden = true;
          try {
            await request(`/api/bookings/${encodeURIComponent(booking.id)}/cancel`, {method: 'POST'});
            busy = false; await load();
          } catch (error) {
            errorBox.textContent = errorMessage(error); errorBox.hidden = false;
          } finally { busy = false; button.disabled = false; }
        });
        card.append(button);
      }
      return card;
    });
    if (!cards.length) {
      const empty = element('section', undefined, 'empty-state');
      empty.append(element('h2', 'No reservations yet.'), element('p', 'Find a desk or room and make a little space for your next chapter.'));
      const link = element('a', 'Find a space', 'button-link'); link.href = '/#availability'; empty.append(link); cards.push(empty);
    }
    list.replaceChildren(...cards);
    status.textContent = `${bookings.length} reservation${bookings.length === 1 ? '' : 's'} · refreshes every 30 seconds`;
  }
  async function load() {
    if (busy) return;
    busy = true; refresh.disabled = true; errorBox.hidden = true;
    try {
      const bookings = await request('/api/bookings/me');
      if (!Array.isArray(bookings)) throw new Error('The server returned an unexpected response. Please refresh.');
      render(bookings);
    } catch (error) {
      errorBox.textContent = errorMessage(error); errorBox.hidden = false;
      status.textContent = 'Bookings could not be refreshed.';
    } finally { busy = false; refresh.disabled = false; }
  }
  refresh.addEventListener('click', load);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  setInterval(() => { if (!document.hidden && !list.contains(document.activeElement)) load(); }, 30000);
  load();
})();
