'use strict';
const form = document.querySelector('#availability-form');
const result = document.querySelector('#availability-result');
const pass = form.elements.package_id;
const slot = form.elements.time_slot_id;
const submit = form.querySelector('button');
let packages = [];

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
    result.textContent = 'Choose your day and pass to see available spaces. A Day Pass uses both slots in the same workspace.';
  } catch (error) {
    result.textContent = `Could not load the catalog. ${error.message} Refresh to try again.`;
  }
}
pass.addEventListener('change', updateSlot);
form.addEventListener('input', () => { result.textContent = 'Selections changed. Check availability for the latest result.'; });
form.addEventListener('submit', async event => {
  event.preventDefault();
  submit.disabled = true;
  result.textContent = 'Checking availability…';
  try {
    const data = await getJSON(`/api/workspaces/availability?${new URLSearchParams(new FormData(form))}`);
    result.textContent = `${data.available} ${data.workspace_type.toLowerCase()} space${data.available === 1 ? '' : 's'} available on ${data.booking_date} · ${data.slots.join(' + ')}. ${data.message}`;
  } catch (error) {
    result.textContent = `Unable to check availability. ${error.message}`;
  } finally {
    submit.disabled = false;
  }
});
loadCatalog();
