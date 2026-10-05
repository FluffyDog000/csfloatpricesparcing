// The order journal: what stands now, what needs a look, and what happened.
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
  const SOURCE = { plan: "план", defence: "защита", sync: "сверка",
    guard: "защита от слива", manual: "кнопкой" };
  // This many orders failing for one reason fold into one line.
  const FOLD_AT = 3;
  // A refusal on an order still held stops being news once this old; one on
  // an order never placed, sooner - the next plan has been and gone.
  const FAIL_FRESH_HOURS = 24;
  const PLACE_FRESH_HOURS = 6;
  const HIDDEN_KEY = "journal_hidden_problems";

  // The last replies, kept so a filter re-renders what is loaded rather than
  // asking the server again.
  let lastJournal = null;
  let lastPositions = null;
  // Which orders are unfolded: the page reloads every minute, and a history
  // that snaps shut while being read is worse than no history.
  const opened = new Set();
  let showHidden = false;

  window.JOURNAL_BUILD = (document.currentScript
    && document.currentScript.src || "").split("?v=")[1] || "?";

  function say(text, kind) {
    const el = $("j-note");
    if (!el) return;
    el.textContent = text || "";
    el.className = kind === "err" ? "err" : (kind === "ok" ? "ok" : "muted");
  }
  function bar(text, kind) {
    const el = $("p-note");
    if (!el) return;
    el.textContent = text || "";
    el.className = kind === "err" ? "err" : (kind === "ok" ? "ok" : "muted");
  }

  function parseT(iso) {
    if (!iso) return NaN;
    const s = String(iso);
    return new Date(s.endsWith("Z") || s.includes("+") ? s : s + "Z").getTime();
  }

  /** Local wall-clock time: the log is written in UTC, read at a desk. */
  function when(iso) {
    if (!iso) return "—";
    const t = new Date(parseT(iso));
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
    if (/noroute|адреса главного ключа недоступны/.test(low))
      return "прокси главного ключа не отвечал — бот повторит";
    if (/429|too many|rate limit|лимит/.test(low))
      return "лимит запросов CSFloat — бот повторит позже";
    if (/http 401|http 403|unauthori|forbidden|учётные данные|отклонил ключ/.test(low))
      return "CSFloat не принял главный ключ (CSFLOAT_API_KEY в .env)";
    if (/http 404|not found|unknown buy order/.test(low))
      return "ордера уже нет на сайте";
    if (/http 400/.test(low)) {
      const rest = raw.split("—").slice(1).join("—").trim();
      return "CSFloat отклонил параметры ордера" + (rest ? ": " + rest : "");
    }
    if (/http 5\d\d/.test(low))
      return "сбой на стороне CSFloat — бот повторит позже";
    if (/timeout|timed out|connection|proxy|network|open files/.test(low))
      return "сеть или прокси не ответили";
    return raw || "без пояснения";
  }

  function search() {
    return (($("j-search") && $("j-search").value) || "")
      .toLowerCase().split(/\s+/).filter(Boolean);
  }
  const matches = (name, words) => {
    const low = String(name || "").toLowerCase();
    return words.every((w) => low.includes(w));
  };

  function periodHours() {
    const v = ($("j-period") && $("j-period").value) || "168";
    if (v === "today") {
      const now = new Date();
      const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      return Math.max((now - midnight) / 3600000, 0.01);
    }
    return Number(v) || 0;
  }

  function hiddenSet() {
    try { return new Set(JSON.parse(localStorage.getItem(HIDDEN_KEY) || "[]")); }
    catch (e) { return new Set(); }
  }
  function hide(key) {
    const set = hiddenSet();
    set.add(key);
    // Old keys go: a problem's key carries its time, and a newer failure is a
    // new key, so the list only ever needs the recent ones.
    const keep = [...set].slice(-300);
    try { localStorage.setItem(HIDDEN_KEY, JSON.stringify(keep)); } catch (e) { /* private window */ }
  }

  const index = (pos) => {
    const out = {};
    ((pos && pos.orders) || []).forEach((r) => { out[keyOf(r.item, r)] = r; });
    return out;
  };
  const atCeiling = (r) => r.room !== undefined && r.room !== null && r.room < 0.005;

  // -- what comes next for an order ------------------------------------------

  /** What the bot will do with this order next, in words - so "outbid and
   *  left alone" reads as a decision, not as the bot having missed it. */
  function outlook(r, defend) {
    if (r.state === "manual") return ["поставлен вручную — бот его не ведёт", "muted"];
    if (!r.book) return ["стакан не читан — положение неизвестно", "muted"];
    const ceil = cash(r.ceiling);
    if (r.price > r.ceiling + 0.005) {
      return [`цена выше нового потолка ${ceil} — защита снизит до потолка`, "warn"];
    }
    if (r.first) {
      return atCeiling(r)
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

  // -- tiles -------------------------------------------------------------------

  function summary(d, pos) {
    const box = $("j-summary");
    box.innerHTML = "";
    const rows = ((pos && pos.orders) || []).filter((r) => r.state !== "manual");
    const read = rows.filter((r) => r.book);
    const day = Date.now() - 24 * 3600 * 1000;
    const counts = {};
    (d.events || []).filter((e) => !e.dry && parseT(e.at) >= day).forEach((e) => {
      const key = e.ok ? e.kind : "fail";
      counts[key] = (counts[key] || 0) + 1;
    });
    const tiles = [
      ["стоит ордеров", pos ? rows.length : d.held, ""],
      [pos && pos.allowance ? `на сумму (лимит CSFloat ${cash(pos.allowance)})`
        : "на сумму", pos ? cash(pos.face) : "—",
        pos && pos.allowance && pos.face > pos.allowance ? "bad" : ""],
      ["первые", read.filter((r) => r.first).length, "good"],
      ["перебиты", read.filter((r) => !r.first).length,
        read.some((r) => !r.first) ? "bad" : ""],
      ["на потолке", read.filter((r) => r.first && atCeiling(r)).length, ""],
      ["поставлено за сутки", counts.place || 0, ""],
      ["поднято / снижено", `${counts.raise || 0} / ${counts.lower || 0}`, ""],
      ["исполнено за сутки", counts.fill || 0, "good"],
      ["отказов за сутки", counts.fail || 0, counts.fail ? "bad" : ""],
    ];
    tiles.forEach(([label, value, tone]) => {
      const tile = node("div", "journal-tile" + (tone ? " " + tone : ""));
      tile.appendChild(node("b", "", value));
      tile.appendChild(node("span", "", label));
      box.appendChild(tile);
    });
  }

  // -- problems ----------------------------------------------------------------

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

  /** Only what is still wrong. A refusal on an order the defence has looked
   *  at again since is settled one way or the other; one on an order that is
   *  not held any more is about nothing; and a day is long enough to say it. */
  function problems(d, pos) {
    const out = [];
    const idx = index(pos);
    const now = Date.now();
    const fresh = openFailures(d.events || []).filter((f) => {
      const at = parseT(f.e.at);
      const r = idx[keyOf(f.e.market_hash_name, f.e)];
      if (r) {
        if (r.verdict && parseT(r.verdict.at) > at) return false;
        return now - at <= FAIL_FRESH_HOURS * 3600e3;
      }
      return f.e.kind === "place" && now - at <= PLACE_FRESH_HOURS * 3600e3;
    });

    // One cause failing many orders is one problem, not forty lines.
    const groups = new Map();
    fresh.forEach((f) => {
      const what = (KIND[f.e.kind] || [f.e.kind])[0];
      const why = explain(f.e);
      const key = what + "|" + why;
      if (!groups.has(key)) groups.set(key, { what, why, rows: [] });
      groups.get(key).rows.push(f);
    });
    groups.forEach(({ what, why, rows }) => {
      const latest = rows.reduce((a, b) => (a.e.at > b.e.at ? a : b)).e;
      if (rows.length < FOLD_AT) {
        rows.forEach(({ e, count }) => out.push({
          key: "fail|" + keyOf(e.market_hash_name, e) + "|" + e.at,
          orders: [keyOf(e.market_hash_name, e)],
          level: "bad", at: e.at, title: e.detail || e.reason || "",
          text: `${e.market_hash_name} ${bandOf(e)}: не ${what}`
            + (count > 1 ? ` (${count} раза подряд)` : "") + ` — ${why}`,
        }));
        return;
      }
      out.push({
        key: "group|" + what + "|" + why + "|" + latest.at,
        orders: rows.map(({ e }) => keyOf(e.market_hash_name, e)),
        level: "bad", at: latest.at, title: latest.detail || latest.reason || "",
        text: `${rows.length} ордер(ов): не ${what} — ${why}`,
        list: rows.map(({ e, count }) => `${e.market_hash_name} ${bandOf(e)}`
          + (count > 1 ? ` (${count} раза подряд)` : "")),
      });
    });

    const orders = (pos && pos.orders) || [];
    if (!d.defend) {
      orders.filter((r) => r.state !== "manual" && r.book && !r.first
        && r.top < r.ceiling - 0.005).forEach((r) => out.push({
        key: "outbid|" + keyOf(r.item, r) + "|" + r.top, level: "warn",
        text: `${r.item} ${bandOf(r)}: перебили (${cash(r.top)}), поднять можно `
          + `до ${cash(r.ceiling)} — автозащита выключена`,
      }));
    }
    const unread = orders.filter((r) => r.state !== "manual" && !r.book).length;
    if (unread) {
      out.push({ key: "unread|" + unread, level: "warn",
        text: `стакан не читан у ${unread} ордер(ов) — нажми «Обновить стаканы»` });
    }
    const manual = orders.filter((r) => r.state === "manual").length;
    if (manual) {
      out.push({ key: "manual|" + manual, level: "warn",
        text: `${manual} ордер(ов) числятся поставленными вручную — бот их не ведёт. `
          + "Если их ставил бот, нажми «Сверить с аккаунтом»: сверка вернёт их боту" });
    }
    if (d.sync && d.sync.error) {
      out.push({ key: "sync|" + (d.sync_at || ""), level: "bad",
        text: "сверка с аккаунтом не удалась: " + d.sync.error });
    }
    return out;
  }

  function renderProblems(d, pos) {
    const box = $("j-attention");
    if (!box) return;
    box.innerHTML = "";
    const all = problems(d, pos);
    const hidden = hiddenSet();
    const shown = showHidden ? all : all.filter((p) => !hidden.has(p.key));
    const hiddenCount = all.length - all.filter((p) => !hidden.has(p.key)).length;
    const title = $("j-attention-title");
    if (title) title.textContent = shown.length ? `Проблемы (${shown.length})` : "Проблемы";
    const unhide = $("j-unhide");
    if (unhide) {
      unhide.hidden = !hiddenCount;
      unhide.textContent = showHidden ? "спрятать скрытые" : `показать скрытые (${hiddenCount})`;
    }
    if (!shown.length) {
      box.appendChild(node("p", "ok", hiddenCount
        ? "Новых проблем нет."
        : "Проблем нет: отказов не осталось, сверка в порядке."));
      return;
    }
    const list = node("ul", "attention");
    shown.forEach((p) => {
      const li = node("li", "att-" + p.level);
      if (p.title) li.title = p.title;
      const text = node("div", "att-text");
      if (p.list) {
        const det = node("details");
        det.appendChild(node("summary", "", p.text));
        const inner = node("ul", "muted");
        p.list.forEach((line) => inner.appendChild(node("li", "", line)));
        det.appendChild(inner);
        text.appendChild(det);
      } else {
        text.appendChild(node("span", "", p.text));
      }
      if (p.at) text.appendChild(node("span", "muted att-when", " · " + when(p.at)));
      li.appendChild(text);
      if (!hidden.has(p.key)) {
        const x = node("button", "att-hide", "×");
        x.title = "скрыть — вернётся, если случится снова";
        x.onclick = () => { hide(p.key); renderProblems(lastJournal, lastPositions); };
        li.appendChild(x);
      }
      list.appendChild(li);
    });
    box.appendChild(list);
  }

  // -- orders standing now -----------------------------------------------------

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

  function chipOf(r) {
    if (r.state === "manual") return ["вручную", "st-muted"];
    if (!r.book) return ["стакан не читан", "st-muted"];
    if (!r.first) return [`перебит: впереди ${r.ahead}`, "st-bad"];
    return atCeiling(r) ? ["первые · на потолке", "st-ok"] : ["первые", "st-ok"];
  }

  /** Worst first: the ones to look at should not be under forty fine ones. */
  function weight(r, failing) {
    if (r.state === "manual") return 4;
    if (failing.has(keyOf(r.item, r))) return 0;
    if (r.book && !r.first) return 1;
    if (!r.book) return 2;
    return 3;
  }

  function positions(pos) {
    const box = $("p-table");
    box.innerHTML = "";
    const rows = (pos && pos.orders) || [];
    if (!rows.length) {
      box.appendChild(node("p", "muted", "Сейчас ордеров нет."));
      return;
    }
    const words = search();
    const show = ($("j-show") && $("j-show").value) || "";
    const events = (lastJournal && lastJournal.events) || [];
    const byKey = new Map();
    events.forEach((e) => {
      const k = keyOf(e.market_hash_name, e);
      if (!byKey.has(k)) byKey.set(k, []);
      byKey.get(k).push(e);
    });
    const failing = new Set(problems(lastJournal || { events: [] }, pos)
      .flatMap((p) => p.orders || []));

    const list = node("div", "order-groups");
    rows.filter((r) => matches(r.item, words))
      .filter((r) => !show
        || (show === "outbid" && r.book && !r.first)
        || (show === "first" && r.book && r.first)
        || (show === "ceiling" && r.book && r.first && atCeiling(r)))
      .sort((a, b) => weight(a, failing) - weight(b, failing)
        || a.item.localeCompare(b.item) || a.float_min - b.float_min)
      .forEach((r) => {
        const key = keyOf(r.item, r);
        const [chip, cls] = chipOf(r);
        const det = node("details", "order-group"
          + (cls === "st-bad" || failing.has(key) ? " og-bad" : "")
          + (r.state === "manual" ? " og-manual" : ""));
        det.open = opened.has(key);
        det.addEventListener("toggle", () => {
          if (det.open) opened.add(key); else opened.delete(key);
        });
        const sum = node("summary");
        const head = node("div", "og-head");
        head.appendChild(node("span", "og-name", r.item));
        head.appendChild(node("span", "og-state " + cls, chip));
        sum.appendChild(head);
        const meta = node("div", "og-meta");
        meta.appendChild(node("span", "mono", bandOf(r)));
        meta.appendChild(node("span", "og-price", cash(r.price)
          + ((r.quantity || 1) > 1 ? ` ×${r.quantity}` : "")));
        if (r.state !== "manual") {
          meta.appendChild(node("span", "og-ceil", `потолок ${cash(r.ceiling)} · `
            + (atCeiling(r) ? "на потолке" : `запас ${cash(r.room)}`)));
        }
        if (r.book && !r.first) {
          meta.appendChild(node("span", "err", `верх стакана ${cash(r.top)}`));
        }
        if (failing.has(key)) meta.appendChild(node("span", "err", "есть отказ — см. проблемы"));
        sum.appendChild(meta);
        const [next, tone] = outlook(r, lastJournal && lastJournal.defend);
        sum.appendChild(node("div", "og-next og-" + tone, next));
        det.appendChild(sum);

        const v = verdictLine(r);
        if (v) det.appendChild(node("p", "og-next muted", v));
        const own = byKey.get(key) || [];
        if (own.length) {
          const chain = node("ol", "og-chain");
          own.slice().reverse().forEach((e) => chain.appendChild(chainItem(e)));
          det.appendChild(chain);
        } else {
          det.appendChild(node("p", "og-next muted",
            "За выбранный в истории период событий по ордеру нет."));
        }
        det.appendChild(node("p", "og-next muted",
          `стакан читан ${r.swept_at ? when(r.swept_at) : "—"} · наш ордер узнан `
          + (!r.book ? "—" : r.seen_in_book ? "по id" : "по цене и float")));
        list.appendChild(det);
      });
    if (!list.children.length) {
      box.appendChild(node("p", "muted", "Под фильтр ничего не попало."));
      return;
    }
    box.appendChild(list);
  }

  // -- history -----------------------------------------------------------------

  function history(d) {
    const box = $("j-table");
    box.innerHTML = "";
    const kind = ($("j-kind") && $("j-kind").value) || "";
    const words = search();
    const events = (d.events || []).filter((e) => {
      if (kind === "fail" && e.ok) return false;
      if (kind && kind !== "fail" && !(e.ok && e.kind === kind)) return false;
      return matches(e.market_hash_name, words);
    });
    const total = (d.events || []).length;
    say((events.length === total ? `${total} записей` : `${events.length} из ${total} записей`)
      + (d.truncated ? " — показаны последние, сузь период" : "") + ".", "ok");
    if (!events.length) {
      box.appendChild(node("p", "muted", total ? "Под фильтр ничего не попало."
        : "Пока ничего не происходило."));
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

  // -- status lines ------------------------------------------------------------

  /** What the last comparison with the account found. */
  function syncState(d) {
    const el = $("j-sync-state");
    if (!el) return;
    el.textContent = "";
    el.className = "muted";
    if (d.sync_pending) {
      el.textContent = "Сверка с аккаунтом в очереди, жду сборщик…";
      return;
    }
    const s = d.sync;
    if (!s) {
      el.textContent = "С аккаунтом ещё не сверялись — число ордеров взято из "
        + "того, что бот записал себе. Нажми «Сверить с аккаунтом».";
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
    const c = s.counts || {};
    const bits = [];
    if (s.found_path) bits.push(`список ордеров нашёлся: ${s.found_path}`);
    if (c.matched) bits.push(`${c.matched} совпало`);
    if (c.filled) bits.push(`${c.filled} исполнено`);
    if (c.gone) bits.push(`${c.gone} ушло с сайта`);
    if (c.repriced) bits.push(`${c.repriced} с другой ценой`);
    if (c.adopted) bits.push(`${c.adopted} не наших`);
    if (s.revived) bits.push(`${s.revived} снова найдены`);
    if (c.duplicate) bits.push(`${c.duplicate} повторных записей убрано`);
    el.textContent = `Сверка ${when(d.sync_at)}: на аккаунте ${s.seen} ордер(ов)`
      + (bits.length ? " — " + bits.join(", ") : "") + ".";
  }

  function state(d) {
    const el = $("j-state");
    if (!el) return;
    el.textContent = d.defend
      ? `Автозащита: каждые ${d.defend_minutes} мин`
        + (d.defend_at ? `, последняя проверка ${when(d.defend_at)}` : ", ещё не запускалась")
      : "Автозащита выключена — перебитые ордера останутся как есть.";
  }

  function render() {
    if (!lastJournal) return;
    summary(lastJournal, lastPositions);
    renderProblems(lastJournal, lastPositions);
    if (lastPositions) positions(lastPositions);
    history(lastJournal);
  }

  async function load() {
    const dry = $("j-dry").checked ? "1" : "0";
    const hours = periodHours();
    try {
      const d = await getJSON(`/api/analysis/journal?limit=2000&dry=${dry}`
        + (hours ? `&hours=${hours.toFixed(2)}` : ""));
      lastJournal = d;
      try {
        lastPositions = await getJSON("/api/analysis/positions");
      } catch (e) {
        lastPositions = null;
        $("p-table").innerHTML = "";
        bar("Ордера не загрузились — " + ((e && e.message) || e), "err");
      }
      state(d);
      syncState(d);
      render();
    } catch (e) {
      bar("Ошибка — " + ((e && e.message) || e), "err");
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("j-reload").onclick = load;

    $("p-refresh").onclick = async () => {
      const btn = $("p-refresh");
      btn.disabled = true;
      bar("Ставлю чтение стаканов в очередь…");
      try {
        const r = await postJSON("/api/analysis/positions/refresh", {}, token());
        bar(r.note);
        for (let i = 0; i < 20; i++) {
          await new Promise((res) => setTimeout(res, 3000));
          const d = await getJSON("/api/analysis/positions");
          if (!d.checking) {
            lastPositions = d;
            render();
            bar("Стаканы перечитаны.", "ok");
            return;
          }
        }
        bar("Сборщик не ответил за минуту — посмотри «Нагрузка».", "err");
      } catch (e) {
        bar("Ошибка — " + ((e && e.message) || e), "err");
      } finally {
        btn.disabled = false;
      }
    };

    $("j-sync").onclick = async () => {
      const btn = $("j-sync");
      btn.disabled = true;
      bar("Ставлю сверку в очередь…");
      try {
        const r = await postJSON("/api/analysis/sync", {}, token());
        bar(r.note);
        // The collector answers on its own schedule; poll rather than guess.
        for (let i = 0; i < 20; i++) {
          await new Promise((res) => setTimeout(res, 3000));
          const d = await getJSON("/api/analysis/journal?limit=1");
          if (!d.sync_pending) { await load(); bar("Сверка выполнена.", "ok"); return; }
        }
        bar("Сборщик не ответил за минуту — посмотри «Нагрузка», "
          + "не на паузе ли он.", "err");
      } catch (e) {
        bar("Ошибка — " + ((e && e.message) || e), "err");
      } finally {
        btn.disabled = false;
      }
    };
    $("j-weakest").onclick = async () => {
      const btn = $("j-weakest");
      btn.disabled = true;
      try {
        bar("Считаю план, чтобы найти самый слабый ордер…");
        const p = await postJSON("/api/analysis/cancel_lowest", { preview: true }, token());
        if (!confirm(`Снять ордер ${p.order}?`)) { bar(""); return; }
        const r = await postJSON("/api/analysis/cancel_lowest", {}, token());
        bar(r.note, "ok");
        setTimeout(load, 8000);
      } catch (e) {
        bar("Не снят — " + ((e && e.message) || e), "err");
      } finally {
        btn.disabled = false;
      }
    };
    $("j-unhide").onclick = () => {
      showHidden = !showHidden;
      renderProblems(lastJournal, lastPositions);
    };
    // The period and rehearsals change what the server sends; the rest only
    // change what is shown of it.
    $("j-period").onchange = load;
    $("j-dry").onchange = load;
    $("j-kind").onchange = render;
    $("j-show").onchange = render;
    $("j-search").addEventListener("input", render);
    load();
    // The defence runs on its own clock; a page left open should show it.
    setInterval(load, 60000);
  });
})();
