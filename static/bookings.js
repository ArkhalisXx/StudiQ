(() => {
  'use strict';
  const list = document.querySelector('#bookings-list');
  const status = document.querySelector('#bookings-status');
  const errorBox = document.querySelector('#bookings-error');
  const refresh = document.querySelector('#bookings-refresh');
  const returnBanner = document.querySelector('#payment-return');
  const labels = {pending_payment: 'Awaiting payment or staff confirmation', confirmed: 'Confirmed', checked_in: 'Checked in', completed: 'Completed', cancelled: 'Cancelled', expired: 'Expired'};
  const returnedFromCheckout = ['success', 'cancel', 'cancelled'].includes(new URLSearchParams(window.location.search).get('payment'));
  if (returnedFromCheckout) {
    returnBanner.hidden = false;
    returnBanner.textContent = 'Checking your latest payment status…';
  }
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
  function receiptDetails(booking, receipt) {
    const panel = element('section', undefined, 'receipt-panel');
    panel.setAttribute('aria-label', 'Booking receipt');
    const heading = element('div', undefined, 'payment-heading');
    const names = {queued: 'Queued', sending: 'Processing', sent: 'Email sent', preview: 'Preview ready', failed: 'Needs attention', not_requested: 'Not requested'};
    heading.append(element('strong', 'Booking receipt'));
    const badge = element('span', names[receipt.status] || 'Status unavailable', 'receipt-badge');
    badge.dataset.status = receipt.status; heading.append(badge); panel.append(heading);
    const messages = {
      queued: 'Your receipt is being prepared. Its status will update automatically.',
      sending: 'Your receipt is being processed. Its status will update automatically.',
      sent: `Receipt sent to your account email${receipt.sent_at ? ` · ${timestamp(receipt.sent_at)} (Manila time)` : ''}.`,
      preview: 'A local receipt preview is ready. It has not been emailed.',
      failed: 'Receipt delivery could not be confirmed. Check your inbox before retrying. If retry is unavailable, refresh shortly or ask staff for help.',
      not_requested: 'An email receipt has not been requested for this payment yet.'
    };
    panel.append(element('p', messages[receipt.status] || 'Receipt status is unavailable. Please refresh your bookings.', 'payment-description'));
    if (typeof receipt.message === 'string' && receipt.message.trim()) panel.append(element('p', receipt.message, 'receipt-note'));
    const actions = element('div', undefined, 'card-actions payment-actions');
    if (typeof receipt.preview_url === 'string' && /^\/api\/receipts\/[A-Za-z0-9_-]+\/preview$/.test(receipt.preview_url)) {
      const link = element('a', 'Download receipt preview', 'button-link');
      link.href = receipt.preview_url; link.download = ''; actions.append(link);
    }
    if (receipt.can_retry === true && ['failed', 'not_requested', 'preview'].includes(receipt.status)) {
      const label = receipt.status === 'failed' ? 'Retry email receipt' : receipt.status === 'preview' ? 'Email this receipt' : 'Prepare email receipt';
      const button = element('button', label, 'secondary-button'); button.type = 'button';
      button.addEventListener('click', async () => {
        if (busy) return;
        busy = true; button.disabled = true; errorBox.hidden = true; button.textContent = 'Preparing receipt…';
        try {
          await request(`/api/checkout/${encodeURIComponent(booking.id)}/receipt`, {method: 'POST'});
          busy = false; await load();
        } catch (error) {
          busy = false; await load();
          errorBox.textContent = `${errorMessage(error)} Check your inbox and refresh the receipt status before retrying.`;
          errorBox.hidden = false;
        } finally { busy = false; }
      });
      actions.append(button);
    }
    if (actions.childElementCount) panel.append(actions);
    return panel;
  }
  function paymentDetails(booking) {
    const payment = booking.payment || {status: 'unpaid', payments_enabled: false};
    const panel = element('section', undefined, 'payment-panel');
    panel.setAttribute('aria-label', 'Payment status');
    const heading = element('div', undefined, 'payment-heading');
    heading.append(element('strong', 'GCash payment'));
    const names = {unpaid: 'Not paid', creating: 'Preparing checkout', pending: 'Pending', paid: 'Paid', review: 'Staff review required', failed: 'Failed', expired: 'Checkout expired', cancelled: 'Checkout cancelled'};
    const badge = element('span', names[payment.status] || 'Status unavailable', 'payment-badge');
    badge.dataset.status = payment.status; heading.append(badge); panel.append(heading);
    if (payment.mode === 'test') panel.append(element('p', 'TEST MODE · Sandbox payment, no real charge.', 'payment-test-note'));
    let message;
    if (payment.status === 'paid') {
      message = `${payment.mode === 'test' ? 'Test GCash payment completed' : 'GCash payment received'}${payment.paid_at ? ` · ${timestamp(payment.paid_at)} (Manila time)` : ''}.`;
    } else if (payment.status === 'review') {
      message = 'Payment received; staff review required. Do not pay again. Contact staff about your reservation.';
    } else if (payment.status === 'creating') {
      message = 'Your GCash checkout is being prepared. Refresh your booking status before trying again.';
    } else if (payment.status === 'pending') {
      message = 'GCash payment is pending. Finish checkout before your hold expires, then refresh your booking status.';
    } else if (!payment.payments_enabled) {
      message = 'GCash checkout is not configured yet. Staff confirmation is available.';
    } else if (payment.status === 'failed') {
      message = 'GCash payment failed. Check your booking status before retrying.';
    } else if (payment.status === 'expired' || payment.status === 'cancelled') {
      message = 'This GCash checkout has ended. Check your booking status before trying again.';
    } else {
      message = 'Pay through PayMongo’s hosted GCash checkout before your reservation hold expires.';
    }
    panel.append(element('p', message, 'payment-description'));
    const actions = element('div', undefined, 'card-actions payment-actions');
    if (payment.can_pay === true && !['paid', 'review', 'creating'].includes(payment.status)) {
      const button = element('button', payment.status === 'pending' ? 'Continue GCash checkout' : 'Pay with GCash', 'gcash-button');
      button.type = 'button';
      button.addEventListener('click', async () => {
        if (busy) return;
        busy = true; button.disabled = true; errorBox.hidden = true;
        button.textContent = 'Opening GCash checkout…';
        let leaving = false;
        try {
          const checkout = await request(`/api/checkout/${encodeURIComponent(booking.id)}`, {method: 'POST'});
          let url;
          try { url = new URL(checkout.checkout_url); } catch { throw new Error('A valid GCash checkout link was not returned.'); }
          if (url.protocol !== 'https:' || url.hostname !== 'checkout.paymongo.com' || url.port || url.username || url.password
              || !['test', 'live'].includes(checkout.mode)) throw new Error('A valid GCash checkout link was not returned.');
          if (checkout.mode !== payment.mode) throw new Error('The payment mode changed. Refresh your booking before starting checkout.');
          leaving = true;
          window.location.assign(url.href);
        } catch (error) {
          busy = false;
          await load();
          errorBox.textContent = `${errorMessage(error)} Check your booking status before retrying checkout.`;
          errorBox.hidden = false;
        } finally {
          if (!leaving) { busy = false; button.disabled = false; }
        }
      });
      actions.append(button);
    }
    if (['creating', 'pending'].includes(payment.status)) {
      const button = element('button', 'Refresh payment status', 'secondary-button'); button.type = 'button';
      button.addEventListener('click', async () => {
        if (busy) return;
        busy = true; button.disabled = true; errorBox.hidden = true; button.textContent = 'Checking GCash status…';
        try {
          await request(`/api/checkout/${encodeURIComponent(booking.id)}/reconcile`, {method: 'POST'});
          busy = false; await load();
        } catch (error) {
          errorBox.textContent = errorMessage(error); errorBox.hidden = false;
        } finally { busy = false; button.disabled = false; button.textContent = 'Refresh payment status'; }
      });
      actions.append(button);
    }
    if (actions.childElementCount) panel.append(actions);
    if (payment.status === 'paid' && payment.receipt && typeof payment.receipt === 'object') panel.append(receiptDetails(booking, payment.receipt));
    return panel;
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
      if (booking.status === 'pending_payment') card.append(element('p', `Hold expires ${timestamp(booking.hold_expires_at)} (Manila time). Complete an available GCash checkout or arrange on-site payment with staff before this deadline.`, 'hold-note'));
      if (booking.status === 'expired') card.append(element('p', 'This hold expired and the space was released. Check availability to make a new reservation.', 'notice'));
      if (booking.status === 'confirmed' && booking.payment?.status !== 'paid') card.append(element('p', 'Your reservation has been confirmed by staff. This status is not a GCash payment receipt.', 'notice'));
      card.append(paymentDetails(booking));
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
      if (returnedFromCheckout) returnBanner.textContent = bookings.length ? 'Your latest payment status is shown below. If a payment is still pending, finish checkout or refresh shortly.' : 'No bookings were found for this account. No payment confirmation can be shown.';
    } catch (error) {
      errorBox.textContent = errorMessage(error); errorBox.hidden = false;
      status.textContent = 'Bookings could not be refreshed.';
    } finally { busy = false; refresh.disabled = false; }
  }
  refresh.addEventListener('click', load);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  window.addEventListener('pageshow', event => { if (event.persisted) { busy = false; load(); } });
  setInterval(() => { if (!document.hidden && !list.contains(document.activeElement)) load(); }, 30000);
  load();
})();
