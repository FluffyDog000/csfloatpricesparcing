"""The one part of placing an order that we do not know: the request itself.

CSFloat's documented API covers listings only - three endpoints, none of them
about buy orders. The order endpoints exist, the site uses them, but their
shape has to be read off the browser rather than a specification, and guessing
it is not an option when the thing being guessed at spends money.

So the shape is configuration, not code. Capture one real request from the
browser's network tab - method, path, and the JSON body, with the Cookie
header left out - and describe it here. Until that is filled in, every call
raises NotConfigured and the executor stays in dry run.

The template is filled from a small, fixed vocabulary, so a captured body can
be transcribed without writing Python:

    {"market_hash_name": "{name}", "price": "{price_cents}",
     "max_float": "{float_max}", "min_float": "{float_min}", "qty": 1}

    {name} {price} {price_cents} {float_min} {float_max} {quantity}
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

PLACEMENT_KEY = "placement_spec"

FIELDS = ("name", "price", "price_cents", "float_min", "float_max", "quantity")

# Confirmed against two real replies from CSFloat - one unscoped order and one
# scoped to a float range - so only the method and path are still guesswork:
#
#   {"id": "1021510122612067461", "qty": 1, "price": 630,
#    "market_hash_name": "AK-47 | Crane Flight (Battle-Scarred)",
#    "hybrid_properties": {"min_float": 0.605, "max_float": 1},
#    "bought_item_count": 0}
#
# Money is in integer cents ($6.30 arrives as 630), and the float bounds sit
# inside hybrid_properties under min_float/max_float - the same shape the
# order-book parser already reads off the book.
#
# The reply calls the price `price`; the request does not. Sending that name
# drew "orders must have a max price above 0" (code 5), which names the field
# and its meaning at once: a buy order carries the most you will pay, and
# CSFloat charges the lower of that and the listing.
DEFAULT_CREATE_BODY = (
    '{"market_hash_name": "{name}", "max_price": {price_cents},'
    ' "qty": {quantity},'
    ' "hybrid_properties": {"min_float": {float_min}, "max_float": {float_max}}}'
)


class NotConfigured(RuntimeError):
    """No captured request to work from, so nothing may be sent."""


@dataclass
class Spec:
    """How to ask CSFloat to create, amend, cancel and list buy orders."""
    create_method: str = "POST"
    create_path: str = ""
    create_body: str = ""
    # Confirmed off the site: an order is amended in place rather than being
    # cancelled and posted again, so answering an outbid keeps the order - and
    # whatever standing it has - instead of going to the back of the queue.
    update_method: str = "PATCH"
    update_path: str = ""          # contains {order_id}
    update_body: str = ""
    cancel_method: str = "DELETE"
    cancel_path: str = ""          # may contain {order_id}
    cancel_body: str = ""
    list_path: str = ""
    remote_id_path: str = "id"     # where the new order's id sits in the reply

    @property
    def can_place(self) -> bool:
        return bool(self.create_path and self.create_body)

    @property
    def can_update(self) -> bool:
        return bool(self.update_path and self.update_body)

    @property
    def can_cancel(self) -> bool:
        return bool(self.cancel_path)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


# Captured from the browser, one request at a time:
#
#   POST   https://csfloat.com/api/v1/buy-orders             create
#   PATCH  https://csfloat.com/api/v1/buy-orders/{order_id}  amend in place
#   DELETE https://csfloat.com/api/v1/buy-orders/{order_id}  take down
#
# What remains guessed is the amend body. It matters on its own: the body is
# what separates changing a price from taking an order down, and getting it
# wrong on a live position is not a cheap mistake. Nothing is sent until the
# configuration is saved deliberately.
SUGGESTED = Spec(
    create_method="POST",                          # confirmed
    create_path="/api/v1/buy-orders",              # confirmed
    create_body=DEFAULT_CREATE_BODY,               # from the reply it returns
    update_method="PATCH",                         # confirmed
    update_path="/api/v1/buy-orders/{order_id}",   # confirmed
    update_body='{"max_price": {price_cents}}',    # same field as create
    cancel_method="DELETE",                        # confirmed
    cancel_path="/api/v1/buy-orders/{order_id}",   # confirmed
    list_path="/api/v1/buy-orders",                # guessed
)

CONFIRMED = {"create_path", "create_method", "update_path", "update_method",
             "cancel_path", "cancel_method"}


def endpoint(path: str, order_id: str | None = None) -> str:
    """Fill {order_id} in a configured path."""
    if "{order_id}" not in path:
        return path
    if not order_id:
        raise NotConfigured(f"путь {path} требует id ордера, а его нет")
    return path.replace("{order_id}", str(order_id))


def load(raw: str | None) -> Spec:
    if not raw:
        return Spec()
    try:
        data = json.loads(raw)
    except ValueError:
        return Spec()
    if not isinstance(data, dict):
        return Spec()
    known = set(Spec().__dict__)
    return Spec(**{k: v for k, v in data.items() if k in known})


def render(template: str, *, name: str, price: float,
           float_min: float | None, float_max: float | None,
           quantity: int = 1) -> Any:
    """Fill a captured body with one order's values.

    Prices go out in cents wherever the capture used cents, which is how every
    other CSFloat endpoint quotes money - passing dollars where cents were
    meant would bid a hundredth of the intended price, or a hundred times it.
    """
    values = {
        "name": name,
        "price": f"{price:.2f}",
        "price_cents": str(int(round(price * 100))),
        "float_min": "" if float_min is None else f"{float_min:g}",
        "float_max": "" if float_max is None else f"{float_max:g}",
        "quantity": str(int(quantity)),
    }
    unknown = [m for m in re.findall(r"\{(\w+)\}", template)
               if m not in values and m != "order_id"]
    if unknown:
        raise NotConfigured(
            f"в шаблоне неизвестные поля: {', '.join(sorted(set(unknown)))}. "
            f"Доступны: {', '.join(FIELDS)}")

    filled = template
    for key, value in values.items():
        filled = filled.replace("{" + key + "}", value)
    try:
        return json.loads(filled)
    except ValueError as exc:
        raise NotConfigured(f"тело запроса не разбирается как JSON: {exc}") from exc


def parse_order(payload: Any) -> dict[str, Any] | None:
    """Normalise one buy order as CSFloat returns it.

    `bought_item_count` is how a fill is noticed: the order that created it
    reports zero, and an order that has bought something reports what it
    bought. Nothing else in the reply says whether our money is still waiting
    or has already been spent.
    """
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if payload.get("id") in (None, ""):
        return None

    props = payload.get("hybrid_properties")
    props = props if isinstance(props, dict) else {}
    price = payload.get("price")

    def number(value):
        if isinstance(value, bool) or value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # Two shapes have been seen for the float bounds: flat min_float/max_float,
    # and a nested float_value.{min,max}. The order-book parser reads both, so
    # this does too rather than trusting whichever one arrived today.
    nested = props.get("float_value")
    nested = nested if isinstance(nested, dict) else {}

    def bound(flat: str, inner: str):
        value = props.get(flat)
        return number(value if value is not None else nested.get(inner))

    return {
        "remote_id": str(payload["id"]),
        # Integer cents everywhere, as every other endpoint quotes money.
        "price": None if number(price) is None else round(number(price) / 100, 2),
        "qty": int(number(payload.get("qty")) or 1),
        "float_min": bound("min_float", "min"),
        "float_max": bound("max_float", "max"),
        "bought": int(number(payload.get("bought_item_count")) or 0),
        "created_at": payload.get("created_at"),
        "market_hash_name": payload.get("market_hash_name"),
    }


def describe(spec: Spec) -> str:
    """One line for the dashboard: what is configured and what is missing."""
    if not spec.can_place:
        return ("постановка не настроена — нужен захваченный запрос создания "
                "ордера (метод, путь, тело JSON)")
    missing = []
    if not spec.can_cancel:
        missing.append("отмена — снять позицию, которую перебили выше потолка, "
                       "будет нечем")
    if not spec.can_update:
        missing.append("правка — перебивать придётся снятием и постановкой "
                       "заново")
    if missing:
        return "настроено создание, но нет: " + "; ".join(missing)
    return f"настроено: {spec.create_method} {spec.create_path}"
