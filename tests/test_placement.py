"""The request that creates an order is configuration, not code.

CSFloat documents three endpoints, all about listings; buy orders are not
among them. The shape of the order request has to be read off the browser, and
guessing at something that spends money is not an option - so until a captured
request is supplied, nothing can be sent.
"""
import pytest

from src.placement import NotConfigured, Spec, describe, load, render


def test_an_unconfigured_bot_cannot_place_anything():
    spec = Spec()
    assert not spec.can_place and not spec.can_cancel
    assert "не настроена" in describe(spec)


def test_cancelling_missing_is_called_out_as_dangerous():
    """Placing without cancelling means no way off a position that has been
    bid past its ceiling - the one rule the whole plan rests on."""
    spec = load('{"create_path": "/api/v1/x", "create_body": "{}"}')
    assert spec.can_place and not spec.can_cancel
    assert "отмена" in describe(spec) and "потолк" in describe(spec)


def test_prices_go_out_in_cents_where_the_capture_used_cents():
    """Every other CSFloat endpoint quotes integer cents. Sending dollars
    where cents were meant bids a hundredth of the price - or a hundred times
    it, which is the direction that empties an account."""
    body = ('{"market_hash_name": "{name}", "price": {price_cents},'
            ' "min_float": {float_min}, "max_float": {float_max}}')
    out = render(body, name="★ Gloves | Fade (Field-Tested)", price=159.0,
                 float_min=0.32, float_max=0.38)
    assert out["price"] == 15900
    assert out["min_float"] == 0.32 and out["max_float"] == 0.38
    assert out["market_hash_name"].startswith("★")


def test_dollars_are_available_too_for_a_capture_that_used_them():
    out = render('{"price": "{price}"}', name="x", price=159.0,
                 float_min=None, float_max=None)
    assert out["price"] == "159.00"


def test_a_template_naming_an_unknown_field_is_refused():
    with pytest.raises(NotConfigured) as err:
        render('{"price": {pennies}}', name="x", price=1.0,
               float_min=None, float_max=None)
    assert "pennies" in str(err.value)


def test_a_template_that_is_not_json_is_refused_before_sending():
    with pytest.raises(NotConfigured):
        render('{"price": {price_cents}', name="x", price=1.0,
               float_min=None, float_max=None)


def test_rubbish_configuration_leaves_the_bot_disarmed():
    for raw in (None, "", "not json", "[1, 2]", '{"unknown": 1}'):
        assert not load(raw).can_place
