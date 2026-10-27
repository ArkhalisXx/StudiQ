'use strict';
const form = document.querySelector('#availability-form');
const result = document.querySelector('#availability-result');
const pass = form.elements.package_id;
const slot = form.elements.time_slot_id;
const submit = form.querySelector('button');
let packages = [];
let selectedSingleSlot = '';
let selectionVersion = 0;

function showResult(message, state = 'idle') {
  result.textContent = message;
  result.dataset.state = state;
}

async function getJSON(url) {
  let response;
  try {
    response = await fetch(url);
  } catch {
    throw new Error('Could not reach the server. Check your connection and try again.');
  }
  if (response.status >= 500) throw new Error('The server is temporarily unavailable. Please try again shortly.');
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error('The server returned an unexpected response. Please refresh and try again.');
  }
  if (!response.ok) throw new Error(typeof data?.detail === 'string' ? data.detail : 'Please check your date and selections.');
  return data;
}
function fill(select, rows, label) {
  select.replaceChildren(...rows.map(row => new Option(label(row), row.id)));
  select.disabled = false;
}
function updateSlot() {
  const day = packages.find(p => String(p.id) === pass.value)?.slot_policy === 'day';
  if (day) {
    if (!slot.disabled) selectedSingleSlot = slot.value;
    if (!slot.querySelector('option[value="day"]')) {
      slot.add(new Option('Both slots · Morning + Afternoon', 'day'));
    }
    slot.value = 'day';
  } else {
    slot.querySelector('option[value="day"]')?.remove();
    if (selectedSingleSlot) slot.value = selectedSingleSlot;
  }
  slot.disabled = day;
  slot.required = !day;
}
async function loadCatalog() {
  try {
    const [types, passes, slots] = await Promise.all([
      getJSON('/api/workspace-types'), getJSON('/api/packages'), getJSON('/api/time-slots')
    ]);
    const validRows = rows => Array.isArray(rows) && rows.length > 0
      && rows.every(row => row && typeof row === 'object' && !Array.isArray(row)
        && Number.isSafeInteger(row.id) && row.id > 0)
      && new Set(rows.map(row => row.id)).size === rows.length;
    const hasLabel = value => typeof value === 'string' && value.trim().length > 0;
    const validTime = value => typeof value === 'string' && /^([01]\d|2[0-3]):[0-5]\d$/.test(value);
    if (![types, passes, slots].every(validRows)
        || !types.every(type => hasLabel(type.name))
        || !passes.every(p => hasLabel(p.name) && Number.isSafeInteger(p.price_minor) && p.price_minor >= 0
          && typeof p.currency === 'string' && /^[A-Z]{3}$/.test(p.currency)
          && ['single', 'day'].includes(p.slot_policy))
        || !slots.every(s => hasLabel(s.label) && validTime(s.starts_at) && validTime(s.ends_at)
          && s.starts_at < s.ends_at)) {
      throw new Error('The workspace catalog is not ready yet.');
    }
    packages = passes;
    fill(form.elements.workspace_type_id, types, t => t.name);
    fill(pass, passes, p => `${p.name} · ${new Intl.NumberFormat('en-PH', {style: 'currency', currency: p.currency}).format(p.price_minor / 100)}`);
    fill(slot, slots, s => `${s.label} · ${s.starts_at}–${s.ends_at}`);
    updateSlot();
    submit.disabled = false;
    showResult('Choose your day and pass to see available spaces. A Day Pass uses both slots in the same workspace.');
  } catch (error) {
    showResult(`Could not load the catalog. ${error.message} Refresh to try again.`, 'error');
  }
}
pass.addEventListener('change', updateSlot);
form.addEventListener('input', () => {
  selectionVersion += 1;
  showResult('Selections changed. Check availability for the latest result.');
});
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (submit.disabled) return;
  const version = selectionVersion;
  submit.disabled = true;
  form.setAttribute('aria-busy', 'true');
  showResult('Checking availability…', 'loading');
  try {
    const data = await getJSON(`/api/workspaces/availability?${new URLSearchParams(new FormData(form))}`);
    if (version !== selectionVersion) return;
    if (!data || !Number.isInteger(data.available) || data.available < 0
        || typeof data.workspace_type !== 'string' || typeof data.booking_date !== 'string'
        || typeof data.message !== 'string' || !Array.isArray(data.slots)
        || !data.slots.every(label => typeof label === 'string')) {
      throw new Error('The server returned an unexpected response. Please refresh and try again.');
    }
    showResult(`${data.available} ${data.workspace_type.toLowerCase()} space${data.available === 1 ? '' : 's'} available on ${data.booking_date} · ${data.slots.join(' + ')}. ${data.message}`, data.available > 0 ? 'success' : 'empty');
  } catch (error) {
    if (version === selectionVersion) showResult(`Unable to check availability. ${error.message}`, 'error');
  } finally {
    submit.disabled = false;
    form.removeAttribute('aria-busy');
  }
});
loadCatalog();
