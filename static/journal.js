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
  const cash = (v) => (v === null || v === undefined) ? "—" : "$" + Number(v).toFixed(2);

  // What happened to an order, in the past tense: the journal is a record.
  const KIND = {
    place: ["поставлен", "act-place"],
    raise: ["поднят", "act-raise"],
    lower: ["снижен", "act-keep"],
    cancel: ["снят", "act-cancel"],
    keep: ["оставлен", "act-keep"],
    fill: ["исполнен", "act-place"],
  };
  const SOURCE = { plan: "план", defence: "защита", sync: "сверка" };
  // This many orders failing for one reason fold into one line.
  const FOLD_AT = 3;

  // The last replies, kept so a filter re-renders what is loaded rather than
  // asking the server again.
  let lastJournal = null;
  let lastPositions = null;
  // Which orders are unfolded: the page reloads every minute, and a history
  // that snaps shut while being read is worse than no history.
  const opened = new Set();

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
      hour: "2-digit", minute: "2-digit",
    });
  }

  /** An element with plain text in it: names, reasons and server replies come
   *  from outside, and pasting them in as markup is how a stray angle bracket
   *  eats the rest of the row. */
  function node(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  }

  const bandOf = (e) => (e.float_min === null || e.float_min === undefined
    || e.float_max === null || e.float_max === undefined) ? "—"
    : Number(e.float_min).toFixed(4) + "–" + Number(e.float_max).toFixed(4);
  const keyOf = (name, e) => name + "|" + bandOf(e);

  /** What a refusal means, in words: the raw reply stays in the tooltip. */
  function explain(e) {
    const raw = String(e.detail || e.reason || "");
    const low = raw.toLowerCase();
    if (/insufficient|balance|not enough|недостат/.test(low))
      return "не хватило баланса на аккаунте";
    if (/vpn/.test(low))
      return "адрес отклонён как VPN или датацентр — нужен другой прокси";
    if (/429|too many|rate limit|лимит/.test(low))
      return "лимит запросов CSFloat — бот повторит позже";
    if (/http 401|http 403|unauthori|forbidden|учётные данные|отклонил ключ/.test(low))
      return "CSFloat не принял главный ключ (CSFLOAT_API_KEY в .env)";
    if (/http 404|not found/.test(low))
      return "ордера уже нет на сайте";
    if (/http 400/.test(low)) {
      const rest = raw.split("—").slice(1).join("—").trim();
      return "CSFloat отклонил параметры ордера" + (rest ? ": " + rest : "");
    }
    if (/http 5\d\d/.test(low))
      return "сбой на стороне CSFloat — бот повторит позже";
    if (/timeout|timed out|connection|proxy|network/.test(low))
      return "сеть или прокси не ответили";
    return raw || "без пояснения";
  }

  // -- filters ---------------------------------------------------------------

  function periodHours() {
    const v = ($("j-period") && $("j-period").value) || "24";
    if (v === "today") {
      const now = new Date();
      const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      return Math.max((now - midnight) / 3600000, 0.01);
    }
    return Number(v) || 0;
  }

  function filtered(events) {
    const kind = ($("j-kind") && $("j-kind").value) || "";
    const words = (($("j-search") && $("j-search").value) || "")
      .toLowerCase().split(/\s+/).filter(Boolean);
    return events.filter((e) => {
      if (kind === "fail" && e.ok) return false;
      if (kind && kind !== "fail" && !(e.ok && e.kind === kind)) return false;
      const name = String(e.market_hash_name || "").toLowerCase();
      return words.every((w) => name.includes(w));
    });
  }

  // -- summary ---------------------------------------------------------------

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
      ["ручных", d.manual || 0],
      ["поставлено", counts.place || 0],
      ["поднято", counts.raise || 0],
      ["снижено", counts.lower || 0],
      ["снято", counts.cancel || 0],
      ["исполнено", counts.fill || 0],
      ["отказов", counts.fail || 0],
    ];
    tiles.forEach(([label, value]) => {
      const tile = node("div", "journal-tile"
        + (label === "отказов" && value ? " bad" : ""));
      tile.appendChild(node("b", "", value));
      tile.appendChild(node("span", "", label));
      box.appendChild(tile);
    });
    const dry = d.events.length - real.length;
    if (dry) {
      const tile = node("div", "journal-tile muted-tile");
      tile.appendChild(node("b", "", dry));
      tile.appendChild(node("span", "", "вхолостую"));
      box.appendChild(tile);
    }
  }

  // -- needs attention -------------------------------------------------------

  /** Refusals nothing has answered since: the latest event of an order is a
   *  failure. One that was followed by a success is history, not a problem. */
  function openFailures(events) {
    const seen = new Set();
    const out = new Map();
    events.filter((e) => !e.dry).forEach((e) => {   // newest first
      const key = keyOf(e.market_hash_name, e);
      if (seen.has(key)) {
        const f = out.get(key);
        if (f && !e.ok && f.streak) f.count += 1;
        else if (f) f.streak = false;
        return;
      }
      seen.add(key);
      if (!e.ok) out.set(key, { e, count: 1, streak: true });
    });
    return [...out.values()];
  }

  function attention(d, pos) {
    const box = $("j-attention");
    if (!box) return;
    box.innerHTML = "";
    const list = node("ul", "attention");
    const add = (level, text, title) => {
      const li = node("li", "att-" + level, text);
      if (title) li.title = title;
      list.appendChild(li);
    };

    // One cause failing many orders is one problem, not forty lines: the
    // same refusal folds into a single line with the orders listed under it.
    const groups = new Map();
    openFailures(d.events || []).forEach((f) => {
      const what = (KIND[f.e.kind] || [f.e.kind])[0];
      const key = what + "|" + explain(f.e);
      if (!groups.has(key)) groups.set(key, { what, why: explain(f.e), rows: [] });
      groups.get(key).rows.push(f);
    });
    groups.forEach(({ what, why, rows }) => {
      if (rows.length < FOLD_AT) {
        rows.forEach(({ e, count }) => add("bad",
          `${e.market_hash_name} ${bandOf(e)}: не ${what}`
          + (count > 1 ? ` (${count} раза подряд)` : "")
          + ` — ${why} · ${when(e.at)}`, e.detail || e.reason || ""));
        return;
      }
      const li = node("li", "att-bad");
      const det = node("details");
      const latest = rows.reduce((a, b) => (a.e.at > b.e.at ? a : b)).e;
      det.appendChild(node("summary", "",
        `${rows.length} ордер(ов): не ${what} — ${why} · ${when(latest.at)}`));
      const inner = node("ul", "muted");
      rows.forEach(({ e, count }) => inner.appendChild(node("li", "",
        `${e.market_hash_name} ${bandOf(e)}`
        + (count > 1 ? ` (${count} раза подряд)` : ""))));
      det.appendChild(inner);
      li.title = latest.detail || latest.reason || "";
      li.appendChild(det);
      list.appendChild(li);
    });

    const orders = (pos && pos.orders) || [];
    let manual = 0, unread = 0;
    orders.forEach((r) => {
      if (r.state === "manual") { manual += 1; return; }
      if (!r.book) { unread += 1; return; }
      if (r.first) return;
      const band = `${r.item} ${Number(r.float_min).toFixed(4)}–${Number(r.float_max).toFixed(4)}`;
      if (r.top >= r.ceiling) {
        add("bad", `${band}: перебили на ${cash(r.top)}, а наш потолок ${cash(r.ceiling)} `
          + "— выше потолка не поднимаем, ордер стоит позади и ждёт, "
          + "пока соперник исполнится или уйдёт");
      } else if (!d.defend) {
        add("warn", `${band}: перебили (${cash(r.top)}), поднять можно до `
          + `${cash(r.ceiling)} — автозащита выключена`);
      }
    });
    if (unread) {
      add("warn", `стакан не читан у ${unread} ордер(ов) — нажми «Обновить стаканы» ниже`);
    }
    if (manual) {
      add("warn", `${manual} ордер(ов) поставлены вручную — бот их не ведёт`);
    }
    if (d.sync && d.sync.error) {
      add("bad", "сверка с аккаунтом не удалась: " + d.sync.error);
    } else if (!d.sync && d.held) {
      add("warn", "с аккаунтом ещё не сверялись — число ордеров взято из записи бота");
    }

    if (list.children.length) {
      box.appendChild(list);
    } else {
      box.appendChild(node("p", "ok", "Всё в порядке: отказов нет, наши ордера впереди."));
    }
  }

  // -- by order --------------------------------------------------------------

  function positionIndex(pos) {
    const out = {};
    ((pos && pos.orders) || []).forEach((r) => {
      out[keyOf(r.item, r)] = r;
    });
    return out;
  }

  /** What the bot will do with this order next, in words - so "outbid and
   *  left alone" reads as a decision, not as the bot having missed it. */
  function outlook(r, defend) {
    if (r.state === "manual") return ["поставлен вручную — бот его не ведёт", "muted"];
    if (!r.book) return ["стакан не читан — положение неизвестно", "muted"];
    const ceil = cash(r.ceiling);
    const atCeiling = r.room !== undefined && r.room !== null && r.room < 0.005;
    if (r.price > r.ceiling + 0.005) {
      return [`цена выше нового потолка ${ceil} — защита снизит до потолка`, "warn"];
    }
    if (r.first) {
      return atCeiling
        ? [`первые, цена на потолке ${ceil} — если перебьют, выше не пойдём `
          + "и будем стоять позади", "warn"]
        : [`первые; если перебьют — поднимем максимум до ${ceil} `
          + `(запас ${cash(r.room)})`, "ok"];
    }
    if (r.top >= r.ceiling - 0.005) {
      return [`перебили на ${cash(r.top)}, это не ниже потолка ${ceil} — не `
        + "перебиваем, стоим позади и ждём, пока соперник исполнится или уйдёт",
        "bad"];
    }
    if (!defend) {
      return [`перебили на ${cash(r.top)}, поднять можно до ${ceil} — `
        + "автозащита выключена", "bad"];
    }
    return [`перебили на ${cash(r.top)} — защита перебьёт, но не выше ${ceil}`
      + " (или подождёт, если очередь скоро разойдётся)", "warn"];
  }

  function verdictLine(r) {
    const v = r && r.verdict;
    if (!v) return null;
    return `защита ${when(v.at)}: ${v.reason}`;
  }

  function statusOf(group, posRow) {
    if (posRow) {
      if (posRow.state === "manual") return ["вручную", "st-muted"];
      if (!posRow.book) return ["стоит", "st-ok"];
      return posRow.first ? ["стоит, первые", "st-ok"]
        : [`перебили: впереди ${posRow.ahead}`, "st-bad"];
    }
    const last = group.events[0];
    if (last.dry) return ["вхолостую", "st-muted"];
    if (!last.ok) return ["отказ", "st-bad"];
    if (last.kind === "fill") return ["исполнен", "st-ok"];
    if (last.kind === "cancel") return ["снят", "st-muted"];
    return ["не стоит", "st-muted"];
  }

  function chainItem(e) {
    const li = node("li", e.ok ? (KIND[e.kind] || ["", ""])[1] : "act-cancel");
    if (e.dry) li.classList.add("row-dry");
    li.appendChild(node("span", "mono", when(e.at)));
    li.appendChild(node("b", "", (KIND[e.kind] || [e.kind])[0]
      + (e.ok ? "" : " — отказ")));
    li.appendChild(node("span", "og-price", e.was
      ? `${cash(e.was)} → ${cash(e.price)}` : cash(e.price)));
    li.appendChild(node("span", "muted", SOURCE[e.source] || e.source || ""));
    const why = node("span", e.ok ? "muted" : "err",
      e.ok ? (e.reason || "") : explain(e));
    if (e.detail) why.title = e.detail;
    li.appendChild(why);
    if (e.dry) li.appendChild(node("span", "muted", "вхолостую"));
    return li;
  }

  function byOrder(events, pos) {
    const box = $("j-table");
    box.innerHTML = "";
    if (!events.length) {
      box.appendChild(node("p", "muted", (lastJournal && lastJournal.events.length)
        ? "Под фильтр ничего не попало."
        : "Пока ничего не происходило. Журнал заполняется, когда бот ставит, "
          + "поднимает или снимает ордер."));
      return;
    }
    const groups = new Map();
    events.forEach((e) => {   // newest first, so a group's first is its last
      const key = keyOf(e.market_hash_name, e);
      if (!groups.has(key)) {
        groups.set(key, { name: e.market_hash_name, band: bandOf(e), events: [] });
      }
      groups.get(key).events.push(e);
    });
    const index = positionIndex(pos);
    const list = node("div", "order-groups");
    groups.forEach((g, key) => {
      const last = g.events[0];
      const [status, cls] = statusOf(g, index[key]);
      const counts = {};
      g.events.forEach((e) => {
        const k = e.ok ? e.kind : "fail";
        counts[k] = (counts[k] || 0) + 1;
      });
      const parts = [];
      if (counts.place) parts.push(`поставлен ${counts.place}`);
      if (counts.raise) parts.push(`поднят ×${counts.raise}`);
      if (counts.lower) parts.push(`снижен ×${counts.lower}`);
      if (counts.cancel) parts.push(`снят ${counts.cancel}`);
      if (counts.fill) parts.push(`исполнен ${counts.fill}`);
      if (counts.fail) parts.push(`отказов ${counts.fail}`);

      const det = node("details", "order-group" + (cls === "st-bad" ? " og-bad" : ""));
      det.open = opened.has(key);
      det.addEventListener("toggle", () => {
        if (det.open) opened.add(key); else opened.delete(key);
      });
      const sum = node("summary");
      const head = node("div", "og-head");
      head.appendChild(node("span", "og-name", g.name));
      head.appendChild(node("span", "og-state " + cls, status));
      sum.appendChild(head);
      const meta = node("div", "og-meta");
      meta.appendChild(node("span", "mono", g.band));
      const pr = index[key];
      meta.appendChild(node("span", "og-price", cash(pr ? pr.price : last.price)
        + (pr && (pr.quantity || 1) > 1 ? ` ×${pr.quantity}` : "")));
      if (pr && pr.state !== "manual") {
        const room = pr.room !== undefined && pr.room !== null && pr.room < 0.005
          ? "на потолке" : `запас ${cash(pr.room)}`;
        meta.appendChild(node("span", "og-ceil", `потолок ${cash(pr.ceiling)} · ${room}`));
      }
      meta.appendChild(node("span", "muted", parts.join(" · ")));
      meta.appendChild(node("span", "muted mono", when(last.at)));
      sum.appendChild(meta);
      det.appendChild(sum);
      if (pr) {
        const [text, tone] = outlook(pr, lastJournal && lastJournal.defend);
        det.appendChild(node("p", "og-next og-" + tone, "Дальше: " + text));
        const v = verdictLine(pr);
        if (v) det.appendChild(node("p", "og-next muted", v));
      }

      // Oldest first inside: a history reads forwards.
      const chain = node("ol", "og-chain");
      g.events.slice().reverse().forEach((e) => chain.appendChild(chainItem(e)));
      det.appendChild(chain);
      list.appendChild(det);
    });
    box.appendChild(list);
  }

  // -- feed ------------------------------------------------------------------

  function feed(events) {
    const box = $("j-table");
    box.innerHTML = "";
    if (!events.length) {
      box.appendChild(node("p", "muted", "Записей нет."));
      return;
    }
    const t = document.createElement("table");
    t.className = "stat journal stack";
    t.innerHTML = `<thead><tr><th>время</th><th>что</th><th>предмет</th>
      <th>float</th><th>цена</th><th>почему</th><th>откуда</th></tr></thead>`;
    const tb = document.createElement("tbody");
    events.forEach((e) => {
      const tr = document.createElement("tr");
      tr.className = !e.ok ? "act-cancel" : (KIND[e.kind] || ["", "act-keep"])[1];
      if (e.dry) tr.classList.add("row-dry");
      const cell = (label, text, cls) => {
        const td = node("td", cls, text);
        td.dataset.label = label;
        tr.appendChild(td);
        return td;
      };
      cell("время", when(e.at), "mono");
      cell("что", (KIND[e.kind] || [e.kind])[0]
        + (e.dry ? " (вхолостую)" : "") + (e.ok ? "" : " — отказ"));
      cell("предмет", e.market_hash_name);
      cell("float", bandOf(e), "mono");
      cell("цена", e.was ? `${cash(e.was)} → ${cash(e.price)}` : cash(e.price));
      const why = cell("почему", e.ok ? (e.reason || "") : explain(e),
        e.ok ? "muted" : "err");
      if (e.detail) why.title = e.detail;
      cell("откуда", SOURCE[e.source] || e.source || "", "muted");
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    box.appendChild(t);
  }

  function render() {
    if (!lastJournal) return;
    const events = filtered(lastJournal.events);
    const view = ($("j-view") && $("j-view").value) || "orders";
    if (view === "feed") feed(events);
    else byOrder(events, lastPositions);
    const total = lastJournal.events.length;
    say((events.length === total ? `${total} записей` : `${events.length} из ${total} записей`)
      + (lastJournal.truncated ? " — показаны последние, сузь период" : "") + ".",
      "ok");
  }

  // -- status lines ----------------------------------------------------------

  /** What the last comparison with the account found. */
  function syncState(d) {
    const el = $("j-sync-state");
    if (!el) return;
    // Cleared rather than appended to: this runs again every minute.
    el.textContent = "";
    el.className = "muted";
    if (d.sync_pending) {
      el.textContent = "Сверка поставлена в очередь, жду сборщик…";
      return;
    }
    const s = d.sync;
    if (!s) {
      el.textContent = "С аккаунтом ещё не сверялись — нажми «Сверить с "
        + "аккаунтом». До этого счётчик ниже показывает то, что бот записал "
        + "себе, а не то, что стоит на сайте.";
      return;
    }
    if (s.error) {
      el.className = "err";
      el.textContent = "Сверка не удалась: " + s.error;
      // What was asked and what came back: without it the next step is
      // guessing at the same paths by hand.
      if (s.tried && s.tried.length) {
        const list = node("ul", "muted");
        s.tried.forEach((line) => list.appendChild(node("li", "", line)));
        el.appendChild(list);
      }
      return;
    }
    if (s.found_path) {
      el.className = "ok";
      el.textContent = `Список ордеров нашёлся: ${s.found_path} — сохранил. `;
    }
    const c = s.counts || {};
    const bits = [];
    if (c.matched) bits.push(`${c.matched} совпало`);
    if (c.gone) bits.push(`${c.gone} нет на сайте`);
    if (c.filled) bits.push(`${c.filled} исполнено`);
    if (c.repriced) bits.push(`${c.repriced} с другой ценой`);
    if (c.adopted) bits.push(`${c.adopted} не наших`);
    if (s.revived) bits.push(`${s.revived} снова найдены на сайте`);
    el.textContent = (el.textContent || "")
      + `Сверено ${when(d.sync_at)}: на аккаунте ${s.seen} `
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

  /** Our standing orders, and whether anyone is above them. */
  function positions(d) {
    const box = $("p-table");
    box.innerHTML = "";
    const rows = d.orders || [];
    if (!rows.length) {
      box.appendChild(node("p", "muted", "Ордеров нет."));
      $("p-note").textContent = "";
      return;
    }
    const t = document.createElement("table");
    t.className = "stat journal stack";
    t.innerHTML = `<thead><tr><th>предмет</th><th>float</th><th>наша цена</th>
      <th>потолок</th><th>запас</th><th>верх стакана</th><th>положение</th>
      <th>что дальше</th>
      <th title="как бот отличил наш ордер от чужих в стакане">узнан</th>
      <th>стакан читан</th></tr></thead>`;
    const tb = document.createElement("tbody");
    rows.forEach((r) => {
      const tr = document.createElement("tr");
      tr.className = r.first ? "act-keep" : "act-cancel";
      const cell = (label, text, cls) => {
        const td = node("td", cls, text);
        td.dataset.label = label;
        tr.appendChild(td);
        return td;
      };
      cell("предмет", r.item + (r.state === "manual" ? "  (вручную)" : ""));
      cell("float", Number(r.float_min).toFixed(4) + "–" + Number(r.float_max).toFixed(4), "mono");
      cell("наша цена", cash(r.price) + ((r.quantity || 1) > 1 ? ` ×${r.quantity}` : ""));
      const moved = Math.abs((r.placed_ceiling || r.ceiling) - r.ceiling) >= 0.005;
      const ceilCell = cell("потолок", cash(r.ceiling));
      if (moved) ceilCell.title = `при выставлении был ${cash(r.placed_ceiling)}`;
      cell("запас", r.state === "manual" ? "—"
        : r.room < 0.005 ? "на потолке" : cash(r.room),
        r.room < 0.005 ? "err" : "");
      cell("верх стакана", r.top ? cash(r.top) : "—");
      cell("положение", !r.book ? "стакан не читан"
        : r.first ? "мы первые"
        : `перебили: впереди ${r.ahead} на ${cash(r.top)}`,
        r.first ? "" : "err");
      const [next] = outlook(r, lastJournal && lastJournal.defend);
      const nextCell = cell("что дальше", next, "muted");
      if (r.verdict) nextCell.title = verdictLine(r);
      cell("узнан", !r.book ? "—" : r.seen_in_book ? "по id" : "по цене и float",
        r.seen_in_book ? "" : "muted");
      cell("стакан читан", r.swept_at ? when(r.swept_at) : "—", "mono");
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    box.appendChild(t);

    const note = $("p-note");
    note.className = d.outbid ? "err" : "muted";
    const unread = rows.filter((r) => !r.book).length;
    note.textContent = (d.outbid
      ? `Перебили ${d.outbid} из ${rows.length}.`
      : unread === rows.length
        ? "Стаканы ещё не читались — нажми «Обновить стаканы»."
        : `Все ${rows.length - unread} с прочитанным стаканом впереди.`
          + (unread ? ` Не читан стакан у ${unread}.` : ""))
      + (d.book_rows
        ? (d.book_named
          ? ` Стакан называет ордера (${d.book_named} из ${d.book_rows}) — `
            + "свои узнаём точно."
          : " Стакан ордера не называет — свои приходится отличать по цене "
            + "и границам float.")
        : "");
  }

  async function load() {
    const dry = $("j-dry").checked ? "1" : "0";
    const hours = periodHours();
    say("Читаю журнал…");
    try {
      const d = await getJSON(`/api/analysis/journal?limit=2000&dry=${dry}`
        + (hours ? `&hours=${hours.toFixed(2)}` : ""));
      lastJournal = d;
      try {
        lastPositions = await getJSON("/api/analysis/positions");
        positions(lastPositions);
      } catch (e) {
        lastPositions = null;
        $("p-note").className = "err";
        $("p-note").textContent = "Позиции не загрузились — "
          + ((e && e.message) || e);
      }
      summary(d);
      state(d);
      syncState(d);
      attention(d, lastPositions);
      render();
    } catch (e) {
      say("Ошибка — " + ((e && e.message) || e), "err");
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("j-reload").onclick = load;

    $("p-refresh").onclick = async () => {
      const btn = $("p-refresh");
      btn.disabled = true;
      say("Ставлю чтение стаканов в очередь…");
      try {
        const r = await postJSON("/api/analysis/positions/refresh", {}, token());
        say(r.note);
        for (let i = 0; i < 20; i++) {
          await new Promise((res) => setTimeout(res, 3000));
          const d = await getJSON("/api/analysis/positions");
          if (!d.checking) {
            lastPositions = d;
            positions(d);
            if (lastJournal) attention(lastJournal, d);
            render();
            say("Стаканы перечитаны.", "ok");
            return;
          }
        }
        say("Сборщик не ответил за минуту — посмотри «Нагрузка».", "err");
      } catch (e) {
        say("Ошибка — " + ((e && e.message) || e), "err");
      } finally {
        btn.disabled = false;
      }
    };

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
    // The period and rehearsals change what the server sends; the rest only
    // change what is shown of it.
    $("j-period").onchange = load;
    $("j-dry").onchange = load;
    $("j-kind").onchange = render;
    $("j-view").onchange = render;
    $("j-search").addEventListener("input", render);
    load();
    // The defence runs on its own clock; a page left open should show it.
    setInterval(load, 60000);
  });
})();
