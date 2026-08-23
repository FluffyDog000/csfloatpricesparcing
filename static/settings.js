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
