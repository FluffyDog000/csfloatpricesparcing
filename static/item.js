// Item page: aggregate tables with expandable rows + live polling.
// Expanded rows survive refreshes (their keys are tracked and re-opened).

const CFG = JSON.parse(document.getElementById("cfg").textContent);

const state = {
  period: "all",
  bucketSize: CFG.bucket,
  expandedBuckets: new Set(),   // keys: bucket_lo as string, e.g. "0.1500"
  expandedSeeds: new Set(),     // keys: seed as string, e.g. "13" or "(none)"
  iconShown: false,
};

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

function renderBuckets(buckets) {
  const body = document.getElementById("buckets-body");
  body.innerHTML = "";
  if (!buckets.length) {
    body.innerHTML = '<tr><td colspan="6" class="muted">нет данных</td></tr>';
    return;
  }
  const maxAvg = Math.max(...buckets.map((b) => b.avg_price ?? -Infinity));
  const minAvg = Math.min(...buckets.map((b) => b.avg_price ?? Infinity));
  buckets.forEach((b) => {
    const lo = b.bucket.split("-")[0];
    const hiCls = b.avg_price === maxAvg ? "hi-max" : (b.avg_price === minAvg ? "hi-min" : "");
    const cells =
      `<td><span class="caret">▶</span> ${esc(b.bucket)}</td>` +
      `<td class="num">${b.count}</td>` +
      numCell(b.avg_price, hiCls) +
      numCell(b.median_price) +
      numCell(b.min_price) +
      numCell(b.max_price);
    const url = () =>
      `/api/item/bucket_sales?item=${encodeURIComponent(CFG.item)}` +
      `&bucket_lo=${lo}&bucket_size=${state.bucketSize}&period=${state.period}`;
    const [tr, dtr] = makeAggRow(cells, 6, "bucket", lo, url, true, state.expandedBuckets);
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
  seeds.forEach((s) => {
    const key = String(s.paint_seed);
    const hiCls = s.avg_price === maxAvg ? "hi-max" : "";
    const cells =
      `<td><span class="caret">▶</span> ${esc(key)}</td>` +
      `<td class="num">${s.count}</td>` +
      numCell(s.avg_price, hiCls) +
      numCell(s.min_price) +
      numCell(s.max_price);
    const url = () =>
      `/api/item/seed_sales?item=${encodeURIComponent(CFG.item)}` +
      `&seed=${encodeURIComponent(key)}&period=${state.period}`;
    const [tr, dtr] = makeAggRow(cells, 5, "seed", key, url, false, state.expandedSeeds);
    body.appendChild(tr);
    body.appendChild(dtr);
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
    document.getElementById("total").textContent = `всего продаж: ${data.total_sales}`;

    renderBuckets(data.buckets);
    renderSeeds(data.seeds);
  } catch (e) {
    document.getElementById("overall").textContent = "Ошибка: " + e.message;
  }
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

refresh();
setInterval(refresh, 45000);
