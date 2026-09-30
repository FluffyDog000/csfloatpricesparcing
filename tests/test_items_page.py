"""The item list, filtered by several things at once.

With thousands of items a folder of three hundred is not browsable; the list
has to answer "Field-Tested or Minimal Wear gloves that sold at least five
times this week" in one go. Every scenario here runs the page's real script.
"""
import json
import os
import shutil
import subprocess
import tempfile

import pytest

from src.catalog import CATEGORIES, WEARS, describe, meta_from, rarity_of

NODE = shutil.which("node")


def item(name, **kw):
    out = {"market_hash_name": name, "active": True, "pattern_sensitive": True,
           "hidden": False, "folder": "", "icon_url": None, "total_sales": 10,
           "sales_1d": 0, "sales_7d": 0, "sales_30d": 0, "sales_90d": 0,
           "avg_price": 10.0, "price": 10.0, "min_price": 1, "max_price": 20,
           "last_polled_at": None, "last_sold_at": "2026-09-29T10:00:00+00:00",
           "rarity": None, "rarity_name": None, "collection": None}
    out.update(describe(name))
    out.update(kw)
    return out


def run(items, scenario):
    if NODE is None:
        pytest.skip("node is not installed")
    reply = {"items": items, "folders": [], "last_update": None,
             "categories": [[k, v] for k, v in CATEGORIES.items()],
             "wears": [[c, ru] for c, _, ru in WEARS]}
    tmp = tempfile.mkdtemp()
    with open(os.path.join(tmp, "r.json"), "w", encoding="utf-8") as fh:
        json.dump(reply, fh, ensure_ascii=False)
    with open(os.path.join(tmp, "s.js"), "w", encoding="utf-8") as fh:
        fh.write(scenario)
    out = subprocess.run([NODE, "tests/js/items_stub.js",
                          os.path.join(tmp, "r.json"), os.path.join(tmp, "s.js")],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


NAMES = "currentList.map((i) => i.market_hash_name)"

MIX = [
    item("★ Driver Gloves | Lunar Weave (Field-Tested)", sales_7d=9, rarity=6,
         rarity_name="Тайное"),
    item("★ Sport Gloves | Vice (Minimal Wear)", sales_7d=2, rarity=6,
         rarity_name="Тайное"),
    item("★ Karambit | Fade (Factory New)", sales_7d=7, rarity=6,
         rarity_name="Тайное"),
    item("AK-47 | Redline (Field-Tested)", sales_7d=40, rarity=5,
         rarity_name="Засекреченное", collection="The Phoenix Collection"),
    item("StatTrak™ AK-47 | Redline (Field-Tested)", sales_7d=12, rarity=5,
         rarity_name="Засекреченное", collection="The Phoenix Collection",
         folder="rifles"),
    item("Sticker | Crown (Foil)", sales_7d=3),
]


def test_groups_narrow_each_other_and_values_inside_one_add_up():
    got = run(MIX, f"""
      filters.sets.category.add("gloves"); filters.sets.category.add("knife");
      filters.sets.wear.add("FT"); filters.sets.wear.add("FN");
      render(); {NAMES}""")
    assert sorted(got) == ["★ Driver Gloves | Lunar Weave (Field-Tested)",
                           "★ Karambit | Fade (Factory New)"]


def test_sales_in_a_chosen_period_combine_with_the_chips():
    got = run(MIX, f"""
      document.getElementById("sales-min").value = "5";
      document.getElementById("sales-period").value = "sales_7d";
      filters.sets.rarity.add("6");
      render(); {NAMES}""")
    assert sorted(got) == ["★ Driver Gloves | Lunar Weave (Field-Tested)",
                           "★ Karambit | Fade (Factory New)"]


def test_stattrak_collection_and_folder_are_filters_too():
    got = run(MIX, f"""
      filters.sets.collection.add("The Phoenix Collection");
      filters.sets.special.add("st");
      render(); {NAMES}""")
    assert got == ["StatTrak™ AK-47 | Redline (Field-Tested)"]
    got = run(MIX, f"""filters.sets.folder.add("rifles"); render(); {NAMES}""")
    assert got == ["StatTrak™ AK-47 | Redline (Field-Tested)"]


def test_each_chip_counts_what_ticking_it_would_show():
    """Faceted: the wear chips count within the chosen type, and the type
    chips ignore the type choice itself - otherwise the other types read 0."""
    got = run(MIX, """
      filters.sets.category.add("gloves");
      document.getElementById("filter-panel").hidden = false;
      render();
      ({wear: __selected['.fgroup[data-group="wear"] .chips'].innerHTML,
        cat: __selected['.fgroup[data-group="category"] .chips'].innerHTML})""")
    assert 'data-value="FT">FT · После полевых испытаний<span class="n">1</span>' in got["wear"]
    assert 'data-value="FN">FN · Прямо с завода<span class="n">0</span>' in got["wear"]
    assert 'data-value="rifle">Винтовки<span class="n">2</span>' in got["cat"]


def test_thousands_are_drawn_a_page_at_a_time_but_selected_whole():
    many = [item(f"AK-47 | Skin {i:04d} (Field-Tested)") for i in range(300)]
    got = run(many, """
      filters.sets.category.add("rifle"); render();
      const grid = __nodes["cards"].children[0];
      visibleNames().forEach((n) => selected.add(n));
      ({drawn: grid.children.length, selected: selected.size,
        more: __nodes["cards"].children[1].textContent})""")
    assert got["drawn"] == 120
    assert got["selected"] == 300, "select all means all that matched"
    assert "из 180" in got["more"]


def test_sorting_by_the_chosen_period_and_missing_values_sink():
    got = run(MIX, f"""
      filters.sets.category.add("rifle"); filters.sets.category.add("knife");
      document.getElementById("sales-period").value = "sales_7d";
      document.getElementById("sort-by").value = "period-desc";
      render(); {NAMES}""")
    assert got == ["AK-47 | Redline (Field-Tested)",
                   "StatTrak™ AK-47 | Redline (Field-Tested)",
                   "★ Karambit | Fade (Factory New)"]
    got = run(MIX, f"""
      filters.sets.category.add("sticker"); filters.sets.category.add("rifle");
      document.getElementById("sort-by").value = "rarity-asc";
      render(); {NAMES}""")
    assert got[-1] == "Sticker | Crown (Foil)", "no rarity goes last either way"


def test_no_filter_keeps_the_folders():
    got = run(MIX, "render(); ({flat: state.flat, tiles: __nodes['cards'].children[0].children.length})")
    assert got == {"flat": False, "tiles": 2}


# -- what the name and the sales record say ----------------------------------

@pytest.mark.parametrize("name,want", [
    ("★ StatTrak™ Karambit | Doppler (Factory New)",
     {"category": "knife", "stattrak": True, "wear": "FN", "star": True}),
    ("★ Hand Wraps | Duct Tape (Field-Tested)", {"category": "gloves"}),
    ("Souvenir AWP | Dragon Lore (Battle-Scarred)",
     {"category": "sniper", "souvenir": True, "wear": "BS", "weapon": "AWP"}),
    ("StatTrak™ Glock-18 | Fade (Factory New)", {"category": "pistol"}),
    ("MP9 | Starlight Protector (Well-Worn)", {"category": "smg"}),
    ("Sticker | Crown (Foil)", {"category": "sticker", "wear": None}),
    ("Kilowatt Case", {"category": "container"}),
    ("Sir Bloody Miami Darryl | The Professionals", {"category": "agent"}),
])
def test_the_name_says_most_of_it(name, want):
    got = describe(name)
    for key, value in want.items():
        assert got[key] == value, (key, got)


def test_rarity_and_collection_come_off_the_sales_record():
    records = [{"price": 1, "item": {"float_value": 0.2}},
               {"price": 1, "item": {"rarity": 6,
                                     "collection": "The Phoenix Collection"}}]
    assert meta_from(records) == {"rarity": 6,
                                  "collection": "The Phoenix Collection"}
    assert rarity_of({"item": {"rarity_name": "Mil-Spec Grade", "rarity": 9}}) == 3
    assert rarity_of({"item": {"rarity": 42}}) is None
    assert meta_from([{"item": {}}]) == {"rarity": None, "collection": None}


def test_the_items_reply_carries_what_the_filters_read():
    from tests.test_analysis_page import _app
    import webapp

    c = _app(["★ Karambit | Fade (Factory New)"])
    db = webapp.Database(os.environ["CSFLOAT_DB_PATH"])
    db.set_meta("★ Karambit | Fade (Factory New)", 6, "The Chroma Collection")
    db.set_meta("★ Karambit | Fade (Factory New)", None, None)
    db.close()
    it = c.get("/api/items").get_json()["items"][0]
    for key in ("sales_1d", "sales_7d", "sales_30d", "sales_90d", "price"):
        assert key in it
    assert (it["category"], it["wear"], it["rarity_name"], it["collection"]) \
        == ("knife", "FN", "Тайное", "The Chroma Collection"), \
        "a poll without the fields must not wipe what an earlier one stored"


def test_a_selection_goes_to_the_analysis_list_in_one_request():
    """Picked here, swept there: the list the order-book sweep runs over."""
    from tests.test_analysis_page import _app

    names = ["AK-47 | Redline (Field-Tested)", "AWP | Asiimov (Field-Tested)",
             "★ Karambit | Fade (Factory New)"]
    c = _app(names)
    c.post("/api/analysis/items", json={"market_hash_name": names[0]})
    r = c.post("/api/analysis/items", json={
        "action": "add_many", "names": names + ["Nobody | Tracks This"]}).get_json()
    assert r["items"] == names, "what was there stays first, nothing twice"
    assert r["unknown"] == ["Nobody | Tracks This"]

    flags = {i["market_hash_name"]: i["in_analysis"]
             for i in c.get("/api/items").get_json()["items"]}
    assert all(flags[n] for n in names)
    assert not any(v for n, v in flags.items() if n not in names), \
        "only what was sent"

    r = c.post("/api/analysis/items", json={
        "action": "remove_many", "names": names[1:]}).get_json()
    assert r["items"] == names[:1]


def test_the_analysis_list_is_a_filter_too():
    items = [item("AK-47 | Redline (Field-Tested)", in_analysis=True),
             item("AWP | Asiimov (Field-Tested)", in_analysis=False)]
    got = run(items, f"""filters.sets.analysis.add("out"); render(); {NAMES}""")
    assert got == ["AWP | Asiimov (Field-Tested)"]


def test_cards_are_picked_one_by_one_and_shift_takes_the_range():
    """The 18px box was the only target, and a click that missed it opened
    the item's page. A click on the card picks it; Shift picks the run
    between the last card clicked and this one, in the order shown."""
    many = [item(f"AK-47 | Skin {i:02d} (Field-Tested)") for i in range(10)]
    got = run(many, """
      manageMode = true;
      filters.sets.category.add("rifle"); render();
      const n = (i) => currentList[i].market_hash_name;
      pickCard(n(1), false);
      const one = Array.from(selected);
      pickCard(n(1), false);
      const none = selected.size;
      pickCard(n(2), false); pickCard(n(6), true);
      ({one, none, range: Array.from(selected).sort()})""")
    assert got["one"] == ["AK-47 | Skin 01 (Field-Tested)"]
    assert got["none"] == 0, "a second click takes it back"
    assert got["range"] == [f"AK-47 | Skin {i:02d} (Field-Tested)"
                            for i in range(2, 7)]
