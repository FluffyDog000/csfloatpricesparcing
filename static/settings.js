// Settings page: daily Telegram export config + manual export + restore upload.

function token() {
  try { return localStorage.getItem("csfloat_admin_token") || ""; } catch (e) { return ""; }
}

function setMsg(id, text, isError) {
  const el = document.getElementById(id);
  el.textContent = text || "";
  el.className = "settings-msg" + (isError ? " err" : "");
}

async function loadSettings() {
  try {
    const s = await getJSON("/api/settings");
    document.getElementById("export-enabled").checked = !!s.export_enabled;
    if (s.export_time_msk) document.getElementById("export-time").value = s.export_time_msk;
    const last = document.getElementById("last-export");
    last.textContent = s.last_export_date_msk
      ? `Последний бэкап: ${s.last_export_date_msk}`
      : "Бэкап ещё не отправлялся";
    document.getElementById("alerts-enabled").checked = !!s.alerts_enabled;
    document.getElementById("alert-stale").value = s.alert_stale_minutes;
    const d = s.digest || {};
    document.getElementById("digest-time").value = d.time || "";
    document.getElementById("digest-last").textContent = !d.time
      ? "Ежедневная сводка выключена — /summary работает всё равно."
      : d.last ? `Последняя сводка: ${d.last}` : "Сводка ещё не отправлялась.";
    if (!s.telegram_configured) setMsg("settings-msg", "Telegram не настроен — бэкапы и уведомления не будут отправляться.", true);
  } catch (e) {
    setMsg("settings-msg", "Ошибка загрузки настроек: " + e.message, true);
  }
}

document.getElementById("save-settings").addEventListener("click", async () => {
  try {
    await postJSON("/api/settings", {
      export_enabled: document.getElementById("export-enabled").checked,
      export_time_msk: document.getElementById("export-time").value,
    }, token());
    setMsg("settings-msg", "Сохранено.");
  } catch (e) {
    setMsg("settings-msg", "Ошибка: " + e.message, true);
  }
});

document.getElementById("export-now").addEventListener("click", async () => {
  setMsg("settings-msg", "Отправляю…");
  try {
    await postJSON("/api/backup/export_now", {}, token());
    setMsg("settings-msg", "Бэкап отправлен в Telegram.");
    loadSettings();
  } catch (e) {
    setMsg("settings-msg", "Ошибка: " + e.message, true);
  }
});

document.getElementById("restore-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = document.getElementById("restore-file").files[0];
  if (!f) { setMsg("restore-msg", "Выбери файл .db", true); return; }
  if (!confirm(`Восстановить базу из «${f.name}»?\nТекущая база будет заменена ` +
               `(старая сохранится в резервную копию).`)) return;
  setMsg("restore-msg", "Загрузка и восстановление…");
  const fd = new FormData();
  fd.append("dbfile", f);
  const headers = {};
  if (token()) headers["X-Admin-Token"] = token();
  try {
    const resp = await fetch("/api/backup/restore", { method: "POST", headers, body: fd });
    const j = await resp.json();
    if (!resp.ok) throw new Error(j.error || resp.statusText);
    setMsg("restore-msg", "База восстановлена. Старая копия: " + (j.backup || "—"));
  } catch (e) {
    setMsg("restore-msg", "Ошибка: " + e.message, true);
  }
});

loadSettings();

document.getElementById("save-alerts").addEventListener("click", async () => {
  try {
    await postJSON("/api/settings", {
      alerts_enabled: document.getElementById("alerts-enabled").checked,
      alert_stale_minutes: document.getElementById("alert-stale").value,
    }, token());
    setMsg("alerts-msg", "Сохранено.");
  } catch (e) {
    setMsg("alerts-msg", "Ошибка: " + e.message, true);
  }
});

async function saveDigest(time) {
  try {
    await postJSON("/api/settings", { digest_time: time }, token());
    setMsg("digest-msg", time ? `Сводка каждый день в ${time} МСК.` : "Ежедневная сводка выключена.");
    loadSettings();
  } catch (e) {
    setMsg("digest-msg", "Ошибка: " + e.message, true);
  }
}
document.getElementById("save-digest").addEventListener("click",
  () => saveDigest(document.getElementById("digest-time").value));
document.getElementById("digest-off").addEventListener("click", () => saveDigest(""));

function bytes(n) {
  if (n === null || n === undefined) return "—";
  if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " ГБ";
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " МБ";
  return Math.round(n / 1024) + " КБ";
}

const TABLE_NAMES = {
  sales: "история продаж", poll_log: "журнал опросов", order_events: "журнал ордеров",
  buy_orders: "стаканы", listing_depth: "листинги", trades: "сделки аккаунта",
  our_orders: "наши ордера", items: "предметы",
};

async function loadStorage() {
  const box = document.getElementById("storage");
  try {
    const s = await getJSON("/api/settings/storage");
    box.innerHTML = "";
    const line = (text) => { const p = document.createElement("div"); p.textContent = text; box.appendChild(p); };
    line(`База: ${bytes(s.db)}` + (s.wal ? ` + журнал WAL ${bytes(s.wal)}` : "")
      + ` · бэкапы на сервере: ${bytes(s.backups)}`);
    if (s.disk_free !== null) {
      const used = s.disk_total ? Math.round((1 - s.disk_free / s.disk_total) * 100) : null;
      line(`Свободно на диске: ${bytes(s.disk_free)} из ${bytes(s.disk_total)}`
        + (used !== null ? ` (занято ${used}%)` : ""));
      if (s.disk_total && s.disk_free / s.disk_total < 0.15) {
        box.lastChild.className = "err";
      }
    }
    const rows = s.tables.filter((t) => t.rows).sort((a, b) => b.rows - a.rows)
      .map((t) => `${TABLE_NAMES[t.table] || t.table} ≈ ${t.rows.toLocaleString("ru-RU")}`);
    if (rows.length) line("Строк: " + rows.join(" · "));
  } catch (e) {
    box.textContent = "Не удалось посчитать: " + e.message;
  }
}
document.getElementById("storage-reload").addEventListener("click", loadStorage);
loadStorage();
