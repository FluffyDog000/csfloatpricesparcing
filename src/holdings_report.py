"""What we are holding, in words - for Telegram and anything else that asks.

The dashboard answers this with tables. A phone needs a paragraph, and it has
to be built from the same numbers rather than from a second opinion: the value
committed, what is standing where, and what it would make if it filled.

Nothing here touches the network. Every figure comes from what the collector
has already stored, so the report is as fresh as the last sweep and says so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .holdings import strip_own
from .orders import wear_range
from .pricing import Params, evaluate


@dataclass
class Position:
    item: str
    float_min: float
    float_max: float
    price: float            # the most we would pay
    ceiling: float
    state: str
    paid: float | None = None      # what a fill is expected to cost
    margin: float | None = None
    profit: float | None = None    # net of fee, per fill
    outbid: int = 0                # orders standing at or above ours
    scored: bool = False           # whether it could be priced at all


@dataclass
class Holdings:
    positions: list[Position] = field(default_factory=list)
    committed: float = 0.0         # capital the standing orders reserve
    profit: float = 0.0            # if every one of them filled
    managed: int = 0
    manual: int = 0
    outbid: int = 0
    unscored: int = 0

    @property
    def items(self) -> list[str]:
        seen: list[str] = []
        for p in self.positions:
            if p.item not in seen:
                seen.append(p.item)
        return seen


def collect(db, params: Params | None = None) -> Holdings:
    """Every standing order, priced from what is already in the database."""
    p = params or Params()
    out = Holdings()
    rows = [r for r in db.our_orders(live_only=False)
            if r["state"] in ("planned", "live", "manual")]
    by_item: dict[int, list] = {}
    for row in rows:
        by_item.setdefault(int(row["item_id"]), []).append(row)

    for item_id, held in by_item.items():
        name = db.item_name(item_id) or "?"
        book = strip_own(db.buy_orders(item_id), held)
        sales = _sales(db, item_id, p)
        try:
            depth = db.listing_depth(item_id)
        except Exception:  # noqa: BLE001 - an older DB has no such table
            depth = []
        span = wear_range(name)

        for row in held:
            lo, hi = float(row["float_min"]), float(row["float_max"])
            price = float(row["price"])
            pos = Position(item=name, float_min=lo, float_max=hi, price=price,
                           ceiling=float(row["ceiling"]), state=row["state"])
            # At or above, not strictly above: who fills first at an equal
            # price is decided by who placed first, which the book does not say.
            pos.outbid = sum(int(o.get("qty") or 1) for o in book
                             if (o.get("float_min") or 0.0) < hi
                             and (o.get("float_max") or 1.0) > lo
                             and float(o["price"]) >= price)

            band = evaluate(lo, hi, sales, book, span, depth, p)
            if band.market:
                net = band.market * (1.0 - p.fee)
                # What a fill costs is the listing's price, and the bid only
                # when nothing cheaper is on offer.
                pos.paid = band.paid if band.paid is not None else price
                pos.profit = net - pos.paid
                pos.margin = pos.profit / pos.paid if pos.paid else None
                pos.scored = True

            out.positions.append(pos)
            out.committed += price
            if pos.state == "manual":
                out.manual += 1
            else:
                out.managed += 1
            if pos.outbid:
                out.outbid += 1
            if pos.scored and pos.profit is not None:
                out.profit += pos.profit
            else:
                out.unscored += 1

    out.positions.sort(key=lambda x: (-(x.profit or -1e9), x.item, x.float_min))
    return out


def _sales(db, item_id: int, p: Params) -> list[dict[str, Any]]:
    """Sales with ages attached, as the scoring reads them."""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=max(p.window_days * 3, 90))).isoformat()
    rows = [dict(r) for r in db.conn.execute(
        "SELECT price, float_value, sold_at FROM sales WHERE item_id = ? "
        "AND float_value IS NOT NULL AND sold_at >= ?", (item_id, cutoff))]
    now = datetime.now(timezone.utc)
    for s in rows:
        try:
            t = datetime.fromisoformat(s["sold_at"])
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            s["age_days"] = (now - t).total_seconds() / 86400
        except (TypeError, ValueError):
            s["age_days"] = None
    return rows


def money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}".replace(",", " ")


def summary_text(h: Holdings) -> str:
    """Two lines: what is committed and what it would make."""
    if not h.positions:
        return "Ордеров нет."
    lines = [
        f"<b>Ордеров: {len(h.positions)}</b> на {len(h.items)} предмет(ах)",
        f"В ордерах: <b>{money(h.committed)}</b>",
        f"Ожидаемая прибыль: <b>{money(h.profit)}</b>"
        + (f" ({h.profit / h.committed * 100:+.1f}%)" if h.committed else ""),
    ]
    if h.manual:
        lines.append(f"Из них поставлены вручную: {h.manual} — бот их не ведёт")
    if h.outbid:
        lines.append(f"⚠ Перебиты: {h.outbid}")
    if h.unscored:
        lines.append(f"Без оценки: {h.unscored} — нет свежих данных по полосе")
    lines.append("")
    lines.append("<i>Прибыль — если исполнятся все, по последним собранным "
                 "данным. Стакан и продажи обновляются обходом.</i>")
    return "\n".join(lines)


def items_text(h: Holdings, limit: int = 20) -> str:
    """One line per order, best first."""
    if not h.positions:
        return "Ордеров нет."
    lines = []
    for pos in h.positions[:limit]:
        flag = " ⚠" if pos.outbid else ""
        hand = " (вручную)" if pos.state == "manual" else ""
        body = (f"{money(pos.profit)} ({pos.margin * 100:.1f}%)"
                if pos.profit is not None and pos.margin is not None
                else "без оценки")
        lines.append(
            f"<b>{pos.item}</b>{hand}{flag}\n"
            f"  {pos.float_min:.4f}–{pos.float_max:.4f} · ставка "
            f"{money(pos.price)} · платим {money(pos.paid)} · прибыль {body}")
    if len(h.positions) > limit:
        lines.append(f"…и ещё {len(h.positions) - limit}")
    return "\n".join(lines)
