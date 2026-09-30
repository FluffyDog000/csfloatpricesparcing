"""Что стоит один опрос и можно ли просить меньше.

Замер обязан отделять провод от разжатого тела: на счёт прокси попадает
первое, а в дашборде до недавнего стояло второе. Первая живая попытка
показала обе графы одинаковыми — счётчик сжатого потока у urllib3 вернул ноль
и замер свалился в длину разжатого тела, выведя одну величину из другой.
Поэтому байты считаются здесь, из сырого потока.

Проверка параметра должна отличать «принят и сработал» от «принят и
проигнорирован» — иначе вывод делается по коду 200, а он тут ничего не значит.
"""
import gzip
import importlib.util
import json
import pathlib

import pytest


def _tool():
    path = pathlib.Path(__file__).resolve().parent.parent / "tools" / "payload_probe.py"
    spec = importlib.util.spec_from_file_location("payload_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def page(records):
    """Страница продаж правдоподобного размера: описание повторяется в каждой
    записи, из-за чего она и жмётся в разы."""
    return json.dumps([
        {"id": i, "price": 1000 + i, "created_at": "2026-09-29T10:00:00Z",
         "item": {"float_value": 0.2 + i / 1000, "paint_seed": i,
                  "market_hash_name": "AK-47 | Redline (Field-Tested)",
                  "description": "лорный текст, одинаковый во всех записях " * 8}}
        for i in range(records)]).encode()


class Raw:
    def __init__(self, body):
        self._body = body

    def read(self, decode_content=False):
        assert decode_content is False, "поток читается сырым, как пришёл"
        return self._body


class Resp:
    def __init__(self, body, status=200, encoding="gzip", headers=None):
        self.status_code = status
        self.headers = dict(headers or {})
        if encoding:
            self.headers["Content-Encoding"] = encoding
        self.raw = Raw(body)
        self.closed = False

    def close(self):
        self.closed = True


class Session:
    """Отдаёт заранее заданные ответы, по одному на запрос."""

    def __init__(self, *responses):
        self._queue = list(responses)
        self.urls = []
        self.kwargs = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        self.kwargs.append(kwargs)
        return self._queue.pop(0)


def measure(tool, resp):
    return tool.measure(Session(resp), "https://x/api", 5, None)


def test_the_wire_size_is_measured_apart_from_the_decoded_body():
    """Обе величины замерены, а не выведены одна из другой."""
    tool = _tool()
    plain = page(40)
    got = measure(tool, Resp(gzip.compress(plain, 6)))
    assert got["decoded"] == len(plain)
    assert got["wire"] < got["decoded"] / 5, "сжатие обязано быть видно"
    assert got["records"] == 40


def test_brotli_is_decoded_like_gzip():
    brotli = pytest.importorskip("brotli")
    tool = _tool()
    plain = page(40)
    got = measure(tool, Resp(brotli.compress(plain, quality=5), encoding="br"))
    assert got["decoded"] == len(plain)
    assert got["records"] == 40


def test_an_uncompressed_answer_measures_the_same_both_ways():
    """Без сжатия провод и тело совпадают — и это законный случай, не сбой."""
    tool = _tool()
    plain = page(10)
    got = measure(tool, Resp(plain, encoding=None))
    assert got["wire"] == got["decoded"] == len(plain)
    assert got["encoding"] == "нет"


def test_a_header_naming_a_codec_the_body_does_not_use_is_reported():
    """Так делает прокси, распаковавший ответ и не снявший заголовок."""
    tool = _tool()
    got = measure(tool, Resp(page(5), encoding="br"))
    assert got["failed"], "молчать об этом нельзя — обе графы совпадут"
    assert got["records"] == 5, "тело всё равно разобрано"


def test_the_record_count_comes_from_a_wrapped_list_too():
    tool = _tool()
    body = json.dumps({"data": [{"id": 1}, {"id": 2}]}).encode()
    assert measure(tool, Resp(body, encoding=None))["records"] == 2


def test_a_body_that_is_not_json_does_not_raise():
    tool = _tool()
    got = measure(tool, Resp(b"<html>Cloudflare</html>", encoding=None))
    assert got["records"] == 0 and got["body"] is None


def test_the_missing_content_length_is_named_rather_than_assumed():
    """Ответ кусками (chunked) заголовка не несёт — это надо видеть в выводе."""
    tool = _tool()
    got = measure(tool, Resp(page(5), encoding=None))
    assert got["declared"] is None
    got = measure(tool, Resp(page(5), encoding=None,
                             headers={"Content-Length": "1234"}))
    assert got["declared"] == "1234"


def test_the_connection_is_released_even_though_the_read_is_streamed():
    """stream=True держит соединение открытым, пока его не закрыть."""
    tool = _tool()
    resp = Resp(page(5), encoding=None)
    session = Session(resp)
    tool.measure(session, "https://x/api", 5, None)
    assert session.kwargs[0]["stream"] is True
    assert resp.closed


def test_the_probe_asks_for_the_parameter_it_is_testing():
    tool = _tool()
    session = Session(Resp(page(5), encoding=None))
    tool.measure(session, "https://x/api?limit=5", 5, None)
    assert session.urls == ["https://x/api?limit=5"]


def test_the_candidate_names_are_tried_one_request_each():
    """Квота не бесконечная: один запрос на имя, не больше."""
    tool = _tool()
    assert len(tool.CANDIDATES) == len(set(tool.CANDIDATES))
    assert all(isinstance(name, str) and name for name in tool.CANDIDATES)


def test_an_empty_params_list_means_measure_only():
    """Пустая строка — «ничего не перебирать», а не «не задано»: иначе просьба
    сделать один дешёвый замер тратит шесть запросов из квоты."""
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--params")
    assert ap.parse_args(["--params", ""]).params == ""
    assert ap.parse_args([]).params is None
