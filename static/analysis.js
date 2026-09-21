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
  // The last scored reply, kept so a filter can re-render without asking the
  // server to score a hundred items again.
  let lastData = null;

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
    lastData = data;
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

  /** Tick boxes over every tracked item, answered in one go.

   * Typing a market_hash_name by hand means getting "★" and "(Field-Tested)"
   * exactly right, and a typo reads as "not tracked" rather than as a typo.
   * The list is what we already collect sales for, which is the only set that
   * can be analysed at all.
   */
  async function openPicker() {
    const [all, current] = await Promise.all([
      getJSON("/api/items"),
      getJSON("/api/analysis"),
    ]);
    const chosen = new Set(current.items.map((i) => i.item));

    const back = document.createElement("div");
    back.className = "picker-backdrop";
    const box = document.createElement("div");
    box.className = "picker";
    back.appendChild(box);

    const head = document.createElement("header");
    const search = document.createElement("input");
    search.className = "input";
    search.placeholder = "поиск по названию…";
    const close = document.createElement("button");
    close.className = "btn";
    close.textContent = "×";
    close.title = "закрыть";
    head.appendChild(search);
    head.appendChild(close);
    box.appendChild(head);

    const body = document.createElement("div");
    body.className = "picker-body";
    box.appendChild(body);

    const foot = document.createElement("footer");
    const count = document.createElement("span");
    count.className = "grow";
    const allBtn = document.createElement("button");
    allBtn.className = "btn";
    allBtn.textContent = "Отметить видимые";
    const noneBtn = document.createElement("button");
    noneBtn.className = "btn";
    noneBtn.textContent = "Снять все";
    const save = document.createElement("button");
    save.className = "btn primary";
    save.textContent = "Готово";
    foot.appendChild(count);
    foot.appendChild(noneBtn);
    foot.appendChild(allBtn);
    foot.appendChild(save);
    box.appendChild(foot);

    // Sorted by how much history each one has: a band needs a sample before
    // its median means anything, so the ones at the bottom cannot be scored.
    const items = all.items.filter((i) => !i.hidden)
      .sort((a, b) => (b.total_sales || 0) - (a.total_sales || 0));
    const rows = [];
    let group = "";
    items.forEach((it) => {
      const name = it.market_hash_name;
      const row = document.createElement("label");
      row.className = "picker-row" + ((it.total_sales || 0) < 30 ? " thin" : "");
      const tick = document.createElement("input");
      tick.type = "checkbox";
      tick.checked = chosen.has(name);
      tick.onchange = () => {
        if (tick.checked) chosen.add(name); else chosen.delete(name);
        refresh();
      };
      const label = document.createElement("span");
      label.className = "grow";
      label.textContent = name;
      const n = document.createElement("span");
      n.className = "n";
      n.textContent = (it.total_sales || 0) + " прод.";
      n.title = (it.total_sales || 0) < 30
        ? "мало истории — большинство полос отвалится с «мало данных»"
        : "продаж в базе";
      row.appendChild(tick);
      row.appendChild(label);
      row.appendChild(n);
      body.appendChild(row);
      rows.push({ row, raw: name, name: name.toLowerCase(), tick });
    });
    if (!rows.length) {
      body.innerHTML = '<p class="muted">Нет отслеживаемых предметов — '
        + 'сначала добавь их на главной странице.</p>';
    }

    function refresh() {
      count.textContent = `отмечено ${chosen.size} из ${items.length}`;
    }
    function filter() {
      const q = search.value.trim().toLowerCase();
      rows.forEach((r) => {
        r.row.style.display = !q || r.name.includes(q) ? "" : "none";
      });
    }
    search.oninput = filter;
    allBtn.onclick = () => {
      rows.forEach((r) => {
        if (r.row.style.display === "none") return;
        r.tick.checked = true;
        chosen.add(r.raw);
      });
      refresh();
    };
    noneBtn.onclick = () => {
      rows.forEach((r) => { r.tick.checked = false; });
      chosen.clear();
      refresh();
    };

    function shut() {
      document.removeEventListener("keydown", onKey);
      back.remove();
    }
    function onKey(e) { if (e.key === "Escape") shut(); }
    document.addEventListener("keydown", onKey);
    close.onclick = shut;
    back.onclick = (e) => { if (e.target === back) shut(); };
    save.onclick = () => {
      shut();
      // One answer, sent once: a stream of adds and removes would leave the
      // list half changed if one of them failed.
      action(`Сохраняю список (${chosen.size})`, async () => {
        const r = await postJSON("/api/analysis/items",
          { action: "set", names: Array.from(chosen) }, token());
        await loadItems(true);
        say(`В списке ${r.items.length} предмет(ов).`
          + (r.unknown && r.unknown.length
            ? " Не отслеживаются и пропущены: " + r.unknown.join(", ")
            : " Дальше — «Обойти стаканы»."),
          r.unknown && r.unknown.length ? "err" : "ok");
      });
    };

    refresh();
    filter();
    document.body.appendChild(back);
    search.focus();
  }

  function bandRow(b) {
    const tr = document.createElement("tr");
    const band = b.float_min.toFixed(2) + "–" + b.float_max.toFixed(2);
    if (!b.take) {
      tr.className = "band-skip";
      tr.innerHTML = `<td>${band}</td><td colspan="10" class="muted">${b.reason}</td>`;
      return tr;
    }
    tr.className = "band-take";
    // The cheapest price that leads the band, beside the one we would pay.
    // Without it the surcharge looks arbitrary next to the order book.
    const over = b.bid - b.entry;
    // Why we are not simply bidding the minimum. Asked twice from a tooltip,
    // so it goes in the open: the surcharge buys flow, and the number it buys
    // is the only thing that justifies it.
    const cheapWhy = (b.entry_t_buy === null || b.entry_t_buy === undefined)
      ? "по ней сделок нет — ордер бы не исполнился"
      : `по ней набор ${days(b.entry_t_buy)}`;
    const why = (b.entry_t_buy === null || b.entry_t_buy === undefined)
      ? `по ${money(b.entry)} мы были бы первыми в стакане, но ни одна сделка `
        + "не проходит по этой цене — ордер стоял бы вечно"
      : `по ${money(b.entry)}: λ ${b.entry_lam.toFixed(2)}/сут, `
        + `набор ${days(b.entry_t_buy)}`;
    tr.innerHTML = `
      <td><b>${band}</b></td>
      <td class="muted" title="${why}">${money(b.entry)}${
        over > 0 ? ` <span class="over">+${over.toFixed(2)}</span>`
          + `<br><small>${cheapWhy}</small>` : ""}</td>
      <td><b>${money(b.bid)}</b></td>
      <td>${money(b.ceiling)}</td>
      <td>${b.wars}</td>
      <td>${money(b.paid)}<span class="muted">${
        b.paid && b.bid ? ` −${(b.bid - b.paid).toFixed(2)}` : ""}</span></td>
      <td>${money(b.market)}<span class="muted"${
        b.borrowed ? ` title="своих продаж ${b.sample}, взято ${b.borrowed} `
          + `ближайших, дальняя за ${b.reach.toFixed(3)} по float. `
          + `Погрешность ${(b.market_error * 100).toFixed(1)}%"` : ""
        }> ${b.priced_from}${b.borrowed ? " ±" + (b.market_error * 100).toFixed(0)
          + "%" : ""}</span></td>
      <td><b>${pct(b.margin)}</b>${
        b.margin_worst !== null && b.margin_worst !== undefined
          ? ` <span class="muted" title="если бы лот обошёлся в полную ставку`
            + ` ${money(b.bid)} — потолок гарантирует, что и тогда не в убыток"`
            + `>(${pct(b.margin_worst)})</span>` : ""}</td>
      <td>${b.lam.toFixed(2)}</td>
      <td>${days(b.t_buy)}</td>
      <td>${days(b.t_sell)}</td>`;
    return tr;
  }

  /** Where the list went: how many items, how many survived, what they cost.
   *
   * Scrolling a hundred item panels to count which ones produced a bid is
   * the wrong way to answer "did this run find anything".
   */
  function renderFunnel(data) {
    const box = $("an-funnel");
    box.innerHTML = "";
    const holder = $("an-funnel-block");
    if (holder) holder.hidden = !data.items.length;
    const total = data.items.length;
    const screened = data.items.filter((i) => i.screened_out);
    const scored = data.items.filter((i) => !i.error && !i.screened_out);
    const withBids = scored.filter(
      (i) => (i.bands || []).some((b) => b.take));
    const noBook = scored.filter((i) => i.sales && !i.orders);
    const capital = withBids.reduce((n, i) => n + (i.capital || 0), 0);
    const orders = withBids.reduce(
      (n, i) => n + (i.bands || []).filter((b) => b.take).length, 0);

    const tiles = document.createElement("div");
    tiles.className = "journal-summary";
    [
      ["в списке", total, ""],
      ["снято до запросов", screened.length, screened.length ? " muted-tile" : ""],
      ["разобрано", scored.length, ""],
      ["с ордерами", withBids.length, ""],
      ["полос под ордера", orders, ""],
      ["капитал", "$" + capital.toFixed(0), ""],
    ].forEach(([label, value, cls]) => {
      const tile = document.createElement("div");
      tile.className = "journal-tile" + cls;
      tile.innerHTML = `<b>${value}</b><span>${label}</span>`;
      tiles.appendChild(tile);
    });
    box.appendChild(tiles);

    // What the free pass threw out, grouped by the threshold that did it.
    if (screened.length) {
      const why = {};
      screened.forEach((i) => {
        // "медиана $412.00 дороже $150.00" — group by the rule, not the value.
        const key = (i.screened_out || "").replace(/[\d.,$%]+/g, "…");
        why[key] = (why[key] || 0) + 1;
      });
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = "Снято отсевом по истории, без единого запроса: "
        + Object.keys(why).sort((a, b) => why[b] - why[a])
          .map((k) => `${why[k]} — ${k}`).join("; ")
        + `. Это примерно ${screened.length * 6} запросов, которые не `
        + "пришлось тратить.";
      box.appendChild(p);
    }

    // Two reasons for an empty report that are not the market saying no.
    if (noBook.length) {
      const p = document.createElement("p");
      p.className = "err";
      p.textContent = `${noBook.length} предмет(ов) прошли отсев, но стакан не `
        + "собран — нажми «Обойти стаканы».";
      box.appendChild(p);
    }
  }

  function renderResults(data) {
    const box = $("an-results");
    box.innerHTML = "";
    renderFunnel(data);
    if (!data.items.length) return;
    const onlyTake = ($("an-only-take") || {}).checked;

    data.items.forEach((it) => {
      const take = (it.bands || []).filter((b) => b.take);
      if (onlyTake && !take.length) return;

      // One <details> per item rather than one open panel: a hundred items
      // is a hundred tables, and the answer for each of them is one line.
      const sec = document.createElement("details");
      sec.className = "settings-block item-report";
      const head = document.createElement("summary");
      head.textContent = it.item + (it.error ? " — " + it.error
        : it.screened_out ? "  ·  снят отсевом: " + it.screened_out
        : take.length ? `  ·  ${take.length} ордер(ов), ${money(it.capital)}`
        : "  ·  нет подходящих полос");
      if (take.length) head.className = "ok-summary";
      sec.appendChild(head);

      const meta = document.createElement("div");
      meta.className = "muted";
      if (it.error) {
        meta.textContent = it.error;
        sec.appendChild(meta);
        box.appendChild(sec);
        return;
      }
      const sc = it.screen;
      if (sc) {
        const line = document.createElement("div");
        line.className = "muted";
        line.textContent = "по истории: "
          + [sc.median !== null ? `медиана ${money(sc.median)}` : null,
             sc.flow !== null ? `поток ${sc.flow.toFixed(2)}/сут` : null,
             sc.quiet_days !== null
               ? `последняя продажа ${sc.quiet_days.toFixed(0)} дн назад` : null,
             sc.spread !== null ? `разброс ${sc.spread.toFixed(2)}` : null,
             sc.gap !== null ? `зазор ${(sc.gap * 100).toFixed(1)}%` : null,
            ].filter(Boolean).join(" · ");
        sec.appendChild(line);
      }
      if (it.screened_out) {
        const p = document.createElement("p");
        p.className = "muted";
        p.textContent = "Отсеян до обхода стакана: " + it.screened_out
          + ". Пороги отсева — в «Пороги расчёта и лимиты».";
        sec.appendChild(p);
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

      const sum = document.createElement("p");
      sum.innerHTML = take.length
        ? `<b>${take.length}</b> ордер(ов) · капитал <b>${money(it.capital)}</b>`
          + ` · прибыль за круг <b>${money(it.profit)}</b>`
          + ` (<b>${it.capital ? (it.profit / it.capital * 100).toFixed(1) : 0}%</b>)`
        : "<b>Ни одной полосы не проходит.</b> Причина по каждой — в таблице.";
      sec.appendChild(sum);

      const t = document.createElement("table");
      t.className = "stat";
      t.innerHTML = `<thead><tr>
        <th>float</th>
        <th title="минимальная цена, которая ставит нас первыми в полосе">минимум</th>
        <th>ставить</th><th>потолок</th><th>запас</th>
        <th title="во сколько обойдётся лот на самом деле: CSFloat берёт цену
лота, а не нашу ставку — ставка это лишь потолок">платим</th>
        <th>рынок</th>
        <th title="считается от того, что платим. В скобках — если бы каждый
лот обошёлся в нашу полную ставку: это то, что гарантирует потолок">маржа</th>
        <th>λ/сут</th>
        <th title="сколько ждать, пока ордер наберётся">набор</th>
        <th title="сколько ждать покупателя после того, как снимут бан">продажа</th>
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

    // Not a blocker, but it changes what the numbers below mean: "we hold
    // four orders" is a different claim from "the account holds four", and
    // until they have been compared only the first one is true.
    const warn = $("plan-sync");
    if (warn) {
      if (!d.sync_at) {
        warn.className = "err";
        warn.textContent = "Ордера ни разу не сверялись с аккаунтом — снятое "
          + "вручную всё ещё числится, и бюджет держит деньги под него. "
          + "Вкладка «Журнал» → «Сверить с аккаунтом».";
      } else if (d.sync && d.sync.error) {
        warn.className = "err";
        warn.textContent = "Последняя сверка с аккаунтом не удалась: "
          + d.sync.error;
      } else {
        warn.className = "muted";
        warn.textContent = "Сверено с аккаунтом "
          + String(d.sync_at).slice(0, 16).replace("T", " ")
          + ((d.sync && d.sync.seen !== undefined)
            ? ` · на аккаунте ${d.sync.seen} ордер(ов)` : "");
      }
    }

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
    // Loaded into the form, not just used: left showing the markup's
    // defaults, the next "save" posted a budget of zero over a real one.
    fillLimits(d.limits);
    fillScreen(d.screen);
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
    // The body as it stands, so "the code updated" and "the saved shape
    // updated" can be told apart without sending anything.
    const msg = $("place-msg");
    msg.innerHTML = "";
    const line = document.createElement("div");
    line.textContent = d.describe || "";
    line.className = d.configured ? "ok" : "muted";
    msg.appendChild(line);
    (d.warnings || []).forEach((w) => {
      const el = document.createElement("div");
      el.className = "err";
      el.textContent = "⚠ " + w;
      msg.appendChild(el);
    });
    Object.keys(d.preview || {}).forEach((k) => {
      const pre = document.createElement("pre");
      pre.className = "preview";
      pre.textContent = k + ": " + JSON.stringify(d.preview[k]);
      msg.appendChild(pre);
    });
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
        <td class="muted"></td>`;
      // The last cell now carries whatever the server said, verbatim, plus the
      // body that drew it - text from outside, so it goes in as text.
      const why = tr.lastElementChild;
      why.textContent = r.detail || a.reason;
      if (r.ok === false && a.sent) why.title = "отправлено: " + JSON.stringify(a.sent);
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
    $("l-patience").value = l.patience_minutes;
    $("l-balance").value = l.balance;
    const note = $("l-allowance");
    if (!note) return;
    if (!l.balance) {
      note.className = "muted";
      note.textContent = "Баланс не указан — проверка «10× баланса» отключена.";
    } else if (l.capped_by_balance) {
      note.className = "err";
      note.textContent = `Баланс ${money(l.balance)} — CSFloat разрешит ордеров `
        + `не больше чем на ${money(l.allowance)}. Лимит `
        + `${money(l.total_capital)} выше этого, планировать буду на `
        + `${money(l.budget)}.`;
    } else {
      note.className = "muted";
      note.textContent = `Баланс ${money(l.balance)} → потолок CSFloat `
        + `${money(l.allowance)}. Твой лимит ${money(l.total_capital)} — в него `
        + `укладывается. Учти: исполнится только то, на что хватит баланса.`;
    }
  }

  const SCREEN_FIELDS = [
    ["s-minprice", "min_price", 1], ["s-maxprice", "max_price", 1],
    ["s-flow", "min_flow", 1], ["s-quiet", "max_quiet_days", 1],
    ["s-spread", "max_spread", 1], ["s-gap", "min_gap", 100],
    ["s-minsales", "min_sales", 1],
  ];

  function fillScreen(s) {
    if (!s) return;
    SCREEN_FIELDS.forEach(([id, key, scale]) => {
      const el = $(id);
      if (el) el.value = Math.round((s[key] || 0) * scale * 1000) / 1000;
    });
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
    $("p-reach").value = p.max_reach;
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
    $("an-pick").onclick = () => openPicker();

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

    // Re-rendered from what is already loaded: scoring a hundred items again
    // to hide some of them is a filter nobody presses twice.
    const filter = $("an-only-take");
    if (filter) {
      filter.onchange = () => {
        if (lastData) renderResults(lastData);
        else action("Считаю", () => loadItems(true));
      };
    }

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
      // Overwrites, because what it is usually needed for is replacing a
      // shape that has since been corrected - filling only the blanks left
      // a body CSFloat had already rejected sitting there.
      const filled = PLACE_FIELDS.some(([k]) => ($("sp-" + k) || {}).value);
      if (filled && !confirm("Заменить текущие значения выведенными?")) return;
      PLACE_FIELDS.forEach(([key]) => {
        const el = $("sp-" + key);
        if (el) el.value = suggestedSpec[key] || "";
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
        an_reach: $("p-reach").value,
      an_sigma_k: $("p-sigma").value,
      an_total_capital: $("l-total").value,
      an_per_item_capital: $("l-item").value,
      an_max_orders: $("l-max").value,
      an_max_per_item: $("l-maxitem").value,
      an_patience_min: $("l-patience").value,
      an_balance: $("l-balance").value,
      scr_min_price: $("s-minprice").value,
      scr_max_price: $("s-maxprice").value,
      scr_min_flow: $("s-flow").value,
      scr_quiet: $("s-quiet").value,
      scr_spread: $("s-spread").value,
      // Typed as a percentage, stored as a fraction, like the other margins.
      scr_gap: (parseFloat($("s-gap").value) || 0) / 100,
      scr_min_sales: $("s-minsales").value,
      }, token());
      fillParams(r.params);
      if (r.limits) fillLimits(r.limits);
      if (r.screen) fillScreen(r.screen);
      await loadItems(true);
      await loadPlan();
      say((r.rejected && r.rejected.length)
        ? "Поправлено: " + r.rejected.join("; ")
        : "Пороги сохранены, пересчитано.",
        (r.rejected && r.rejected.length) ? "err" : "ok");
    });
  });
})();
