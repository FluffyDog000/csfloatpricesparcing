// Order analysis: what would we bid, and why not the bands we skip.
function token() {
  try { return localStorage.getItem("csfloat_admin_token") || ""; } catch (e) { return ""; }
}
const $ = (id) => document.getElementById(id);
const money = (v) => (v === null || v === undefined) ? "—" : "$" + v.toFixed(2);
const pct = (v) => (v === null || v === undefined) ? "—" : (v * 100).toFixed(1) + "%";
const days = (v) => (v === null || v === undefined) ? "—" : v.toFixed(1) + " д";

function note(text, bad) {
  const el = $("an-note");
  el.textContent = text || "";
  el.className = bad ? "err" : "muted";
}

async function loadItems() {
  const data = await getJSON("/api/analysis");
  renderList(data.items.map((i) => i.item));
  renderResults(data);
  return data;
}

function renderList(names) {
  const box = $("an-list");
  box.innerHTML = "";
  if (!names.length) {
    box.innerHTML = '<span class="muted">список пуст — добавь предмет выше</span>';
    return;
  }
  names.forEach((name) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = name;
    const x = document.createElement("button");
    x.className = "chip-x";
    x.textContent = "×";
    x.title = "убрать";
    x.onclick = async () => {
      await postJSON("/api/analysis/items",
        { market_hash_name: name, action: "remove" }, token());
      loadItems();
    };
    chip.appendChild(x);
    box.appendChild(chip);
  });
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
  if (data.waiting && data.waiting.length) {
    note("Сборщик занят: " + data.waiting.join("; "));
  }
  data.items.forEach((it) => {
    const sec = document.createElement("section");
    sec.className = "settings-block";
    const take = it.bands.filter((b) => b.take);
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
      (it.depth ? ` · листингов по полосам ${it.depth}` : " · листинги не собраны (цена выхода из истории, завышена)");
    sec.appendChild(meta);

    if (!it.orders) {
      const warn = document.createElement("p");
      warn.className = "err";
      warn.textContent = "Стакан не собран — нажми «Обойти стаканы».";
      sec.appendChild(warn);
    }

    const sum = document.createElement("p");
    sum.innerHTML = take.length
      ? `<b>${take.length}</b> ордер(ов) · капитал <b>${money(it.capital)}</b>` +
        ` · ожидаемо <b>${money(it.monthly)}</b>/мес` +
        ` (<b>${it.capital ? (it.monthly / it.capital * 100).toFixed(0) : 0}%</b>)`
      : "<b>Ни одной полосы не проходит.</b> Причины ниже.";
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
    const items = await getJSON("/api/items");
    const dl = $("an-known");
    dl.innerHTML = "";
    (items.items || items || []).forEach((it) => {
      const name = it.market_hash_name || it.name;
      if (!name) return;
      const o = document.createElement("option");
      o.value = name;
      dl.appendChild(o);
    });
  } catch (e) { /* the datalist is a convenience, not a requirement */ }
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

document.addEventListener("DOMContentLoaded", async () => {
  fillKnown();
  try {
    const data = await loadItems();
    fillParams(data.params);
  } catch (e) { note(e.message, true); }

  $("an-add").onclick = async () => {
    const name = $("an-name").value.trim();
    if (!name) return;
    try {
      await postJSON("/api/analysis/items", { market_hash_name: name }, token());
      $("an-name").value = "";
      note("");
      loadItems();
    } catch (e) { note(e.message, true); }
  };
  $("an-name").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("an-add").click();
  });

  $("an-sweep").onclick = async () => {
    try {
      const r = await postJSON("/api/analysis/sweep", {}, token());
      note(r.note);
      // The collector does the fetching, so poll until the books land.
      let left = 20;
      const timer = setInterval(async () => {
        if (--left <= 0) return clearInterval(timer);
        try { await loadItems(); } catch (e) { /* keep polling */ }
      }, 6000);
    } catch (e) { note(e.message, true); }
  };

  $("an-run").onclick = async () => {
    try { await loadItems(); note("Пересчитано."); }
    catch (e) { note(e.message, true); }
  };

  $("an-clear").onclick = async () => {
    if (!confirm("Очистить список предметов для анализа?")) return;
    try {
      await postJSON("/api/analysis/items", { action: "clear" }, token());
      loadItems();
    } catch (e) { note(e.message, true); }
  };

  $("p-save").onclick = async () => {
    try {
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
      note("Пороги сохранены.");
      loadItems();
    } catch (e) { note(e.message, true); }
  };
});
