// Order analysis: what would we bid, and why not the bands we skip.
//
// Every action says what it is doing. The page shipped silent, and a button
// that fetches, computes and re-renders looks identical to a broken one while
// it works — which is exactly how a missing common.js read to the user.
//
// Everything lives inside one function. Declared at the top level, this
// file's `const money` collided with common.js's `function money`, and two
// declarations of one name in the same scope is a SyntaxError at parse time:
// the whole file was skipped, silently, so no listener was ever attached and
// every button on the page did nothing.

(function () {
  "use strict";

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
      tr.innerHTML = `<td>${band}</td><td colspan="9" class="muted">${b.reason}</td>`;
      return tr;
    }
    tr.className = "band-take";
    // The cheapest price that leads the band, beside the one we would pay.
    // Without it the surcharge looks arbitrary next to the order book.
    const over = b.bid - b.entry;
    const why = (b.entry_monthly === null || b.entry_monthly === undefined)
      ? `по ${money(b.entry)} мы первые, но подходящих сделок почти нет`
      : `по ${money(b.entry)}: λ ${b.entry_lam.toFixed(2)}/сут, `
        + `набор ${days(b.entry_t_buy)}, ${(b.entry_monthly * 100).toFixed(0)}%/мес`;
    tr.innerHTML = `
      <td><b>${band}</b></td>
      <td class="muted" title="${why}">${money(b.entry)}${
        over > 0 ? ` <span class="over">+${over.toFixed(2)}</span>` : ""}</td>
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
        <th>float</th>
        <th title="минимальная цена, которая ставит нас первыми в полосе">минимум</th>
        <th>ставить</th><th>потолок</th><th>запас</th>
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

  const KIND = {
    place: ["поставить", "act-place"],
    raise: ["перебить", "act-raise"],
    cancel: ["снять", "act-cancel"],
    keep: ["оставить", "act-keep"],
  };

  async function loadPlan() {
    const d = await getJSON("/api/analysis/plan") || {};
    // Defaults rather than assumptions: a reply missing a field must not take
    // the page down, which is how a whole tab went dark earlier today.
    d.actions = d.actions || [];
    d.limits = d.limits || {};
    const box = $("plan-out");
    box.innerHTML = "";

    // What stops it acting, said once and plainly: an unconfigured request
    // and a zero budget are different problems with the same symptom.
    const state = $("plan-state");
    const blocks = [];
    if (!d.can_place) blocks.push(d.placement);
    else if (!d.can_cancel) blocks.push(d.placement);
    if (!d.limits.total_capital) blocks.push("бюджет не задан — лимиты ниже");
    state.textContent = blocks.length
      ? "Выставление недоступно: " + blocks.join(" · ")
      : "Запрос постановки настроен. Выставление всё равно выполняется вручную.";
    state.className = blocks.length ? "err" : "ok";

    if (!d.actions.length) {
      box.innerHTML = '<p class="muted">Действий нет.</p>';
      return;
    }
    const t = document.createElement("table");
    t.className = "stat";
    t.innerHTML = `<thead><tr><th>что</th><th>предмет</th><th>float</th>
      <th>цена</th><th>потолок</th><th>почему</th></tr></thead>`;
    const tb = document.createElement("tbody");
    let need = 0;
    d.actions.forEach((a) => {
      if (a.kind === "place") need += a.price;
      if (a.kind === "raise" && a.was) need += a.price - a.was;
      const [label, cls] = KIND[a.kind] || [a.kind, ""];
      const tr = document.createElement("tr");
      tr.className = cls;
      tr.innerHTML = `<td><b>${label}</b></td>
        <td>${a.item}</td>
        <td>${a.float_min.toFixed(4)}–${a.float_max.toFixed(4)}</td>
        <td><b>${money(a.price)}</b>${
          a.was ? ` <span class="muted">было ${money(a.was)}</span>` : ""}</td>
        <td>${money(a.ceiling)}</td>
        <td class="muted">${a.reason}</td>`;
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    $("plan-arm").checked = !!d.armed;
    $("plan-dry").checked = d.dry_run !== false;
    $("plan-defend").checked = !!d.defend;
    $("plan-defend-min").value = d.defend_minutes || 60;
    renderDefence(d);
    renderApplyResult(d.last_apply, d.pending);

    // A button that will refuse should say so before it is pressed, not
    // after: "ничего не произошло" is the one outcome that teaches nothing.
    const doing = d.actions.filter((a) => a.kind !== "keep");
    const blocking = !d.can_place ? "запрос постановки не настроен"
      : !d.limits.total_capital ? "бюджет равен нулю"
      : !doing.length ? "в плане нечего выполнять"
      : !d.armed ? "выставление не разрешено"
      : "";
    const apply = $("plan-apply");
    apply.disabled = !!blocking;
    apply.title = blocking || "Передать план сборщику";
    apply.textContent = blocking
      ? "Применить план — " + blocking : "Применить план";

    const places = d.actions.filter((a) => a.kind === "place").length;
    const sum = document.createElement("p");
    sum.innerHTML = `Потребуется <b>${money(need)}</b> из лимита `
      + `<b>${money(d.limits.total_capital)}</b> · ${places} ордер(ов) `
      + `из ${d.limits.max_orders}, не больше `
      + `${d.limits.max_orders_per_item} на предмет`;
    box.appendChild(sum);

    // Several orders on one item are one bet in pieces: they fill together
    // when that market moves. The split is what says whether that happened.
    const byItem = d.by_item || {};
    const names = Object.keys(byItem);
    if (names.length && d.concentration > 0.5) {
      const worst = names.reduce((a, b) => (byItem[a] > byItem[b] ? a : b));
      const warn = document.createElement("p");
      warn.className = "err";
      warn.textContent = `${worst} держал бы ${money(byItem[worst])} — `
        + `${(d.concentration * 100).toFixed(0)}% всего капитала. `
        + (names.length === 1
          ? "Это один предмет: просядет он — просядет всё. Добавь ещё предметы."
          : "Опусти «максимум на предмет», чтобы разложить по разным предметам.");
      box.appendChild(warn);
    }
    if (names.length > 1) {
      const split = document.createElement("p");
      split.className = "muted";
      split.textContent = "по предметам: " + names
        .sort((a, b) => byItem[b] - byItem[a])
        .map((n) => `${n} ${money(byItem[n])}`).join(" · ");
      box.appendChild(split);
    }
    box.appendChild(t);
  }

  const PLACE_FIELDS = [
    ["create_method", "метод создания"], ["create_path", "путь создания"],
    ["create_body", "тело создания"],
    ["update_method", "метод правки"], ["update_path", "путь правки"],
    ["update_body", "тело правки"],
    ["cancel_method", "метод отмены"], ["cancel_path", "путь отмены"],
    ["cancel_body", "тело отмены"], ["list_path", "путь списка"],
  ];
  let suggestedSpec = {};

  async function loadPlacement() {
    const d = await getJSON("/api/analysis/placement");
    suggestedSpec = d.suggested || {};
    const box = $("place-fields");
    box.innerHTML = "";
    PLACE_FIELDS.forEach(([key, label]) => {
      const wrap = document.createElement("label");
      // Which of these came off the site and which were inferred from it is
      // the difference between a fact and a guess that spends money.
      const sure = (d.confirmed || []).indexOf(key) >= 0;
      wrap.textContent = label;
      const mark = document.createElement("small");
      mark.className = sure ? "ok" : "over";
      mark.textContent = sure ? " ✓ снято с сайта" : " выведено — проверь";
      wrap.appendChild(mark);
      const input = document.createElement("input");
      input.className = "input";
      input.id = "sp-" + key;
      input.value = (d.spec && d.spec[key]) || "";
      input.placeholder = suggestedSpec[key] || "";
      wrap.appendChild(input);
      box.appendChild(wrap);
    });
    $("place-msg").textContent = d.describe || "";
    $("place-msg").className = d.configured ? "ok" : "muted";
  }

  function actionTable(rows, extra) {
    const t = document.createElement("table");
    t.className = "stat";
    t.innerHTML = `<thead><tr><th>что</th><th>предмет</th><th>float</th>
      <th>цена</th>${extra ? `<th>${extra}</th>` : "<th>почему</th>"}
      </tr></thead>`;
    const tb = document.createElement("tbody");
    rows.forEach((r) => {
      const a = r.action || r;
      const tr = document.createElement("tr");
      tr.className = r.ok === false ? "act-cancel"
        : (KIND[a.kind] || ["", "act-keep"])[1];
      tr.innerHTML = `<td><b>${(KIND[a.kind] || [a.kind])[0]}</b></td>
        <td>${a.item}</td>
        <td>${a.float_min.toFixed(4)}–${a.float_max.toFixed(4)}</td>
        <td><b>${money(a.price)}</b>${
          a.was ? ` <span class="muted">было ${money(a.was)}</span>` : ""}</td>
        <td class="muted">${r.detail || a.reason}</td>`;
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    return t;
  }

  /** What was just handed over, before the collector has touched it. */
  function renderQueued(actions, note) {
    const box = $("plan-result");
    box.innerHTML = "";
    const head = document.createElement("p");
    head.className = "ok";
    head.textContent = note;
    box.appendChild(head);
    box.appendChild(actionTable(actions));
    const wait = document.createElement("p");
    wait.className = "muted";
    wait.id = "plan-waiting";
    wait.textContent = "Жду сборщик…";
    box.appendChild(wait);
  }

  function renderDefence(d) {
    const box = $("plan-defence");
    if (!box) return;
    if (!d.defend) {
      box.textContent = "Автозащита выключена — перебитые ордера останутся "
        + "как есть, пока не применишь план руками.";
      box.className = "muted";
      return;
    }
    const res = d.last_defend;
    const when = d.defend_at
      ? String(d.defend_at).slice(0, 16).replace("T", " ") : "ещё не было";
    box.className = "ok";
    box.textContent = `Автозащита включена, каждые ${d.defend_minutes} мин. `
      + `Последняя проверка: ${when}`
      + (res ? ` — предметов ${res.items}, действий ${res.actions}`
             + (res.dry_run ? " (вхолостую)" : "") : "");
  }

  function renderApplyResult(res, pending) {
    const box = $("plan-result");
    box.innerHTML = "";
    if (pending) {
      box.innerHTML = '<p class="muted">План передан сборщику, жду выполнения…</p>';
      return;
    }
    if (!res || !res.results) return;
    const head = document.createElement("p");
    head.className = res.failed ? "err" : "ok";
    head.textContent = `Выполнено ${res.done} из ${res.done + res.failed}`
      + (res.dry_run ? " (вхолостую, ничего не отправлялось)" : "")
      + ` · ${String(res.at).slice(0, 16).replace("T", " ")}`;
    box.appendChild(head);
    box.appendChild(actionTable(res.results, "итог"));
  }

  function fillLimits(l) {
    $("l-total").value = l.total_capital;
    $("l-item").value = l.per_item_capital;
    $("l-max").value = l.max_orders;
    $("l-maxitem").value = l.max_orders_per_item;
    $("l-patience").value = l.patience_days;
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
    $("p-bidtol").value = Math.round(p.bid_tolerance * 100);
    $("p-sigma").value = p.sigma_k;
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
      await loadPlacement();
      await loadPlan();
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
      await loadPlan();
    });

    $("plan-run").onclick = () => action("Строю план", async () => {
      await loadPlan();
      say("План пересчитан.", "ok");
    });

    $("plan-arm").onchange = () => action("Меняю разрешение", async () => {
      const r = await postJSON("/api/analysis/arm",
        { armed: $("plan-arm").checked, dry_run: $("plan-dry").checked },
        token());
      say(r.armed
        ? (r.dry_run ? "Разрешено, но вхолостую — ничего не уйдёт."
                     : "РАЗРЕШЕНО ПО-НАСТОЯЩЕМУ. Применение потратит деньги.")
        : "Разрешение снято.", r.armed && !r.dry_run ? "err" : "ok");
    });
    $("plan-dry").onchange = () => $("plan-arm").onchange();

    const saveDefence = () => action("Настраиваю защиту", async () => {
      const on = $("plan-defend").checked;
      const r = await postJSON("/api/analysis/arm", {
        armed: $("plan-arm").checked,
        dry_run: $("plan-dry").checked,
        defend: on,
        defend_minutes: $("plan-defend-min").value,
      }, token());
      say(r.defend
        ? `Автозащита включена, каждые ${r.defend_minutes} мин.`
          + (r.dry_run ? " Вхолостую." : " По-настоящему.")
        : "Автозащита выключена.", r.defend && !r.dry_run ? "err" : "ok");
      await loadPlan();
    });
    $("plan-defend").onchange = saveDefence;
    $("plan-defend-min").onchange = saveDefence;

    $("plan-apply").onclick = () => {
      const real = $("plan-arm").checked && !$("plan-dry").checked;
      if (real && !confirm("Выставить ордера по-настоящему? Это потратит деньги."))
        return;
      action("Применяю план", async () => {
        const r = await postJSON("/api/analysis/apply", {}, token());
        if (!r.queued) { say(r.note, "err"); return; }
        renderQueued(r.actions, `Передано сборщику: ${r.queued} действий`
          + (r.dry_run ? " (вхолостую)" : "") + ".");
        say(r.note, "ok");

        // The collector picks work up on its own cycle, so the page waits
        // rather than leaving a handover looking like a dead button.
        for (let i = 1; i <= 24; i += 1) {
          await new Promise((ok) => setTimeout(ok, 5000));
          let d;
          try { d = await getJSON("/api/analysis/plan"); }
          catch (e) { continue; }
          if (!d.pending && d.last_apply) {
            await loadPlan();
            const res = d.last_apply;
            say(`Выполнено ${res.done} из ${res.done + res.failed}`
              + (res.dry_run ? " (вхолостую — ничего не отправлялось)" : ""),
              res.failed ? "err" : "ok");
            return;
          }
          const w = $("plan-waiting");
          if (w) w.textContent = `Жду сборщик… проверка ${i} из 24`;
        }
        say("Сборщик не отчитался за две минуты — посмотри «Нагрузка», "
          + "не на паузе ли он.", "err");
      });
    };

    $("place-suggest").onclick = () => {
      PLACE_FIELDS.forEach(([key]) => {
        const el = $("sp-" + key);
        if (el && !el.value) el.value = suggestedSpec[key] || "";
      });
      $("place-msg").textContent = "Подставлено. Проверь пути и сохрани.";
      $("place-msg").className = "muted";
    };

    $("place-save").onclick = () => action("Сохраняю запрос", async () => {
      const body = {};
      PLACE_FIELDS.forEach(([key]) => {
        const el = $("sp-" + key);
        if (el) body[key] = el.value.trim();
      });
      try {
        const r = await postJSON("/api/analysis/placement", body, token());
        $("place-msg").textContent = r.describe;
        $("place-msg").className = "ok";
        say("Запрос сохранён. Выставление по-прежнему выполняется вручную.", "ok");
      } catch (e) {
        $("place-msg").textContent = e.message;
        $("place-msg").className = "err";
        throw e;
      }
      await loadPlacement();
      await loadPlan();
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
      an_bid_tol: (parseFloat($("p-bidtol").value) || 0) / 100,
      an_sigma_k: $("p-sigma").value,
      an_total_capital: $("l-total").value,
      an_per_item_capital: $("l-item").value,
      an_max_orders: $("l-max").value,
      an_max_per_item: $("l-maxitem").value,
      an_patience: $("l-patience").value,
      }, token());
      fillParams(r.params);
      await loadItems(true);
      say((r.rejected && r.rejected.length)
        ? "Поправлено: " + r.rejected.join("; ")
        : "Пороги сохранены, пересчитано.",
        (r.rejected && r.rejected.length) ? "err" : "ok");
    });
  });
})();
