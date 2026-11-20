(() => {
  'use strict';

  const form = document.querySelector('#menu-item-form');
  const fields = document.querySelector('#menu-form-fields');
  const heading = document.querySelector('#menu-form-title');
  const nameInput = document.querySelector('#menu-name');
  const descriptionInput = document.querySelector('#menu-description');
  const categoryInput = document.querySelector('#menu-category');
  const priceInput = document.querySelector('#menu-price');
  const activeInput = document.querySelector('#menu-active');
  const availableInput = document.querySelector('#menu-available');
  const saveButton = document.querySelector('#menu-save');
  const cancelButton = document.querySelector('#menu-cancel');
  const refreshButton = document.querySelector('#menu-refresh');
  const retryButton = document.querySelector('#menu-retry');
  const formStatus = document.querySelector('#menu-form-status');
  const formError = document.querySelector('#menu-form-error');
  const listStatus = document.querySelector('#menu-list-status');
  const listError = document.querySelector('#menu-list-error');
  const list = document.querySelector('#admin-menu-list');
  const forbiddenMessage = document.querySelector('#menu-forbidden');
  const categories = {drinks: 'Drinks', snacks: 'Snacks', meals: 'Meals'};
  const currency = new Intl.NumberFormat('en-PH', {style: 'currency', currency: 'PHP'});
  let items = [];
  let editingId = null;
  let loaded = false;
  let loading = false;
  let mutating = false;
  let forbidden = false;

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function updateControls() {
    fields.disabled = forbidden || mutating || !loaded;
    saveButton.textContent = mutating ? 'Saving…' : editingId === null ? 'Add item' : 'Save changes';
    saveButton.disabled = loading;
    refreshButton.disabled = forbidden || loading || mutating;
    retryButton.disabled = loading || mutating;
    for (const button of list.querySelectorAll('button')) button.disabled = loading || mutating || forbidden;
  }

  function denyAccess() {
    forbidden = true;
    items = [];
    list.replaceChildren();
    listStatus.textContent = '';
    listError.hidden = true;
    formError.hidden = true;
    formStatus.textContent = '';
    retryButton.hidden = true;
    forbiddenMessage.hidden = false;
    updateControls();
  }

  async function request(url, options = {}) {
    let response;
    try {
      response = await fetch(url, {credentials: 'same-origin', ...options});
    } catch {
      throw new Error('Could not reach the server. Please try again.');
    }
    if (response.status === 401) {
      window.location.assign('/login?next=%2Fadmin%2Fmenu');
      throw new Error('Your session has expired. Please log in again.');
    }
    if (response.status === 403) denyAccess();
    if (response.status === 204) return null;
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      const error = new Error(typeof body?.detail === 'string' ? body.detail : 'The request could not be completed. Please try again.');
      error.status = response.status;
      throw error;
    }
    if (body === null) throw new Error('The server returned an unexpected response. Please try again.');
    return body;
  }

  function setEditMode(id) {
    editingId = id;
    heading.textContent = id === null ? 'Add a menu item' : 'Edit menu item';
    cancelButton.hidden = id === null;
    updateControls();
  }

  function resetForm() {
    form.reset();
    priceInput.setCustomValidity('');
    setEditMode(null);
  }

  function editItem(item) {
    if (loading || mutating || forbidden) return;
    nameInput.value = item.name;
    descriptionInput.value = item.description;
    categoryInput.value = item.category;
    priceInput.value = `${Math.floor(item.price_minor / 100)}.${String(item.price_minor % 100).padStart(2, '0')}`;
    priceInput.setCustomValidity('');
    activeInput.checked = item.is_active;
    availableInput.checked = item.is_available;
    formError.hidden = true;
    formStatus.textContent = '';
    setEditMode(item.id);
    heading.scrollIntoView({block: 'nearest'});
    nameInput.focus({preventScroll: true});
  }

  function badge(text, state) {
    const node = element('span', text, 'menu-state');
    node.dataset.state = state;
    return node;
  }

  function renderItems() {
    const cards = items.map(item => {
      const card = element('article', undefined, 'admin-menu-card');
      const top = element('div', undefined, 'admin-menu-card-heading');
      const details = element('div');
      details.append(element('h3', item.name), element('p', categories[item.category], 'admin-menu-category'));
      top.append(details, element('p', currency.format(item.price_minor / 100), 'admin-menu-price'));
      card.append(top);
      if (item.description) card.append(element('p', item.description, 'admin-menu-description'));
      const states = element('div', undefined, 'admin-menu-badges');
      states.append(badge(item.is_active ? 'Shown on public menu' : 'Hidden from public menu', item.is_active ? 'shown' : 'hidden'));
      states.append(badge(item.is_available ? 'Available today' : 'Sold out', item.is_available ? 'available' : 'sold-out'));
      const actions = element('div', undefined, 'card-actions');
      const editButton = element('button', 'Edit', 'secondary-button small-button');
      editButton.type = 'button';
      editButton.setAttribute('aria-label', `Edit ${item.name}`);
      editButton.addEventListener('click', () => editItem(item));
      const deleteButton = element('button', 'Delete', 'secondary-button small-button menu-delete');
      deleteButton.type = 'button';
      deleteButton.setAttribute('aria-label', `Delete ${item.name}`);
      deleteButton.addEventListener('click', () => deleteItem(item));
      actions.append(editButton, deleteButton);
      card.append(states, actions);
      return card;
    });
    list.replaceChildren(...cards);
    if (!cards.length) list.append(element('p', 'No menu items yet. Add your first item using the form.', 'empty-state'));
    updateControls();
  }

  async function loadItems() {
    if (loading || forbidden) return;
    loading = true;
    list.setAttribute('aria-busy', 'true');
    listStatus.textContent = 'Loading menu items…';
    listError.hidden = true;
    retryButton.hidden = true;
    updateControls();
    try {
      const data = await request('/api/admin/menu');
      if (!Array.isArray(data) || !data.every(item => item && item.id !== undefined && typeof item.name === 'string' && typeof item.description === 'string' && Object.hasOwn(categories, item.category) && Number.isInteger(item.price_minor) && typeof item.is_active === 'boolean' && typeof item.is_available === 'boolean')) {
        throw new Error('The server returned an unexpected menu. Please try again.');
      }
      items = data;
      loaded = true;
      renderItems();
      listStatus.textContent = `${items.length} menu item${items.length === 1 ? '' : 's'} · includes hidden and sold-out items`;
    } catch (error) {
      if (!forbidden) {
        listError.textContent = error.message;
        listError.hidden = false;
        retryButton.hidden = false;
        listStatus.textContent = loaded ? 'Could not refresh menu items. The previous list is still shown.' : 'Menu items could not be loaded.';
      }
    } finally {
      loading = false;
      list.setAttribute('aria-busy', 'false');
      updateControls();
    }
  }

  function priceInCentavos() {
    const value = priceInput.value.trim();
    if (!/^\d+(?:\.\d{1,2})?$/.test(value)) return null;
    const [pesos, centavos = ''] = value.split('.');
    const amount = Number(pesos) * 100 + Number(centavos.padEnd(2, '0'));
    return Number.isSafeInteger(amount) && amount >= 0 && amount <= 1000000 ? amount : null;
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (loading || mutating || forbidden || !loaded) return;
    formError.hidden = true;
    formStatus.textContent = '';
    const price = priceInCentavos();
    priceInput.setCustomValidity(price === null ? 'Enter a price from 0 to 10000 with up to two decimal places.' : '');
    if (!form.reportValidity()) return;
    const name = nameInput.value.trim();
    if (!name) {
      formError.textContent = 'Enter a name for this menu item.';
      formError.hidden = false;
      nameInput.focus();
      return;
    }
    const id = editingId;
    const payload = {name, description: descriptionInput.value.trim(), category: categoryInput.value, price_minor: price, is_available: availableInput.checked, is_active: activeInput.checked};
    mutating = true;
    updateControls();
    try {
      await request(id === null ? '/api/admin/menu' : `/api/admin/menu/${encodeURIComponent(id)}`, {method: id === null ? 'POST' : 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      resetForm();
      formStatus.textContent = id === null ? 'Menu item added.' : 'Menu item updated.';
      await loadItems();
    } catch (error) {
      if (!forbidden) {
        if (error.status === 404 && id !== null) {
          setEditMode(null);
          formError.textContent = 'This item no longer exists. Your entries are still in the form. Choose Add item to create it again.';
          await loadItems();
        } else formError.textContent = error.message;
        if (!forbidden) formError.hidden = false;
      }
    } finally {
      mutating = false;
      updateControls();
    }
  });

  async function deleteItem(item) {
    if (loading || mutating || forbidden || !window.confirm(`Delete “${item.name}”? This permanently removes it from the menu.`)) return;
    mutating = true;
    formError.hidden = true;
    formStatus.textContent = '';
    updateControls();
    try {
      await request(`/api/admin/menu/${encodeURIComponent(item.id)}`, {method: 'DELETE'});
      if (editingId === item.id) resetForm();
      formStatus.textContent = 'Menu item deleted.';
      await loadItems();
    } catch (error) {
      if (!forbidden) {
        if (error.status === 404) {
          if (editingId === item.id) setEditMode(null);
          formError.textContent = 'This item has already been deleted. The list has been refreshed; any form entries are still here.';
          await loadItems();
        } else formError.textContent = error.message;
        if (!forbidden) formError.hidden = false;
      }
    } finally {
      mutating = false;
      updateControls();
    }
  }

  priceInput.addEventListener('input', () => priceInput.setCustomValidity(''));
  cancelButton.addEventListener('click', () => {
    resetForm();
    formError.hidden = true;
    formStatus.textContent = 'Edit cancelled.';
  });
  refreshButton.addEventListener('click', loadItems);
  retryButton.addEventListener('click', loadItems);
  loadItems();
})();
