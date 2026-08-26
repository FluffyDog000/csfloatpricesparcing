// Profit calculator. Every input carries its own currency, so a skin bought in
// CNY and sold in USD works without the user converting anything by hand.

const RATE = (window.CFG_RATE && Number(window.CFG_RATE)) || null;

function toUsd(value, cur) {
  if (value === null) return null;
  return cur === "cny" && RATE ? value / RATE : value;
}

function fromUsd(usd, cur) {
  if (usd === null) return null;
  return cur === "cny" && RATE ? usd * RATE : usd;
}

function fmt(usd, cur) {
  if (usd === null || !isFinite(usd)) return "—";
  const v = fromUsd(usd, cur);
  const sign = v < 0 ? "-" : "";
  return sign + (cur === "cny" ? "¥" : "$") + Math.abs(v).toFixed(2);
}

function num(id) {
  const raw = document.getElementById(id).value;
  if (raw === "" || raw === null) return null;
  const n = Number(raw);
  return isFinite(n) ? n : null;
}

function row(label, value, note, cls) {
  return `<tr><td>${label}</td><td class="${cls || ""}">${value}</td>
    <td class="muted">${note || ""}</td></tr>`;
}

function calc() {
  const outCur = document.getElementById("out-cur").value;
  const buy = toUsd(num("buy"), document.getElementById("buy-cur").value);
  const sell = toUsd(num("sell"), document.getElementById("sell-cur").value);
  const feePct = num("fee") ?? 0;
  const body = document.getElementById("calc-result");

  if (buy === null || sell === null) {
    body.innerHTML = row("—", "введи обе цены", "прибыль считается от цены покупки");
    return;
  }

  const fee = sell * (feePct / 100);
  const net = sell - fee;              // what actually reaches you
  const profit = net - buy;
  // Percent is measured against what you put in, which is the buy price: it
  // answers "how much did this position earn", not "what margin on the sale".
  const pct = buy > 0 ? (profit / buy) * 100 : null;
  const cls = profit > 0 ? "good-val" : profit < 0 ? "bad-val" : "";

  body.innerHTML =
    row("вы получите после комиссии", fmt(net, outCur),
        feePct ? `комиссия ${feePct}% = ${fmt(fee, outCur)}` : "без комиссии") +
    row("прибыль", fmt(profit, outCur),
        profit >= 0 ? "чистыми на руки" : "убыток", cls) +
    row("процент прибыли", pct === null ? "—" : `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`,
        "к цене покупки", cls) +
    row("окупаемость", buy > 0 ? fmt(buy / (1 - feePct / 100), outCur) : "—",
        "цена продажи, при которой выходишь в ноль");
}

["buy", "sell", "fee", "buy-cur", "sell-cur", "out-cur"].forEach((id) => {
  const el = document.getElementById(id);
  el.addEventListener("input", calc);
  el.addEventListener("change", calc);
});

// -- rate ------------------------------------------------------------------

function token() {
  try { return localStorage.getItem("csfloat_admin_token") || ""; } catch (e) { return ""; }
}

function rateMsg(text, isError) {
  const el = document.getElementById("rate-msg");
  el.textContent = text || "";
  el.className = "settings-msg" + (isError ? " err" : "");
}

async function loadRate() {
  try {
    const d = await getJSON("/api/rate");
    const hint = document.getElementById("rate-hint");
    if (d.rate) {
      document.getElementById("rate-input").value = d.rate;
      hint.textContent = `Курс: 1 USD = ${d.rate} CNY` +
        (d.updated_at ? ` · обновлён ${timeFmt(d.updated_at)}` : "");
    } else {
      hint.textContent = "Курс неизвестен — впиши его ниже, иначе доступен только USD.";
    }
    document.getElementById("rate-manual").checked = d.source === "manual";
    const st = document.getElementById("rate-status");
    st.textContent = d.error
      ? `Автообновление не сработало: ${d.error}. Впиши курс вручную.`
      : d.source === "manual"
        ? "Режим: вручную — бот курс не трогает."
        : `Режим: автоматически, раз в 12 ч из ${d.url}`;
    st.className = d.error ? "settings-msg err" : "muted";
  } catch (e) {
    document.getElementById("rate-hint").textContent = "Не удалось прочитать курс.";
  }
}

document.getElementById("save-rate").addEventListener("click", async () => {
  try {
    await postJSON("/api/rate", {
      rate: document.getElementById("rate-input").value,
      source: document.getElementById("rate-manual").checked ? "manual" : "auto",
    }, token());
    rateMsg("Сохранено. Обнови страницу, чтобы курс подхватился везде.");
    loadRate();
    calc();
  } catch (e) {
    rateMsg("Ошибка: " + e.message, true);
  }
});

// A rate we do not have makes the CNY options a lie; hide them.
if (!RATE) {
  document.querySelectorAll("select.cur-select option[value='cny'], " +
                            "#out-cur option[value='cny']").forEach((o) => o.remove());
}

loadRate();
calc();
