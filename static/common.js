// Shared helpers for the dashboard.

async function getJSON(url) {
  const resp = await fetch(url, { headers: { "Accept": "application/json" } });
  if (!resp.ok) {
    let msg = resp.statusText;
    try { msg = (await resp.json()).error || msg; } catch (e) {}
    throw new Error(msg);
  }
  return resp.json();
}

async function postJSON(url, body, token) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers["X-Admin-Token"] = token;
  const resp = await fetch(url, {
    method: "POST", headers, body: JSON.stringify(body || {}),
  });
  if (!resp.ok) {
    let msg = resp.statusText;
    try { msg = (await resp.json()).error || msg; } catch (e) {}
    throw new Error(msg);
  }
  return resp.json();
}

// Copy text to clipboard. Uses the async Clipboard API when available (secure
// contexts: https or localhost) and falls back to a hidden textarea + execCommand
// so it also works over plain http on a phone in the LAN.
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) { /* fall through */ }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch (e) {
    return false;
  }
}

// Currency display. Prices are collected and stored in USD; CNY is a view of
// them, so the conversion lives here at the single formatting point rather
// than in each page's render code.
const CNY_RATE = (window.CFG_RATE && Number(window.CFG_RATE)) || null;

function currency() {
  if (!CNY_RATE) return "usd";           // no rate -> nothing to switch to
  try { return localStorage.getItem("csfloat_currency") === "cny" ? "cny" : "usd"; }
  catch (e) { return "usd"; }
}

function setCurrency(code) {
  try { localStorage.setItem("csfloat_currency", code === "cny" ? "cny" : "usd"); }
  catch (e) { /* private mode: the page still works, it just won't remember */ }
}

function money(v) {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  return currency() === "cny"
    ? "¥" + (n * CNY_RATE).toFixed(2)
    : "$" + n.toFixed(2);
}

// Wire up a "$ / ¥" button. Hidden entirely when no rate is known, so the UI
// never offers a conversion it cannot do.
function initCurrencyToggle(id, onChange) {
  const btn = document.getElementById(id);
  if (!btn) return;
  if (!CNY_RATE) { btn.hidden = true; return; }
  const paint = () => {
    btn.textContent = currency() === "cny" ? "¥ CNY" : "$ USD";
    btn.title = currency() === "cny"
      ? `Показать в USD (курс ${CNY_RATE})`
      : `Показать в CNY (курс ${CNY_RATE})`;
  };
  paint();
  btn.addEventListener("click", () => {
    setCurrency(currency() === "cny" ? "usd" : "cny");
    paint();
    if (onChange) onChange();
  });
}

function floatFmt(v) {
  if (v === null || v === undefined) return "—";
  return Number(v).toFixed(6);
}

// "2024-01-02T11:00:00+00:00" -> local "02.01 14:00"
function timeFmt(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getDate())}.${p(d.getMonth() + 1)} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// Drive periodic refresh with a visible countdown in the top bar.
// A single 1-second ticker updates "обновление через Nс" and fires `fn`
// (an async function) when the countdown reaches zero. Runs `fn` once now.
function startAutoRefresh(fn, intervalMs) {
  const el = document.getElementById("refresh-timer");
  const total = Math.max(1, Math.round(intervalMs / 1000));
  let remaining = total;
  let running = false;

  function render() {
    if (!el) return;
    el.textContent = running ? "обновление…" : `обновление через ${remaining}с`;
  }

  async function run() {
    running = true;
    render();
    try { await fn(); } finally {
      running = false;
      remaining = total;
      render();
    }
  }

  run();
  setInterval(() => {
    if (running) return;           // don't count down while a refresh is in flight
    remaining -= 1;
    if (remaining <= 0) {
      remaining = total;
      run();
    }
    render();
  }, 1000);
}

// Show "данные актуальны на HH:MM" in the top bar; warn if stale.
function renderStatus(lastUpdateIso) {
  const el = document.getElementById("status");
  if (!el) return;
  if (!lastUpdateIso) { el.textContent = "сбор ещё не запускался"; return; }
  const d = new Date(lastUpdateIso);
  const ageMin = (Date.now() - d.getTime()) / 60000;
  const p = (n) => String(n).padStart(2, "0");
  const t = `${p(d.getHours())}:${p(d.getMinutes())}`;
  el.textContent = `данные актуальны на ${t}`;
  el.style.color = ageMin > 60 ? "var(--bad)" : "var(--muted)";
}
