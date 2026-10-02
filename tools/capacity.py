"""Сколько будет стоить список из N предметов: запросы, трафик, адреса, время.

Считается тем же правилом, по которому опрашивает сборщик (src/schedule.py):
интервал предмета — время, за которое накопится ~15 новых продаж, в пределах
потолка его уровня. Поэтому ликвидный предмет стоит дороже спящего, и наивный
счёт «N ÷ интервал» здесь не годится.

Откуда берутся числа. По каждому предмету в базе планировщик называет его
интервал; предметы делятся на ликвид, средние и неликвид, и по каждой группе
берётся среднее число опросов в сутки. Список из N предметов получает ту же
смесь групп, что в базе, — или заданную через --mix, если база на неё не похожа
(сейчас в ней, например, одни перчатки). Новые предметы считаются уровнем
«остальные»: ордеров на них нет и в анализе их нет. Размер ответа — измеренный
по poll_log, по нему считает метрический прокси.

Оценка верхняя: пустые опросы в работе растягивают следующий интервал в 1.5
раза, а здесь это не учитывается.

    .venv/bin/python tools/capacity.py --items 6000
    .venv/bin/python tools/capacity.py --items 6000 --mix 10,30,60
    .venv/bin/python tools/capacity.py --items 6000 --rest-hours 24 --spacing 3
"""
from __future__ import annotations

import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Накладные расходы поверх тела: заголовки ответа, TLS-рукопожатие, TCP.
WIRE_OVERHEAD = 1.15
# Доля квоты адреса, которую разумно занимать. Сотня процентов означает, что
# первый же повтор или всплеск упирается в 429.
SAFE_UTILISATION = 0.6

GROUPS = ("liquid", "middle", "thin")
# Если в базе нет ни одного предмета группы — типичная скорость продаж для неё,
# в штуках в сутки. Только для оценки; сборщик всегда меряет по истории.
TYPICAL_PER_DAY = {"liquid": 40.0, "middle": 6.0, "thin": 0.5}


def group_rates(plans, settings) -> dict[str, dict]:
    """По группам ликвидности: сколько предметов в базе и сколько опросов в
    сутки в среднем на предмет, если он в уровне «остальные»."""
    from src import schedule as sch

    out = {g: {"items": 0, "per_day": 0.0, "measured": True} for g in GROUPS}
    for p in plans:
        g = sch.liquidity_group(p.rate)
        minutes = sch.interval_minutes(p.rate, settings.floor,
                                       settings.ceiling("rest"),
                                       target=settings.target)
        out[g]["items"] += 1
        out[g]["per_day"] += 1440.0 / minutes
    for g in GROUPS:
        row = out[g]
        if row["items"]:
            row["per_day"] /= row["items"]
        else:
            rate = TYPICAL_PER_DAY[g] / 24.0
            minutes = sch.interval_minutes(rate, settings.floor,
                                           settings.ceiling("rest"),
                                           target=settings.target)
            row["per_day"] = 1440.0 / minutes
            row["measured"] = False
    return out


def estimate(plans, settings, items: int, mix: dict[str, float] | None = None):
    """Опросов в сутки на N предметов, по группам и по уровням.

    Предметы с ордерами и в анализе остаются как есть; прочие N − их число
    раскладываются по смеси групп.
    """
    from src import schedule as sch

    rates = group_rates(plans, settings)
    held = [p for p in plans if p.tier != "rest"]
    held_per_day = sum(p.per_day for p in held)
    rest_items = max(items - len(held), 0)

    if mix is None:
        seen = sum(rates[g]["items"] for g in GROUPS)
        mix = ({g: rates[g]["items"] / seen for g in GROUPS} if seen
               else {"liquid": 0.1, "middle": 0.3, "thin": 0.6})
    total_share = sum(mix.values()) or 1.0
    groups = {}
    for g in GROUPS:
        count = rest_items * mix.get(g, 0.0) / total_share
        groups[g] = {"items": count, "per_item": rates[g]["per_day"],
                     "per_day": count * rates[g]["per_day"],
                     "measured": rates[g]["measured"]}
    demand = {"orders": sum(p.per_day for p in held if p.tier == "orders"),
              "analysis": sum(p.per_day for p in held if p.tier == "analysis"),
              "rest": sum(v["per_day"] for v in groups.values())}
    return {"groups": groups, "demand": demand, "held": len(held),
            "held_per_day": held_per_day, "rest_items": rest_items}


def parse_mix(text: str | None) -> dict[str, float] | None:
    if not text:
        return None
    parts = [float(x) for x in text.replace(" ", "").split(",")]
    if len(parts) != 3 or any(x < 0 for x in parts) or not sum(parts):
        raise ValueError("--mix: три неотрицательных числа через запятую, "
                         "доли ликвида, средних и неликвида, например 10,30,60")
    return dict(zip(GROUPS, parts))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--items", type=int,
                    help="сколько предметов планируется держать в сборе "
                         "(по умолчанию — сколько активно сейчас)")
    ap.add_argument("--mix",
                    help="доли ликвида/средних/неликвида, например 10,30,60 "
                         "(по умолчанию — как в базе)")
    ap.add_argument("--rest-hours", type=float,
                    help="потолок интервала уровня «остальные», в часах "
                         "(по умолчанию — как на странице «Нагрузка»)")
    ap.add_argument("--every", type=float, help=argparse.SUPPRESS)  # старое имя
    ap.add_argument("--spacing", type=float,
                    help="пауза между запросами, с (по умолчанию — как на дашборде)")
    ap.add_argument("--ip-limit", type=int, default=200,
                    help="запросов в час на один адрес (по умолчанию 200 — "
                         "измеренное значение)")
    ap.add_argument("--sample", type=int, default=500,
                    help="по скольким последним ответам считать размер")
    ap.add_argument("--bytes", type=int,
                    help="средний размер ответа, если в базе ещё нет замеров")
    ap.add_argument("--db", help="путь к базе")
    args = ap.parse_args()

    try:
        mix = parse_mix(args.mix)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    try:
        from src import schedule as sch
        from src.config import load_config
        from src.db import Database
    except ModuleNotFoundError as exc:
        print(f"не хватает модуля '{exc.name}' — запусти через .venv/bin/python",
              file=sys.stderr)
        return 1

    config = load_config()
    if args.db:
        config.db_path = args.db

    db = Database(config.db_path)
    try:
        def stored(key: str, default: float) -> float:
            raw = db.get_setting(key)
            try:
                return float(raw) if raw not in (None, "") else default
            except (TypeError, ValueError):
                return default

        p = config.polling
        floor = stored("poll_interval_min_minutes", p.interval_min_minutes)
        spacing = args.spacing or stored("min_seconds_between_requests",
                                         p.min_seconds_between_requests)
        plans, settings = sch.plan_from_db(db, floor, p.gap_warning_min_overlap)
        rest_hours = args.rest_hours or args.every
        if rest_hours:
            settings.ceilings["rest"] = rest_hours * 60.0
            plans = sch.plan_items(db.get_active_items(),
                                   db.recent_sold_at_all(sch.rate_since()),
                                   db.recent_polls_all(), db.item_ids_with_live_orders(),
                                   sch.analysis_ids(db), settings, None,
                                   p.gap_warning_min_overlap)
        sizes = db.recent_response_size(args.sample)
        active = len(db.get_active_items())
    finally:
        db.close()

    items = args.items or active
    if not items:
        print("в базе нет активных предметов — задай --items", file=sys.stderr)
        return 1

    est = estimate(plans, settings, items, mix)
    demand = est["demand"]
    capacity = sch.capacity_per_day(spacing)
    stretch = sch.rest_stretch(demand, capacity)
    per_day = (demand["orders"] + demand["analysis"]
               + demand["rest"] / stretch)
    per_hour = per_day / 24.0

    if args.bytes:
        avg_bytes, source = float(args.bytes), "задан вручную"
    elif sizes["samples"]:
        avg_bytes = float(sizes["avg_bytes"])
        source = f"измерено по {sizes['samples']} последним ответам"
    else:
        avg_bytes, source = 17000.0, "замеров нет, взято 17 КБ"

    hrs = lambda m: f"{m / 60:g} ч"  # noqa: E731
    print(f"Расчёт на {items} предмет(ов)"
          + ("" if args.items else " (столько активно сейчас)"))
    print(f"Правило: ~{settings.target:g} новых продаж между опросами, минимум "
          f"{settings.floor:g} мин; потолки — ордер {hrs(settings.ceiling('orders'))}, "
          f"анализ {hrs(settings.ceiling('analysis'))}, "
          f"остальные {hrs(settings.ceiling('rest'))}")
    print(f"В базе {len(plans)} предмет(ов) — по ним измерена стоимость групп"
          + (f"; смесь групп задана: {args.mix}" if mix else "; смесь групп — как в базе"))
    print()

    print("ПО ЛИКВИДНОСТИ   (уровень «остальные»)")
    print(f"  {'группа':<28}{'предметов':>10}{'опр/сутки на 1':>16}{'опр/сутки':>12}")
    for g in GROUPS:
        row = est["groups"][g]
        note = "" if row["measured"] else "  ← в базе таких нет, взята типичная скорость"
        print(f"  {sch.GROUP_LABELS[g]:<28}{row['items']:>10,.0f}"
              f"{row['per_item']:>16.2f}{row['per_day']:>12,.0f}{note}")
    if est["held"]:
        print(f"  {'ордер / анализ':<28}{est['held']:>10}{'':>16}"
              f"{est['held_per_day']:>12,.0f}")
    print()

    print("ЗАПРОСЫ")
    print(f"  {per_hour:>10,.0f} в час")
    print(f"  {per_day:>10,.0f} в сутки   (опрос каждого раз в 6 ч дал бы {items * 4:,})")
    print(f"  {per_day * 30:>10,.0f} в месяц")
    print()

    wire = avg_bytes * WIRE_OVERHEAD
    mb_day = per_day * wire / 1_048_576
    print(f"ТРАФИК   (ответ {avg_bytes / 1024:.1f} КБ — {source}; "
          f"+{(WIRE_OVERHEAD - 1) * 100:.0f}% на заголовки и TLS)")
    print(f"  {mb_day:>10,.0f} МБ в сутки")
    print(f"  {mb_day * 30 / 1024:>10,.1f} ГБ в месяц")
    print()

    usable = args.ip_limit * SAFE_UTILISATION
    need = math.ceil(per_hour / usable) if usable else 0
    bare = math.ceil(per_hour / args.ip_limit) if args.ip_limit else 0
    print(f"АДРЕСА   (квота {args.ip_limit} запр/час на адрес)")
    print(f"  {bare} — впритык, на 100% квоты: любой повтор упирается в 429")
    print(f"  {need} — с запасом, занимая {SAFE_UTILISATION * 100:.0f}% квоты адреса")
    print()

    asked = sum(demand.values())
    share = asked / capacity * 100 if capacity else 0
    print(f"ВРЕМЯ    (пауза {spacing:g} с + ~{sch.ROUND_TRIP_SECONDS:g} с на ответ, "
          f"опросы идут по очереди)")
    print(f"  сборщик успевает {capacity:,.0f} опрос(ов) в сутки; правило просит "
          f"{asked:,.0f} — {share:.0f}%")
    if stretch > 1:
        print(f"  НЕ ВЛЕЗАЕТ в {sch.TARGET_UTILISATION * 100:.0f}% суток: «остальные» "
              f"будут опрашиваться в {stretch:.1f} раза реже, чем просит правило "
              f"(ордера и анализ — без изменений)")
        if stretch >= sch.REST_STRETCH_MAX:
            print("  и даже так очередь будет отставать — уменьши паузу, добавь "
                  "ключи или сократи список")
    elif share > 50:
        print("  запас есть, но небольшой — всплеск ликвидности его съест")
    else:
        print("  с запасом")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
