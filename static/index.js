// Home page: Explorer-style folder navigation.
//  * default view = grid of folders; click a folder to open its items
//  * items with no folder live in "Other"
//  * combinable filters (type, wear, rarity, collection, sales in a period,
//    price, ...) and sorting; any filter shows one list across all folders
//  * hidden items are collapsed away by default
//  * management mode: add items, per-card actions, and bulk selection

const OTHER = "Other";

let allItems = [];
let allFolders = [];
let manageMode = false;
const selected = new Set();          // market_hash_names picked for bulk actions

// view: "folders" | "items"; flat: a filtered list across every folder
const state = { view: "folders", folder: null, flat: false };

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
//
// Groups of chips, combined: inside a group the ticked values add up ("FT or
// MW"), across groups they narrow ("FT or MW, and Covert"). With thousands of
// items the list is only usable if any mix of these can be asked at once.

const FILTER_KEY = "items_filters_v1";
const GROUPS = ["category", "wear", "rarity", "special", "status", "folder",
                "collection"];
const PAGE = 120;                    // cards drawn per "показать ещё"

// Every group reads one value off an item. "none" stands for "the item has
// nothing here", so it can be picked like any other value.
const VALUE = {
  category: (it) => it.category || "other",
  wear: (it) => it.wear || "none",
  rarity: (it) => (it.rarity === null || it.rarity === undefined
    ? "none" : String(it.rarity)),
  special: (it) => (it.stattrak ? "st" : it.souvenir ? "sv" : "plain"),
  status: (it) => (it.active ? "active" : "paused"),
  folder: (it) => folderOf(it),
  collection: (it) => it.collection || "none",
};

const RARITY_COLOR = {
  1: "#8a9bb0", 2: "#5e98d9", 3: "#4b69ff", 4: "#8847ff",
  5: "#d32ce6", 6: "#eb4b4b", 7: "#e4ae39",
};

let labels = { categories: {}, wears: [] };
let limit = PAGE;
let currentList = [];               // what "выбрать всё" selects

const filters = loadFilters();

function emptyFilters() {
  const out = { sets: {} };
  GROUPS.forEach((g) => { out.sets[g] = new Set(); });
  return out;
}

function loadFilters() {
  const out = emptyFilters();
  try {
    const raw = JSON.parse(localStorage.getItem(FILTER_KEY) || "{}");
    GROUPS.forEach((g) => { out.sets[g] = new Set(raw[g] || []); });
    ["salesMin", "salesMax", "period", "lastDays", "priceMin", "priceMax",
     "sort"].forEach((k) => { if (raw[k] !== undefined) out[k] = raw[k]; });
  } catch (e) { /* a fresh start is fine */ }
  return out;
}

function saveFilters() {
  const raw = {};
  GROUPS.forEach((g) => { raw[g] = Array.from(filters.sets[g]); });
  raw.salesMin = val("sales-min");
  raw.salesMax = val("sales-max");
  raw.period = document.getElementById("sales-period").value;
  raw.lastDays = val("last-days");
  raw.priceMin = val("price-min");
  raw.priceMax = val("price-max");
  raw.sort = document.getElementById("sort-by").value;
  try { localStorage.setItem(FILTER_KEY, JSON.stringify(raw)); } catch (e) {}
}

function val(id) { return (document.getElementById(id).value || "").trim(); }

function restoreInputs() {
  const put = (id, v) => { if (v !== undefined && v !== null) document.getElementById(id).value = v; };
  put("sales-min", filters.salesMin);
  put("sales-max", filters.salesMax);
  put("sales-period", filters.period);
  put("last-days", filters.lastDays);
  put("price-min", filters.priceMin);
  put("price-max", filters.priceMax);
  put("sort-by", filters.sort);
}

function showHidden() {
  return document.getElementById("show-hidden").checked;
}

function numOrNull(id) {
  const v = val(id);
  if (!v) return null;
  const n = Number(v);
  return isNaN(n) ? null : n;
}

function period() {
  return document.getElementById("sales-period").value || "sales_7d";
}

function daysSince(iso) {
  if (!iso) return Infinity;
  const t = Date.parse(iso);
  return isNaN(t) ? Infinity : (Date.now() - t) / 86400000;
}

// The number filters: price, sales in the period, age of the last sale, and
// whether hidden items count at all. Search is applied here too, so the chip
// counts answer for what is actually on screen.
function numericPass(it, q) {
  if (!showHidden() && it.hidden) return false;
  if (q && !it.market_hash_name.toLowerCase().includes(q)) return false;
  const lo = numOrNull("price-min");
  const hi = numOrNull("price-max");
  if (lo !== null && !(it.price !== null && it.price >= lo)) return false;
  if (hi !== null && !(it.price !== null && it.price <= hi)) return false;
  const smin = numOrNull("sales-min");
  const smax = numOrNull("sales-max");
  const n = it[period()] || 0;
  if (smin !== null && n < smin) return false;
  if (smax !== null && n > smax) return false;
  const days = numOrNull("last-days");
  if (days !== null && daysSince(it.last_sold_at) > days) return false;
  return true;
}

function groupPass(it, skip) {
  return GROUPS.every((g) => g === skip || !filters.sets[g].size
    || filters.sets[g].has(VALUE[g](it)));
}

function activeFilterCount() {
  let n = GROUPS.filter((g) => filters.sets[g].size).length;
  ["sales-min", "sales-max", "last-days", "price-min", "price-max"]
    .forEach((id) => { if (val(id)) n += 1; });
  return n;
}

function filteredItems(list, q) {
  return list.filter((it) => numericPass(it, q) && groupPass(it));
}

// Sales counts sort on several windows: the all-time total mostly reflects when
// an item was added, so a recent window is the honest measure of how briskly it
// actually trades.
const SALES_FIELD = {
  "sales1": "sales_1d", "sales7": "sales_7d", "sales30": "sales_30d",
  "sales90": "sales_90d", "sales": "total_sales",
};

function sortItems(list) {
  const by = document.getElementById("sort-by").value;
  const arr = list.slice();
  const [kind, dir] = by.split("-");
  const sign = dir === "asc" ? 1 : -1;
  const byName = (a, b) => a.market_hash_name.localeCompare(b.market_hash_name);
  let key = null;
  if (kind === "price") key = (x) => (x.price === null || x.price === undefined ? null : x.price);
  else if (kind === "period") key = (x) => x[period()] || 0;
  else if (SALES_FIELD[kind]) key = (x) => x[SALES_FIELD[kind]] || 0;
  else if (kind === "last") key = (x) => (x.last_sold_at ? Date.parse(x.last_sold_at) : null);
  else if (kind === "rarity") key = (x) => (x.rarity === null || x.rarity === undefined ? null : x.rarity);
  if (!key) { arr.sort(byName); return arr; }
  // Items with no value sink to the end whichever way the sort runs; ties are
  // broken by name so the order is stable between refreshes.
  arr.sort((a, b) => {
    const ka = key(a); const kb = key(b);
    if (ka === null && kb === null) return byName(a, b);
    if (ka === null) return 1;
    if (kb === null) return -1;
    return (ka - kb) * sign || byName(a, b);
  });
  return arr;
}

function updateFilterInfo(shown, total) {
  const el = document.getElementById("filter-info");
  const hiddenCount = allItems.filter((i) => i.hidden).length;
  const parts = [];
  if (shown !== total) parts.push(`показано ${shown} из ${total}`);
  if (hiddenCount && !showHidden()) parts.push(`скрыто: ${hiddenCount}`);
  el.textContent = parts.join(" · ");
  const n = activeFilterCount();
  document.getElementById("filters-count").textContent = n ? `(${n})` : "";
  document.getElementById("filters-reset").hidden = !n;
}

// -- the chips -------------------------------------------------------------

function chipLabel(group, value, sample) {
  if (group === "category") return labels.categories[value] || value;
  if (group === "wear") {
    if (value === "none") return "без износа";
    const w = labels.wears.find(([code]) => code === value);
    return w ? `${value} · ${w[1]}` : value;
  }
  if (group === "rarity") {
    if (value === "none") return "нет данных";
    const color = RARITY_COLOR[value] || "#888";
    return `<span class="dot" style="background:${color}"></span>`
      + escapeHtml((sample && sample.rarity_name) || value);
  }
  if (group === "special") {
    return { st: "StatTrak™", sv: "Souvenir", plain: "обычные" }[value] || value;
  }
  if (group === "status") return value === "active" ? "собирается" : "на паузе";
  if (group === "collection" && value === "none") return "нет данных";
  return escapeHtml(value);
}

function chipOrder(group, values) {
  const rank = {
    category: labels.catOrder || [],
    wear: ["FN", "MW", "FT", "WW", "BS", "none"],
    special: ["plain", "st", "sv"],
    status: ["active", "paused"],
  }[group];
  if (rank) return values.sort((a, b) => rank.indexOf(a) - rank.indexOf(b));
  if (group === "rarity") {
    return values.sort((a, b) => (a === "none") - (b === "none") || Number(a) - Number(b));
  }
  if (group === "folder") return values.sort((a, b) => (a === OTHER) - (b === OTHER) || a.localeCompare(b));
  return values.sort((a, b) => (a === "none") - (b === "none") || a.localeCompare(b));
}

// Counts are faceted: each chip says how many items would show if it were
// ticked, with every other group's choice still applied. Zero is shown dimmed
// rather than dropped, so a choice already made never disappears.
function renderChips(base) {
  const panel = document.getElementById("filter-panel");
  if (panel.hidden) return;
  const collQ = val("collection-search").toLowerCase();
  GROUPS.forEach((g) => {
    const body = document.querySelector(`.fgroup[data-group="${g}"] .chips`);
    if (!body) return;
    const counts = {};
    const sample = {};
    allItems.forEach((it) => {
      const v = VALUE[g](it);
      if (!(v in counts)) { counts[v] = 0; sample[v] = it; }
    });
    base.forEach((it) => {
      if (!groupPass(it, g)) return;
      counts[VALUE[g](it)] += 1;
    });
    filters.sets[g].forEach((v) => { if (!(v in counts)) counts[v] = 0; });
    let values = chipOrder(g, Object.keys(counts));
    if (g === "collection" && collQ) {
      values = values.filter((v) => filters.sets[g].has(v)
        || v.toLowerCase().includes(collQ));
    }
    body.innerHTML = values.map((v) => {
      const on = filters.sets[g].has(v);
      const cls = "chip" + (on ? " on" : "") + (!counts[v] && !on ? " empty" : "");
      return `<button type="button" class="${cls}" data-group="${g}"
        data-value="${escapeHtml(v)}">${chipLabel(g, v, sample[v])}<span class="n">${counts[v]}</span></button>`;
    }).join("") || '<span class="muted">—</span>';
  });
}

document.getElementById("filter-panel").addEventListener("click", (ev) => {
  const chip = ev.target.closest(".chip");
  if (!chip) return;
  const set = filters.sets[chip.dataset.group];
  const v = chip.dataset.value;
  if (set.has(v)) set.delete(v); else set.add(v);
  onFilterChange();
});

function onFilterChange() {
  limit = PAGE;
  saveFilters();
  render();
}

function resetFilters() {
  GROUPS.forEach((g) => filters.sets[g].clear());
  ["sales-min", "sales-max", "last-days", "price-min", "price-max",
   "collection-search"].forEach((id) => { document.getElementById(id).value = ""; });
  onFilterChange();
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
  const rar = it.rarity_name
    ? `<span class="rar" style="background:${RARITY_COLOR[it.rarity] || "#888"}">${escapeHtml(it.rarity_name)}</span>`
    : "";
  // Where it is filed and what set it comes from - in a list drawn across
  // folders, the folder is no longer implied by the page.
  const sub = [it.collection, state.flat ? "📁 " + folderOf(it) : null]
    .filter(Boolean).map(escapeHtml).join(" · ");
  return `
    <a class="card-main" href="/item/${encodeURIComponent(it.market_hash_name)}">
      ${img}
      <div>
        <div class="name">${escapeHtml(it.market_hash_name)} ${inactive} ${pat} ${hid}</div>
        ${rar || sub ? `<div class="sub">${rar}${sub}</div>` : ""}
        <div class="meta">
          продаж: <b>${it.total_sales}</b>
          <span class="muted">· 24ч: ${it.sales_1d ?? 0} · 7д: ${it.sales_7d ?? 0} · 30д: ${it.sales_30d ?? 0}</span><br>
          30д: <b>${money(it.price)}</b> &nbsp; avg: ${money(it.avg_price)}
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
  items.slice(0, limit).forEach((it) => grid.appendChild(card(it)));
  wrap.appendChild(grid);
  // Thousands of cards with images at once is a page that freezes; the rest
  // are a button away, and "выбрать всё" still takes all of them.
  if (items.length > limit) {
    const more = document.createElement("button");
    more.className = "btn more-btn";
    more.textContent = `показать ещё (${Math.min(PAGE, items.length - limit)} из ${items.length - limit})`;
    more.addEventListener("click", () => { limit += PAGE; render(); });
    wrap.appendChild(more);
  }
}

function renderItemsView(list) {
  const map = foldersMap(list);
  const items = sortItems(map[state.folder] || []);
  currentList = items;
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

  // Search and number filters first; the chips count against this.
  const base = allItems.filter((it) => numericPass(it, q));
  const matched = base.filter((it) => groupPass(it));
  updateFilterInfo(matched.length, allItems.length);
  renderChips(base);

  // Any filter or a search: one list across every folder, since the folder is
  // now just another thing to filter on. Nothing asked: the folders, as before.
  state.flat = !!q || activeFilterCount() > 0;
  if (state.flat) {
    const items = sortItems(matched);
    currentList = items;
    const title = q ? `🔎 «${escapeHtml(q)}»` : "Отобрано";
    renderItemList(items,
      `<div class="items-head"><span class="crumb">${title} (${items.length})</span></div>`);
  } else if (state.view === "items" && state.folder) {
    renderItemsView(matched);
  } else {
    state.view = "folders";
    currentList = [];
    renderFolders(matched);
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
    const cats = data.categories || [];
    labels = { categories: Object.fromEntries(cats), catOrder: cats.map(([k]) => k),
               wears: data.wears || [] };
    allFolders = data.folders || [];
    refreshFolderDatalist();
    render();
  } catch (e) {
    document.getElementById("cards").innerHTML =
      `<p class="muted">Ошибка загрузки: ${escapeHtml(e.message)}</p>`;
  }
}

// -- bulk selection ---------------------------------------------------------

// Everything the current list holds, drawn or not: with a filter of two
// thousand items, "select all" meaning "the first 120" would be a trap.
function visibleNames() {
  return currentList.map((it) => it.market_hash_name);
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

document.getElementById("search").addEventListener("input", () => { limit = PAGE; render(); });
["price-min", "price-max", "sales-min", "sales-max", "last-days"].forEach((id) =>
  document.getElementById(id).addEventListener("input", onFilterChange));
["sort-by", "sales-period"].forEach((id) =>
  document.getElementById(id).addEventListener("change", onFilterChange));
document.getElementById("show-hidden").addEventListener("change", onFilterChange);
document.getElementById("collection-search").addEventListener("input", render);
document.getElementById("filters-reset").addEventListener("click", resetFilters);
document.getElementById("filters-toggle").addEventListener("click", () => {
  const panel = document.getElementById("filter-panel");
  panel.hidden = !panel.hidden;
  document.getElementById("filters-toggle").classList.toggle("active", !panel.hidden);
  try { localStorage.setItem("items_filters_open", panel.hidden ? "0" : "1"); } catch (e) {}
  render();
});
try {
  if (localStorage.getItem("items_filters_open") === "1") {
    document.getElementById("filter-panel").hidden = false;
    document.getElementById("filters-toggle").classList.add("active");
  }
} catch (e) { /* closed is fine */ }
restoreInputs();

startAutoRefresh(load, 60000);

initCurrencyToggle("currency-toggle", render);
