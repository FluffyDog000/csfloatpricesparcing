"""What kind of thing an item is, for sorting a list of thousands.

Two sources, and they are not equally sure.

The name. A market_hash_name carries most of it outright: "StatTrak™" and
"Souvenir" as prefixes, "★" for knives and gloves, the wear in brackets, and
the weapon before the "|". Nothing has to be fetched and nothing can be stale,
so everything that can come from the name does.

The sales record. Rarity and collection are not in the name. CSFloat puts them
on the item of every sale it returns, so the collector copies them onto the
item on each poll - free, the response is already paid for. The field names
are read from several candidates because the endpoint is undocumented: an item
nobody has polled since, or a response without them, simply has none, and the
page says "нет данных" rather than guessing.
"""
from __future__ import annotations

import re
from typing import Any

WEARS = (
    ("FN", "Factory New", "Прямо с завода"),
    ("MW", "Minimal Wear", "Немного поношенное"),
    ("FT", "Field-Tested", "После полевых испытаний"),
    ("WW", "Well-Worn", "Поношенное"),
    ("BS", "Battle-Scarred", "Закалённое в боях"),
)
_WEAR_BY_NAME = {full: code for code, full, _ in WEARS}

# The weapon before the "|" decides the group. Kept as the game's own names so
# a new skin on an old weapon needs nothing added here.
WEAPON_GROUPS = {
    "pistol": ("Glock-18", "USP-S", "P2000", "P250", "Five-SeveN", "Tec-9",
               "CZ75-Auto", "Desert Eagle", "Dual Berettas", "R8 Revolver"),
    "smg": ("MAC-10", "MP9", "MP7", "MP5-SD", "UMP-45", "P90", "PP-Bizon"),
    "rifle": ("AK-47", "M4A4", "M4A1-S", "Galil AR", "FAMAS", "AUG", "SG 553"),
    "sniper": ("AWP", "SSG 08", "SCAR-20", "G3SG1"),
    "heavy": ("Nova", "XM1014", "Sawed-Off", "MAG-7", "M249", "Negev"),
}
_GROUP_BY_WEAPON = {w: g for g, ws in WEAPON_GROUPS.items() for w in ws}

CATEGORIES = {
    "knife": "Ножи", "gloves": "Перчатки", "pistol": "Пистолеты",
    "smg": "Пистолеты-пулемёты", "rifle": "Винтовки", "sniper": "Снайперские",
    "heavy": "Тяжёлое", "sticker": "Наклейки", "patch": "Нашивки",
    "charm": "Брелоки", "graffiti": "Граффити", "music": "Музыка",
    "container": "Кейсы и капсулы", "agent": "Агенты", "other": "Другое",
}

# The game's rarity scale for weapon finishes, lowest first. CSFloat reports
# it as a number on the item; the name, where it comes, wins over the number.
RARITIES = {
    1: ("Consumer Grade", "Ширпотреб"),
    2: ("Industrial Grade", "Промышленное"),
    3: ("Mil-Spec Grade", "Армейское"),
    4: ("Restricted", "Запрещённое"),
    5: ("Classified", "Засекреченное"),
    6: ("Covert", "Тайное"),
    7: ("Contraband", "Контрабанда"),
}
_RARITY_BY_NAME = {en.lower(): n for n, (en, _) in RARITIES.items()}
_RARITY_BY_NAME.update({"mil-spec": 3, "consumer": 1, "industrial": 2})

RARITY_PATHS = ("item.rarity", "rarity")
RARITY_NAME_PATHS = ("item.rarity_name", "rarity_name")
COLLECTION_PATHS = ("item.collection", "collection", "item.set_name")

_CONTAINER = re.compile(r"(\bCase|\bCapsule|\bPackage|\bPin|Case Key)$")
_BRACKET = re.compile(r"\s*\(([^()]+)\)\s*$")


def describe(name: str) -> dict[str, Any]:
    """Everything the name says: category, weapon, wear, StatTrak, Souvenir."""
    rest = name.strip()
    star = rest.startswith("★")
    if star:
        rest = rest[1:].strip()
    stattrak = rest.startswith("StatTrak™")
    if stattrak:
        rest = rest[len("StatTrak™"):].strip()
    souvenir = rest.startswith("Souvenir ")
    if souvenir:
        rest = rest[len("Souvenir "):].strip()

    wear = None
    m = _BRACKET.search(rest)
    if m and m.group(1) in _WEAR_BY_NAME:
        wear = _WEAR_BY_NAME[m.group(1)]
        rest = rest[:m.start()].strip()

    weapon = rest.split(" | ")[0].strip() if " | " in rest else rest
    if star:
        category = "gloves" if ("Gloves" in weapon or "Wraps" in weapon) \
            else "knife"
    elif weapon in _GROUP_BY_WEAPON:
        category = _GROUP_BY_WEAPON[weapon]
    elif weapon == "Sticker":
        category = "sticker"
    elif weapon == "Patch":
        category = "patch"
    elif weapon == "Charm":
        category = "charm"
    elif weapon in ("Sealed Graffiti", "Graffiti"):
        category = "graffiti"
    elif weapon == "Music Kit":
        category = "music"
    elif _CONTAINER.search(rest):
        category = "container"
    elif " | " in rest and wear is None:
        # "Name | Faction" with no wear and no known prefix: that is how agents
        # are named, and little else is.
        category = "agent"
    else:
        category = "other"

    return {"category": category, "weapon": weapon if " | " in rest else None,
            "wear": wear, "stattrak": stattrak, "souvenir": souvenir,
            "star": star}


def _dig(obj: Any, path: str) -> Any:
    for seg in path.split("."):
        if not isinstance(obj, dict) or seg not in obj:
            return None
        obj = obj[seg]
    return obj


def _first(obj: Any, paths) -> Any:
    for path in paths:
        value = _dig(obj, path)
        if value not in (None, ""):
            return value
    return None


def rarity_of(record: Any) -> int | None:
    """The rarity number off one sale record, or None."""
    named = _first(record, RARITY_NAME_PATHS)
    if isinstance(named, str):
        key = named.strip().lower()
        for prefix, number in _RARITY_BY_NAME.items():
            if key.startswith(prefix):
                return number
    raw = _first(record, RARITY_PATHS)
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return None
    return number if number in RARITIES else None


def meta_from(records) -> dict[str, Any]:
    """Rarity and collection from the first sale records that carry them."""
    out: dict[str, Any] = {"rarity": None, "collection": None}
    for record in records or ():
        if not isinstance(record, dict):
            continue
        if out["rarity"] is None:
            out["rarity"] = rarity_of(record)
        if out["collection"] is None:
            value = _first(record, COLLECTION_PATHS)
            if isinstance(value, str) and value.strip():
                out["collection"] = value.strip()
        if out["rarity"] is not None and out["collection"] is not None:
            break
    return out


def rarity_label(number: int | None) -> str | None:
    return RARITIES[number][1] if number in RARITIES else None
