// Home page: render item cards, refresh periodically.

function card(it) {
  const a = document.createElement("a");
  a.className = "card";
  a.href = "/item/" + encodeURIComponent(it.market_hash_name);

  const img = it.icon_url
    ? `<img src="${it.icon_url}" alt="" loading="lazy">`
    : `<div class="noimg">нет фото</div>`;

  const inactive = it.active ? "" : `<div class="inactive">неактивен</div>`;

  a.innerHTML = `
    ${img}
    <div>
      <div class="name">${escapeHtml(it.market_hash_name)}</div>
      <div class="meta">
        продаж: <b>${it.total_sales}</b><br>
        avg: <b>${money(it.avg_price)}</b>
        &nbsp; min: ${money(it.min_price)} &nbsp; max: ${money(it.max_price)}
      </div>
      ${inactive}
    </div>`;
  return a;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

async function load() {
  try {
    const data = await getJSON("/api/items");
    renderStatus(data.last_update);
    const wrap = document.getElementById("cards");
    wrap.innerHTML = "";
    if (!data.items.length) {
      wrap.innerHTML = '<p class="muted">Нет предметов. Добавь их в items.yaml и запусти сборщик.</p>';
      return;
    }
    data.items.forEach((it) => wrap.appendChild(card(it)));
  } catch (e) {
    document.getElementById("cards").innerHTML =
      `<p class="muted">Ошибка загрузки: ${escapeHtml(e.message)}</p>`;
  }
}

startAutoRefresh(load, 60000);
