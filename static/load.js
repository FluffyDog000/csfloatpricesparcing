// Load / health page: request-rate estimate, poll stats, gap warnings, log.

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function tile(label, value, sub, cls) {
  return `<div class="tile ${cls || ""}">
    <div class="tile-val">${value}</div>
    <div class="tile-label">${esc(label)}</div>
    ${sub ? `<div class="tile-sub">${esc(sub)}</div>` : ""}
  </div>`;
}

function renderTiles(d) {
  const rlHour = d.stats_hour.rate_limited;
  // The % is only a theoretical cap (spacing-based). Real 429s override it.
  const budgetCls = rlHour ? "bad"
    : d.budget_used_pct == null ? ""
    : d.budget_used_pct >= 80 ? "bad" : d.budget_used_pct >= 50 ? "warn" : "good";
  const authHour = d.stats_hour.auth_error;
  document.getElementById("tiles").innerHTML =
    tile("активных предметов", d.active_items, `всего в базе: ${d.total_items}`) +
    tile("запросов/мин (оценка)", d.reqs_per_min_est,
         `лимит ~${d.budget_per_min}/мин (пауза ${d.min_seconds_between_requests}s)`) +
    tile("использование лимита", d.budget_used_pct == null ? "—" : d.budget_used_pct + "%",
         rlHour ? "лимит уже бьётся — увеличь интервалы опроса"
                : "теоретическая оценка; реальный сигнал — 429 справа", budgetCls) +
    tile("429 за час", rlHour, rlHour ? "упираешься в лимит CSFloat" : "лимит не бьётся",
         rlHour ? "bad" : "good") +
    tile("auth-ошибок за час", authHour,
         authHour ? "обнови cookie в .env!" : "cookie в порядке", authHour ? "bad" : "good") +
    tile("данные актуальны на", d.last_update ? timeFmt(d.last_update) : "—",
         "последний успешный сбор");
}

function statsRow(name, s) {
  return `<tr><td>${name}</td>
    <td class="num">${s.total}</td>
    <td class="num">${s.new_sales}</td>
    <td class="num ${s.rate_limited ? "hi-min" : ""}">${s.rate_limited}</td>
    <td class="num ${s.auth_error ? "hi-min" : ""}">${s.auth_error}</td>
    <td class="num ${s.error ? "hi-min" : ""}">${s.error}</td></tr>`;
}

function renderWarnings(list) {
  const el = document.getElementById("warnings");
  if (!list.length) {
    el.innerHTML = '<p class="muted">Пропусков не зафиксировано — частота опроса достаточная. ✅</p>';
    return;
  }
  const rows = list.map((w) =>
    `<tr><td>${timeFmt(w.polled_at)}</td><td>${esc(w.market_hash_name)}</td>
     <td class="num">${w.overlap_count}</td></tr>`).join("");
  el.innerHTML =
    `<p class="muted">Для этих предметов окно 40 продаж прокручивалось между опросами —
      стоит уменьшить их интервал:</p>
     <table class="stat"><thead><tr><th>время</th><th>предмет</th>
       <th class="num">совпало</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderRecent(list) {
  const body = document.getElementById("recent-body");
  if (!list.length) { body.innerHTML = '<tr><td colspan="6" class="muted">пока пусто</td></tr>'; return; }
  body.innerHTML = list.map((r) => {
    const cls = r.status === "ok" ? "" : "hi-min";
    return `<tr>
      <td>${timeFmt(r.polled_at)}</td>
      <td>${esc(r.market_hash_name)}</td>
      <td class="num">${r.fetched_count}</td>
      <td class="num">${r.new_count}</td>
      <td class="num">${r.overlap_count}</td>
      <td class="${cls}">${esc(r.status)}</td></tr>`;
  }).join("");
}

async function refresh() {
  try {
    const d = await getJSON("/api/load");
    renderStatus(d.last_update);
    renderTiles(d);
    document.getElementById("stats-body").innerHTML =
      statsRow("за час", d.stats_hour) + statsRow("за сутки", d.stats_day);
    renderWarnings(d.gap_warnings);
    renderRecent(d.recent);
  } catch (e) {
    document.getElementById("tiles").innerHTML =
      `<p class="muted">Ошибка: ${esc(e.message)}</p>`;
  }
}

startAutoRefresh(refresh, 30000);
