"""Doppler phases: one market name, told apart by paint index."""
import logging

from src import phases

logging.disable(logging.WARNING)


def test_a_phase_name_becomes_the_real_name_and_its_paint_index():
    assert phases.split("★ Bayonet | Doppler Phase 1 (Factory New)") == \
        ("★ Bayonet | Doppler (Factory New)", 418)
    assert phases.split("★ Bowie Knife | Doppler Ruby (Factory New)") == \
        ("★ Bowie Knife | Doppler (Factory New)", 415)
    assert phases.split("★ Karambit | Doppler Black Pearl (Factory New)")[1] == 417
    assert phases.split("★ Bayonet | Gamma Doppler Emerald (Factory New)") == \
        ("★ Bayonet | Gamma Doppler (Factory New)", 568)
    assert phases.split("★ StatTrak™ M9 Bayonet | Gamma Doppler Phase 3 (Minimal Wear)") == \
        ("★ StatTrak™ M9 Bayonet | Gamma Doppler (Minimal Wear)", 571)
    assert phases.split("Glock-18 | Gamma Doppler Phase 2 (Factory New)") == \
        ("Glock-18 | Gamma Doppler (Factory New)", 1121)
    assert phases.split("★ Karambit | Doppler (Factory New) - Phase 4") == \
        ("★ Karambit | Doppler (Factory New)", 421)


def test_ordinary_names_pass_through():
    for name in ("★ Bayonet | Doppler (Factory New)", "AK-47 | Redline (Field-Tested)",
                 "★ Karambit | Marble Fade (Factory New)"):
        assert phases.split(name) == (name, None)
        assert not phases.is_phase(name)


def test_requests_carry_the_paint_index():
    from src.config import load_config
    from src.csfloat_client import CSFloatClient
    from src.depth import depth_url

    cfg = load_config()
    client = CSFloatClient(cfg.http, cfg.polling)
    url = client.sales_url("★ Bayonet | Doppler Phase 2 (Factory New)")
    assert "Doppler%20%28Factory%20New%29/sales" in url and url.endswith("paint_index=419")
    assert "Phase" not in url
    d = depth_url("https://csfloat.com", "★ Bayonet | Doppler Sapphire (Factory New)", 0.0, 0.01)
    assert "paint_index=416" in d and "Sapphire" not in d
    assert phases.query("AK-47 | Redline (Field-Tested)") == \
        "market_hash_name=AK-47%20%7C%20Redline%20%28Field-Tested%29"


def test_only_this_phases_sales_are_kept():
    class S:
        def __init__(self, idx):
            self.paint_index = idx
    sales = [S(418), S(419), S(418), S(None)]
    kept = phases.keep("★ Bayonet | Doppler Phase 1 (Factory New)", sales)
    assert [s.paint_index for s in kept] == [418, 418]
    assert phases.keep("AK-47 | Redline (Field-Tested)", sales) == sales


def test_a_phase_item_is_scored_but_never_planned():
    from tests.test_analysis_page import _stocked

    c, _ = _stocked("★ Bayonet | Doppler Phase 1 (Factory New)")
    c.post("/api/analysis/params", json={"an_total_capital": "2000"})
    plan = c.get("/api/analysis/plan").get_json()
    assert plan["phase_items"] == 1
    assert not [a for a in plan["actions"] if a["kind"] == "place"]
