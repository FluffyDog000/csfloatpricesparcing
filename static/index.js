// Home page: Explorer-style folder navigation.
//  * default view = grid of folders; click a folder to open its items
//  * items with no folder live in "Other"
//  * price filter + sorting; hidden items are collapsed away by default
//  * management mode: add items, per-card actions, and bulk selection

const OTHER = "Other";

let allItems = [];
let allFolders = [];
let manageMode = false;
const selected = new Set();          // market_hash_names picked for bulk actions

const state = { view: "folders", folder: null };  // view: "folders" | "items"

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function folderOf(it) {
  const f = (it.folder || "").trim();
  return f || OTHER;
}

function token() {
  try { return localStorage.getItem("csfloat_admin_token") || ""; } catch (e) { return ""; }
}

function msg(text, isError) {
  const el = document.getElementById("manage-msg");
  if (!el) return;
  el.textContent = text || "";
  el.className = "manage-msg" + (isError ? " err" : "");
}

// -- filtering / sorting ----------------------------------------------------

function showHidden() {
  return document.getElementById("show-hidden").checked;
}

function numOrNull(id) {
  const v = document.getElementById(id).value.trim();
  if (!v) return null;
  const n = Number(v);
  return isNaN(n) ? null : n;
}

// Items after hidden/price filters (search & folder applied separately).
function filteredItems(list) {
  const lo = numOrNull("price-min");
  const hi = numOrNull("price-max");
  return list.filter((it) => {
    if (!showHidden() && it.hidden) return false;
    if (lo !== null && !(it.avg_price !== null && it.avg_price >= lo)) return false;
    if (hi !== null && !(it.avg_price !== null && it.avg_price <= hi)) return false;
    return true;
  });
}

// Sales counts sort on several windows: the all-time total mostly reflects when
// an item was added, so a recent window is the honest measure of how briskly it
// actually trades.
const SALES_FIELD = {
  "sales7": "sales_7d", "sales30": "sales_30d", "sales": "total_sales",
};

function sortItems(list) {
  const by = document.getElementById("sort-by").value;
  const arr = list.slice();
  const p = (x) => (x.avg_price === null || x.avg_price === undefined ? -Infinity : x.avg_price);
  if (by === "price-desc") { arr.sort((a, b) => p(b) - p(a)); return arr; }
  if (by === "price-asc") { arr.sort((a, b) => p(a) - p(b)); return arr; }

  const [kind, dir] = by.split("-");
  const field = SALES_FIELD[kind];
  if (field) {
    const n = (x) => x[field] ?? 0;
    // Ties broken by name so the order is stable between refreshes.
    arr.sort((a, b) => (dir === "asc" ? n(a) - n(b) : n(b) - n(a))
      || a.market_hash_name.localeCompare(b.market_hash_name));
    return arr;
  }
  arr.sort((a, b) => a.market_hash_name.localeCompare(b.market_hash_name));
  return arr;
}

function updateFilterInfo(shown, total) {
  const el = document.getElementById("filter-info");
  const hiddenCount = allItems.filter((i) => i.hidden).length;
  const parts = [];
  if (shown !== total) parts.push(`показано ${shown} из ${total}`);
  if (hiddenCount && !showHidden()) parts.push(`скрыто: ${hiddenCount}`);
  el.textContent = parts.join(" · ");
}

// -- folder + item structures ----------------------------------------------

function foldersMap(list) {
  const map = {};
  list.forEach((it) => {
    const f = folderOf(it);
    (map[f] = map[f] || []).push(it);
  });
  return map;
}

function folderNamesSorted(map) {
  return Object.keys(map).sort((a, b) => {
    if (a === OTHER) return 1;      // Other last
    if (b === OTHER) return -1;
    return a.localeCompare(b);
  });
}

// -- rendering: folders grid ------------------------------------------------

function renderFolders(list) {
  const wrap = document.getElementById("cards");
  const map = foldersMap(list);
  const names = folderNamesSorted(map);
  wrap.innerHTML = "";
  if (!names.length) {
    wrap.innerHTML = '<p class="muted">Ничего не подходит под фильтр.</p>';
    return;
  }

  const grid = document.createElement("div");
  grid.className = "folders-grid";
  names.forEach((name) => {
    const tile = document.createElement("div");
    tile.className = "folder-tile";
    tile.dataset.folder = name;
    const active = map[name].filter((it) => it.active).length;
    tile.innerHTML = `
      <div class="ficon">📁</div>
      <div class="fname">${escapeHtml(name)}</div>
      <div class="fcount">${map[name].length} предм.${active < map[name].length ? ` · ${active} актив.` : ""}</div>`;
    tile.addEventListener("click", () => {
      state.view = "items";
      state.folder = name;
      render();
    });
    tile.addEventListener("dragover", (e) => { e.preventDefault(); tile.classList.add("drop"); });
    tile.addEventListener("dragleave", () => tile.classList.remove("drop"));
    tile.addEventListener("drop", (e) => {
      e.preventDefault();
      tile.classList.remove("drop");
      const name2 = e.dataTransfer.getData("text/plain");
      if (name2) moveToFolder(name2, name === OTHER ? "" : name);
    });
    grid.appendChild(tile);
  });
  wrap.appendChild(grid);
}

// -- rendering: item cards --------------------------------------------------

function cardMain(it) {
  const img = it.icon_url
    ? `<img src="${it.icon_url}" alt="" loading="lazy">`
    : `<div class="noimg">нет фото</div>`;
  const inactive = it.active ? "" : `<span class="inactive">на паузе</span>`;
  const pat = it.pattern_sensitive ? "" : `<span class="tag">seed н/в</span>`;
  const hid = it.hidden ? `<span class="tag">скрыт</span>` : "";
  return `
    <a class="card-main" href="/item/${encodeURIComponent(it.market_hash_name)}">
      ${img}
      <div>
        <div class="name">${escapeHtml(it.market_hash_name)} ${inactive} ${pat} ${hid}</div>
        <div class="meta">
          продаж: <b>${it.total_sales}</b>
          <span class="muted">· 7д: ${it.sales_7d ?? 0} · 30д: ${it.sales_30d ?? 0}</span><br>
          avg: <b>${money(it.avg_price)}</b>
          &nbsp; min: ${money(it.min_price)} &nbsp; max: ${money(it.max_price)}
        </div>
      </div>
    </a>`;
}

function cardControls(it) {
  const n = escapeHtml(it.market_hash_name);
  return `
    <div class="card-ctrls">
      <button class="cbtn" data-act="toggle-active" data-name="${n}">
        ${it.active ? "⏸ Пауза" : "▶ Возобновить"}</button>
      <button class="cbtn" data-act="toggle-pattern" data-name="${n}">
        ${it.pattern_sensitive ? "🎨 паттерн: вкл" : "🎨 паттерн: выкл"}</button>
      <button class="cbtn" data-act="toggle-hidden" data-name="${n}">
        ${it.hidden ? "👁 показать" : "🙈 скрыть"}</button>
      <button class="cbtn" data-act="poll" data-name="${n}">⟳ спарсить</button>
      <button class="cbtn" data-act="folder" data-name="${n}">📁 переместить</button>
      <button class="cbtn danger" data-act="delete" data-name="${n}">🗑 удалить</button>
    </div>`;
}

function card(it) {
  const div = document.createElement("div");
  const name = it.market_hash_name;
  div.className = "card" + (it.active ? "" : " paused") + (it.hidden ? " is-hidden" : "");
  const pick = manageMode
    ? `<label class="pick"><input type="checkbox" class="pick-box" data-name="${escapeHtml(name)}"
         ${selected.has(name) ? "checked" : ""}></label>`
    : "";
  div.innerHTML = pick + cardMain(it) + (manageMode ? cardControls(it) : "");
  div.draggable = true;
  div.addEventListener("dragstart", (e) => e.dataTransfer.setData("text/plain", name));
  return div;
}

function renderItemList(items, headerHtml) {
  const wrap = document.getElementById("cards");
  wrap.innerHTML = headerHtml || "";
  if (!items.length) {
    wrap.insertAdjacentHTML("beforeend", '<p class="muted">Пусто.</p>');
    return;
  }
  const grid = document.createElement("div");
  grid.className = "cards-grid";
  items.forEach((it) => grid.appendChild(card(it)));
  wrap.appendChild(grid);
}

function renderItemsView(list) {
  const map = foldersMap(list);
  const items = sortItems(map[state.folder] || []);
  const header =
    `<div class="items-head">
       <span class="back-link" id="to-folders">← Папки</span>
       <span class="crumb">📁 ${escapeHtml(state.folder)} (${items.length})</span>
     </div>`;
  renderItemList(items, header);
  const back = document.getElementById("to-folders");
  if (back) back.addEventListener("click", () => { state.view = "folders"; render(); });
}

// -- top-level render -------------------------------------------------------

function render() {
  const q = (document.getElementById("search").value || "").trim().toLowerCase();
  const wrap = document.getElementById("cards");

  if (!allItems.length) {
    wrap.innerHTML = manageMode
      ? '<p class="muted">Список пуст. Добавь предмет формой выше.</p>'
      : '<p class="muted">Нет предметов. Открой «Управление» и добавь предмет.</p>';
    updateFilterInfo(0, 0);
    updateBulkBar();
    return;
  }

  const base = filteredItems(allItems);
  updateFilterInfo(base.length, allItems.length);

  if (q) {
    const items = sortItems(base.filter((it) => it.market_hash_name.toLowerCase().includes(q)));
    renderItemList(items,
      `<div class="items-head"><span class="crumb">🔎 Поиск: «${escapeHtml(q)}» (${items.length})</span></div>`);
  } else if (state.view === "items" && state.folder) {
    renderItemsView(base);
  } else {
    state.view = "folders";
    renderFolders(base);
  }
  updateBulkBar();
}

function refreshFolderDatalist() {
  const dl = document.getElementById("folder-list");
  if (dl) dl.innerHTML = folderNamesSorted(foldersMap(allItems))
    .filter((f) => f !== OTHER)
    .map((f) => `<option value="${escapeHtml(f)}">`).join("");
}

async function load() {
  try {
    const data = await getJSON("/api/items");
    renderStatus(data.last_update);
    allItems = data.items;
    allFolders = data.folders || [];
    refreshFolderDatalist();
    render();
  } catch (e) {
    document.getElementById("cards").innerHTML =
      `<p class="muted">Ошибка загрузки: ${escapeHtml(e.message)}</p>`;
  }
}

// -- bulk selection ---------------------------------------------------------

function visibleNames() {
  return Array.from(document.querySelectorAll(".pick-box")).map((b) => b.dataset.name);
}

function updateBulkBar() {
  const bar = document.getElementById("bulk-bar");
  bar.hidden = !manageMode;
  document.getElementById("bulk-count").textContent = `выбрано: ${selected.size}`;
}

document.getElementById("cards").addEventListener("change", (ev) => {
  const box = ev.target.closest(".pick-box");
  if (!box) return;
  if (box.checked) selected.add(box.dataset.name);
  else selected.delete(box.dataset.name);
  updateBulkBar();
});

async function bulk(action, extra) {
  if (!selected.size) { msg("Ничего не выбрано", true); return; }
  const names = Array.from(selected);
  try {
    const res = await postJSON("/api/items/bulk",
      Object.assign({ names, action }, extra || {}), token());
    msg(`Готово: ${res.applied} предм.`);
    selected.clear();
    await load();
  } catch (e) {
    msg("Ошибка: " + e.message, true);
  }
}

document.getElementById("bulk-bar").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-bulk]");
  if (!btn) return;
  const act = btn.dataset.bulk;
  if (act === "all") {
    visibleNames().forEach((n) => selected.add(n));
    render();
    return;
  }
  if (act === "none") { selected.clear(); render(); return; }
  if (act === "folder") {
    const f = prompt(`Переместить ${selected.size} предм. в папку (пусто = Other):`, "");
    if (f === null) return;
    await bulk("folder", { folder: f.trim() });
    return;
  }
  if (act === "delete") {
    if (!confirm(`Удалить ${selected.size} предм.?\n\nИСТОРИЯ ПРОДАЖ будет удалена безвозвратно.`)) return;
    await bulk("delete");
    return;
  }
  await bulk(act);
});

// -- per-card management actions --------------------------------------------

async function moveToFolder(name, folder) {
  try {
    await postJSON("/api/items/update", { market_hash_name: name, folder }, token());
    msg(`«${name}» → ${folder || OTHER}`);
    await load();
  } catch (e) {
    msg("Ошибка: " + e.message, true);
  }
}

async function doAction(act, name) {
  const it = allItems.find((x) => x.market_hash_name === name);
  if (!it) return;
  try {
    if (act === "toggle-active") {
      await postJSON("/api/items/update", { market_hash_name: name, active: !it.active }, token());
      msg(`«${name}»: ${!it.active ? "возобновлён" : "на паузе"}`);
      await load();
    } else if (act === "toggle-pattern") {
      await postJSON("/api/items/update",
        { market_hash_name: name, pattern_sensitive: !it.pattern_sensitive }, token());
      msg(`«${name}»: паттерн ${!it.pattern_sensitive ? "вкл" : "выкл"}`);
      await load();
    } else if (act === "toggle-hidden") {
      await postJSON("/api/items/update", { market_hash_name: name, hidden: !it.hidden }, token());
      msg(`«${name}»: ${!it.hidden ? "скрыт (парсинг продолжается)" : "показан"}`);
      await load();
    } else if (act === "poll") {
      const r = await postJSON("/api/items/poll", { market_hash_name: name }, token());
      msg(`«${name}»: ${r.note || "поставлен в очередь на опрос"}`);
    } else if (act === "folder") {
      const existing = folderNamesSorted(foldersMap(allItems)).filter((f) => f !== OTHER).join(", ");
      const f = prompt(
        `Папка для «${name}» (пусто = Other).` +
        (existing ? `\nСуществующие: ${existing}` : ""), it.folder || "");
      if (f === null) return;
      await moveToFolder(name, f.trim());
    } else if (act === "delete") {
      if (!confirm(`Удалить «${name}»?\n\nИСТОРИЯ ПРОДАЖ будет удалена безвозвратно.\n` +
                   `Чтобы просто перестать собирать, но сохранить историю — используй «Пауза».`)) {
        return;
      }
      await postJSON("/api/items/delete", { market_hash_name: name, purge_history: true }, token());
      msg(`«${name}» удалён`);
      await load();
    }
  } catch (e) {
    msg("Ошибка: " + e.message, true);
  }
}

document.getElementById("cards").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button.cbtn");
  if (!btn) return;
  ev.preventDefault();
  doAction(btn.dataset.act, btn.dataset.name);
});

// Add one or many items (one market_hash_name per line).
document.getElementById("add-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const raw = document.getElementById("add-name").value;
  const names = raw.split("\n").map((s) => s.trim()).filter(Boolean);
  if (!names.length) { msg("Введи хотя бы один market_hash_name", true); return; }
  let folder = document.getElementById("add-folder").value.trim();
  if (!folder && state.view === "items" && state.folder && state.folder !== OTHER) {
    folder = state.folder;
  }
  const pattern = document.getElementById("add-pattern").checked;
  try {
    const res = await postJSON("/api/items/add_bulk",
      { names, folder, pattern_sensitive: pattern }, token());
    document.getElementById("add-name").value = "";
    msg(`Добавлено: ${res.added}${folder ? " → " + folder : ""} — сборщик подхватит в ~30с`);
    await load();
  } catch (e) {
    msg("Ошибка: " + e.message, true);
  }
});

// Admin token persistence
const tokenInput = document.getElementById("admin-token");
if (tokenInput) {
  tokenInput.value = token();
  tokenInput.addEventListener("input", () => {
    try { localStorage.setItem("csfloat_admin_token", tokenInput.value); } catch (e) {}
  });
}

document.getElementById("manage-toggle").addEventListener("click", () => {
  manageMode = !manageMode;
  if (!manageMode) selected.clear();
  document.getElementById("manage-toggle").classList.toggle("active", manageMode);
  document.getElementById("manage-panel").hidden = !manageMode;
  render();
});

document.getElementById("search").addEventListener("input", render);
["price-min", "price-max"].forEach((id) =>
  document.getElementById(id).addEventListener("input", render));
document.getElementById("sort-by").addEventListener("change", render);
document.getElementById("show-hidden").addEventListener("change", render);

startAutoRefresh(load, 60000);
