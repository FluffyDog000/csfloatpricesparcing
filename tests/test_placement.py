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
    body = ('{"market_hash_name": "{name}", "max_price": {price_cents},'
            ' "min_float": {float_min}, "max_float": {float_max}}')
    out = render(body, name="★ Gloves | Fade (Field-Tested)", price=159.0,
                 float_min=0.32, float_max=0.38)
    assert out["max_price"] == 15900
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


UNSCOPED = {
    "id": "1021510122612067461",
    "created_at": "2026-09-19T19:56:31.971261Z",
    "qty": 1, "price": 630,
    "market_hash_name": "AK-47 | Crane Flight (Battle-Scarred)",
    "hybrid_properties": {}, "bought_item_count": 0,
}
SCOPED = dict(UNSCOPED,
              hybrid_properties={"min_float": 0.605, "max_float": 1})


def test_a_created_order_is_read_back_as_we_store_it():
    """Both replies came off the site: money in integer cents, and the float
    bounds inside hybrid_properties rather than beside them."""
    from src.placement import parse_order

    got = parse_order(SCOPED)
    assert got["remote_id"] == "1021510122612067461"
    assert got["price"] == 6.30, "630 is cents, not dollars"
    assert got["float_min"] == 0.605 and got["float_max"] == 1.0
    assert got["qty"] == 1

    plain = parse_order(UNSCOPED)
    assert plain["float_min"] is None and plain["float_max"] is None


def test_a_fill_is_visible_only_through_bought_item_count():
    """Nothing else in the reply says whether the money is still waiting."""
    from src.placement import parse_order

    assert parse_order(SCOPED)["bought"] == 0
    assert parse_order(dict(SCOPED, bought_item_count=1))["bought"] == 1


def test_the_older_nested_float_shape_is_read_too():
    from src.placement import parse_order

    nested = dict(UNSCOPED, hybrid_properties={
        "float_value": {"min": 0.15, "max": 0.179999}})
    got = parse_order(nested)
    assert got["float_min"] == 0.15 and got["float_max"] == 0.179999


def test_a_reply_without_an_id_is_not_an_order():
    from src.placement import parse_order

    assert parse_order({"price": 630}) is None
    assert parse_order(None) is None
    assert parse_order({"data": SCOPED})["remote_id"] == SCOPED["id"]


def test_the_default_body_matches_what_the_site_returned():
    """The reply echoes the request's fields, so the template is built from
    it: only the method and path are still unknown."""
    import json

    from src.placement import DEFAULT_CREATE_BODY, parse_order, render

    sent = render(DEFAULT_CREATE_BODY,
                  name="AK-47 | Crane Flight (Battle-Scarred)",
                  price=6.30, float_min=0.605, float_max=1.0)
    assert sent["max_price"] == 630, "cents, and named as the request wants it"
    assert sent["hybrid_properties"] == {"min_float": 0.605, "max_float": 1.0}
    assert sent["market_hash_name"] == SCOPED["market_hash_name"]

    # Round trip: what we would send, read back the way a reply is read -
    # which means renaming max_price to price, as CSFloat does.
    echoed = parse_order(dict(sent, id="x", price=sent.pop("max_price"),
                              bought_item_count=0))
    assert echoed["price"] == 6.30
    assert echoed["float_min"] == 0.605 and echoed["float_max"] == 1.0


def test_amending_an_order_is_its_own_operation():
    """PATCH /api/v1/buy-orders/{id} was captured off the site, so answering
    an outbid changes the order rather than replacing it - a replacement would
    give up whatever standing the original had."""
    from src.placement import SUGGESTED, endpoint, render

    assert SUGGESTED.update_method == "PATCH"
    assert SUGGESTED.can_update
    assert endpoint(SUGGESTED.update_path, "1021510122612067461") == \
        "/api/v1/buy-orders/1021510122612067461"

    # The captured body carries the order's whole shape, not just the price:
    # if CSFloat reads a PATCH as a replacement, sending the price alone would
    # clear the float filter and an order buying 0.15-0.17 would start buying
    # anything. An unscoped order sends nulls, exactly as the site does.
    body = render(SUGGESTED.update_body, name="x", price=6.30,
                  float_min=None, float_max=None)
    assert body == {"max_price": 630, "quantity": 1,
                    "min_float": None, "max_float": None}

    scoped = render(SUGGESTED.update_body, name="x", price=6.30,
                    float_min=0.15, float_max=0.17)
    assert scoped["min_float"] == 0.15 and scoped["max_float"] == 0.17, \
        "amending a price must not widen what the order will buy"


def test_a_path_needing_an_id_refuses_to_render_without_one():
    import pytest as pt

    from src.placement import NotConfigured, endpoint

    with pt.raises(NotConfigured):
        endpoint("/api/v1/buy-orders/{order_id}", None)
    assert endpoint("/api/v1/buy-orders", None) == "/api/v1/buy-orders"


def test_the_suggestion_is_not_the_configuration():
    """Create, amend and cancel were all captured from the browser. The amend
    body was not, and that is the piece that separates changing a price from
    taking the order down. An empty spec stays empty until something is saved
    deliberately."""
    from src.placement import CONFIRMED, SUGGESTED, load

    assert not load(None).can_place
    assert CONFIRMED == {"create_path", "create_method", "create_body",
                         "update_path", "update_method", "update_body",
                         "cancel_path", "cancel_method"}
    assert SUGGESTED.create_path == "/api/v1/buy-orders"
    assert SUGGESTED.cancel_method == "DELETE"
    # The amend body was the last piece worked out rather than seen; it was
    # captured in the end, and it carries the float bounds because a PATCH
    # that omits them may be read as clearing them.
    assert '"min_float"' in SUGGESTED.update_body


def test_a_missing_amend_endpoint_is_called_out_as_a_cost():
    from src.placement import describe, load

    spec = load('{"create_path": "/x", "create_body": "{}", '
                '"cancel_path": "/x/{order_id}"}')
    assert spec.can_place and spec.can_cancel and not spec.can_update
    assert "правка" in describe(spec)


def test_the_price_field_is_named_as_the_request_wants_it():
    """Six orders came back "orders must have a max price above 0" (code 5).
    The reply calls the field price; the request calls it max_price, and the
    message names its meaning too - a buy order carries the most you will pay,
    and CSFloat charges the lower of that and the listing."""
    from src.placement import DEFAULT_CREATE_BODY, SUGGESTED, render

    created = render(DEFAULT_CREATE_BODY, name="x", price=49.0,
                     float_min=0.1501, float_max=0.158)
    assert created["max_price"] == 4900
    assert "price" not in created, "the name the reply uses is not the one sent"

    amended = render(SUGGESTED.update_body, name="x", price=49.0,
                     float_min=None, float_max=None)
    assert amended["max_price"] == 4900, "the same field amends it"
    assert "price" not in amended
