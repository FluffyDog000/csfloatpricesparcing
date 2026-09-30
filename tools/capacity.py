"""Сколько будет стоить список из N предметов: запросы, трафик, адреса.

Наивный счёт «N предметов ÷ интервал» занижает, и заметно. Интервал каждого
предмета подбирается под его собственную скорость продаж — окно CSFloat в 40
продаж иначе прокрутится и история потеряется, — поэтому потолок интервала
связывает только медленные предметы. Ликвидный AK опрашивается раз в час
независимо от того, что стоит в настройках, и десяток таких предметов стоит
дороже сотни спящих.

Поэтому счёт идёт от уже собранной истории: по каждому отслеживаемому предмету
берётся интервал, который выдало бы само правило, из них получается среднее
число опросов в сутки на предмет, и оно умножается на N. Размер ответа берётся
измеренный из poll_log, а не выдуманный: метрический прокси выставляет счёт
ровно за эти байты.

    .venv/bin/python tools/capacity.py --items 3000 --every 6
    .venv/bin/python tools/capacity.py --items 3000 --every 6 --ip-limit 500
"""
from __future__ import annotations

import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Накладные расходы поверх тела ответа: заголовки, TLS-рукопожатие, TCP.
# Прокси считает байты на проводе, а не длину JSON.
WIRE_OVERHEAD = 1.15
# Доля квоты адреса, которую разумно занимать. Сотня процентов означает, что
# первый же повтор или всплеск упирается в 429.
SAFE_UTILISATION = 0.6
# Сколько секунд занимает один опрос помимо паузы: DNS, TLS, ответ.
ROUND_TRIP_SECONDS = 1.0


def polls_per_day(db, ceiling_minutes: float, floor_minutes: float,
                  plain_minutes: float) -> tuple[float, int, int]:
    """Среднее число опросов в сутки на один предмет, по живой истории.

    Возвращает (среднее, сколько предметов учтено, у скольких хватило истории).
    """
    from src.pacing import adaptive_minutes, window_start

    rates = db.sales_rates(window_start())
    items = db.get_active_items()
    if not items:
        return 1440.0 / ceiling_minutes, 0, 0

    total, measured = 0.0, 0
    for it in items:
        lo, hi = it.get("interval_min_minutes"), it.get("interval_max_minutes")
        if lo or hi:
            minutes = ((lo or floor_minutes) + (hi or plain_minutes)) / 2.0
        else:
            count, first = rates.get(it["id"], (0, None))
            adaptive = adaptive_minutes(count, first, floor_minutes=floor_minutes,
                                        ceiling_minutes=ceiling_minutes)
            if adaptive is None:
                minutes = min(plain_minutes, ceiling_minutes)
            else:
                minutes = adaptive
                measured += 1
        total += 1440.0 / minutes
    return total / len(items), len(items), measured


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--items", type=int, required=True,
                    help="сколько предметов планируется держать в сборе")
    ap.add_argument("--every", type=float, default=6.0,
                    help="потолок интервала в часах (по умолчанию 6)")
    ap.add_argument("--ip-limit", type=int, default=200,
                    help="запросов в час на один адрес (по умолчанию 200 — "
                         "измеренное значение; в заголовках CSFloat пишет 500)")
    ap.add_argument("--bytes", type=int,
                    help="средний размер ответа, если в базе ещё нет замеров")
    ap.add_argument("--db", help="путь к базе")
    args = ap.parse_args()

    try:
        from datetime import datetime, timedelta, timezone

        from src.config import load_config
        from src.db import Database
    except ModuleNotFoundError as exc:
        print(f"не хватает модуля '{exc.name}' — запусти через .venv/bin/python",
              file=sys.stderr)
        return 1

    config = load_config()
    if args.db:
        config.db_path = args.db
    ceiling = args.every * 60.0
    floor = config.polling.interval_min_minutes
    plain = (config.polling.interval_min_minutes
             + config.polling.interval_max_minutes) / 2.0
    spacing = config.polling.min_seconds_between_requests

    db = Database(config.db_path)
    try:
        week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        sizes = db.response_size_stats(week)
        per_item, seen, measured = polls_per_day(db, ceiling, floor, plain)
    finally:
        db.close()

    if args.bytes:
        avg_bytes, source = float(args.bytes), "задан вручную"
    elif sizes["samples"]:
        avg_bytes = float(sizes["avg_bytes"])
        source = f"измерено, {sizes['samples']} ответ(ов) за неделю"
    else:
        avg_bytes, source = 9000.0, "замеров нет, взято 9 КБ"

    per_day = per_item * args.items
    per_hour = per_day / 24.0
    wire = avg_bytes * WIRE_OVERHEAD
    mb_day = per_day * wire / 1_048_576
    floor_day = args.items * 24.0 / args.every

    print(f"Расчёт на {args.items} предмет(ов), потолок интервала {args.every:g} ч\n")
    if seen:
        print(f"История: {seen} предмет(ов) в сборе, у {measured} хватило продаж, "
              "чтобы правило назвало свой интервал")
        print(f"  в среднем {per_item:.1f} опрос(ов) в сутки на предмет "
              f"(при потолке было бы {24.0 / args.every:.1f})")
        if per_item > 24.0 / args.every * 1.2:
            print("  разница — ликвидные предметы: потолок их не касается, "
                  "они опрашиваются чаще")
    else:
        print("В базе нет активных предметов — считаю всех по потолку")
    print()

    print("ЗАПРОСЫ")
    print(f"  {per_hour:>10,.0f} в час")
    print(f"  {per_day:>10,.0f} в сутки   (голый минимум по потолку: {floor_day:,.0f})")
    print(f"  {per_day * 30:>10,.0f} в месяц")
    print()

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

    seconds = per_day * (spacing + ROUND_TRIP_SECONDS)
    share = seconds / 86400 * 100
    print(f"ВРЕМЯ    (пауза между запросами {spacing:g} с, опросы идут по очереди)")
    print(f"  {seconds / 3600:.1f} ч машинного времени в сутки — {share:.0f}% суток")
    if share > 80:
        print("  СБОРЩИК НЕ УСПЕВАЕТ: очередь растёт, интервалы поедут сами. "
              f"Потолок пропускной способности — {86400 / (spacing + ROUND_TRIP_SECONDS):,.0f} "
              "опрос(ов) в сутки; уменьши паузу или сократи список")
    elif share > 50:
        print("  запас есть, но небольшой — всплеск ликвидности его съест")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
