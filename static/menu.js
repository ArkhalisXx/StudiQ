const menuSearch = document.getElementById("menu-search");
const categoryButtons = document.querySelectorAll("[data-menu-category]");
const menuList = document.getElementById("menu-list");
const menuStatus = document.getElementById("menu-status");
const menuError = document.getElementById("menu-error");
const menuEmpty = document.getElementById("menu-empty");
const menuNoResults = document.getElementById("menu-no-results");
const menuRetry = document.getElementById("menu-retry");
const menuClear = document.getElementById("menu-clear");
const categoryNames = { drinks: "Drinks", snacks: "Snacks", meals: "Meals" };
const pesoFormatter = new Intl.NumberFormat("en-PH", { style: "currency", currency: "PHP" });
let menuItems = [];
let selectedCategory = "all";

function makeElement(tag, className, text) {
  const element = document.createElement(tag);
  element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function makeMenuCard(item) {
  const listItem = makeElement("li", "menu-list-item");
  const card = makeElement("article", "menu-card");
  card.dataset.category = item.category;
  const top = makeElement("div", "menu-card-top");
  top.append(makeElement("span", "menu-item-category", categoryNames[item.category]));
  const availability = item.is_available ? "Available" : "Sold out";
  const badge = makeElement("span", "menu-availability", availability);
  badge.dataset.available = String(item.is_available);
  top.append(badge);
  card.append(top, makeElement("h3", "menu-item-name", item.name));
  card.append(makeElement("p", "menu-item-description", item.description || ""));
  card.append(makeElement("p", "menu-item-price", pesoFormatter.format(item.price_minor / 100)));
  listItem.append(card);
  return listItem;
}

function renderMenu() {
  const query = menuSearch.value.trim().toLocaleLowerCase();
  const visibleItems = menuItems.filter((item) => {
    const matchesCategory = selectedCategory === "all" || item.category === selectedCategory;
    const matchesSearch = `${item.name} ${item.description || ""}`.toLocaleLowerCase().includes(query);
    return matchesCategory && matchesSearch;
  });

  menuList.replaceChildren(...visibleItems.map(makeMenuCard));
  menuEmpty.hidden = menuItems.length > 0;
  menuNoResults.hidden = menuItems.length === 0 || visibleItems.length > 0;
  const count = visibleItems.length;
  const hasFilter = query !== "" || selectedCategory !== "all";
  if (menuItems.length === 0) {
    menuStatus.textContent = "No menu items yet.";
  } else if (hasFilter) {
    menuStatus.textContent = `${count} of ${menuItems.length} ${menuItems.length === 1 ? "item" : "items"} shown`;
  } else {
    menuStatus.textContent = `${count} ${count === 1 ? "item" : "items"} on the menu`;
  }
}

function setMenuControlsDisabled(disabled) {
  menuSearch.disabled = disabled;
  categoryButtons.forEach((button) => { button.disabled = disabled; });
}

function selectCategory(category) {
  selectedCategory = category;
  categoryButtons.forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.menuCategory === category));
  });
}

async function loadMenu() {
  setMenuControlsDisabled(true);
  menuRetry.disabled = true;
  menuError.hidden = true;
  menuEmpty.hidden = true;
  menuNoResults.hidden = true;
  menuList.replaceChildren();
  menuList.setAttribute("aria-busy", "true");
  menuStatus.textContent = "Loading the menu…";

  try {
    const response = await fetch("/api/menu", { headers: { Accept: "application/json" }, cache: "no-store" });
    if (!response.ok) throw new Error("Menu request failed");
    const items = await response.json();
    if (!Array.isArray(items) || items.some((item) => !item ||
        typeof item.name !== "string" ||
        (item.description !== null && item.description !== undefined && typeof item.description !== "string") ||
        !Object.hasOwn(categoryNames, item.category) ||
        !Number.isSafeInteger(item.price_minor) || item.price_minor < 0 ||
        item.currency !== "PHP" || typeof item.is_available !== "boolean")) {
      throw new Error("Invalid menu response");
    }
    menuItems = items;
    setMenuControlsDisabled(false);
    renderMenu();
  } catch {
    menuError.hidden = false;
    menuStatus.textContent = "The menu is temporarily unavailable.";
  } finally {
    menuList.setAttribute("aria-busy", "false");
    menuRetry.disabled = false;
  }
}

menuSearch.addEventListener("input", renderMenu);
categoryButtons.forEach((button) => {
  button.addEventListener("click", () => {
    selectCategory(button.dataset.menuCategory);
    renderMenu();
  });
});
menuClear.addEventListener("click", () => {
  menuSearch.value = "";
  selectCategory("all");
  renderMenu();
  menuSearch.focus();
});
menuRetry.addEventListener("click", loadMenu);
loadMenu();
