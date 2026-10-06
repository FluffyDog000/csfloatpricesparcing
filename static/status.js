// The bot's status panel: what runs on its own, when it runs next, and what
// is going out or being read right now. Shared by the analysis and journal
// pages; renders into #bot-status when the page has one.
(function () {
  "use strict";
  const box = () => document.getElementById("bot-status");
  let last = null;

  const SOURCES = { plan: "план", auto: "автодобор", room: "освобождение места",
                    manual: "кнопка", defence: "защита" };

  function minutesText(m) {
    m = Math.round(m);
    if (m < 60) return `${m} мин`;
    const h = Math.floor(m / 60), r = m % 60;
    if (h < 48) return r ? `${h} ч ${r} мин` : `${h} ч`;
    return `${Math.round(h / 24)} дн`;
  }

  function rel(iso) {
    if (!iso) return "—";
    const t = Date.parse(iso);
    if (isNaN(t)) return "—";
    const diff = (t - Date.now()) / 60000;
    if (diff >= 1) return "через " + minutesText(diff);
    if (diff > -1) return "сейчас";
    return minutesText(-diff) + " назад";
  }

  /** Due, but the collector has not got to it yet: it checks once a minute
   *  and waits behind a sweep or a plan in progress. */
  function nextText(iso) {
    if (!iso) return "после запуска";
    return Date.parse(iso) <= Date.now() + 30000 ? "вот-вот" : rel(iso);
  }

  function running(state, hours) {
    if (!state || state.finished_at) return false;
    const t = Date.parse(state.started_at || "");
    return !isNaN(t) && Date.now() - t < hours * 3600000;
  }

  function row(label, tone, text) {
    const r = document.createElement("div");
    r.className = "bs-row";
    const dot = document.createElement("span");
    dot.className = "bs-dot " + tone;
    const b = document.createElement("b");
    b.textContent = label;
    const s = document.createElement("span");
    s.className = "bs-text";
    s.textContent = text;
    r.appendChild(dot);
    r.appendChild(b);
    r.appendChild(s);
    return r;
  }

  function fillText(res) {
    if (!res) return "";
    if (res.skipped) return `пропуск — ${res.skipped}`;
    return `поставлено в очередь ${res.queued || 0}`
      + (res.swept ? `, на обход ${res.swept}` : "");
  }

  function sweepText(res) {
    if (!res) return "";
    if (res.skipped) return `пропуск — ${res.skipped}`;
    return `отправлено ${res.queued || 0}, свежих ${res.fresh || 0}`
      + (res.screened ? `, отсеяно ${res.screened}` : "");
  }

  function render() {
    const el = box();
    if (!el || !last) return;
    const d = last;
    const rows = [];

    const df = d.defence || {};
    rows.push(df.on
      ? row("Защита", "on", `каждые ${df.minutes} мин · последняя ${rel(df.last_at)}`
          + ` · следующая ${nextText(df.next_at)}`)
      : row("Защита", "off", "выключена"));

    const sw = d.auto_sweep || {};
    rows.push(sw.on
      ? row("Автообход стаканов", "on", `каждые ${minutesText(sw.minutes)} · следующий `
          + nextText(sw.next_at)
          + (sw.result ? ` · прошлый ${rel(sw.result.at)}: ${sweepText(sw.result)}` : ""))
      : row("Автообход стаканов", "off",
          "выключен — стаканы анализа читаются только кнопкой «Обойти стаканы»"));

    const af = d.auto_fill || {};
    rows.push(af.on
      ? row("Автодобор", "on", `каждые ${af.minutes} мин`
          + (sw.on ? " и сразу после автообхода" : "")
          + ` · следующий ${nextText(af.next_at)}`
          + (af.result ? ` · прошлый ${rel(af.result.at)}: ${fillText(af.result)}` : ""))
      : row("Автодобор", "off", "выключен"));

    const sweep = d.sweep || {};
    const st = sweep.state;
    if (running(st, 3)) {
      const cur = (st.current || []);
      rows.push(row("Обход сейчас", "busy",
        `идёт: ${st.done} из ${st.total}`
        + (cur.length ? ` · читаю: ${cur.slice(0, 3).join(", ")}`
          + (cur.length > 3 ? ` и ещё ${cur.length - 3}` : "") : "")));
    } else if (sweep.queued) {
      rows.push(row("Обход сейчас", "wait",
        `в очереди ${sweep.queued}: ${(sweep.queued_names || []).join(", ")}`
        + (sweep.queued > (sweep.queued_names || []).length ? " …" : "")
        + " — сборщик начнёт в ближайшую минуту"));
    } else {
      rows.push(row("Обход сейчас", "idle", st && st.finished_at
        ? `не идёт · последний закончился ${rel(st.finished_at)}: ${st.done} предм.`
          + (st.failed ? `, ошибок ${st.failed}` : "")
        : "не идёт"));
    }

    const pl = d.placing || {};
    const ps = pl.state;
    if (running(ps, 1)) {
      rows.push(row("Выставление сейчас", "busy",
        `идёт (${SOURCES[ps.source] || ps.source}${ps.dry_run ? ", вхолостую" : ""}): `
        + `${ps.done} из ${ps.total}` + (ps.current ? ` · ${ps.current}` : "")));
    } else if (pl.pending) {
      rows.push(row("Выставление сейчас", "wait",
        `в очереди ${pl.pending} действий (${SOURCES[pl.pending_source] || pl.pending_source})`
        + " — сборщик выполнит в ближайшую минуту"));
    } else {
      rows.push(row("Выставление сейчас", "idle", ps && ps.finished_at
        ? `не идёт · последнее ${rel(ps.finished_at)} (${SOURCES[ps.source] || ps.source}`
          + `${ps.dry_run ? ", вхолостую" : ""}): успешно ${ps.ok} из ${ps.total}`
        : "не идёт"));
    }

    const bk = d.books || {};
    if (bk.items) {
      const tone = bk.fresh === bk.items ? "on" : (bk.fresh ? "wait" : "warn");
      rows.push(row("Стаканы анализа", tone,
        `свежие (до ${bk.fresh_hours} ч): ${bk.fresh} из ${bk.items}`
        + (bk.stale ? ` · устаревшие: ${bk.stale}` : "")
        + (bk.never ? ` · ни разу не читались: ${bk.never}` : "")
        + (bk.oldest ? ` · самый старый прочитан ${rel(bk.oldest)}` : "")));
    }

    const cr = d.creates || {};
    if (cr.limit) {
      rows.push(row("Создания ордеров", cr.left ? "on" : "warn",
        `за сутки ${cr.used} из ${cr.limit} · осталось ${cr.left}`
        + (cr.reset && cr.left < cr.limit ? ` · освобождаться начнут ${rel(cr.reset)}` : "")));
    }

    if ((d.waiting || []).length) {
      rows.push(row("Пауза", "warn", d.waiting.join("; ")));
    }

    el.innerHTML = "";
    const head = document.createElement("div");
    head.className = "bs-head";
    head.textContent = "Статус бота";
    el.appendChild(head);
    rows.forEach((r) => el.appendChild(r));
  }

  async function load() {
    if (!box()) return;
    try {
      const resp = await fetch("/api/bot_status", { headers: { Accept: "application/json" } });
      if (!resp.ok) return;
      last = await resp.json();
      render();
    } catch (e) {
      /* the panel stays as it was; the page itself reports connection trouble */
    }
  }

  window.botStatus = { load, render };
  document.addEventListener("DOMContentLoaded", () => {
    load();
    setInterval(load, 30000);
    setInterval(render, 15000);
  });
})();
