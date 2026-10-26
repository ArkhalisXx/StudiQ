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
  const response = await fetch(url);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Please check your date and selections.');
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
    if (!types.length || !passes.length || !slots.length) throw new Error('The workspace catalog is not ready yet.');
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
    showResult(`${data.available} ${data.workspace_type.toLowerCase()} space${data.available === 1 ? '' : 's'} available on ${data.booking_date} · ${data.slots.join(' + ')}. ${data.message}`, data.available > 0 ? 'success' : 'empty');
  } catch (error) {
    if (version === selectionVersion) showResult(`Unable to check availability. ${error.message}`, 'error');
  } finally {
    submit.disabled = false;
    form.removeAttribute('aria-busy');
  }
});
loadCatalog();
