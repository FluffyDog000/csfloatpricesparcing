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

function money(v) {
  if (v === null || v === undefined) return "—";
  return "$" + Number(v).toFixed(2);
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
