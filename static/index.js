// Home page: Explorer-style folder navigation.
//  * default view = grid of folders; click a folder to open its items
//  * items with no folder live in "Other"
//  * management mode: add item, and per-card pause / pattern / move / delete
//  * search shows matching items across all folders

const OTHER = "Other";

let allItems = [];
let allFolders = [];
let manageMode = false;

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

// -- folder + item structures ----------------------------------------------

function foldersMap() {
  const map = {};
  allItems.forEach((it) => {
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

function renderFolders() {
  const wrap = document.getElementById("cards");
  const map = foldersMap();
  const names = folderNamesSorted(map);
  wrap.innerHTML = "";

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
    // Allow dropping a dragged item card onto a folder tile to move it.
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
  return `
    <a class="card-main" href="/item/${encodeURIComponent(it.market_hash_name)}">
      ${img}
      <div>
        <div class="name">${escapeHtml(it.market_hash_name)} ${inactive} ${pat}</div>
        <div class="meta">
          продаж: <b>${it.total_sales}</b><br>
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
      <button class="cbtn" data-act="folder" data-name="${n}">📁 переместить</button>
      <button class="cbtn danger" data-act="delete" data-name="${n}">🗑 удалить</button>
    </div>`;
}

function card(it) {
  const div = document.createElement("div");
  div.className = "card" + (it.active ? "" : " paused");
  div.innerHTML = cardMain(it) + (manageMode ? cardControls(it) : "");
  // Draggable so it can be dropped onto a folder tile (from the items view
  // header breadcrumb the folders are one click away, but drag also works when
  // searching).
  div.draggable = true;
  div.addEventListener("dragstart", (e) =>
    e.dataTransfer.setData("text/plain", it.market_hash_name));
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

function renderItemsView() {
  const map = foldersMap();
  const items = map[state.folder] || [];
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
    return;
  }

  if (q) {
    const items = allItems.filter((it) => it.market_hash_name.toLowerCase().includes(q));
    renderItemList(items, `<div class="items-head"><span class="crumb">🔎 Поиск: «${escapeHtml(q)}» (${items.length})</span></div>`);
    return;
  }

  if (state.view === "items" && state.folder) {
    renderItemsView();
  } else {
    state.view = "folders";
    renderFolders();
  }
}

function refreshFolderDatalist() {
  const dl = document.getElementById("folder-list");
  if (dl) dl.innerHTML = folderNamesSorted(foldersMap())
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

// -- management actions -----------------------------------------------------

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
    } else if (act === "folder") {
      const existing = folderNamesSorted(foldersMap()).filter((f) => f !== OTHER).join(", ");
      const cur = it.folder || "";
      const f = prompt(
        `Папка для «${name}» (пусто = Other).` +
        (existing ? `\nСуществующие: ${existing}` : ""), cur);
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

// Add item — respects the current folder when opened inside a folder.
document.getElementById("add-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const name = document.getElementById("add-name").value.trim();
  if (!name) { msg("Введи market_hash_name", true); return; }
  let folder = document.getElementById("add-folder").value.trim();
  if (!folder && state.view === "items" && state.folder && state.folder !== OTHER) {
    folder = state.folder;  // default new item into the folder you're viewing
  }
  const pattern = document.getElementById("add-pattern").checked;
  try {
    await postJSON("/api/items/add",
      { market_hash_name: name, folder, pattern_sensitive: pattern }, token());
    document.getElementById("add-name").value = "";
    msg(`«${name}» добавлен${folder ? " → " + folder : ""} — сборщик подхватит в ~30с`);
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
  document.getElementById("manage-toggle").classList.toggle("active", manageMode);
  document.getElementById("manage-panel").hidden = !manageMode;
  render();
});

document.getElementById("search").addEventListener("input", render);

startAutoRefresh(load, 60000);
