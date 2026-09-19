"""Carrying out one action against CSFloat.

Kept apart from both the deciding and the transport: the executor says what
should happen, the client knows about routes and limits, and this turns one
into the other. Three things it insists on.

A write is never retried. A create that timed out may already have placed the
order, and sending it again to find out is how an account ends up holding two.
A failed action is reported and left alone.

Every send is preceded by the same rendering the configuration was checked
with, so a template that cannot produce valid JSON fails before a request goes
out rather than halfway through a plan.

Nothing happens unless it was armed. Arming is separate from configuring and
separate from planning, and it is spent by use: a plan is applied once.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from .executor import CANCEL, KEEP, PLACE, RAISE, Action
from .placement import NotConfigured, Spec, endpoint, parse_order, render

log = logging.getLogger("csfloat.sender")


@dataclass
class Result:
    action: Action
    ok: bool
    detail: str
    remote_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        out["action"] = self.action.as_dict()
        return out


class Sender:
    """Performs actions. `send` is the client's send_json, or a stand-in."""

    def __init__(self, base_url: str, spec: Spec,
                 send: Callable[..., Any],
                 headers: dict[str, str] | None = None,
                 dry_run: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.spec = spec
        self.send = send
        self.headers = headers
        self.dry_run = dry_run

    def _url(self, path: str, order_id: str | None = None) -> str:
        return self.base_url + endpoint(path, order_id)

    def place(self, action: Action, name: str) -> Result:
        if not self.spec.can_place:
            return Result(action, False, "запрос создания не настроен")
        body = render(self.spec.create_body, name=name, price=action.price,
                      float_min=action.float_min, float_max=action.float_max)
        url = self._url(self.spec.create_path)
        if self.dry_run:
            return Result(action, True,
                          f"[вхолостую] {self.spec.create_method} {url} {body}")
        # Carried into the failure below: a refusal that does not say what was
        # sent leaves "the code was corrected" and "the saved shape was
        # corrected" looking identical, and only the second one is what travels.
        action.sent = body
        reply = self.send(self.spec.create_method, url, body, self.headers)
        parsed = parse_order(reply)
        if not parsed:
            return Result(action, False,
                          f"ответ без id ордера: {reply!r:.120} · отправлено: "
                          + json.dumps(body, ensure_ascii=False))
        log.info("Placed %s %.4f-%.4f at $%.2f -> %s", name, action.float_min,
                 action.float_max, action.price, parsed["remote_id"])
        return Result(action, True, f"поставлен по ${parsed['price']:.2f}",
                      remote_id=parsed["remote_id"])

    def amend(self, action: Action, name: str) -> Result:
        if not self.spec.can_update:
            return Result(action, False, "запрос правки не настроен")
        if not action.remote_id:
            return Result(action, False, "нет id ордера на сайте — нечего править")
        body = render(self.spec.update_body, name=name, price=action.price,
                      float_min=action.float_min, float_max=action.float_max)
        url = self._url(self.spec.update_path, action.remote_id)
        if self.dry_run:
            return Result(action, True,
                          f"[вхолостую] {self.spec.update_method} {url} {body}")
        action.sent = body
        self.send(self.spec.update_method, url, body, self.headers)
        log.info("Amended %s to $%.2f", action.remote_id, action.price)
        return Result(action, True, f"цена изменена на ${action.price:.2f}",
                      remote_id=action.remote_id)

    def cancel(self, action: Action, name: str) -> Result:
        if not self.spec.can_cancel:
            return Result(action, False, "запрос отмены не настроен")
        if not action.remote_id:
            # Never placed, or placed and lost track of. Dropping our row is
            # right either way; sending nothing is what keeps it honest.
            return Result(action, True, "не стоял на сайте — снят только у нас")
        body = (render(self.spec.cancel_body, name=name, price=action.price,
                       float_min=action.float_min, float_max=action.float_max)
                if self.spec.cancel_body else None)
        url = self._url(self.spec.cancel_path, action.remote_id)
        if self.dry_run:
            return Result(action, True,
                          f"[вхолостую] {self.spec.cancel_method} {url}")
        self.send(self.spec.cancel_method, url, body, self.headers)
        log.info("Cancelled %s", action.remote_id)
        return Result(action, True, "снят", remote_id=action.remote_id)

    def perform(self, action: Action) -> Result:
        """One action. Failures are returned, not raised: one refusal must not
        abandon the rest of a plan half-applied."""
        if action.kind == KEEP:
            return Result(action, True, action.reason)
        try:
            if action.kind == PLACE:
                return self.place(action, action.item)
            if action.kind == RAISE:
                return self.amend(action, action.item)
            if action.kind == CANCEL:
                return self.cancel(action, action.item)
        except NotConfigured as exc:
            return Result(action, False, str(exc))
        except Exception as exc:  # noqa: BLE001 - the plan continues
            log.warning("Action %s on %s failed: %s", action.kind, action.item, exc)
            detail = f"{type(exc).__name__}: {exc}"
            if action.sent is not None:
                detail += " · отправлено: " + json.dumps(action.sent,
                                                         ensure_ascii=False)
            return Result(action, False, detail)
        return Result(action, False, f"неизвестное действие {action.kind}")
