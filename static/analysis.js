// Order analysis: what would we bid, and why not the bands we skip.
//
// Every action says what it is doing. The page shipped silent, and a button
// that fetches, computes and re-renders looks identical to a broken one while
// it works — which is exactly how a missing common.js read to the user.

function token() {
  try { return localStorage.getItem("csfloat_admin_token") || ""; } catch (e) { return ""; }
}
const $ = (id) => document.getElementById(id);
const money = (v) => (v === null || v === undefined) ? "—" : "$" + v.toFixed(2);
const pct = (v) => (v === null || v === undefined) ? "—" : (v * 100).toFixed(1) + "%";
const days = (v) => (v === null || v === undefined) ? "—" : v.toFixed(1) + " д";

let busy = false;

const BUILD = (document.currentScript && document.currentScript.src || "")
  .split("?v=")[1] || "?";
// Announced before anything else runs, so the inline bootstrap in the page
// can compare it against what the server meant to serve.
window.ANALYSIS_BUILD = BUILD;

function say(text, kind) {
  const el = $("an-note");
  if (!el) return;
  el.textContent = (text || "") + (text ? `  [сборка ${BUILD}]` : "");
  el.className = kind === "err" ? "err" : (kind === "ok" ? "ok" : "muted");
}

/** Run an action with the buttons locked and the status line narrating it. */
async function action(label, fn) {
  if (busy) { say("Подожди, идёт: " + busy, "err"); return; }
  busy = label;
  const buttons = document.querySelectorAll(".analysis-actions .btn, #an-add");
  buttons.forEach((b) => { b.disabled = true; });
  say(label + "…");
  try {
    await fn();
  } catch (e) {
    const msg = (e && e.message) ? e.message : String(e);
    say("Ошибка — " + msg, "err");
    if (msg.indexOf("сессия истекла") === 0) {
      const el = $("an-note");
      const a = document.createElement("a");
      a.href = "/login";
      a.textContent = "  → войти";
      el.appendChild(a);
    }
    console.error(label, e);
  } finally {
    busy = false;
    buttons.forEach((b) => { b.disabled = false; });
  }
}

async function loadItems(quiet) {
  const data = await getJSON("/api/analysis");
  const names = data.items.map((i) => i.item);
  renderList(names);
  renderResults(data);
  if (!quiet) {
    const taken = data.items.reduce(
      (n, it) => n + (it.bands || []).filter((b) => b.take).length, 0);
    say(names.length
      ? `Готово: ${names.length} предмет(ов), подходящих полос ${taken}.`
      : "Список пуст — впиши название предмета выше и нажми «Добавить».",
      names.length ? "ok" : null);
  }
  return data;
}

function renderList(names) {
  const box = $("an-list");
  if (!names.length) {
    box.innerHTML = '<span class="muted">список пуст</span>';
    return;
  }
  // Built aside and swapped in one go: clearing first meant a failure
  // halfway through left the list blank, which reads as "nothing was added".
  const chips = [];
  names.forEach((name) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    const label = document.createElement("span");
    label.textContent = name;
    chip.appendChild(label);
    const x = document.createElement("button");
    x.className = "chip-x";
    x.textContent = "×";
    x.title = "убрать";
    x.onclick = () => action("Убираю " + name, async () => {
      await postJSON("/api/analysis/items",
        { market_hash_name: name, action: "remove" }, token());
      await loadItems();
    });
    chip.appendChild(x);
    chips.push(chip);
  });
  box.innerHTML = "";
  chips.forEach((c) => box.appendChild(c));
}

function bandRow(b) {
  const tr = document.createElement("tr");
  const band = b.float_min.toFixed(2) + "–" + b.float_max.toFixed(2);
  if (!b.take) {
    tr.className = "band-skip";
    tr.innerHTML = `<td>${band}</td><td colspan="8" class="muted">${b.reason}</td>`;
    return tr;
  }
  tr.className = "band-take";
  tr.innerHTML = `
    <td><b>${band}</b></td>
    <td><b>${money(b.bid)}</b></td>
    <td>${money(b.ceiling)}</td>
    <td>${b.wars}</td>
    <td>${money(b.market)}<span class="muted"> ${b.priced_from}</span></td>
    <td>${pct(b.margin)}</td>
    <td>${b.lam.toFixed(2)}</td>
    <td>${days(b.t_buy)}</td>
    <td><b>${(b.monthly * 100).toFixed(0)}%</b></td>`;
  return tr;
}

function renderResults(data) {
  const box = $("an-results");
  box.innerHTML = "";
  if (!data.items.length) return;

  data.items.forEach((it) => {
    const sec = document.createElement("section");
    sec.className = "settings-block";
    const head = document.createElement("h2");
    head.textContent = it.item;
    sec.appendChild(head);

    const meta = document.createElement("div");
    meta.className = "muted";
    if (it.error) {
      meta.textContent = it.error;
      sec.appendChild(meta);
      box.appendChild(sec);
      return;
    }
    meta.textContent =
      `продаж ${it.sales} · ордеров в стакане ${it.orders}` +
      (it.swept_at ? ` · обойдён ${it.swept_at.slice(0, 16).replace("T", " ")}` : "") +
      (it.depth ? ` · листингов по полосам ${it.depth}`
                : " · листинги не собраны (цена выхода из истории, завышена)");
    sec.appendChild(meta);

    // The two ways a report is empty for a reason that is not the market.
    if (!it.sales) {
      const warn = document.createElement("p");
      warn.className = "err";
      warn.textContent = "Продаж в базе нет — предмет добавлен недавно, "
        + "история ещё собирается. Считать пока не из чего.";
      sec.appendChild(warn);
    } else if (!it.orders) {
      const warn = document.createElement("p");
      warn.className = "err";
      warn.textContent = "Стакан не собран — нажми «Обойти стаканы». "
        + "Без него не видно, кто уже стоит в полосе.";
      sec.appendChild(warn);
    }

    const take = it.bands.filter((b) => b.take);
    const sum = document.createElement("p");
    sum.innerHTML = take.length
      ? `<b>${take.length}</b> ордер(ов) · капитал <b>${money(it.capital)}</b>`
        + ` · ожидаемо <b>${money(it.monthly)}</b>/мес`
        + ` (<b>${it.capital ? (it.monthly / it.capital * 100).toFixed(0) : 0}%</b>)`
      : "<b>Ни одной полосы не проходит.</b> Причина по каждой — в таблице.";
    sec.appendChild(sum);

    const t = document.createElement("table");
    t.className = "stat";
    t.innerHTML = `<thead><tr>
      <th>float</th><th>ставить</th><th>потолок</th><th>запас</th>
      <th>рынок</th><th>маржа</th><th>λ/сут</th><th>набор</th><th>%/мес</th>
    </tr></thead>`;
    const tb = document.createElement("tbody");
    it.bands.forEach((b) => tb.appendChild(bandRow(b)));
    t.appendChild(tb);
    sec.appendChild(t);
    box.appendChild(sec);
  });
}

async function fillKnown() {
  try {
    const data = await getJSON("/api/items");
    const dl = $("an-known");
    dl.innerHTML = "";
    (data.items || []).forEach((it) => {
      const name = it.market_hash_name || it.name;
      if (!name) return;
      const o = document.createElement("option");
      o.value = name;
      dl.appendChild(o);
    });
  } catch (e) {
    console.error("автодополнение не загрузилось", e);
  }
}

function fillParams(p) {
  $("p-fee").value = (p.fee * 100).toFixed(1);
  $("p-margin").value = (p.min_margin * 100).toFixed(1);
  $("p-window").value = p.window_days;
  $("p-step").value = p.band_step;
  $("p-lambda").value = p.min_lambda;
  $("p-wars").value = p.min_wars;
  $("p-fill").value = p.max_fill_days;
  $("p-sample").value = p.min_sample;
}

document.addEventListener("DOMContentLoaded", () => {
  // A script error must never again look like a button that does nothing.
  window.addEventListener("error", (e) => {
    say("Ошибка в скрипте: " + e.message, "err");
  });

  fillKnown();
  action("Загружаю список", async () => {
    const data = await loadItems(true);
    fillParams(data.params);
    const n = data.items.length;
    say(n ? `Список: ${n} предмет(ов). Нажми «Проанализировать».`
          : "Список пуст — впиши название предмета выше и нажми «Добавить».");
  });

  $("an-add").onclick = () => {
    const name = $("an-name").value.trim();
    if (!name) { say("Сначала впиши название предмета.", "err"); return; }
    action("Добавляю «" + name + "»", async () => {
      const r = await postJSON("/api/analysis/items",
        { market_hash_name: name }, token());
      $("an-name").value = "";
      await loadItems(true);
      say(`Добавлен «${name}». Всего в списке: ${r.items.length}. `
        + "Дальше — «Обойти стаканы».", "ok");
    });
  };
  $("an-name").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("an-add").click();
  });

  $("an-sweep").onclick = () => action("Ставлю обход в очередь", async () => {
    const r = await postJSON("/api/analysis/sweep", {}, token());
    if (!r.queued.length) {
      say("Нечего обходить — список пуст.", "err");
      return;
    }
    if (r.waiting && r.waiting.length) {
      say("Обход поставлен в очередь, но сборщик занят: "
        + r.waiting.join("; ") + ". Проверяю каждые 6 с…");
    }
    // The collector owns the routes, so the page waits for it rather than
    // fetching itself. Show that the wait is progress, not a hang.
    const before = {};
    (await getJSON("/api/analysis")).items.forEach((it) => {
      before[it.item] = it.swept_at || "";
    });
    let tries = 0;
    const total = 25;
    await new Promise((resolve) => {
      const timer = setInterval(async () => {
        tries += 1;
        try {
          const data = await getJSON("/api/analysis");
          renderList(data.items.map((i) => i.item));
          renderResults(data);
          const done = data.items.filter(
            (it) => (it.swept_at || "") !== (before[it.item] || "")).length;
          say(`Жду сборщик: обойдено ${done} из ${r.queued.length}`
            + ` · проверка ${tries} из ${total}`
            + (data.error ? ` · последняя ошибка: ${data.error}` : ""));
          if (done >= r.queued.length) { clearInterval(timer); resolve(); }
        } catch (e) {
          say("Ошибка при проверке — " + e.message, "err");
        }
        if (tries >= total) { clearInterval(timer); resolve(); }
      }, 6000);
    });
    const data = await loadItems(true);
    const fresh = data.items.filter(
      (it) => (it.swept_at || "") !== (before[it.item] || "")).length;
    say(fresh >= r.queued.length
      ? `Обход закончен: ${fresh} из ${r.queued.length}. Считаю…`
      : `Обойдено ${fresh} из ${r.queued.length} — сборщик не успел или на паузе. `
        + "Проверь вкладку «Нагрузка».", fresh ? "ok" : "err");
    await loadItems();
  });

  $("an-run").onclick = () => action("Считаю", async () => {
    await loadItems();
  });

  $("an-clear").onclick = () => {
    if (!confirm("Очистить список предметов для анализа?")) return;
    action("Очищаю список", async () => {
      await postJSON("/api/analysis/items", { action: "clear" }, token());
      await loadItems(true);
      say("Список очищен.", "ok");
    });
  };

  $("p-save").onclick = () => action("Сохраняю пороги", async () => {
    const r = await postJSON("/api/analysis/params", {
      an_fee: (parseFloat($("p-fee").value) / 100) || 0.02,
      an_min_margin: (parseFloat($("p-margin").value) / 100) || 0.03,
      an_window: $("p-window").value,
      an_step: $("p-step").value,
      an_min_lambda: $("p-lambda").value,
      an_min_wars: $("p-wars").value,
      an_max_fill: $("p-fill").value,
      an_min_sample: $("p-sample").value,
    }, token());
    fillParams(r.params);
    await loadItems(true);
    say((r.rejected && r.rejected.length)
      ? "Поправлено: " + r.rejected.join("; ")
      : "Пороги сохранены, пересчитано.",
      (r.rejected && r.rejected.length) ? "err" : "ok");
  });
});
