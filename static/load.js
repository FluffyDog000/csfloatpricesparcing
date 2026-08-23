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
    tile("данные актуальны на", d.last_update ? timeFmt(d.last_update) : "—",
         d.stale_minutes == null ? "последний успешный сбор"
                                 : `${d.stale_minutes} мин назад`,
         d.stale_minutes != null && d.stale_minutes > 60 ? "bad" : "") +
    tile("пауза (кулдаун)",
         d.cooldown_remaining_sec > 0 ? fmtLeft(d.cooldown_remaining_sec) : "нет",
         d.cooldown_remaining_sec > 0
           ? `до ${timeFmt(d.cooldown_until)} · подряд 429: ${d.cooldown_consecutive}`
           : "опрос идёт без ограничений",
         d.cooldown_remaining_sec > 0 ? "warn" : "good", "tile-cooldown") +
    quotaTile(d);
}

// CSFloat's own quota (x-ratelimit-*): the real constraint on a big item list.
function quotaTile(d) {
  if (d.quota_limit == null && d.quota_remaining == null) {
    return tile("квота CSFloat", "—", "пока не видели заголовков лимита");
  }
  const left = d.quota_remaining;
  const lim = d.quota_limit;
  const pct = lim ? left / lim : null;
  const cls = pct == null ? "" : pct <= 0.05 ? "bad" : pct <= 0.25 ? "warn" : "good";
  let sub = lim ? `из ${lim} на окно` : "";
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
                      auth: "bad", stale: "bad", idle: "" };

const STATE_ICON = { ok: "✅", cooldown: "⏸", limited: "⚠️", auth: "🔑",
                     stale: "⚠️", idle: "💤" };

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

function renderPace(d) {
  // Don't fight the user while they're typing.
  if (document.activeElement && document.activeElement.closest(".settings-block")) return;
  document.getElementById("int-min").value = d.interval_min_minutes;
  document.getElementById("int-max").value = d.interval_max_minutes;
  document.getElementById("spacing").value = d.min_seconds_between_requests;
  document.getElementById("adaptive-on").checked = !!d.adaptive_enabled;
  document.getElementById("adaptive-max").value = d.adaptive_max_minutes;
  const parts = [
    `${d.active_items} предм. · средний интервал ~${d.avg_interval_minutes ?? "—"} мин ` +
    `≈ ${d.reqs_per_min_est} запр/мин`,
    d.adaptive_enabled
      ? `адаптивно: от ${d.interval_min_minutes} до ${d.adaptive_max_minutes} мин по скорости продаж`
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

document.getElementById("save-pace").addEventListener("click", async () => {
  const body = {
    interval_min_minutes: document.getElementById("int-min").value,
    interval_max_minutes: document.getElementById("int-max").value,
    min_seconds_between_requests: document.getElementById("spacing").value,
    adaptive_intervals: document.getElementById("adaptive-on").checked,
    adaptive_max_minutes: document.getElementById("adaptive-max").value,
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

// Per-route quota (only shown when proxies are configured).
function renderRoutes(routes) {
  const sec = document.getElementById("routes-section");
  if (!sec) return;
  // Show as soon as a proxy exists; a lone direct route has nothing to compare.
  sec.hidden = routes.length < 2 && !routes.some((r) => !r.direct);
  if (sec.hidden) return;
  document.getElementById("routes-body").innerHTML = routes.map((r) => {
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
  const perItem = d.avg_interval_minutes
    ? (d.avg_interval_minutes >= 60
        ? `каждый предмет раз в ${(d.avg_interval_minutes / 60).toFixed(1)} ч`
        : `каждый предмет раз в ${Math.round(d.avg_interval_minutes)} мин`)
    : "";

  body.innerHTML =
    usageRow("запросов в сутки", d.requests_per_day, perItem) +
    usageRow("запросов в месяц", d.requests_per_month, quota) +
    usageRow("средний ответ", fmtSize(d.avg_response_bytes),
             d.response_measured
               ? `замерено по ${d.response_samples} опросам за сутки`
               : "оценка — реальных замеров пока нет") +
    usageRow("трафик в сутки", fmtMb(d.traffic_day_mb), "") +
    usageRow("трафик в месяц", fmtMb(d.traffic_month_mb),
             "столько спишет прокси с тарификацией по трафику");

  const parts = [`${d.active_items} активных предм.`];
  if (d.quota_factor > 1.05) {
    parts.push(`растянуто под квоту ×${d.quota_factor.toFixed(1)} — без неё было бы ` +
               `${Math.round(d.requests_per_day * d.quota_factor)} запр/сут`);
  } else {
    parts.push("квота не ограничивает: бот опрашивает так часто, как задано");
  }
  document.getElementById("usage-hint").textContent = parts.join("  ·  ");
}
