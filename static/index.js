// Home page: item cards grouped by folder, search + folder filter, and an
// item-management panel (add / delete / pause / pattern toggle / move folder).

let allItems = [];
let allFolders = [];
let manageMode = false;

const NO_FOLDER = "Без папки";

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
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

// -- card rendering ---------------------------------------------------------

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
      <button class="cbtn" data-act="folder" data-name="${n}">📁 папка</button>
      <button class="cbtn danger" data-act="delete" data-name="${n}">🗑 удалить</button>
    </div>`;
}

function card(it) {
  const div = document.createElement("div");
  div.className = "card" + (it.active ? "" : " paused");
  div.innerHTML = cardMain(it) + (manageMode ? cardControls(it) : "");
  return div;
}

function render() {
  const q = (document.getElementById("search").value || "").trim().toLowerCase();
  const folderSel = document.getElementById("folder-filter").value;
  const wrap = document.getElementById("cards");
  wrap.innerHTML = "";

  if (!allItems.length) {
    wrap.innerHTML = manageMode
      ? '<p class="muted">Список пуст. Добавь предмет формой выше.</p>'
      : '<p class="muted">Нет предметов. Открой «Управление» и добавь предмет.</p>';
    return;
  }

  let items = allItems;
  if (q) items = items.filter((it) => it.market_hash_name.toLowerCase().includes(q));
  if (folderSel) items = items.filter((it) => (it.folder || NO_FOLDER) === folderSel);

  if (!items.length) {
    wrap.innerHTML = '<p class="muted">Ничего не найдено.</p>';
    return;
  }

  // Group by folder.
  const groups = {};
  items.forEach((it) => {
    const f = it.folder || NO_FOLDER;
    (groups[f] = groups[f] || []).push(it);
  });
  const folderNames = Object.keys(groups).sort((a, b) => {
    if (a === NO_FOLDER) return 1;      // "Без папки" last
    if (b === NO_FOLDER) return -1;
    return a.localeCompare(b);
  });

  folderNames.forEach((f) => {
    const h = document.createElement("div");
    h.className = "folder-head";
    h.textContent = `${f} (${groups[f].length})`;
    wrap.appendChild(h);
    const grid = document.createElement("div");
    grid.className = "cards-grid";
    groups[f].forEach((it) => grid.appendChild(card(it)));
    wrap.appendChild(grid);
  });
}

function refreshFolderControls() {
  // Folder filter dropdown
  const sel = document.getElementById("folder-filter");
  const cur = sel.value;
  const opts = ['<option value="">Все папки</option>']
    .concat(allFolders.map((f) => `<option value="${escapeHtml(f)}">${escapeHtml(f)}</option>`));
  // include "Без папки" if some items have no folder
  if (allItems.some((it) => !it.folder)) {
    opts.push(`<option value="${NO_FOLDER}">${NO_FOLDER}</option>`);
  }
  sel.innerHTML = opts.join("");
  sel.value = cur;

  // datalist for the add-form folder input
  const dl = document.getElementById("folder-list");
  if (dl) dl.innerHTML = allFolders.map((f) => `<option value="${escapeHtml(f)}">`).join("");
}

async function load() {
  try {
    const data = await getJSON("/api/items");
    renderStatus(data.last_update);
    allItems = data.items;
    allFolders = data.folders || [];
    refreshFolderControls();
    render();
  } catch (e) {
    document.getElementById("cards").innerHTML =
      `<p class="muted">Ошибка загрузки: ${escapeHtml(e.message)}</p>`;
  }
}

// -- management actions -----------------------------------------------------

async function doAction(act, name) {
  const it = allItems.find((x) => x.market_hash_name === name);
  if (!it) return;
  try {
    if (act === "toggle-active") {
      await postJSON("/api/items/update", { market_hash_name: name, active: !it.active }, token());
      msg(`«${name}»: ${!it.active ? "возобновлён" : "на паузе"}`);
    } else if (act === "toggle-pattern") {
      await postJSON("/api/items/update",
        { market_hash_name: name, pattern_sensitive: !it.pattern_sensitive }, token());
      msg(`«${name}»: паттерн ${!it.pattern_sensitive ? "вкл" : "выкл"}`);
    } else if (act === "folder") {
      const f = prompt(`Папка для «${name}» (пусто = без папки):`, it.folder || "");
      if (f === null) return;
      await postJSON("/api/items/update", { market_hash_name: name, folder: f.trim() }, token());
      msg(`«${name}»: папка обновлена`);
    } else if (act === "delete") {
      if (!confirm(`Удалить «${name}»?\n\nИСТОРИЯ ПРОДАЖ будет удалена безвозвратно.\n` +
                   `Если нужно просто перестать собирать, но сохранить историю — используй «Пауза».`)) {
        return;
      }
      await postJSON("/api/items/delete", { market_hash_name: name, purge_history: true }, token());
      msg(`«${name}» удалён`);
    }
    await load();
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

// Add item
document.getElementById("add-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const name = document.getElementById("add-name").value.trim();
  if (!name) { msg("Введи market_hash_name", true); return; }
  const folder = document.getElementById("add-folder").value.trim();
  const pattern = document.getElementById("add-pattern").checked;
  try {
    await postJSON("/api/items/add",
      { market_hash_name: name, folder, pattern_sensitive: pattern }, token());
    document.getElementById("add-name").value = "";
    msg(`«${name}» добавлен — сборщик подхватит в течение ~30с`);
    await load();
  } catch (e) {
    msg("Ошибка: " + e.message, true);
  }
});

// Save admin token as typed
const tokenInput = document.getElementById("admin-token");
if (tokenInput) {
  tokenInput.value = token();
  tokenInput.addEventListener("input", () => {
    try { localStorage.setItem("csfloat_admin_token", tokenInput.value); } catch (e) {}
  });
}

// Toggle management mode
document.getElementById("manage-toggle").addEventListener("click", () => {
  manageMode = !manageMode;
  document.getElementById("manage-toggle").classList.toggle("active", manageMode);
  document.getElementById("manage-panel").hidden = !manageMode;
  render();
});

document.getElementById("search").addEventListener("input", render);
document.getElementById("folder-filter").addEventListener("change", render);

startAutoRefresh(load, 60000);
