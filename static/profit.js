// Earnings: each sale paired with the purchase of the same skin.
//
// Wrapped like every other page script — a top-level `const` here would
// collide with common.js and take the whole file down at parse time.

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  function token() {
    try { return localStorage.getItem("csfloat_admin_token") || ""; } catch (e) { return ""; }
  }
  const cash = (v) => (v === null || v === undefined) ? "—"
    : (v < 0 ? "−$" : "$") + Math.abs(Number(v)).toFixed(2);
  const signed = (v) => (v === null || v === undefined) ? "—"
    : (v > 0 ? "+" : "") + cash(v);
  const pct = (v) => (v === null || v === undefined) ? "—"
    : (v > 0 ? "+" : "") + Number(v).toFixed(1) + "%";
  const tone = (v) => (v === null || v === undefined) ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "";
  const flt = (v) => (v === null || v === undefined) ? "—" : Number(v).toFixed(10).replace(/0+$/, "").replace(/\.$/, "");

  let last = null;

  const STATES = {
    queued: "в очереди", pending: "ждёт обмена в Steam",
    verified: "завершена", failed: "не состоялась", cancelled: "отменена",
  };

  function say(text, kind) {
    const el = $("f-note");
    if (!el) return;
    el.textContent = text || "";
    el.className = kind === "err" ? "err" : (kind === "ok" ? "ok" : "muted");
  }

  function when(iso) {
    if (!iso) return "—";
    const t = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
    if (isNaN(t)) return iso;
    return t.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "2-digit" });
  }

  /** Date and time, for a purchase: the day alone does not say which of
   *  today's fills came first. */
  function stamp(iso) {
    if (!iso) return "—";
    const t = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
    if (isNaN(t)) return iso;
    return t.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit",
      year: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  function node(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  }

  function matches(name) {
    const words = (($("f-search") && $("f-search").value) || "")
      .toLowerCase().split(/\s+/).filter(Boolean);
    const low = String(name || "").toLowerCase();
    return words.every((w) => low.includes(w));
  }
  const onlyBot = () => !!($("f-bot") && $("f-bot").checked);

  /** A table that turns into labelled cards on a phone (see .stack). */
  function table(box, heads, rows, empty) {
    box.innerHTML = "";
    if (!rows.length) {
      box.appendChild(node("p", "muted", empty));
      return;
    }
    const t = document.createElement("table");
    t.className = "stat journal stack";
    const head = node("thead");
    const hr = node("tr");
    heads.forEach((h) => hr.appendChild(node("th", "", h)));
    head.appendChild(hr);
    const tb = node("tbody");
    rows.forEach((cells) => {
      const tr = node("tr");
      cells.forEach(([text, cls, title, el], i) => {
        const td = node("td", cls || "", text);
        td.dataset.label = heads[i];
        if (title) td.title = title;
        if (el) td.appendChild(el);
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
    t.appendChild(head);
    t.appendChild(tb);
    box.appendChild(t);
  }

  function tiles(d, closed) {
    const box = $("f-tiles");
    box.innerHTML = "";
    const spent = closed.reduce((a, x) => a + x.bought, 0);
    const profit = closed.reduce((a, x) => a + x.profit, 0);
    const wins = closed.filter((x) => x.profit > 0).length;
    const days = closed.map((x) => x.days).sort((a, b) => a - b);
    const median = days.length ? days[Math.floor(days.length / 2)] : null;
    const h = d.holding_totals || {};
    [
      ["прибыль", signed(Math.round(profit * 100) / 100), tone(profit)],
      ["маржа", spent ? pct(profit / spent * 100) : "—", tone(profit)],
      ["сделок", closed.length ? `${wins}/${closed.length}` : "0", ""],
      ["вложено", cash(spent), ""],
      ["комиссия", cash(closed.reduce((a, x) => a + x.fee, 0)), ""],
      ["срок, медиана", median === null ? "—" : median + " дн", ""],
      ["в наличии", `${h.count || 0} · ${cash(h.spent || 0)}`, ""],
      ["ожидаемая прибыль", h.count ? signed(h.est_profit || 0)
        + (h.est_pct !== null && h.est_pct !== undefined ? ` · ${pct(h.est_pct)}` : "")
        : "—", tone(h.est_profit)],
    ].forEach(([label, value, cls]) => {
      const tile = node("div", "journal-tile");
      tile.appendChild(node("b", cls, value));
      tile.appendChild(node("span", "", label));
      box.appendChild(tile);
    });
    const all = d.all_time || {};
    $("f-alltime").textContent = all.deals
      ? `За всё время: ${all.deals} сделок, прибыль ${signed(all.profit)}`
        + (all.pct !== null && all.pct !== undefined ? ` (${pct(all.pct)})` : "")
        + `, комиссия CSFloat ${(d.fee * 100).toFixed(1)}%.`
      : `Комиссия CSFloat при продаже — ${(d.fee * 100).toFixed(1)}%.`;
  }

  /** The item's name as a link to its sales history, with the order mark
   *  after it - the cell `table` expects. */
  function named(name, byBot) {
    const wrap = node("span");
    if (name) {
      const a = document.createElement("a");
      a.href = "/item/" + encodeURIComponent(name);
      a.className = "item-link";
      a.textContent = name;
      a.title = "история продаж";
      wrap.appendChild(a);
    } else {
      wrap.appendChild(node("span", "", "?"));
    }
    if (byBot) wrap.appendChild(node("span", "muted", "  · ордер бота"));
    return ["", "", "", wrap];
  }

  /** A × that takes trades out of the count; put back from the list below. */
  function dropButton(ids, what) {
    const b = node("button", "att-hide", "×");
    b.title = "убрать из учёта — " + what;
    b.onclick = async () => {
      b.disabled = true;
      try {
        await postJSON("/api/profit/exclude", { trade_ids: ids, excluded: true }, token());
        await load();
      } catch (e) {
        say("Ошибка — " + ((e && e.message) || e), "err");
        b.disabled = false;
      }
    };
    return ["", "drop-cell", "", b];
  }

  function excluded(d) {
    const rows = (d.excluded || []).filter((x) => matches(x.market_hash_name));
    $("f-excluded-box").hidden = !(d.excluded || []).length;
    $("f-excluded-title").textContent = `Убранные из учёта (${(d.excluded || []).length})`;
    table($("f-excluded"), ["", "предмет", "float", "цена", "когда", ""],
      rows.map((x) => {
        const b = node("button", "btn small", "вернуть");
        b.onclick = async () => {
          b.disabled = true;
          try {
            await postJSON("/api/profit/exclude",
              { trade_ids: [x.trade_id], excluded: false }, token());
            await load();
          } catch (e) {
            say("Ошибка — " + ((e && e.message) || e), "err");
            b.disabled = false;
          }
        };
        return [
          [x.role === "buy" ? "покупка" : x.role === "sell" ? "продажа" : "?", "muted"],
          named(x.market_hash_name), [flt(x.float_value), "mono"],
          [cash(x.price)], [when(x.at), "mono"], ["", "", "", b],
        ];
      }), "");
  }

  function settings(d) {
    const s = d.settings || {};
    const since = $("f-since"), days = $("f-est-days");
    // Not while it is being typed into: the page reloads every five minutes.
    if (since && document.activeElement !== since) since.value = s.since || "";
    if (days && document.activeElement !== days) days.value = s.estimate_days || 30;
    const note = $("f-settings-note");
    if (note) {
      const [y, m, dd] = String(s.since || "").split("-");
      note.textContent = (s.since
        ? `Сделки до ${dd}.${m}.${y} не учитываются (купленное до даты и `
          + "проданное после — тоже)."
        : "Учитываются все сделки — задай дату, чтобы отсечь старые.")
        + ` «В наличии» оценивается по медиане продаж за ${s.estimate_days || 30} дн.`;
    }
  }

  function render() {
    const d = last;
    if (!d) return;
    const closed = d.closed.filter((x) => matches(x.market_hash_name)
      && (!onlyBot() || x.by_bot));
    tiles(d, closed);

    table($("f-closed"),
      ["предмет", "float", "паттерн", "купили", "продали", "комиссия", "профит", "%", "дней", ""],
      closed.map((x) => [
        named(x.market_hash_name, x.by_bot),
        [flt(x.float_value), "mono"],
        [x.paint_seed ?? "—", "mono"],
        [`${cash(x.bought)} · ${stamp(x.bought_at)}`],
        [`${cash(x.sold)} · ${stamp(x.sold_at)}`],
        [cash(x.fee), "muted"],
        [signed(x.profit), tone(x.profit)],
        [pct(x.pct), tone(x.profit)],
        [x.days ?? "—", "mono"],
        dropButton([x.buy_id, x.sell_id], "и покупку, и продажу"),
      ]),
      d.trades ? "За этот период закрытых сделок нет."
        : "Сделок ещё нет — нажми «Прочитать сделки».");

    const holding = d.holding.filter((x) => matches(x.market_hash_name)
      && (!onlyBot() || x.by_bot));
    const h = d.holding_totals || {};
    $("f-holding-note").textContent = h.count
      ? `Куплено на ${cash(h.spent)}; по оценке стоит ${cash(h.estimate)}, `
        + `после комиссии это ${signed(h.est_profit)}`
        + (h.est_pct !== null && h.est_pct !== undefined ? ` (${pct(h.est_pct)})` : "")
        + (h.unvalued ? ` — без ${h.unvalued} скин(ов), которые не оценить` : "")
        + ". Оценка, а не сделка: цена может уйти."
        + (h.pending ? ` ${h.pending} из них ещё ждут обмена — если продавец `
          + "не отдаст скин, сделка отменится и деньги вернутся." : "")
      : "";
    table($("f-holding"),
      ["предмет", "float", "паттерн", "купили", "у нас", "оценка", "ожид. профит", "%", ""],
      holding.map((x) => [
        named(x.market_hash_name, x.by_bot),
        [flt(x.float_value), "mono"],
        [x.paint_seed ?? "—", "mono"],
        [`${cash(x.bought)} · ${stamp(x.bought_at)}`],
        [x.pending ? "ждёт обмена" : (x.days === null ? "—" : x.days + " дн"),
          x.pending ? "warn-text" : "mono",
          x.pending ? `сделка ещё не завершена (${STATES[x.state] || x.state || "—"})` : ""],
        [cash(x.estimate), "", x.tracked ? x.basis
          : "предмет не отслеживается — добавь его, чтобы была история продаж"],
        [signed(x.est_profit), tone(x.est_profit)],
        [pct(x.est_pct), tone(x.est_profit)],
        dropButton([x.trade_id], "например, оставил себе или продал не на CSFloat"),
      ]),
      "Всё купленное продано.");

    const unmatched = d.unmatched.filter((x) => matches(x.market_hash_name));
    $("f-unmatched-box").hidden = !unmatched.length;
    table($("f-unmatched"), ["предмет", "float", "паттерн", "продали", "когда", ""],
      unmatched.map((x) => [
        named(x.market_hash_name), [flt(x.float_value), "mono"],
        [x.paint_seed ?? "—", "mono"], [cash(x.price)],
        [when(x.done_at || x.created_at), "mono"],
        dropButton([x.trade_id], "продажа без покупки"),
      ]), "");
    excluded(d);

    const pending = d.pending.filter((x) => matches(x.market_hash_name));
    $("f-pending-box").hidden = !pending.length;
    table($("f-pending"), ["", "предмет", "float", "цена", "состояние", "создана"],
      pending.map((x) => [
        [x.role === "buy" ? "покупка" : x.role === "sell" ? "продажа" : "?"],
        named(x.market_hash_name), [flt(x.float_value), "mono"],
        [cash(x.price)], [STATES[x.state] || x.state || "—", "muted"],
        [when(x.created_at), "mono"],
      ]), "");
  }

  function syncState(d) {
    const el = $("f-sync-state");
    el.textContent = "";
    el.className = "muted";
    if (d.sync_pending) {
      el.textContent = "Чтение сделок поставлено в очередь, жду сборщик…";
      return;
    }
    const s = d.sync;
    if (!s) {
      el.textContent = "Сделки ещё не читались. Сборщик читает их сам раз в "
        + "полчаса, или нажми «Прочитать сделки».";
      return;
    }
    el.textContent = `Прочитано ${when(d.sync_at)}: ${s.seen} сделок`
      + (s.new ? `, новых или изменившихся ${s.new}` : "")
      + `; всего в базе ${d.trades}.`;
    if (s.error) {
      el.className = "err";
      el.appendChild(node("div", "", s.error));
      if (s.tried && s.tried.length) {
        const list = node("ul", "muted");
        s.tried.forEach((line) => list.appendChild(node("li", "", line)));
        el.appendChild(list);
      }
    }
    // The shape of one trade, without its values: what to send when a reply
    // does not parse.
    if (s.sample_keys && s.sample_keys.length && s.error) {
      const det = node("details", "hint");
      det.appendChild(node("summary", "muted", "поля сделки"));
      det.appendChild(node("pre", "preview", s.sample_keys.join("\n")));
      el.appendChild(det);
    }
  }

  async function load() {
    const days = ($("f-days") && $("f-days").value) || "30";
    say("Считаю…");
    try {
      last = await getJSON(`/api/profit?days=${encodeURIComponent(days)}`);
      settings(last);
      syncState(last);
      render();
      say("", "ok");
    } catch (e) {
      say("Ошибка — " + ((e && e.message) || e), "err");
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("f-reload").onclick = load;
    $("f-days").onchange = load;
    $("f-bot").onchange = render;
    $("f-search").addEventListener("input", render);
    $("f-save").onclick = async () => {
      const btn = $("f-save");
      btn.disabled = true;
      try {
        await postJSON("/api/profit/settings", {
          since: $("f-since").value || "",
          estimate_days: $("f-est-days").value,
        }, token());
        $("f-since").blur();
        $("f-est-days").blur();
        await load();
        say("Сохранено.", "ok");
      } catch (e) {
        say("Не сохранено — " + ((e && e.message) || e), "err");
      } finally {
        btn.disabled = false;
      }
    };
    $("f-sync").onclick = async () => {
      const btn = $("f-sync");
      btn.disabled = true;
      try {
        const r = await postJSON("/api/profit/sync", {}, token());
        say(r.note);
        for (let i = 0; i < 40; i++) {
          await new Promise((res) => setTimeout(res, 3000));
          const d = await getJSON("/api/profit?days=0");
          if (!d.sync_pending) { await load(); say("Сделки прочитаны.", "ok"); return; }
        }
        say("Сборщик не ответил за две минуты — посмотри «Нагрузка».", "err");
      } catch (e) {
        say("Ошибка — " + ((e && e.message) || e), "err");
      } finally {
        btn.disabled = false;
      }
    };
    load();
    setInterval(load, 300000);
  });
})();
