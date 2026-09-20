// The order journal: what the bot did, when, and why.
//
// Wrapped like every other page script — a top-level `const` here would
// collide with common.js and take the whole file down at parse time.

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  function token() {
    try { return localStorage.getItem("csfloat_admin_token") || ""; } catch (e) { return ""; }
  }
  const cash = (v) => (v === null || v === undefined) ? "—" : "$" + v.toFixed(2);

  const KIND = {
    place: ["поставить", "act-place"],
    raise: ["перебить", "act-raise"],
    cancel: ["снять", "act-cancel"],
    keep: ["оставить", "act-keep"],
  };

  window.JOURNAL_BUILD = (document.currentScript
    && document.currentScript.src || "").split("?v=")[1] || "?";

  function say(text, kind) {
    const el = $("j-note");
    if (!el) return;
    el.textContent = text || "";
    el.className = kind === "err" ? "err" : (kind === "ok" ? "ok" : "muted");
  }

  /** Local wall-clock time: the log is written in UTC, read at a desk. */
  function when(iso) {
    if (!iso) return "—";
    const t = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
    if (isNaN(t)) return iso;
    return t.toLocaleString("ru-RU", {
      day: "2-digit", month: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  }

  function summary(d) {
    const box = $("j-summary");
    box.innerHTML = "";
    const real = d.events.filter((e) => !e.dry);
    const counts = {};
    real.forEach((e) => {
      const key = e.ok ? e.kind : "fail";
      counts[key] = (counts[key] || 0) + 1;
    });
    const tiles = [
      ["ордеров стоит", d.held],
      ["чужих/ручных", d.manual || 0],
      ["поставлено", counts.place || 0],
      ["перебито", counts.raise || 0],
      ["снято", counts.cancel || 0],
      ["отказов", counts.fail || 0],
    ];
    tiles.forEach(([label, value]) => {
      const tile = document.createElement("div");
      tile.className = "journal-tile" + (label === "отказов" && value ? " bad" : "");
      tile.innerHTML = `<b>${value}</b><span>${label}</span>`;
      box.appendChild(tile);
    });
    const dry = d.events.length - real.length;
    if (dry) {
      const note = document.createElement("div");
      note.className = "journal-tile muted-tile";
      note.innerHTML = `<b>${dry}</b><span>вхолостую</span>`;
      box.appendChild(note);
    }
  }

  function table(events) {
    const box = $("j-table");
    box.innerHTML = "";
    if (!events.length) {
      box.innerHTML = '<p class="muted">Пока ничего не происходило. '
        + 'Журнал заполняется, когда бот ставит, перебивает или снимает ордер.</p>';
      return;
    }
    const t = document.createElement("table");
    t.className = "stat journal";
    t.innerHTML = `<thead><tr><th>время</th><th>что</th><th>предмет</th>
      <th>float</th><th>цена</th><th>почему</th><th>откуда</th></tr></thead>`;
    const tb = document.createElement("tbody");
    events.forEach((e) => {
      const tr = document.createElement("tr");
      tr.className = !e.ok ? "act-cancel"
        : (KIND[e.kind] || ["", "act-keep"])[1];
      if (e.dry) tr.classList.add("row-dry");
      const band = (e.float_min === null || e.float_max === null) ? "—"
        : e.float_min.toFixed(4) + "–" + e.float_max.toFixed(4);
      // Built cell by cell rather than as one innerHTML string: every other
      // column here is text from outside - an item name, a reason, whatever
      // the server said when it refused - and pasting that in as markup is
      // how a stray angle bracket eats the rest of the row.
      const cell = (text, cls) => {
        const td = document.createElement("td");
        td.textContent = text;
        if (cls) td.className = cls;
        tr.appendChild(td);
        return td;
      };
      cell(when(e.at), "mono");

      const what = cell((KIND[e.kind] || [e.kind])[0]);
      what.innerHTML = `<b>${(KIND[e.kind] || [e.kind])[0]}</b>`
        + (e.dry ? ' <span class="muted">вхолостую</span>' : "")
        + (e.ok ? "" : ' <span class="err">отказ</span>');

      cell(e.market_hash_name);
      cell(band, "mono");
      const price = cell(cash(e.price));
      if (e.was) price.innerHTML = `${cash(e.price)} `
        + `<span class="muted">было ${cash(e.was)}</span>`;

      const why = cell(e.ok ? (e.reason || "") : (e.detail || e.reason || ""),
                       "muted");
      if (e.ok && e.detail) why.title = e.detail;
      cell(e.source === "defence" ? "защита" : "план", "muted");
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    box.appendChild(t);
  }

  /** What the last comparison with the account found. */
  function syncState(d) {
    const el = $("j-sync-state");
    if (!el) return;
    el.className = "muted";
    if (d.sync_pending) {
      el.textContent = "Сверка поставлена в очередь, жду сборщик…";
      return;
    }
    const s = d.sync;
    if (!s) {
      el.textContent = "С аккаунтом ещё не сверялись — нажми «Сверить с "
        + "аккаунтом». До этого счётчик слева показывает то, что бот записал "
        + "себе, а не то, что стоит на сайте.";
      return;
    }
    if (s.error) {
      el.className = "err";
      el.textContent = "Сверка не удалась: " + s.error;
      return;
    }
    const c = s.counts || {};
    const bits = [];
    if (c.matched) bits.push(`${c.matched} совпало`);
    if (c.gone) bits.push(`${c.gone} нет на сайте`);
    if (c.filled) bits.push(`${c.filled} исполнено`);
    if (c.repriced) bits.push(`${c.repriced} с другой ценой`);
    if (c.adopted) bits.push(`${c.adopted} не наших`);
    el.textContent = `Сверено ${when(d.sync_at)}: на аккаунте ${s.seen} `
      + `ордер(ов)` + (bits.length ? " — " + bits.join(", ") : "") + ".";
    if (c.gone || c.adopted) el.className = "err";
  }

  function state(d) {
    const el = $("j-state");
    if (!el) return;
    el.textContent = d.defend
      ? `Автозащита включена, проверка каждые ${d.defend_minutes} мин`
        + (d.defend_at ? ` · последняя ${when(d.defend_at)}` : " · ещё не запускалась")
      : "Автозащита выключена — перебитые ордера останутся как есть.";
  }

  async function load() {
    const limit = $("j-limit").value;
    const item = $("j-item").value;
    const dry = $("j-dry").checked ? "1" : "0";
    say("Читаю журнал…");
    try {
      const d = await getJSON(`/api/analysis/journal?limit=${limit}&dry=${dry}`
        + (item ? `&item=${encodeURIComponent(item)}` : ""));
      const select = $("j-item");
      const chosen = select.value;
      // Rebuilt from what the log actually holds, so the filter can never
      // offer a name that would return nothing.
      select.innerHTML = '<option value="">все</option>';
      d.items.forEach((n) => {
        const o = document.createElement("option");
        o.value = n;
        o.textContent = n;
        if (n === chosen) o.selected = true;
        select.appendChild(o);
      });
      summary(d);
      state(d);
      syncState(d);
      table(d.events);
      say(d.events.length
        ? `${d.events.length} записей.` : "Записей нет.", "ok");
    } catch (e) {
      say("Ошибка — " + ((e && e.message) || e), "err");
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("j-reload").onclick = load;
    $("j-sync").onclick = async () => {
      const btn = $("j-sync");
      btn.disabled = true;
      say("Ставлю сверку в очередь…");
      try {
        const r = await postJSON("/api/analysis/sync", {}, token());
        say(r.note);
        // The collector answers on its own schedule; poll rather than guess.
        for (let i = 0; i < 20; i++) {
          await new Promise((res) => setTimeout(res, 3000));
          const d = await getJSON("/api/analysis/journal?limit=1");
          if (!d.sync_pending) { await load(); return; }
        }
        say("Сборщик не ответил за минуту — посмотри «Нагрузка», "
          + "не на паузе ли он.", "err");
      } catch (e) {
        say("Ошибка — " + ((e && e.message) || e), "err");
      } finally {
        btn.disabled = false;
      }
    };
    $("j-limit").onchange = load;
    $("j-item").onchange = load;
    $("j-dry").onchange = load;
    load();
    // The defence runs on its own clock; a page left open should show it.
    setInterval(load, 60000);
  });
})();
