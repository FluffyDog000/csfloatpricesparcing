"""Earnings: what we bought on CSFloat, what it sold for, what is still held.

The account's trades say both halves. A purchase and a later sale of the same
skin are paired by name, float and pattern: the asset id changes every time an
item moves between Steam inventories, while the float and the paint seed are
the skin itself and never change.

The trades endpoint is not documented, so - like the sales parser - every
field is looked for under a few candidate names, and the collector keeps the
key paths of the first trade it saw, so a shape that does not parse can be
pinned from the page rather than guessed at.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from .pacing import parse_iso
from .parser import _price_to_cents, _to_float, _to_int

# Tried in order until one answers with trades. CSFloat pages with page/limit
# everywhere else on the account API.
TRADES_CANDIDATES = (
    "/api/v1/me/trades?page={page}&limit=100",
    "/api/v1/me/trades?page={page}&limit=30",
    "/api/v1/me/trades",
)
ME_PATH = "/api/v1/me"

DONE = {"verified", "completed", "complete", "accepted", "success", "finished"}
FAILED = {"failed", "cancelled", "canceled", "reverted", "expired", "declined",
          "rejected"}

BUY, SELL = "buy", "sell"

# How the current price of a held skin is estimated: the median of recent
# sales in its own hundredth of float, given enough of them, else of the item.
ESTIMATE_DAYS = 30
BUCKET_MIN_SALES = 5
ITEM_MIN_SALES = 3


def _dig(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _first(obj: Any, paths: Sequence[str]) -> Any:
    for p in paths:
        val = _dig(obj, p)
        if val not in (None, ""):
            return val
    return None


ITEM_PATHS = ("contract.item", "listing.item", "item")
NAME_PATHS = ("market_hash_name", "item_name", "name")
FLOAT_PATHS = ("float_value", "floatvalue", "float")
SEED_PATHS = ("paint_seed", "paintseed", "seed")
ASSET_PATHS = ("asset_id", "assetid")
PRICE_PATHS = ("contract.price", "listing.price", "price", "amount")
STATE_PATHS = ("state", "status")
CREATED_PATHS = ("created_at", "contract.created_at")
DONE_PATHS = ("verified_at", "completed_at", "accepted_at", "updated_at")
BUYER_PATHS = ("buyer_id", "buyer.steam_id", "buyer.id", "contract.buyer_id")
SELLER_PATHS = ("seller_id", "seller.steam_id", "seller.id",
                "contract.seller_id", "contract.seller.steam_id",
                "contract.seller.id")


def trade_rows(payload: Any) -> list[dict]:
    """The trades in a reply, whatever it wraps them in."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("trades", "data", "items", "results"):
            val = payload.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
    return []


def key_paths(obj: Any, prefix: str = "", depth: int = 3) -> list[str]:
    """Dotted key paths of a record, without its values: enough to pin a
    parser, and nothing personal goes into the database or onto a page."""
    out: list[str] = []
    if not isinstance(obj, dict) or depth <= 0:
        return out
    for k, v in obj.items():
        path = f"{prefix}{k}"
        out.append(path)
        if isinstance(v, dict):
            out.extend(key_paths(v, path + ".", depth - 1))
    return out


def parse_trade(raw: dict, me: str | None = None,
                role_hint: str | None = None) -> dict | None:
    """One trade, normalised. None when it has no id to store it under."""
    trade_id = raw.get("id") or raw.get("trade_id")
    if trade_id in (None, ""):
        return None
    item = _first(raw, ITEM_PATHS) or {}
    cents = _price_to_cents(_first(raw, PRICE_PATHS))
    buyer = _first(raw, BUYER_PATHS)
    seller = _first(raw, SELLER_PATHS)
    role = None
    if me:
        if buyer is not None and str(buyer) == str(me):
            role = BUY
        elif seller is not None and str(seller) == str(me):
            role = SELL
    if role is None:
        hint = str(raw.get("role") or raw.get("side") or role_hint or "").lower()
        role = BUY if hint in ("buyer", "buy") else SELL if hint in ("seller", "sell") else None
    state = str(_first(raw, STATE_PATHS) or "").lower()
    return {
        "trade_id": str(trade_id),
        "role": role,
        "state": state,
        "market_hash_name": _first(item, NAME_PATHS)
        or _first(raw, ("market_hash_name",)),
        "float_value": _to_float(_first(item, FLOAT_PATHS)),
        "paint_seed": _to_int(_first(item, SEED_PATHS)),
        "asset_id": (str(_first(item, ASSET_PATHS))
                     if _first(item, ASSET_PATHS) is not None else None),
        "price": cents / 100.0 if cents is not None else None,
        "created_at": _first(raw, CREATED_PATHS),
        "done_at": _first(raw, DONE_PATHS) if state in DONE else None,
    }


BALANCE_PATHS = ("user.balance", "balance", "user.wallet.balance")


def my_balance(payload: Any) -> float | None:
    """The account's balance in dollars, from GET /api/v1/me.

    CSFloat quotes money in cents everywhere else, so a whole number is cents;
    one with a fractional part is already dollars."""
    raw = _first(payload, BALANCE_PATHS)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    if isinstance(raw, int) or (isinstance(raw, str) and raw.strip().isdigit()):
        return round(value / 100.0, 2)
    return round(value, 2) if value != int(value) else round(value / 100.0, 2)


def my_id(payload: Any) -> str | None:
    """Our own Steam id, from GET /api/v1/me."""
    for path in ("user.steam_id", "steam_id", "user.id", "id"):
        val = _dig(payload, path)
        if val not in (None, ""):
            return str(val)
    return None


# -- pairing -----------------------------------------------------------------

def skin_key(t: dict) -> tuple | None:
    """The skin itself: name, float to the precision CSFloat gives it, seed.
    None without a float - nothing else tells two copies apart."""
    if t.get("float_value") is None or not t.get("market_hash_name"):
        return None
    return (t["market_hash_name"], round(float(t["float_value"]), 10),
            t.get("paint_seed"))


def _when(t: dict) -> datetime:
    return (parse_iso(t.get("done_at")) or parse_iso(t.get("created_at"))
            or datetime.min.replace(tzinfo=timezone.utc))


@dataclass
class Book:
    closed: list[dict] = field(default_factory=list)     # bought and sold
    holding: list[dict] = field(default_factory=list)    # bought, not sold yet
    unmatched: list[dict] = field(default_factory=list)  # sold, purchase unseen
    pending: list[dict] = field(default_factory=list)    # trades still running


def pair(trades: Iterable[dict], fee: float) -> Book:
    """Match each completed sale to the earliest unsold purchase of the same
    skin made before it."""
    book = Book()
    done = [t for t in trades if t.get("state") in DONE and t.get("role")]
    book.pending = [t for t in trades
                    if t.get("state") not in DONE and t.get("state") not in FAILED]
    buys: dict[tuple, list[dict]] = {}
    for t in sorted((t for t in done if t["role"] == BUY), key=_when):
        key = skin_key(t)
        if key is not None:
            buys.setdefault(key, []).append(t)
    used: set[str] = set()
    for sell in sorted((t for t in done if t["role"] == SELL), key=_when):
        key = skin_key(sell)
        match = None
        for buy in buys.get(key, ()) if key else ():
            if buy["trade_id"] not in used and _when(buy) <= _when(sell):
                match = buy
                break
        if match is None:
            book.unmatched.append(sell)
            continue
        used.add(match["trade_id"])
        book.closed.append(deal(match, sell, fee))
    for t in done:
        if t["role"] == BUY and t["trade_id"] not in used:
            book.holding.append(t)
    book.closed.sort(key=lambda d: d["sold_at"] or "", reverse=True)
    book.holding.sort(key=_when, reverse=True)
    book.unmatched.sort(key=_when, reverse=True)
    return book


def deal(buy: dict, sell: dict, fee: float) -> dict:
    bought, sold = float(buy["price"] or 0), float(sell["price"] or 0)
    net = sold * (1.0 - fee)
    profit = net - bought
    held = (_when(sell) - _when(buy)).total_seconds() / 86400.0
    return {
        "market_hash_name": sell["market_hash_name"],
        "float_value": sell["float_value"],
        "paint_seed": sell["paint_seed"],
        "bought": bought, "bought_at": buy.get("done_at") or buy.get("created_at"),
        "sold": sold, "sold_at": sell.get("done_at") or sell.get("created_at"),
        "fee": round(sold - net, 2),
        "profit": round(profit, 2),
        "pct": round(profit / bought * 100.0, 1) if bought else None,
        "days": round(max(held, 0.0), 1),
        "buy_id": buy["trade_id"], "sell_id": sell["trade_id"],
    }


# -- what a held skin is worth -------------------------------------------------

def estimate(sales: Sequence[dict], float_value: float | None) -> tuple[float | None, str]:
    """Median of recent sales in the skin's own hundredth of float, or of the
    whole item when that hundredth has too few. Returns (price, basis)."""
    prices = [float(s["price"]) for s in sales if s.get("price")]
    if float_value is not None:
        lo = int(float_value * 100) / 100.0
        near = [float(s["price"]) for s in sales
                if s.get("price") and s.get("float_value") is not None
                and lo <= float(s["float_value"]) < lo + 0.01]
        if len(near) >= BUCKET_MIN_SALES:
            return statistics.median(near), f"медиана {len(near)} продаж в {lo:.2f}–{lo + 0.01:.2f}"
    if len(prices) >= ITEM_MIN_SALES:
        return statistics.median(prices), f"медиана {len(prices)} продаж предмета"
    return None, "мало продаж для оценки"


def since_iso(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)) \
        .replace(microsecond=0).isoformat()


def by_bot(buy: dict, events: Sequence[dict]) -> bool:
    """Whether a purchase was one of the bot's orders filling: an order of
    ours, on this item, at this price, over a float range holding this float."""
    f = buy.get("float_value")
    price = buy.get("price")
    if f is None or price is None:
        return False
    for e in events:
        if e.get("market_hash_name") != buy.get("market_hash_name"):
            continue
        if e.get("price") is None or e.get("float_min") is None:
            continue
        if (abs(float(e["price"]) - float(price)) < 0.011
                and float(e["float_min"]) <= f <= float(e["float_max"])):
            return True
    return False


def totals(closed: Sequence[dict]) -> dict:
    spent = sum(d["bought"] for d in closed)
    profit = sum(d["profit"] for d in closed)
    return {
        "deals": len(closed),
        "spent": round(spent, 2),
        "revenue": round(sum(d["sold"] for d in closed), 2),
        "fees": round(sum(d["fee"] for d in closed), 2),
        "profit": round(profit, 2),
        "pct": round(profit / spent * 100.0, 1) if spent else None,
        "wins": sum(1 for d in closed if d["profit"] > 0),
        "days": (round(statistics.median(d["days"] for d in closed), 1)
                 if closed else None),
    }
