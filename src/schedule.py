"""When to poll each item's sales next: one rule, read by everything.

The collector, the load page and tools/capacity.py each used to carry their
own copy of the interval rule, and the copies had begun to disagree. This is
the one copy.

The rule, in four parts.

How fast it sells. Measured on the item's last RECENT_SALES sales within
RATE_DAYS, timed up to now rather than up to the last sale - so an item that
has gone quiet reads slower with every quiet day, instead of keeping the rate
it had when it last sold. The old estimate needed five sales in fourteen days
and fell back to the plain interval below that, which is where most of a
large list sits: an item selling once a week was polled four times a day,
twenty-eight polls per sale.

How many sales to let gather. CSFloat returns the latest WINDOW sales and no
more, so anything past that between two polls is gone. TARGET_SALES is well
inside it.

What the last polls said. Nothing new, and the next wait grows by EMPTY_GROWTH
per empty poll in a row. The window nearly full, or barely overlapping what
was already stored, and the next poll comes at the floor - that is the moment
sales are being lost.

How much the item matters. One ceiling would have to be both "an hour, for an
item we hold an order on" and "three days, for one nobody is looking at".
So three:

    orders    a live order of ours is on it - the price we defend moves with it
    analysis  it is on the analysis list - it is about to be priced
    rest      everything else

And when the list asks for more polls than the day holds, the rest tier is
stretched first; the two that matter keep their pace.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Sequence

from .pacing import parse_iso

WINDOW = 40               # sales one response carries
TARGET_SALES = 15.0       # sales to let gather between polls
RECENT_SALES = 20         # how many of the latest sales the rate is read from
RATE_DAYS = 90            # ...and how far back they may be
EMPTY_GROWTH = 1.5        # each empty poll in a row stretches the next wait
EMPTY_STEPS_MAX = 6       # at most 1.5^6, about 11x
CROWDED = 30              # new sales in one poll that say "poll sooner"
FEEDBACK_POLLS = 6        # how many of the latest polls the feedback reads

TIERS = ("orders", "analysis", "rest")
TIER_LABELS = {"orders": "стоит наш ордер", "analysis": "в списке анализа",
               "rest": "остальные"}
DEFAULT_CEILINGS = {"orders": 60.0, "analysis": 360.0, "rest": 4320.0}
CEILING_KEYS = {"orders": "ceiling_orders_minutes",
                "analysis": "ceiling_analysis_minutes",
                "rest": "ceiling_rest_minutes"}
CEILING_BOUNDS = (15.0, 10080.0)   # a quarter hour to a week

# A poll is not only the pause: DNS, TLS and the answer take their share.
ROUND_TRIP_SECONDS = 1.0
# Plan to fill this much of the day, not all of it: a burst of liquidity, a
# manual poll or a sweep needs the rest.
TARGET_UTILISATION = 0.8
REST_STRETCH_MAX = 20.0


@dataclass
class Feedback:
    """What the latest polls of one item came back with, newest first."""
    empty_streak: int = 0     # successful polls in a row with nothing new
    crowded: bool = False     # the last one was nearly a full window
    gap: bool = False         # the last one barely overlapped what we had


def sales_per_hour(sold_at: Sequence, now: datetime | None = None,
                   days: float = RATE_DAYS) -> float | None:
    """Sales an hour, from the latest sales (any order). None with none."""
    now = now or datetime.now(timezone.utc)
    times = sorted((t for t in (parse_iso(s) for s in sold_at) if t is not None),
                   reverse=True)[:RECENT_SALES]
    if not times:
        return None
    hours = (now - times[-1]).total_seconds() / 3600.0
    # A handful of sales close together is a burst, not a rate: three in the
    # last ten minutes are not eighteen an hour. With enough of them the span
    # is the rate, and clamping it would be the expensive mistake - twenty
    # sales in two hours read over a day would stretch a liquid item's wait
    # tenfold and let the window roll past.
    least = 24.0 if len(times) < 5 else 0.5
    hours = min(max(hours, least), days * 24.0)
    return len(times) / hours


def feedback_from(polls: Iterable[dict], gap_min_overlap: int = 5) -> Feedback:
    """Read poll_log rows (newest first) into what the next wait should know."""
    out = Feedback()
    rows = [p for p in polls if p.get("status") == "ok"][:FEEDBACK_POLLS]
    if not rows:
        return out
    last = rows[0]
    new = int(last.get("new_count") or 0)
    fetched = int(last.get("fetched_count") or 0)
    overlap = int(last.get("overlap_count") or 0)
    out.crowded = new >= CROWDED
    # A gap only means something when there was history to overlap with; the
    # first poll of an item overlaps nothing and is not a loss.
    out.gap = (fetched >= WINDOW and overlap < gap_min_overlap
               and len(rows) > 1)
    for row in rows:
        if int(row.get("new_count") or 0) == 0 and int(row.get("fetched_count") or 0) > 0:
            out.empty_streak += 1
        else:
            break
    return out


def interval_minutes(rate: float | None, floor: float, ceiling: float,
                     feedback: Feedback | None = None,
                     target: float = TARGET_SALES) -> float:
    """Minutes until this item's next poll, before any global stretch."""
    fb = feedback or Feedback()
    if fb.gap or fb.crowded:
        return floor
    if rate is None or rate <= 0:
        base = ceiling
    else:
        base = target / rate * 60.0
    base *= EMPTY_GROWTH ** min(fb.empty_streak, EMPTY_STEPS_MAX)
    return min(max(base, floor), ceiling)


def tier_of(item_id: int, with_orders: set[int], in_analysis: set[int]) -> str:
    if item_id in with_orders:
        return "orders"
    if item_id in in_analysis:
        return "analysis"
    return "rest"


@dataclass
class Settings:
    floor: float = 15.0
    ceilings: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_CEILINGS))
    target: float = TARGET_SALES

    def ceiling(self, tier: str) -> float:
        return max(self.ceilings.get(tier, DEFAULT_CEILINGS["rest"]), self.floor)


def read_settings(db, floor: float) -> Settings:
    """The ceilings as the load page saved them, defaults where it did not."""
    out = Settings(floor=floor)
    lo, hi = CEILING_BOUNDS
    for tier, key in CEILING_KEYS.items():
        raw = db.get_setting(key)
        if raw in (None, ""):
            continue
        try:
            out.ceilings[tier] = min(max(float(raw), lo), hi)
        except (TypeError, ValueError):
            continue
    return out


def capacity_per_day(spacing: float, workers: int = 1) -> float:
    """Polls a day the collector can make: one at a time, each costing the
    pause and a round trip."""
    return 86400.0 / max(spacing + ROUND_TRIP_SECONDS, 0.1) * max(workers, 1)


def rest_stretch(demand: dict[str, float], capacity: float,
                 utilisation: float = TARGET_UTILISATION) -> float:
    """How much to stretch the rest tier so the whole plan fits the day.

    Only the rest tier gives way: an item we hold an order on, or are about
    to price, keeps its pace. If even the rest stretched as far as allowed
    does not fit, the queue lags - which is what it did before, for everyone.
    """
    room = capacity * utilisation
    total = sum(demand.values())
    if total <= room:
        return 1.0
    rest = demand.get("rest", 0.0)
    keep = total - rest
    if rest <= 0:
        return 1.0
    left = room - keep
    if left <= rest / REST_STRETCH_MAX:
        return REST_STRETCH_MAX
    return min(rest / left, REST_STRETCH_MAX)


@dataclass
class ItemPlan:
    item_id: int
    tier: str
    rate: float | None          # sales an hour
    minutes: float              # interval before any stretch
    expected: float             # sales expected to gather in that interval

    @property
    def per_day(self) -> float:
        return 1440.0 / self.minutes


def plan_items(items: Sequence[dict], recent: dict[int, list],
               polls: dict[int, list], with_orders: set[int],
               in_analysis: set[int], settings: Settings,
               now: datetime | None = None,
               gap_min_overlap: int = 5) -> list[ItemPlan]:
    """Every active item's interval, for the load page and the estimate.

    Items with an interval set by hand keep it: that is a decision someone
    made about that item, and the rule does not override it.
    """
    now = now or datetime.now(timezone.utc)
    out = []
    for it in items:
        item_id = int(it["id"])
        tier = tier_of(item_id, with_orders, in_analysis)
        rate = sales_per_hour(recent.get(item_id, ()), now)
        lo, hi = it.get("interval_min_minutes"), it.get("interval_max_minutes")
        if lo or hi:
            minutes = ((lo or settings.floor) + (hi or lo or settings.floor)) / 2.0
        else:
            minutes = interval_minutes(
                rate, settings.floor, settings.ceiling(tier),
                feedback_from(polls.get(item_id, ()), gap_min_overlap),
                settings.target)
        expected = (rate or 0.0) * minutes / 60.0
        out.append(ItemPlan(item_id, tier, rate, minutes, expected))
    return out


def demand(plans: Iterable[ItemPlan]) -> dict[str, float]:
    out = {t: 0.0 for t in TIERS}
    for p in plans:
        out[p.tier] += p.per_day
    return out


def liquidity_group(rate: float | None) -> str:
    """For reports: sales a day, in three bands."""
    per_day = (rate or 0.0) * 24.0
    if per_day >= 20:
        return "liquid"
    if per_day >= 2:
        return "middle"
    return "thin"


GROUP_LABELS = {"liquid": "ликвид (20+ продаж/день)",
                "middle": "средние (2–20/день)",
                "thin": "неликвид (<2/день)"}



def analysis_ids(db) -> set[int]:
    """Items on the analysis list, by id (the list is stored by name)."""
    import json
    try:
        names = json.loads(db.get_setting("analysis_items") or "[]")
    except ValueError:
        return set()
    out = set()
    for name in names:
        if isinstance(name, str):
            item_id = db.get_item_id(name)
            if item_id is not None:
                out.add(int(item_id))
    return out


def rate_since(now: datetime | None = None) -> str:
    from datetime import timedelta
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=RATE_DAYS)).replace(microsecond=0).isoformat()


def plan_from_db(db, floor: float, gap_min_overlap: int = 5,
                 now: datetime | None = None) -> tuple[list[ItemPlan], Settings]:
    """Every active item's plan, read in three queries rather than three per
    item - six thousand items is the size this has to work at."""
    settings = read_settings(db, floor)
    items = db.get_active_items()
    plans = plan_items(items, db.recent_sold_at_all(rate_since(now), RECENT_SALES),
                       db.recent_polls_all(FEEDBACK_POLLS),
                       db.item_ids_with_live_orders(), analysis_ids(db),
                       settings, now, gap_min_overlap)
    return plans, settings
