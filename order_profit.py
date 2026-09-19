#!/usr/bin/env python3
"""Стоит ли ставить ордер: выгодность ставки по истории продаж и стакану.

Ордер приносит деньги не тогда, когда в нём большая маржа, а когда маржа и
скорость заполнения вместе дают доходность. Две ошибки, которые считалка
исключает по построению:

1. Диапазон флота выбирает ПРОДАВЕЦ. Ордер на 0.15–0.29 заполнят предметом с
   флотом 0.289, а не 0.151, поэтому рыночная цена берётся по худшему краю
   диапазона, а не по его медиане. На перчатках разница доходила до шести раз.
2. Прибыль без срока заполнения — не прибыль. Ставка на 8% ниже рынка, которую
   никто не трогает три недели, хуже ставки на 2%, оборачивающейся за сутки.

Формула (одна ставка B в диапазоне флота [a, b]):

    худший край   Fw = [b − share·(b−a), b]        share = 0.2
    рынок          P  = медиана продаж с флотом в Fw за окно
    выручка        V  = P × (1 − комиссия)
    прибыль        G  = V − B,   в процентах G/B
    предложение    λ  = продаж в [a, b] по цене ≤ B, в сутки
    очередь        Q  = сумма qty чужих ордеров с ценой ≥ B на пересекающемся
                        диапазоне — их заполнят раньше
    срок покупки   T1 = (1 + Q) / λ         (λ = 0 → не заполнится)
    срок продажи   T2 = 1 / (продаж в Fw в сутки)
    доходность     R  = G/B × 30 / (T1 + T2)   процентов в месяц

Usage:
    python order_profit.py "Printstream"              # разбор стакана
    python order_profit.py "Printstream" --suggest    # + где ставить свою
    python order_profit.py --all --min-return 10      # все предметы со стаканом
    python order_profit.py "Fade" --fee 2 --days 30
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone

from src.config import load_config
from src.orders import wear_range

# Доля диапазона у верхней границы, из которой продавец и отдаст предмет.
WORST_SHARE = 0.2
# Сколько продаж нужно, чтобы медиане можно было верить; иначе окно расширяется.
MIN_SAMPLE = 5
# Ставка, которая заполняется дольше этого, для оборота бесполезна.
MAX_DAYS_TO_FILL = 21.0


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def find_items(conn: sqlite3.Connection, needle: str | None) -> list[dict]:
    """Предметы по куску названия; без него — все, у кого собран стакан."""
    if not needle:
        rows = conn.execute(
            "SELECT i.id, i.market_hash_name FROM items i "
            "WHERE EXISTS (SELECT 1 FROM buy_orders b WHERE b.item_id = i.id) "
            "ORDER BY i.market_hash_name")
        return [dict(r) for r in rows]

    exact = conn.execute(
        "SELECT id, market_hash_name FROM items WHERE market_hash_name = ?",
        (needle,)).fetchall()
    if exact:
        return [dict(r) for r in exact]

    pattern = needle
    for ch in ("\\", "%", "_"):
        pattern = pattern.replace(ch, "\\" + ch)
    rows = conn.execute(
        "SELECT id, market_hash_name FROM items "
        "WHERE market_hash_name LIKE ? ESCAPE '\\' ORDER BY market_hash_name",
        (f"%{pattern}%",)).fetchall()
    return [dict(r) for r in rows]


def load_sales(conn: sqlite3.Connection, item_id: int, days: int) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT price, float_value, sold_at FROM sales WHERE item_id = ? "
        "AND price IS NOT NULL AND float_value IS NOT NULL "
        "AND sold_at IS NOT NULL AND sold_at >= ? ORDER BY sold_at",
        (item_id, since)).fetchall()
    return [dict(r) for r in rows]


def observed_days(sales: list[dict], cap: int) -> float:
    """Сколько суток реально покрывает выборка (короче окна — если предмет
    добавили недавно). Делить на окно, которого не было, значит занижать темп."""
    stamps = [parse_ts(s["sold_at"]) for s in sales]
    stamps = [s for s in stamps if s]
    if len(stamps) < 2:
        return float(cap)
    span = (max(stamps) - min(stamps)).total_seconds() / 86400.0
    return max(min(span, float(cap)), 0.5)


def in_band(sales: list[dict], lo: float, hi: float) -> list[dict]:
    return [s for s in sales if lo <= s["float_value"] <= hi]


def market_price(sales: list[dict], lo: float,
                 hi: float) -> tuple[float | None, int, tuple[float, float]]:
    """Медиана по худшему краю диапазона, с расширением окна до MIN_SAMPLE."""
    span = max(hi - lo, 1e-6)
    share = WORST_SHARE
    while share <= 1.0:
        window = (max(hi - span * share, lo), hi)
        sample = in_band(sales, *window)
        if len(sample) >= MIN_SAMPLE:
            return statistics.median(s["price"] for s in sample), len(sample), window
        share += 0.1
    sample = in_band(sales, lo, hi)
    if not sample:
        return None, 0, (lo, hi)
    return statistics.median(s["price"] for s in sample), len(sample), (lo, hi)


def order_band(order: dict, wear: tuple[float, float] | None) -> tuple[float, float]:
    """Диапазон ордера; у нецелевого — весь износ предмета."""
    lo = order.get("float_min")
    hi = order.get("float_max")
    if lo is None or hi is None:
        return wear or (0.0, 1.0)
    return float(lo), float(hi)


def queue_ahead(orders: list[dict], bid: float, lo: float, hi: float,
                wear: tuple[float, float] | None) -> int:
    """Сколько чужих ордеров заполнят раньше нашего: цена не ниже и диапазон
    пересекается — такой ордер претендует на тот же предмет."""
    total = 0
    for other in orders:
        if other["price"] < bid:
            continue
        o_lo, o_hi = order_band(other, wear)
        if o_hi < lo or o_lo > hi:
            continue
        total += int(other.get("qty") or 1)
    return total


def evaluate(bid: float, lo: float, hi: float, sales: list[dict],
             orders: list[dict], wear: tuple[float, float] | None,
             fee: float, days: float) -> dict:
    price, sample, window = market_price(sales, lo, hi)
    band_sales = in_band(sales, lo, hi)
    cheap = [s for s in band_sales if s["price"] <= bid]
    lam_buy = len(cheap) / days if days else 0.0
    queue = queue_ahead(orders, bid, lo, hi, wear)
    resale = in_band(sales, *window)
    lam_sell = len(resale) / days if days else 0.0

    out = {
        "bid": bid, "lo": lo, "hi": hi, "window": window,
        "market": price, "sample": sample, "queue": queue,
        "lam_buy": lam_buy, "lam_sell": lam_sell,
        "net": None, "profit": None, "profit_pct": None,
        "t_buy": None, "t_sell": None, "monthly": None,
    }
    if price is None:
        return out

    net = price * (1.0 - fee)
    out["net"] = net
    out["profit"] = net - bid
    out["profit_pct"] = (net - bid) / bid * 100.0 if bid else None
    out["t_buy"] = (1 + queue) / lam_buy if lam_buy > 0 else None
    out["t_sell"] = 1.0 / lam_sell if lam_sell > 0 else None
    if out["t_buy"] is not None and out["profit_pct"] is not None:
        cycle = out["t_buy"] + (out["t_sell"] or 0.0)
        if cycle > 0:
            out["monthly"] = out["profit_pct"] * 30.0 / cycle
    return out


def fmt(value, digits=2, dash="—"):
    return dash if value is None else f"{value:,.{digits}f}".replace(",", " ")


def band_label(lo: float, hi: float, wear: tuple[float, float] | None) -> str:
    if wear and (lo, hi) == wear:
        return "любой"
    return f"{lo:g}–{hi:g}"


def report_item(conn, item, fee: float, days_window: int, suggest: bool,
                min_return: float | None) -> None:
    sales = load_sales(conn, item["id"], days_window)
    orders = [dict(r) for r in conn.execute(
        "SELECT price, qty, float_min, float_max, paint_seed, fetched_at "
        "FROM buy_orders WHERE item_id = ? ORDER BY position", (item["id"],))]
    wear = wear_range(item["market_hash_name"])
    days = observed_days(sales, days_window)

    print(f"\n=== {item['market_hash_name']}")
    if not sales:
        print("  нет продаж с флотом за окно — считать не из чего")
        return
    print(f"  продаж: {len(sales)} за {days:.1f} дн. "
          f"({len(sales) / days:.1f}/сут) · комиссия {fee * 100:.1f}% · "
          f"ордеров в стакане: {len(orders)}")
    if not orders:
        print("  стакан не собран — нажми 📥 на странице предмета")

    rows = []
    for order in orders:
        lo, hi = order_band(order, wear)
        # Своя ставка перебивает чужую на цент: иначе очередь будет впереди нас.
        rows.append((order, evaluate(order["price"] + 0.01, lo, hi, sales,
                                     orders, wear, fee, days)))

    if rows:
        print(f"\n  {'полоса':<14}{'перебить':>10}{'рынок':>10}{'после ком.':>12}"
              f"{'приб.%':>9}{'очередь':>9}{'λ/сут':>8}{'купить':>9}"
              f"{'продать':>9}{'%/мес':>9}")
        for order, ev in rows:
            print(f"  {band_label(ev['lo'], ev['hi'], wear):<14}"
                  f"{fmt(ev['bid']):>10}{fmt(ev['market']):>10}{fmt(ev['net']):>12}"
                  f"{fmt(ev['profit_pct'], 1):>9}{ev['queue']:>9}"
                  f"{fmt(ev['lam_buy'], 2):>8}"
                  f"{(fmt(ev['t_buy'], 1) + ' д') if ev['t_buy'] else 'никогда':>9}"
                  f"{(fmt(ev['t_sell'], 1) + ' д') if ev['t_sell'] else '—':>9}"
                  f"{fmt(ev['monthly'], 1):>9}")

    if not suggest:
        return

    # Где ставить свою: перебор по перцентилям цен внутри полосы. Ставка на
    # p-м перцентиле ловит самых торопливых продавцов — тех, кто согласен
    # отдать дешевле рынка; выше перцентиль — быстрее заполнение, меньше маржа.
    bands = sorted({(ev["lo"], ev["hi"]) for _, ev in rows}) or [wear or (0.0, 1.0)]
    print("\n  где ставить свою (перебор по перцентилям цен в полосе):")
    best_all = []
    for lo, hi in bands:
        prices = sorted(s["price"] for s in in_band(sales, lo, hi))
        if len(prices) < MIN_SAMPLE:
            continue
        seen = set()
        for pct in (2, 5, 10, 15, 20, 25, 30, 40):
            idx = max(int(len(prices) * pct / 100) - 1, 0)
            bid = round(prices[idx], 2)
            if bid in seen:
                continue
            seen.add(bid)
            ev = evaluate(bid, lo, hi, sales, orders, wear, fee, days)
            if ev["monthly"] is None or ev["profit_pct"] is None:
                continue
            if ev["profit_pct"] <= 0 or ev["t_buy"] > MAX_DAYS_TO_FILL:
                continue
            best_all.append((ev["monthly"], pct, ev))

    if not best_all:
        print("    нет ставки, которая и приносит прибыль, и заполняется "
              f"быстрее {MAX_DAYS_TO_FILL:.0f} дней")
        return

    best_all.sort(key=lambda x: -x[0])
    print(f"    {'полоса':<14}{'ставка':>10}{'рынок':>10}{'приб.%':>9}"
          f"{'купить':>9}{'продать':>9}{'%/мес':>9}")
    for monthly, _pct, ev in best_all[:6]:
        if min_return is not None and monthly < min_return:
            continue
        print(f"    {band_label(ev['lo'], ev['hi'], wear):<14}"
              f"{fmt(ev['bid']):>10}{fmt(ev['market']):>10}"
              f"{fmt(ev['profit_pct'], 1):>9}"
              f"{fmt(ev['t_buy'], 1) + ' д':>9}"
              f"{(fmt(ev['t_sell'], 1) + ' д') if ev['t_sell'] else '—':>9}"
              f"{fmt(monthly, 1):>9}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Выгодность ордеров по истории продаж и стакану")
    ap.add_argument("name", nargs="?", help="название предмета или его часть")
    ap.add_argument("--all", action="store_true",
                    help="все предметы, у которых собран стакан")
    ap.add_argument("--fee", type=float, default=2.0,
                    help="комиссия площадки при продаже, %% (по умолчанию 2)")
    ap.add_argument("--days", type=int, default=30,
                    help="окно истории продаж в днях (по умолчанию 30)")
    ap.add_argument("--suggest", action="store_true",
                    help="подобрать свою ставку, а не только разобрать чужие")
    ap.add_argument("--min-return", type=float,
                    help="показывать только ставки с доходностью выше, %%/мес")
    ap.add_argument("--db", help="путь к базе (по умолчанию из конфига)")
    args = ap.parse_args()

    if not args.name and not args.all:
        ap.error("укажи название предмета или --all")

    path = str(args.db or load_config().db_path)
    if not os.path.exists(path):
        print(f"База не найдена: {path}")
        return 1

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        items = find_items(conn, None if args.all else args.name)
        if not items:
            print("Ничего не найдено. Для --all нужен хотя бы один собранный стакан.")
            return 1
        if len(items) > 1 and not args.all:
            print("Под запрос подходит несколько предметов:", file=sys.stderr)
            for it in items[:20]:
                print(f"  {it['market_hash_name']}", file=sys.stderr)
            print("Уточни название.", file=sys.stderr)
            return 2
        for item in items:
            report_item(conn, item, args.fee / 100.0, args.days,
                        args.suggest, args.min_return)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
