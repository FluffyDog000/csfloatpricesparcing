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


class NotConfigured(RuntimeError):
    """No captured request to work from, so nothing may be sent."""


@dataclass
class Spec:
    """How to ask CSFloat to create, cancel and list buy orders."""
    create_method: str = "POST"
    create_path: str = ""
    create_body: str = ""
    cancel_method: str = "DELETE"
    cancel_path: str = ""          # may contain {order_id}
    cancel_body: str = ""
    list_path: str = ""
    remote_id_path: str = "id"     # where the new order's id sits in the reply

    @property
    def can_place(self) -> bool:
        return bool(self.create_path and self.create_body)

    @property
    def can_cancel(self) -> bool:
        return bool(self.cancel_path)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


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


def describe(spec: Spec) -> str:
    """One line for the dashboard: what is configured and what is missing."""
    if not spec.can_place:
        return ("постановка не настроена — нужен захваченный запрос создания "
                "ордера (метод, путь, тело JSON)")
    if not spec.can_cancel:
        return ("создание настроено, отмена — нет: снять ордер бот не сможет, "
                "и защита потолком работать не будет")
    return f"настроено: {spec.create_method} {spec.create_path}"
