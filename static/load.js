// Load / health page: request-rate estimate, poll stats, gap warnings, log.

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function tile(label, value, sub, cls, id) {
  return `<div class="tile ${cls || ""}"${id ? ` id="${id}"` : ""}>
    <div class="tile-val">${value}</div>
    <div class="tile-label">${esc(label)}</div>
    ${sub ? `<div class="tile-sub">${esc(sub)}</div>` : ""}
  </div>`;
}

// Live state: the cooldown ticks down locally between server refreshes.
let latest = null;
let cooldownEnd = 0;      // epoch ms when the global 429 pause ends (0 = none)

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
    tile("прокси заблокирован", d.stats_hour.proxy_blocked || 0,
         d.stats_hour.proxy_blocked
           ? "Cloudflare режет выходной IP — смени прокси"
           : "выходные IP проходят",
         d.stats_hour.proxy_blocked ? "warn" : "good") +
    tile("данные актуальны на", d.last_update ? timeFmt(d.last_update) : "—",
         d.stale_minutes == null ? "последний успешный сбор"
                                 : `${d.stale_minutes} мин назад`,
         d.stale_minutes != null && d.stale_minutes > 60 ? "bad" : "") +
    tile("пауза (кулдаун)",
         d.cooldown_remaining_sec > 0 ? fmtLeft(d.cooldown_remaining_sec) : "нет",
         pauseReason(d), d.cooldown_remaining_sec > 0 ? "warn" : "good",
         "tile-cooldown") +
    quotaTile(d);
}

// A pause caused by every route being parked is not a 429 backoff waiting to
// expire, and saying "429 подряд: 1" under a four-hour stop sends the user
// looking at the wrong thing.
function pauseReason(d) {
  if (!(d.cooldown_remaining_sec > 0)) return "опрос идёт без ограничений";
  if (d.routes_total && !d.routes_usable) {
    return d.has_direct
      ? "все маршруты в карантине — ждём выхода"
      : "все прокси в карантине, свой IP выключен — включи его";
  }
  return `до ${timeFmt(d.cooldown_until)} · подряд 429: ${d.cooldown_consecutive}`;
}

// CSFloat's own quota (x-ratelimit-*): the real constraint on a big item list.
function quotaTile(d) {
  if (d.quota_limit == null && d.quota_remaining == null) {
    return tile("квота CSFloat", "—", "пока не видели заголовков лимита");
  }
  // What can be spent now, not what the routes nominally hold: a parked route
  // keeps its budget, so the total reads as "plenty left" while nothing moves.
  const usable = d.quota_usable;
  const left = usable != null && d.routes_total > 1 ? usable : d.quota_remaining;
  const lim = d.quota_limit;
  const pct = lim ? left / lim : null;
  const cls = pct == null ? "" : pct <= 0.05 ? "bad" : pct <= 0.25 ? "warn" : "good";
  let sub = lim ? `из ${lim} на окно` : "";
  if (usable != null && d.quota_remaining != null && usable < d.quota_remaining) {
    sub = `доступно сейчас · ${d.quota_remaining} числится за маршрутами` +
          (d.routes_usable === 0 ? ", но все они в карантине" : "");
  }
  if (d.quota_reset) {
    const resetMs = d.quota_reset * 1000;
    const mins = Math.max(0, Math.round((resetMs - Date.now()) / 60000));
    sub += ` · сброс через ${mins >= 60 ? Math.floor(mins / 60) + "ч " + (mins % 60) + "м" : mins + "м"}`;
  }
  return tile("квота CSFloat", left == null ? "—" : left, sub, cls);
}

function fmtLeft(sec) {
  if (sec < 60) return `${sec}с`;
  return `${Math.floor(sec / 60)}м ${sec % 60}с`;
}

const STATE_CLASS = { ok: "good", cooldown: "warn", limited: "warn",
                      auth: "bad", stale: "bad", blocked: "bad", idle: "" };

const STATE_ICON = { ok: "✅", cooldown: "⏸", limited: "⚠️", auth: "🔑",
                     stale: "⚠️", blocked: "⛔", idle: "💤" };

function renderBanner(d, textOverride) {
  const el = document.getElementById("state-banner");
  el.className = "state-banner " + (STATE_CLASS[d.state] || "");
  el.textContent = `${STATE_ICON[d.state] || ""} ${textOverride || d.state_text}`;
}

// Tick the cooldown down every second without hitting the server; when it
// expires, refresh immediately so counters and status catch up at once.
function tickCooldown() {
  if (!cooldownEnd) return;
  const left = Math.max(0, Math.round((cooldownEnd - Date.now()) / 1000));
  const el = document.getElementById("tile-cooldown");
  if (el) {
    el.className = "tile " + (left > 0 ? "warn" : "good");
    el.querySelector(".tile-val").textContent = left > 0 ? fmtLeft(left) : "нет";
  }
  if (left > 0 && latest && latest.state === "cooldown") {
    renderBanner(latest, `Пауза из-за лимита CSFloat, осталось ${fmtLeft(left)}`);
  }
  if (left <= 0) {
    cooldownEnd = 0;
    refresh();
  }
}
setInterval(tickCooldown, 1000);

/** The planner's picture: polls a day by tier and by liquidity, against what
 * the collector can make in a day. */
function renderPlan(p) {
  const box = document.getElementById("plan-box");
  if (!box) return;
  if (!p) { box.innerHTML = ""; return; }
  const n = (v) => Number(v || 0).toLocaleString("ru-RU");
  const share = p.capacity_day ? Math.round(p.demand_day / p.capacity_day * 100) : 0;
  const rows = (list, extra) => list.map((r) =>
    `<tr><td>${r.label}</td><td>${n(r.items)}</td><td>${n(r.per_day)}</td>${extra(r)}</tr>`).join("");
  box.innerHTML = `
    <h3>План опросов</h3>
    <p class="${share > 80 ? "err" : "muted"}">${n(p.demand_day)} опросов в сутки из
      ${n(p.capacity_day)} возможных (${share}%)${p.rest_stretch > 1
        ? ` · «остальные» растянуты ×${p.rest_stretch}, чтобы уложиться` : ""}</p>
    <table class="stat"><thead><tr><th>уровень</th><th>предметов</th>
      <th>опросов/сутки</th><th>потолок</th></tr></thead><tbody>
      ${rows(p.tiers, (r) => `<td>${Math.round(r.ceiling_minutes / 60 * 100) / 100} ч</td>`)}
    </tbody></table>
    <table class="stat" style="margin-top:8px"><thead><tr><th>ликвидность</th>
      <th>предметов</th><th>опросов/сутки</th><th>продаж за опрос</th></tr></thead><tbody>
      ${rows(p.groups, (r) => `<td>${r.sales_per_poll ?? "—"}</td>`)}
    </tbody></table>`;
}

function renderPace(d) {
  // Don't overwrite a field the user is editing — but always refresh the
  // summary, or the page ends up quoting settings that are no longer set.
  const setField = (id, value, prop) => {
    const el = document.getElementById(id);
    if (el && document.activeElement !== el) el[prop || "value"] = value;
  };
  setField("int-min", d.interval_min_minutes);
  setField("int-max", d.interval_max_minutes);
  setField("spacing", d.min_seconds_between_requests);
  setField("adaptive-on", !!d.adaptive_enabled, "checked");
  const hrs = (m) => (m === undefined || m === null ? "" : Math.round(m / 60 * 100) / 100);
  if (d.ceilings) {
    setField("ceil-orders", hrs(d.ceilings.orders));
    setField("ceil-analysis", hrs(d.ceilings.analysis));
    setField("ceil-rest", hrs(d.ceilings.rest));
  }
  renderPlan(d.plan);
  const parts = [
    `${d.active_items} предм. · ${effectiveInterval(d)} ` +
    `≈ ${d.reqs_per_min_est} запр/мин`,
    d.adaptive_enabled
      ? `по скорости продаж: от ${d.interval_min_minutes} мин до потолка своего уровня`
      : `фиксированно ${d.interval_min_minutes}–${d.interval_max_minutes} мин`,
  ];
  if (d.quota_factor > 1.05) {
    parts.push(`растянуто под квоту ×${d.quota_factor.toFixed(1)} ` +
               `(${d.quota_limit ?? "?"} запросов на окно)`);
  }
  if (d.pace_multiplier > 1) {
    parts.push(`авто-замедление ×${d.pace_multiplier} (после 429; спадает за час без ошибок)`);
  }
  parts.push(d.intervals_customized ? "задано через дашборд" : "из config.yaml");
  document.getElementById("pace-hint").textContent = parts.join("  ·  ");

  // Diagnostics: what CSFloat itself said about the limit.
  const diag = document.getElementById("diag");
  if (d.last_429_at) {
    let hdrs = "";
    try {
      const h = JSON.parse(d.last_429_headers || "{}");
      hdrs = Object.keys(h).length
        ? Object.entries(h).map(([k, v]) => `${k}: ${v}`).join(" · ")
        : "заголовков с лимитом сервер не прислал";
    } catch (e) { hdrs = "—"; }
    diag.textContent = `Последний 429: ${timeFmt(d.last_429_at)} · ${hdrs}` +
      (d.last_429_body ? `\nОтвет сервера: ${d.last_429_body}` : "");
    diag.style.whiteSpace = "pre-wrap";
  } else {
    diag.textContent = "429 ещё не было — ограничений от CSFloat не фиксировалось. ✅";
  }
}

function statsRow(name, s) {
  return `<tr><td>${name}</td>
    <td class="num">${s.total}</td>
    <td class="num">${s.new_sales}</td>
    <td class="num ${s.rate_limited ? "hi-min" : ""}">${s.rate_limited}</td>
    <td class="num ${s.auth_error ? "hi-min" : ""}">${s.auth_error}</td>
    <td class="num ${s.proxy_blocked ? "hi-min" : ""}">${s.proxy_blocked || 0}</td>
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
    latest = d;
    cooldownEnd = d.cooldown_remaining_sec > 0
      ? Date.now() + d.cooldown_remaining_sec * 1000 : 0;
    renderStatus(d.last_update);
    renderBanner(d);
    renderTiles(d);
    renderPace(d);
    renderUsage(d);
    renderProxies(d);
    document.getElementById("stats-body").innerHTML =
      statsRow("за час", d.stats_hour) + statsRow("за сутки", d.stats_day);
    renderRoutes(d.routes || []);
    renderKeyRing(d);
    renderWarnings(d.gap_warnings);
    renderRecent(d.recent);
  } catch (e) {
    document.getElementById("tiles").innerHTML =
      `<p class="muted">Ошибка: ${esc(e.message)}</p>`;
  }
}

startAutoRefresh(refresh, 10000);

// -- polling pace editor ----------------------------------------------------

function token() {
  try { return localStorage.getItem("csfloat_admin_token") || ""; } catch (e) { return ""; }
}

function paceMsg(text, isError) {
  const el = document.getElementById("pace-msg");
  el.textContent = text || "";
  el.className = "settings-msg" + (isError ? " err" : "");
}

// Typed in hours, stored in minutes like every other interval.
function minutesOf(id) {
  const v = parseFloat(document.getElementById(id).value);
  return isNaN(v) ? "" : Math.round(v * 60);
}

document.getElementById("save-pace").addEventListener("click", async () => {
  const body = {
    interval_min_minutes: document.getElementById("int-min").value,
    interval_max_minutes: document.getElementById("int-max").value,
    min_seconds_between_requests: document.getElementById("spacing").value,
    adaptive_intervals: document.getElementById("adaptive-on").checked,
    ceiling_orders_minutes: minutesOf("ceil-orders"),
    ceiling_analysis_minutes: minutesOf("ceil-analysis"),
    ceiling_rest_minutes: minutesOf("ceil-rest"),
  };
  try {
    await postJSON("/api/load/settings", body, token());
    paceMsg("Сохранено — применится со следующего опроса.");
    refresh();
  } catch (e) {
    paceMsg("Ошибка: " + e.message, true);
  }
});

document.getElementById("reset-pace").addEventListener("click", async () => {
  try {
    await postJSON("/api/load/settings", { reset: true }, token());
    paceMsg("Сброшено к значениям из config.yaml.");
    refresh();
  } catch (e) {
    paceMsg("Ошибка: " + e.message, true);
  }
});

document.getElementById("reset-pace-mult").addEventListener("click", async () => {
  try {
    await postJSON("/api/load/settings", { reset_pace: true }, token());
    paceMsg("Авто-замедление сброшено к ×1.");
    refresh();
  } catch (e) {
    paceMsg("Ошибка: " + e.message, true);
  }
});

// One row per route: quota left, reset, and whether it can be used now.
function routeRows(routes) {
  return routes.map((r) => {
    let state = "готов", cls = "";
    if (r.parked_sec > 0) { state = `недоступен ${fmtLeft(r.parked_sec)}`; cls = "hi-min"; }
    else if (r.cooldown_sec > 0) { state = `пауза ${fmtLeft(r.cooldown_sec)}`; cls = "hi-min"; }
    else if (!r.available) { state = "квота исчерпана"; cls = "hi-min"; }
    const reset = r.reset
      ? timeFmt(new Date(r.reset * 1000).toISOString()) : "—";
    const tag = r.direct ? " (сервер)"
      : r.rotating ? ' <span class="badge">ротация</span>' : "";
    return `<tr>
      <td>${esc(r.key)}${tag}</td>
      <td class="num">${r.remaining ?? "—"}</td>
      <td class="num">${r.limit ?? "—"}</td>
      <td>${reset}</td>
      <td class="${cls}">${state}</td></tr>`;
  }).join("");
}

// Per-route quota (only shown when proxies are configured).
function renderRoutes(routes) {
  const sec = document.getElementById("routes-section");
  if (!sec) return;
  // Show as soon as a proxy exists; a lone direct route has nothing to compare.
  sec.hidden = routes.length < 2 && !routes.some((r) => !r.direct);
  if (sec.hidden) return;
  document.getElementById("routes-body").innerHTML = routeRows(routes);
}

// -- the analysis keys' own addresses ---------------------------------------

let keyProxiesDirty = false;

function renderKeyRing(d) {
  const info = document.getElementById("keys-info");
  if (!info) return;
  const routes = d.key_routes || [];
  const parts = [];
  parts.push(d.main_key ? "главный ключ (.env): задан"
                        : "главный ключ (.env): НЕ задан — ордера пойдут с cookie/токеном");
  if (!d.keys_file) parts.push("CSFLOAT_KEYS_FILE не задан — ключей анализа нет");
  else parts.push(`ключей в keys.txt: ${d.keys}`);
  const n = (d.key_proxies_text || "").split("\n").filter((s) => s.trim()).length;
  parts.push(n ? `своих адресов: ${n}` + (d.keys ? ` (~${(d.keys * 3 / n).toFixed(1)} ключа на адрес)` : "")
               : "своих адресов нет — ключи ходят через общий список");
  info.textContent = parts.join(" · ");
  renderMainKey(d);
  const box = document.getElementById("key-proxies-text");
  if (box && !keyProxiesDirty && document.activeElement !== box) {
    box.value = d.key_proxies_text || "";
  }
  const table = document.getElementById("key-routes-table");
  table.hidden = !routes.length;
  if (routes.length) document.getElementById("key-routes-body").innerHTML = routeRows(routes);
  renderKeyStates(d.key_ring || []);
}

let mainProxiesDirty = false;
const mainProxiesBox = document.getElementById("main-proxies-text");
if (mainProxiesBox) {
  mainProxiesBox.addEventListener("input", () => { mainProxiesDirty = true; });
  document.getElementById("save-main-proxies").addEventListener("click", async () => {
    const msg = document.getElementById("main-proxies-msg");
    try {
      const r = await postJSON("/api/load/main_proxies",
        { main_proxies: mainProxiesBox.value }, token());
      mainProxiesDirty = false;
      msg.className = "settings-msg" + (r.count > 4 ? " err" : "");
      msg.textContent = !r.count
        ? "Список очищен — главный ключ возьмёт 3 постоянных адреса из общего списка."
        : `Сохранено: ${r.count} адрес(ов) — сборщик подхватит за ~30 секунд.`
          + (r.count > 4 ? " Это больше 4: CSFloat может снова пожаловаться на много IP." : "");
      refresh();
    } catch (e) {
      msg.className = "settings-msg err";
      msg.textContent = "Ошибка: " + e.message;
    }
  });
}

// The main key's counters, one row per kind of request it has made.
function renderMainKey(d) {
  const box = document.getElementById("main-key-box");
  if (!box) return;
  const rows = (d.main_key_state || []).filter((m) => m.remaining != null || m.wait_sec);
  if (!d.main_key) {
    box.innerHTML = '<p class="settings-msg err">CSFLOAT_API_KEY в .env не задан.</p>';
    return;
  }
  const addrs = (d.main_key_routes || []);
  const own = (d.main_proxies_text || "").trim();
  const where = addrs.length
    ? `<p class="muted">Ходит только через ${addrs.length} адрес(а)`
      + (own ? " из своего списка" : ", выбранных из общего списка") + ": "
      + addrs.map((a) => `<code>${esc(a)}</code>`).join(", ") + "</p>"
    : "";
  const box2 = document.getElementById("main-proxies-text");
  if (box2 && !mainProxiesDirty && document.activeElement !== box2) {
    box2.value = d.main_proxies_text || "";
  }
  const table = document.getElementById("main-routes-table");
  const state = d.main_proxy_state || [];
  if (table) {
    table.hidden = !state.length;
    if (state.length) document.getElementById("main-routes-body").innerHTML = routeRows(state);
  }
  if (!rows.length) {
    box.innerHTML = where + '<p class="muted">Ключ задан. Лимиты появятся после первого '
      + "запроса им с момента запуска сборщика — чтение сделок идёт через минуту.</p>";
    return;
  }
  box.innerHTML = where + `<table class="stat"><thead><tr><th>запросы</th>
    <th class="num">осталось</th><th class="num">лимит</th><th>сброс</th>
    <th>состояние</th></tr></thead><tbody>${rows.map((m) => {
      const reset = m.reset ? timeFmt(new Date(m.reset * 1000).toISOString()) : "—";
      const out = m.wait_sec > 0;
      const low = m.remaining != null && m.limit && m.remaining <= m.limit * 0.05;
      return `<tr><td>${esc(m.label)}</td>
        <td class="num">${m.remaining ?? "—"}</td><td class="num">${m.limit ?? "—"}</td>
        <td>${reset}</td>
        <td class="${out || low ? "hi-min" : ""}">${out
          ? "лимит, сброс через " + fmtLeft(m.wait_sec)
          : low ? "на исходе" : "работает"}</td></tr>`;
    }).join("")}</tbody></table>`;
}

// One row per analysis key. A key is named by the end of it - the part that
// can be searched for in keys.txt - never in full.
function keyQuota(k, kind) {
  const q = (k.quota || {})[kind];
  if (!q || q.remaining == null) return "—";
  return `${q.remaining}/${q.limit ?? "?"}`;
}

function keyState(k) {
  if (k.disabled) return { text: "отключён: " + k.disabled, cls: "hi-min", bad: true };
  const c = k.cooling || {};
  const parts = [];
  if (c.listings > 0) parts.push(`листинги — пауза ${fmtLeft(c.listings)}`);
  if (c.book > 0) parts.push(`стакан — пауза ${fmtLeft(c.book)}`);
  if (c.other > 0) parts.push(`пауза ${fmtLeft(c.other)}`);
  if (parts.length) return { text: parts.join(", "), cls: "hi-min" };
  if (k.last_error && k.failures) return { text: "сбои сети: " + k.last_error, cls: "hi-min" };
  return { text: "работает", cls: "" };
}

function renderKeyStates(keys) {
  const box = document.getElementById("key-ring-box");
  if (!box) return;
  box.hidden = !keys.length;
  if (!keys.length) return;
  const states = keys.map((k) => ({ k, s: keyState(k) }));
  const off = states.filter((x) => x.s.bad);
  const cooling = states.filter((x) => !x.s.bad && x.s.cls);
  let lim = 0, left = 0, seen = 0;
  for (const k of keys) {
    const q = (k.quota || {}).listings;
    if (k.disabled || !q || q.remaining == null) continue;
    lim += q.limit || 0; left += q.remaining; seen += 1;
  }
  document.getElementById("key-ring-summary").textContent =
    `ключей: ${keys.length} · работают: ${keys.length - off.length - cooling.length}`
    + ` · на паузе: ${cooling.length} · отключены: ${off.length}`
    + (seen ? ` · листингов осталось ${left} из ${lim} (по ${seen} ключам, что уже ходили)` : "");
  document.getElementById("key-ring-bad").innerHTML = off.length
    ? `<p class="settings-msg err">Не работают — найди их в keys.txt по концу ключа
       и замени или удали:<br>${off.map((x) =>
         `<b>${esc(x.k.tail || x.k.key)}</b> — ${esc(x.k.disabled)}`).join("<br>")}</p>`
    : "";
  states.sort((a, b) => (b.s.bad - a.s.bad) || ((b.s.cls ? 1 : 0) - (a.s.cls ? 1 : 0)));
  document.getElementById("key-ring-body").innerHTML = states.map(({ k, s }) => {
    const q = (k.quota || {}).listings;
    const reset = q && q.reset ? timeFmt(new Date(q.reset * 1000).toISOString()) : "—";
    return `<tr><td><code>${esc(k.tail || k.key)}</code></td>
      <td class="num">${keyQuota(k, "listings")}</td><td>${reset}</td>
      <td class="num">${keyQuota(k, "book")}</td>
      <td class="num">${k.requests ?? 0}</td><td class="num">${k.failures ?? 0}</td>
      <td class="${s.cls}">${esc(s.text)}</td></tr>`;
  }).join("");
}

const keyProxiesBox = document.getElementById("key-proxies-text");
if (keyProxiesBox) {
  keyProxiesBox.addEventListener("input", () => { keyProxiesDirty = true; });
  document.getElementById("save-key-proxies").addEventListener("click", async () => {
    const msg = document.getElementById("key-proxies-msg");
    try {
      const r = await postJSON("/api/load/key_proxies",
        { key_proxies: keyProxiesBox.value }, token());
      keyProxiesDirty = false;
      msg.className = "settings-msg";
      msg.textContent = r.count
        ? `Сохранено: ${r.count} адрес(ов) для ключей — сборщик подхватит за ~30 секунд.`
        : "Список очищен — ключи будут ходить через общий список прокси.";
      refresh();
    } catch (e) {
      msg.className = "settings-msg err";
      msg.textContent = "Ошибка: " + e.message;
    }
  });
}

// -- proxy editor -----------------------------------------------------------

// The DB is the source of truth for proxies; the collector re-reads it every
// ~30s, so edits here apply without a restart.
let proxiesDirty = false;

function proxyMsg(text, isError) {
  const el = document.getElementById("proxies-msg");
  el.textContent = text || "";
  el.className = "settings-msg" + (isError ? " err" : "");
}

function renderProxies(d) {
  const box = document.getElementById("proxies-text");
  if (!box) return;
  const direct = document.getElementById("use-direct");
  // Never overwrite an edit in progress.
  if (!proxiesDirty && document.activeElement !== box) {
    box.value = d.proxies_text || "";
  }
  if (!proxiesDirty && document.activeElement !== direct) {
    direct.checked = d.use_direct !== false;
  }
  const rot = document.getElementById("rot-limit");
  if (rot && !proxiesDirty && document.activeElement !== rot) {
    rot.value = d.rotating_daily_limit;
  }
  const proxied = (d.routes || []).filter((r) => !r.direct);
  const rotating = proxied.filter((r) => r.rotating).length;
  const hint = document.getElementById("proxies-hint");
  if (!proxied.length) {
    hint.textContent = "Прокси не заданы — все запросы идут с IP сервера.";
  } else {
    hint.textContent =
      `Активных прокси: ${proxied.length}` +
      (rotating ? ` (из них ротационных: ${rotating})` : "") +
      (d.use_direct === false ? " · свой IP не используется" : " + свой IP") +
      ` · суммарный запас квоты: ${d.quota_remaining ?? "—"}`;
  }
  // The account-level complaint is a different failure from a route's quota.
  const warn = document.getElementById("proxies-warn");
  if (d.account_ip_block_at) {
    warn.hidden = false;
    warn.textContent =
      `⚠ ${timeFmt(d.account_ip_block_at)}: CSFloat пожаловался, что с аккаунта ` +
      "идут запросы со слишком многих IP. Ротационные маршруты остановлены на 6 часов. " +
      "Переключи провайдера на sticky-сессии (несколько постоянных IP) — иначе " +
      "ограничение вернётся и станет жёстче.";
  } else {
    warn.hidden = true;
  }
  // The quarantine is our own caution, not a block by CSFloat, so offer a way
  // out — with the consequence stated rather than buried.
  const row = document.getElementById("quarantine-row");
  if (row) {
    row.hidden = !d.account_ip_block_at;
    const btn = document.getElementById("clear-quarantine");
    if (btn) btn.textContent = d.quarantine_clearing
      ? "снимаю…" : "Снять карантин сейчас";
  }
}

const quarantineBtn = document.getElementById("clear-quarantine");
if (quarantineBtn) {
  quarantineBtn.addEventListener("click", async () => {
    if (!confirm(
      "Снять карантин с ротационных маршрутов?\n\n" +
      "Сами прокси никто не блокировал — паузу поставил бот после жалобы " +
      "CSFloat на то, что с аккаунта идут запросы со слишком многих IP.\n\n" +
      "Если сразу выйти на все маршруты, жалоба, скорее всего, повторится, " +
      "и следующее ограничение может быть жёстче. Безопаснее сначала " +
      "сократить список до 5–6 сессий.")) return;
    try {
      const r = await postJSON("/api/load/quarantine", {}, token());
      proxyMsg(r.note);
      refresh();
    } catch (e) {
      proxyMsg("Ошибка: " + e.message, true);
    }
  });
}

const proxiesBox = document.getElementById("proxies-text");
if (proxiesBox) {
  proxiesBox.addEventListener("input", () => { proxiesDirty = true; });
  document.getElementById("use-direct")
    .addEventListener("change", () => { proxiesDirty = true; });
  document.getElementById("rot-limit")
    .addEventListener("input", () => { proxiesDirty = true; });

  document.getElementById("save-proxies").addEventListener("click", async () => {
    const body = {
      proxies: proxiesBox.value,
      use_direct: document.getElementById("use-direct").checked,
      rotating_daily_limit: document.getElementById("rot-limit").value,
    };
    try {
      const r = await postJSON("/api/load/proxies", body, token());
      proxiesDirty = false;
      proxyMsg(r.count
        ? `Сохранено: ${r.count} прокси — сборщик подхватит в течение ~30 секунд.`
        : "Список очищен — запросы идут напрямую с IP сервера.");
      refresh();
    } catch (e) {
      proxyMsg("Ошибка: " + e.message, true);
    }
  });
}

// -- what the current schedule costs ----------------------------------------

// The number that actually explains the request count: total requests spread
// over all items. The arithmetic mean of per-item intervals does NOT reconcile
// with it (fast items dominate the rate), and showing both invited exactly the
// "these numbers disagree" reaction.
function effectiveInterval(d) {
  if (!d.requests_per_day || !d.active_items) return "интервал —";
  const minutes = (1440 * d.active_items) / d.requests_per_day;
  const shown = minutes >= 60
    ? `${(minutes / 60).toFixed(1)} ч` : `${Math.round(minutes)} мин`;
  return `в среднем предмет раз в ${shown}`;
}

function fmtSize(bytes) {
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + " МБ";
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + " КБ";
  return bytes + " Б";
}

function fmtMb(mb) {
  return mb >= 1024 ? (mb / 1024).toFixed(2) + " ГБ" : mb.toFixed(1) + " МБ";
}

function usageRow(label, value, note) {
  return `<tr><td>${esc(label)}</td><td>${esc(value)}</td>
    <td>${note ? esc(note) : ""}</td></tr>`;
}

function renderUsage(d) {
  const body = document.getElementById("usage-body");
  if (!body) return;

  const quota = d.quota_remaining != null && d.quota_limit != null
    ? `квота даёт ${d.quota_limit} на окно` : "";
  const perItem = `${d.active_items} предм. · ${effectiveInterval(d)}`;

  // Forecast vs reality: a gap means the settings changed recently, the
  // collector was down, or a backoff is holding it below plan.
  const plan = d.requests_per_day;
  const fact = d.requests_day_actual;
  let factNote = "фактически отправлено за последние 24 ч";
  if (plan > 0 && fact > 0) {
    const ratio = fact / plan;
    if (ratio < 0.7) factNote += ` — ниже плана (${Math.round(ratio * 100)}%)`;
    else if (ratio > 1.3) factNote += ` — выше плана (${Math.round(ratio * 100)}%)`;
  } else if (!fact) {
    factNote = "за сутки не было ни одного запроса";
  }

  body.innerHTML =
    usageRow("запросов за сутки (факт)", fact, factNote) +
    usageRow("трафик за сутки (факт)", fmtMb(d.traffic_day_actual_mb),
             "реально скачано за 24 ч") +
    usageRow("запросов в сутки (план)", d.requests_per_day, perItem) +
    usageRow("запросов в месяц (план)", d.requests_per_month, quota) +
    usageRow("средний ответ", fmtSize(d.avg_response_bytes),
             d.response_measured
               ? `замерено по ${d.response_samples} опросам за сутки`
               : "оценка — реальных замеров пока нет") +
    usageRow("трафик в сутки (план)", fmtMb(d.traffic_day_mb), "") +
    usageRow("трафик в месяц (план)", fmtMb(d.traffic_month_mb),
             "столько спишет прокси с тарификацией по трафику");

  const parts = [`${d.active_items} активных предм.`];
  if (d.routes_total && !d.routes_usable) {
    // The quota looks untouched because parked routes still report their
    // budget — but none of it can be spent, so saying it is fine is a lie.
    parts.push("⛔ сейчас нет доступных маршрутов — запросы не идут, " +
               "цифры ниже это план, а не факт");
  } else if (d.quota_factor > 1.05) {
    parts.push(`растянуто под квоту ×${d.quota_factor.toFixed(1)} — без неё было бы ` +
               `${Math.round(d.requests_per_day * d.quota_factor)} запр/сут`);
  } else {
    parts.push("квота не ограничивает: бот опрашивает так часто, как задано");
  }
  document.getElementById("usage-hint").textContent = parts.join("  ·  ");
}
