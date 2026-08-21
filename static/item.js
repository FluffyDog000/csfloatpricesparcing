// Item page: aggregate tables with expandable rows + live polling.
// Expanded rows survive refreshes (their keys are tracked and re-opened).

const CFG = JSON.parse(document.getElementById("cfg").textContent);

// --- velocity colour thresholds (relative to this item's own median) -------
// Tweak these to change how aggressively fast/slow groups are highlighted.
const VELOCITY_FAST_MULT = 1.5;   // >= median * this  -> green (fast)
const VELOCITY_SLOW_MULT = 0.5;   // <  median * this  -> red   (slow)
const VELOCITY_MIN_GROUPS = 4;    // need at least this many groups to colour

const state = {
  period: "all",
  bucketSize: CFG.bucket,
  expandedBuckets: new Set(),   // keys: bucket_lo as string, e.g. "0.1500"
  expandedSeeds: new Set(),     // keys: seed as string, e.g. "13" or "(none)"
  iconShown: false,
  data: null,                   // last aggregates payload (for re-render on sort)
  sort: { buckets: null, seeds: null },  // null | "asc" | "desc" (by velocity)
};

function median(nums) {
  const xs = nums.filter((n) => n !== null && n !== undefined).slice().sort((a, b) => a - b);
  if (!xs.length) return null;
  const mid = Math.floor(xs.length / 2);
  return xs.length % 2 ? xs[mid] : (xs[mid - 1] + xs[mid]) / 2;
}

// Colour class for a velocity value, relative to the item's own median.
function velocityTier(v, med, groupCount) {
  if (med === null || med <= 0 || groupCount < VELOCITY_MIN_GROUPS) return "";
  if (v >= med * VELOCITY_FAST_MULT) return "vel-fast";
  if (v < med * VELOCITY_SLOW_MULT) return "vel-slow";
  return "vel-mid";
}

// Apply the current velocity sort (if any) to a copy of the rows.
function applySort(rows, table) {
  const dir = state.sort[table];
  if (!dir) return rows;
  const sorted = rows.slice().sort((a, b) => (a.velocity ?? 0) - (b.velocity ?? 0));
  if (dir === "desc") sorted.reverse();
  return sorted;
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
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

function makeAggRow(cells, colspan, kind, key, detailUrl, showSeed, openSet) {
  const tr = document.createElement("tr");
  tr.className = "agg-row";
  tr.dataset.kind = kind;
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

  // Restore previously-open state (and refresh its data).
  if (openSet.has(key)) {
    tr.classList.add("open");
    detailTr.hidden = false;
    loadDetail(wrap, detailUrl(), showSeed);
  }
  return [tr, detailTr];
}

function velCell(v, med, groupCount) {
  const cls = velocityTier(v, med, groupCount);
  const shown = v === null || v === undefined ? "—" : Number(v).toFixed(2);
  return `<td class="num vel ${cls}">${shown}</td>`;
}

function renderBuckets(buckets) {
  const body = document.getElementById("buckets-body");
  body.innerHTML = "";
  if (!buckets.length) {
    body.innerHTML = '<tr><td colspan="7" class="muted">нет данных</td></tr>';
    return;
  }
  const maxAvg = Math.max(...buckets.map((b) => b.avg_price ?? -Infinity));
  const minAvg = Math.min(...buckets.map((b) => b.avg_price ?? Infinity));
  const med = median(buckets.map((b) => b.velocity));
  applySort(buckets, "buckets").forEach((b) => {
    const lo = b.bucket.split("-")[0];
    const hiCls = b.avg_price === maxAvg ? "hi-max" : (b.avg_price === minAvg ? "hi-min" : "");
    const cells =
      `<td><span class="caret">▶</span> ${esc(b.bucket)}</td>` +
      `<td class="num">${b.count}</td>` +
      velCell(b.velocity, med, buckets.length) +
      numCell(b.avg_price, hiCls) +
      numCell(b.median_price) +
      numCell(b.min_price) +
      numCell(b.max_price);
    const url = () =>
      `/api/item/bucket_sales?item=${encodeURIComponent(CFG.item)}` +
      `&bucket_lo=${lo}&bucket_size=${state.bucketSize}&period=${state.period}`;
    const [tr, dtr] = makeAggRow(cells, 7, "bucket", lo, url, true, state.expandedBuckets);
    body.appendChild(tr);
    body.appendChild(dtr);
  });
}

function renderSeeds(seeds) {
  const body = document.getElementById("seeds-body");
  body.innerHTML = "";
  if (!seeds.length) {
    body.innerHTML = '<tr><td colspan="6" class="muted">нет данных</td></tr>';
    return;
  }
  const maxAvg = Math.max(...seeds.map((s) => s.avg_price ?? -Infinity));
  const med = median(seeds.map((s) => s.velocity));
  applySort(seeds, "seeds").forEach((s) => {
    const key = String(s.paint_seed);
    const hiCls = s.avg_price === maxAvg ? "hi-max" : "";
    const cells =
      `<td><span class="caret">▶</span> ${esc(key)}</td>` +
      `<td class="num">${s.count}</td>` +
      velCell(s.velocity, med, seeds.length) +
      numCell(s.avg_price, hiCls) +
      numCell(s.min_price) +
      numCell(s.max_price);
    const url = () =>
      `/api/item/seed_sales?item=${encodeURIComponent(CFG.item)}` +
      `&seed=${encodeURIComponent(key)}&period=${state.period}`;
    const [tr, dtr] = makeAggRow(cells, 6, "seed", key, url, false, state.expandedSeeds);
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
    `&period=${state.period}&bucket=${state.bucketSize}`;
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
    const days = data.period_days;
    document.getElementById("total").textContent =
      `всего продаж: ${data.total_sales}` +
      (days ? ` · норм. на ${Number(days).toFixed(days < 10 ? 1 : 0)} дн.` : "");

    state.data = data;
    renderBuckets(data.buckets);
    renderSeeds(data.seeds);
    updateSortIndicators();
  } catch (e) {
    document.getElementById("overall").textContent = "Ошибка: " + e.message;
  }
}

// Re-render tables from cached data (used after a sort toggle, no refetch).
function rerender() {
  if (!state.data) return;
  renderBuckets(state.data.buckets);
  renderSeeds(state.data.seeds);
  updateSortIndicators();
}

// Period switcher
document.getElementById("periods").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button");
  if (!btn) return;
  state.period = btn.dataset.period;
  document.querySelectorAll("#periods button").forEach((b) =>
    b.classList.toggle("active", b === btn));
  refresh();
});

// Velocity column sort: click cycles desc -> asc -> off, per table.
document.querySelectorAll("th.sortable").forEach((th) => {
  th.addEventListener("click", () => {
    const t = th.dataset.table;
    state.sort[t] = state.sort[t] === "desc" ? "asc"
                  : state.sort[t] === "asc" ? null : "desc";
    rerender();
  });
});

startAutoRefresh(refresh, 45000);
