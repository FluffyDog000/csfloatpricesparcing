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
