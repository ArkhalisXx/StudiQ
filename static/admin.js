(() => {
  'use strict';
  const dateForm = document.querySelector('#admin-date-form');
  const dateInput = document.querySelector('#admin-date');
  const status = document.querySelector('#admin-status');
  const errorBox = document.querySelector('#admin-error');
  const grid = document.querySelector('#allocation-grid');
  const roster = document.querySelector('#admin-roster');
  const members = document.querySelector('#member-list');
  const maintenanceForm = document.querySelector('#maintenance-form');
  const workspaceSelect = document.querySelector('#maintenance-workspace');
  const slotChoices = document.querySelector('#maintenance-slots');
  const maintenanceResult = document.querySelector('#maintenance-result');
  const labels = {available: 'Available', inactive: 'Inactive', maintenance: 'Maintenance', pending_payment: 'Awaiting confirmation', confirmed: 'Confirmed', checked_in: 'Checked in', completed: 'Completed', cancelled: 'Cancelled', expired: 'Expired'};
  let loadedDate = null;
  let version = 0;
  let busy = false;
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  async function request(url, options) {
    let response;
    try { response = await fetch(url, {credentials: 'same-origin', ...options}); }
    catch { throw new Error('Could not reach the server. Please try again.'); }
    if (response.status === 401) { window.location.assign('/login'); throw new Error('Please log in again.'); }
    if (response.status === 204) return {};
    const body = await response.json().catch(() => null);
    if (response.status === 403) {
      grid.replaceChildren(); roster.replaceChildren(); members.replaceChildren();
      loadedDate = null; maintenanceForm.querySelector('button').disabled = true;
    }
    if (!response.ok) throw new Error(typeof body?.detail === 'string' ? body.detail : 'The request could not be completed. Please refresh and try again.');
    if (body === null) throw new Error('The server returned an unexpected response. Please refresh.');
    return body;
  }
  function showError(error) { errorBox.textContent = error instanceof TypeError || error instanceof RangeError ? 'The server returned an unexpected response. Please refresh.' : error.message; errorBox.hidden = false; }
  function badge(value) {
    const node = element('span', labels[value] || value, 'booking-badge'); node.dataset.status = value; return node;
  }
  async function mutate(url, options, button) {
    if (busy) return;
    busy = true; button.disabled = true; dateInput.disabled = true; errorBox.hidden = true;
    try { await request(url, options); busy = false; await loadDay(); }
    catch (error) { showError(error); }
    finally { busy = false; button.disabled = false; dateInput.disabled = false; }
  }
  function renderGrid(data) {
    const table = element('table', undefined, 'data-table allocation-table');
    const caption = element('caption', `Workspace allocations for ${data.booking_date}. All times are in Manila.`, 'sr-only');
    const head = element('thead'); const heading = element('tr');
    const workspaceHeader = element('th', 'Workspace'); workspaceHeader.scope = 'col'; heading.append(workspaceHeader);
    for (const slot of data.slots) {
      const cell = element('th'); cell.scope = 'col';
      cell.append(element('span', slot.label), element('small', `${slot.starts_at}–${slot.ends_at}`)); heading.append(cell);
    }
    head.append(heading); const body = element('tbody');
    for (const workspace of data.workspaces) {
      const row = element('tr'); const name = element('th'); name.scope = 'row';
      name.append(element('strong', workspace.code), element('small', `${workspace.workspace_type}${workspace.is_active ? '' : ' · inactive'}`)); row.append(name);
      for (const slot of data.slots) {
        const cell = element('td');
        const allocation = workspace.allocations.find(item => item.slot_id === slot.id);
        if (!allocation) { cell.append(element('span', 'Unavailable')); row.append(cell); continue; }
        cell.append(badge(!workspace.is_active && allocation.status === 'available' ? 'inactive' : allocation.status));
        if (allocation.customer_name) cell.append(element('p', allocation.customer_name, 'allocation-person'));
        if (allocation.reason) cell.append(element('p', allocation.reason, 'allocation-person'));
        if (allocation.maintenance_block_id) {
          const release = element('button', 'Release block', 'secondary-button small-button'); release.type = 'button';
          release.setAttribute('aria-label', `Release maintenance block for ${workspace.code}, ${slot.label}`);
          release.addEventListener('click', () => {
            if (window.confirm('Release this maintenance block and all the slots it covers?')) {
              mutate(`/api/admin/maintenance-blocks/${encodeURIComponent(allocation.maintenance_block_id)}`, {method: 'DELETE'}, release);
            }
          }); cell.append(release);
        }
        row.append(cell);
      }
      body.append(row);
    }
    table.append(caption, head, body); grid.replaceChildren(table);
  }
  function renderRoster(bookings) {
    const cards = bookings.map(booking => {
      const card = element('article', undefined, 'reservation-card');
      const top = element('div', undefined, 'reservation-heading');
      const title = element('div'); title.append(element('h3', booking.customer_name), element('p', booking.customer_email, 'notice'));
      top.append(title, badge(booking.status)); card.append(top);
      card.append(element('p', `${booking.workspace_code} · ${booking.workspace_type} · ${booking.package_name}`, 'roster-summary'));
      card.append(element('p', booking.slots.map(slot => `${slot.label} ${slot.starts_at}–${slot.ends_at}`).join(' + '), 'notice'));
      card.append(element('p', new Intl.NumberFormat('en-PH', {style: 'currency', currency: booking.currency}).format(booking.price_minor / 100), 'roster-price'));
      if (booking.status === 'pending_payment') {
        const expiry = new Date(booking.hold_expires_at);
        const deadline = Number.isNaN(expiry.getTime()) ? 'unavailable' : new Intl.DateTimeFormat('en-PH', {dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Manila'}).format(expiry);
        card.append(element('p', `Hold expires ${deadline} · Manila time`, 'hold-note'));
      }
      const actions = element('div', undefined, 'card-actions');
      const choices = [
        ['can_confirm', 'confirm', 'Confirm for on-site payment'],
        ['can_check_in', 'check-in', 'Check in'],
        ['can_complete', 'complete', 'Complete visit'],
        ['can_cancel', 'cancel', 'Cancel reservation']
      ];
      for (const [flag, path, label] of choices) {
        if (!booking[flag]) continue;
        const button = element('button', label, path === 'cancel' ? 'secondary-button small-button' : 'small-button'); button.type = 'button';
        button.addEventListener('click', () => {
          if (path === 'confirm' && !window.confirm('Confirm this reservation for on-site payment? This does not record an online payment.')) return;
          if (path === 'cancel' && !window.confirm(`Cancel the reservation for ${booking.customer_name}?`)) return;
          mutate(`/api/admin/bookings/${encodeURIComponent(booking.id)}/${path}`, {method: 'POST'}, button);
        }); actions.append(button);
      }
      card.append(actions); return card;
    });
    roster.replaceChildren(...cards);
    if (!cards.length) roster.append(element('p', 'No reservations for this date.', 'empty-state'));
  }
  function renderMembers(rows) {
    if (!rows.length) { members.replaceChildren(element('p', 'No members yet.', 'empty-state')); return; }
    const table = element('table', undefined, 'data-table'); const head = element('thead'); const heading = element('tr');
    for (const label of ['Member', 'Email', 'Completed visits', 'Total reservations']) {
      const cell = element('th', label); cell.scope = 'col'; heading.append(cell);
    }
    head.append(heading); const body = element('tbody');
    for (const member of rows) {
      const row = element('tr');
      row.append(element('td', member.full_name), element('td', member.email), element('td', member.completed_bookings), element('td', member.total_bookings)); body.append(row);
    }
    table.append(head, body); members.replaceChildren(table);
  }
  function renderMaintenance(data) {
    const selectedWorkspace = workspaceSelect.value;
    const selectedSlots = Array.from(slotChoices.querySelectorAll('input:checked')).map(input => input.value);
    workspaceSelect.replaceChildren(new Option('Choose a workspace', ''), ...data.workspaces.filter(workspace => workspace.is_active).map(workspace => new Option(`${workspace.code} · ${workspace.workspace_type}`, workspace.id)));
    if (Array.from(workspaceSelect.options).some(option => option.value === selectedWorkspace)) workspaceSelect.value = selectedWorkspace;
    workspaceSelect.disabled = false;
    slotChoices.replaceChildren(...data.slots.map(slot => {
      const label = element('label', undefined, 'slot-option'); const input = element('input');
      input.type = 'checkbox'; input.name = 'time_slot_ids'; input.value = slot.id;
      input.checked = selectedSlots.includes(String(slot.id));
      label.append(input, element('span', `${slot.label} · ${slot.starts_at}–${slot.ends_at}`)); return label;
    }));
    maintenanceForm.querySelector('button').disabled = false;
  }
  async function loadDay() {
    if (busy || !dateInput.value) return;
    const currentVersion = ++version; const selectedDate = dateInput.value;
    busy = true; dateForm.querySelector('button').disabled = true; errorBox.hidden = true;
    status.textContent = `Loading ${selectedDate}…`;
    try {
      const [allocations, bookings, directory] = await Promise.all([
        request(`/api/admin/allocations?booking_date=${encodeURIComponent(selectedDate)}`),
        request(`/api/admin/roster?booking_date=${encodeURIComponent(selectedDate)}`),
        request('/api/admin/members')
      ]);
      if (currentVersion !== version || selectedDate !== dateInput.value) return;
      if (!allocations || !Array.isArray(allocations.slots) || !Array.isArray(allocations.workspaces) || !Array.isArray(bookings) || !Array.isArray(directory)) throw new Error('The daily view is unavailable. Please refresh.');
      renderGrid(allocations); renderRoster(bookings); renderMembers(directory); renderMaintenance(allocations);
      loadedDate = selectedDate; status.textContent = `Showing ${selectedDate} · all times are in Manila. Refreshes every 30 seconds.`;
    } catch (error) { if (currentVersion === version) { showError(error); status.textContent = 'Daily view could not be refreshed.'; } }
    finally { busy = false; dateForm.querySelector('button').disabled = false; }
  }
  dateInput.addEventListener('input', () => {
    ++version; loadedDate = null; grid.replaceChildren(); roster.replaceChildren();
    workspaceSelect.disabled = true; maintenanceForm.querySelector('button').disabled = true;
    status.textContent = 'Load the selected day to see its spaces and reservations.';
  });
  dateForm.addEventListener('submit', event => { event.preventDefault(); loadDay(); });
  maintenanceForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || !loadedDate || loadedDate !== dateInput.value) return;
    const selected = Array.from(slotChoices.querySelectorAll('input:checked')).map(input => Number(input.value));
    maintenanceResult.textContent = '';
    if (!selected.length) { maintenanceResult.textContent = 'Select at least one time slot.'; return; }
    const reason = document.querySelector('#maintenance-reason').value.trim();
    if (!reason) { maintenanceResult.textContent = 'Enter a reason for this maintenance block.'; return; }
    const payload = {booking_date: loadedDate, workspace_id: Number(workspaceSelect.value), time_slot_ids: selected, reason};
    busy = true; dateInput.disabled = true; maintenanceForm.querySelector('button').disabled = true; errorBox.hidden = true;
    try {
      await request('/api/admin/maintenance-blocks', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      maintenanceResult.textContent = 'Maintenance block created.'; document.querySelector('#maintenance-reason').value = '';
      busy = false; await loadDay();
    } catch (error) { maintenanceResult.textContent = error.message; }
    finally { busy = false; dateInput.disabled = false; maintenanceForm.querySelector('button').disabled = !loadedDate; }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && loadedDate) loadDay(); });
  setInterval(() => {
    const focused = document.activeElement;
    if (!document.hidden && loadedDate && !focused.closest('#admin-roster, #maintenance-form, #allocation-grid')) loadDay();
  }, 30000);
  loadDay();
})();
