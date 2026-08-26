// Item page: aggregate tables with expandable rows, count sort + colour,
// period presets + custom date range, live polling that preserves open rows.

const CFG = JSON.parse(document.getElementById("cfg").textContent);

// --- count colour thresholds (relative to this item's own median count) ----
const COUNT_FAST_MULT = 1.5;   // >= median * this  -> green (много продаж)
const COUNT_SLOW_MULT = 0.5;   // <  median * this  -> red   (мало)
const COUNT_MIN_GROUPS = 4;    // need at least this many groups to colour

const state = {
  period: "all",                 // "7d" | "30d" | "all" | "custom"
  from: null, to: null,          // for custom range (YYYY-MM-DD)
  bucketSize: CFG.bucket,
  expandedBuckets: new Set(),
  expandedSeeds: new Set(),
  iconShown: false,
  data: null,
  sort: { buckets: null, seeds: null },   // null | "asc" | "desc" (by count)
};

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function median(nums) {
  const xs = nums.filter((n) => n !== null && n !== undefined).slice().sort((a, b) => a - b);
  if (!xs.length) return null;
  const mid = Math.floor(xs.length / 2);
  return xs.length % 2 ? xs[mid] : (xs[mid - 1] + xs[mid]) / 2;
}

function countTier(v, med, groupCount) {
  if (med === null || med <= 0 || groupCount < COUNT_MIN_GROUPS) return "";
  if (v >= med * COUNT_FAST_MULT) return "vel-fast";
  if (v < med * COUNT_SLOW_MULT) return "vel-slow";
  return "vel-mid";
}

function applySort(rows, table) {
  const dir = state.sort[table];
  if (!dir) return rows;
  const sorted = rows.slice().sort((a, b) => (a.count ?? 0) - (b.count ?? 0));
  if (dir === "desc") sorted.reverse();
  return sorted;
}

// Query fragment for the current time window (preset or custom range).
function rangeParam() {
  if (state.period === "custom" && (state.from || state.to)) {
    return `from=${state.from || ""}&to=${state.to || ""}`;
  }
  return `period=${state.period === "custom" ? "all" : state.period}`;
}

// -- detail rendering -------------------------------------------------------

function stickersHtml(stickers) {
  if (!stickers || !stickers.length) return "";
  return stickers
    .map((s) => `<span class="sticker">${esc(s.name || s.slot || "?")}</span>`)
    .join("");
}

function detailTable(sales, showSeed) {
  if (!sales.length) return '<div class="loading">нет продаж</div>';
  const head = showSeed
    ? "<tr><th>float</th><th>seed</th><th class='num'>цена</th><th>дата</th><th>стикеры</th></tr>"
    : "<tr><th>float</th><th class='num'>цена</th><th>дата</th><th>стикеры</th></tr>";
  const rows = sales.map((s) => {
    const est = s.sold_at_estimated ? " <span class='est'>≈</span>" : "";
    const f = `<td class="num">${floatFmt(s.float_value)}</td>`;
    const seed = showSeed ? `<td class="num">${s.paint_seed ?? "—"}</td>` : "";
    const price = `<td class="num">${money(s.price)}</td>`;
    const date = `<td>${timeFmt(s.sold_at)}${est}</td>`;
    const st = `<td>${stickersHtml(s.stickers)}</td>`;
    return `<tr>${f}${seed}${price}${date}${st}</tr>`;
  }).join("");
  return `<table class="detail"><thead>${head}</thead><tbody>${rows}</tbody></table>`;
}

async function loadDetail(wrap, url, showSeed) {
  wrap.innerHTML = '<div class="loading">загрузка…</div>';
  try {
    const data = await getJSON(url);
    wrap.innerHTML = detailTable(data.sales, showSeed);
  } catch (e) {
    wrap.innerHTML = `<div class="loading">ошибка: ${esc(e.message)}</div>`;
  }
}

// -- aggregate tables -------------------------------------------------------

function numCell(v, cls) {
  return `<td class="num ${cls || ""}">${v === null || v === undefined ? "—" : Number(v).toFixed(2)}</td>`;
}

function countCell(v, med, groupCount) {
  return `<td class="num vel ${countTier(v, med, groupCount)}">${v}</td>`;
}

function makeAggRow(cells, colspan, key, detailUrl, showSeed, openSet) {
  const tr = document.createElement("tr");
  tr.className = "agg-row";
  tr.dataset.key = key;
  tr.innerHTML = cells;

  const detailTr = document.createElement("tr");
  detailTr.className = "detail-row";
  detailTr.hidden = true;
  const wrap = document.createElement("div");
  wrap.className = "detail-wrap";
  const td = document.createElement("td");
  td.colSpan = colspan;
  td.appendChild(wrap);
  detailTr.appendChild(td);

  function toggle() {
    const isOpen = tr.classList.toggle("open");
    detailTr.hidden = !isOpen;
    if (isOpen) {
      openSet.add(key);
      loadDetail(wrap, detailUrl(), showSeed);
    } else {
      openSet.delete(key);
    }
  }
  tr.addEventListener("click", toggle);

  if (openSet.has(key)) {
    tr.classList.add("open");
    detailTr.hidden = false;
    loadDetail(wrap, detailUrl(), showSeed);
  }
  return [tr, detailTr];
}

function renderBuckets(buckets) {
  const body = document.getElementById("buckets-body");
  body.innerHTML = "";
  if (!buckets.length) {
    body.innerHTML = '<tr><td colspan="6" class="muted">нет данных</td></tr>';
    return;
  }
  const maxAvg = Math.max(...buckets.map((b) => b.avg_price ?? -Infinity));
  const minAvg = Math.min(...buckets.map((b) => b.avg_price ?? Infinity));
  const medCount = median(buckets.map((b) => b.count));
  applySort(buckets, "buckets").forEach((b) => {
    const lo = b.bucket.split("-")[0];
    const hiCls = b.avg_price === maxAvg ? "hi-max" : (b.avg_price === minAvg ? "hi-min" : "");
    const cells =
      `<td><span class="caret">▶</span> ${esc(b.bucket)}</td>` +
      countCell(b.count, medCount, buckets.length) +
      numCell(b.avg_price, hiCls) +
      numCell(b.median_price) +
      numCell(b.min_price) +
      numCell(b.max_price);
    const url = () =>
      `/api/item/bucket_sales?item=${encodeURIComponent(CFG.item)}` +
      `&bucket_lo=${lo}&bucket_size=${state.bucketSize}&${rangeParam()}`;
    const [tr, dtr] = makeAggRow(cells, 6, "b:" + lo, url, true, state.expandedBuckets);
    body.appendChild(tr);
    body.appendChild(dtr);
  });
}

function renderSeeds(seeds) {
  const body = document.getElementById("seeds-body");
  body.innerHTML = "";
  if (!seeds.length) {
    body.innerHTML = '<tr><td colspan="5" class="muted">нет данных</td></tr>';
    return;
  }
  const maxAvg = Math.max(...seeds.map((s) => s.avg_price ?? -Infinity));
  const medCount = median(seeds.map((s) => s.count));
  applySort(seeds, "seeds").forEach((s) => {
    const key = String(s.paint_seed);
    const hiCls = s.avg_price === maxAvg ? "hi-max" : "";
    const cells =
      `<td><span class="caret">▶</span> ${esc(key)}</td>` +
      countCell(s.count, medCount, seeds.length) +
      numCell(s.avg_price, hiCls) +
      numCell(s.min_price) +
      numCell(s.max_price);
    const url = () =>
      `/api/item/seed_sales?item=${encodeURIComponent(CFG.item)}` +
      `&seed=${encodeURIComponent(key)}&${rangeParam()}`;
    const [tr, dtr] = makeAggRow(cells, 5, "s:" + key, url, false, state.expandedSeeds);
    body.appendChild(tr);
    body.appendChild(dtr);
  });
}

function updateSortIndicators() {
  document.querySelectorAll("th.sortable").forEach((th) => {
    const dir = state.sort[th.dataset.table];
    const ind = th.querySelector(".sort-ind");
    if (ind) ind.textContent = dir === "desc" ? "▼" : dir === "asc" ? "▲" : "";
    th.classList.toggle("sorted", !!dir);
  });
}

// -- top-level refresh ------------------------------------------------------

async function refresh() {
  const url =
    `/api/item/aggregates?item=${encodeURIComponent(CFG.item)}` +
    `&bucket=${state.bucketSize}&${rangeParam()}`;
  try {
    const data = await getJSON(url);
    renderStatus(data.last_update);

    if (!state.iconShown && data.icon_url) {
      const img = document.getElementById("item-icon");
      img.src = data.icon_url;
      img.hidden = false;
      state.iconShown = true;
    }

    const ov = data.overall;
    document.getElementById("overall").innerHTML = data.total_sales
      ? `avg <b>${money(ov.avg_price)}</b> · median <b>${money(ov.median_price)}</b> · ` +
        `min ${money(ov.min_price)} · max ${money(ov.max_price)}`
      : "нет продаж за период";
    document.getElementById("total").textContent = `всего продаж: ${data.total_sales}`;

    state.data = data;
    renderBuckets(data.buckets);
    renderSeeds(data.seeds);
    updateSortIndicators();
  } catch (e) {
    document.getElementById("overall").textContent = "Ошибка: " + e.message;
  }
  refreshLatest();
}

// Latest sales side panel (like CSFloat's "Latest Sales").
async function refreshLatest() {
  try {
    const data = await getJSON(
      `/api/item/latest_sales?item=${encodeURIComponent(CFG.item)}&limit=40&${rangeParam()}`
    );
    const body = document.getElementById("latest-body");
    const cnt = document.getElementById("latest-count");
    if (cnt) cnt.textContent = data.sales.length ? `(${data.sales.length})` : "";
    if (!data.sales.length) {
      body.innerHTML = '<tr><td colspan="4" class="muted">нет продаж</td></tr>';
      return;
    }
    body.innerHTML = data.sales.map((s) => {
      const est = s.sold_at_estimated ? " <span class='est'>≈</span>" : "";
      return `<tr>
        <td>${timeFmt(s.sold_at)}${est}</td>
        <td class="num">${floatFmt(s.float_value)}</td>
        <td class="num">${money(s.price)}</td>
        <td class="num">${s.paint_seed ?? "—"}</td></tr>`;
    }).join("");
  } catch (e) {
    /* leave previous content on transient error */
  }
}

function rerender() {
  if (!state.data) return;
  renderBuckets(state.data.buckets);
  renderSeeds(state.data.seeds);
  updateSortIndicators();
}

// Period switcher (presets + custom range toggle)
document.getElementById("periods").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button");
  if (!btn) return;
  state.period = btn.dataset.period;
  document.querySelectorAll("#periods button").forEach((b) =>
    b.classList.toggle("active", b === btn));
  document.getElementById("daterange").hidden = state.period !== "custom";
  if (state.period !== "custom") refresh();
});

document.getElementById("apply-range").addEventListener("click", () => {
  state.from = document.getElementById("date-from").value || null;
  state.to = document.getElementById("date-to").value || null;
  refresh();
});

// Count column sort: click cycles desc -> asc -> off, per table.
document.querySelectorAll("th.sortable").forEach((th) => {
  th.addEventListener("click", () => {
    const t = th.dataset.table;
    state.sort[t] = state.sort[t] === "desc" ? "asc"
                  : state.sort[t] === "asc" ? null : "desc";
    rerender();
  });
});

// Copy the item name to the clipboard.
const copyBtn = document.getElementById("copy-name");
if (copyBtn) {
  copyBtn.addEventListener("click", async () => {
    const ok = await copyText(CFG.item);
    copyBtn.textContent = ok ? "✔" : "✕";
    copyBtn.classList.toggle("copied", ok);
    setTimeout(() => {
      copyBtn.textContent = "📋";
      copyBtn.classList.remove("copied");
    }, 1200);
  });
}

// Paint seeds section collapse when the item is not pattern-sensitive.
let seedsCollapsed = CFG.pattern_sensitive === false;
function applySeedsCollapse() {
  document.getElementById("seeds-wrap").hidden = seedsCollapsed;
  document.getElementById("seeds-ind").textContent = seedsCollapsed ? "▶" : "▼";
  const hint = document.getElementById("seeds-hint");
  if (hint) hint.hidden = !seedsCollapsed;
}
document.getElementById("seeds-toggle").addEventListener("click", () => {
  seedsCollapsed = !seedsCollapsed;
  applySeedsCollapse();
});
applySeedsCollapse();

startAutoRefresh(refresh, 45000);

// -- poll this item now -----------------------------------------------------

const pollBtn = document.getElementById("poll-now");
if (pollBtn) {
  pollBtn.addEventListener("click", async () => {
    const el = document.getElementById("poll-msg");
    const token = (() => {
      try { return localStorage.getItem("csfloat_admin_token") || ""; }
      catch (e) { return ""; }
    })();
    pollBtn.disabled = true;
    el.textContent = "ставлю в очередь…";
    el.className = "poll-msg";
    try {
      const r = await postJSON("/api/items/poll", { market_hash_name: CFG.item }, token);
      el.textContent = r.note || "Поставлено в очередь.";
      // The collector picks the request up within ~5s; give it a moment, then
      // reload so the new sales actually show up.
      if (!(r.waiting || []).length) setTimeout(() => location.reload(), 9000);
    } catch (e) {
      el.textContent = "Ошибка: " + e.message;
      el.className = "poll-msg err";
    } finally {
      setTimeout(() => { pollBtn.disabled = false; }, 3000);
    }
  });
}

// Currency switch: re-render every table with the new formatting.
// The overall line, both tables and the latest-sales panel all show money.
initCurrencyToggle("currency-toggle", () => { refresh(); refreshLatest(); });
