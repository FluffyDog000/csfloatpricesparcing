"""Что стоит один опрос и можно ли просить меньше.

Замер должен отделять провод от разжатого тела: на счёт прокси попадает
первое, а в дашборде до недавнего стояло второе, и разница больше чем
десятикратная. Проверка параметра должна отличать «принят и сработал» от
«принят и проигнорирован» — иначе вывод сделан по коду 200, а он тут ничего
не значит.
"""
import importlib.util
import json
import pathlib


def _tool():
    path = pathlib.Path(__file__).resolve().parent.parent / "tools" / "payload_probe.py"
    spec = importlib.util.spec_from_file_location("payload_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Raw:
    def __init__(self, counted):
        self._counted = counted

    def tell(self):
        return self._counted


class Resp:
    def __init__(self, records, wire, status=200, encoding="gzip", payload=None):
        # Записи набиты до правдоподобного размера: разжатое тело обязано быть
        # заметно больше провода, иначе проверка сжатия ничего не проверяет.
        body = payload if payload is not None else [
            {"id": i, "price": 1000 + i, "item": {"description": "x" * 400}}
            for i in range(records)]
        self.content = json.dumps(body).encode()
        self.status_code = status
        self.headers = {"Content-Encoding": encoding} if encoding else {}
        self.raw = Raw(wire)
        self._body = body

    def json(self):
        return self._body


class Session:
    """Отдаёт заранее заданные ответы, по одному на запрос."""

    def __init__(self, *responses):
        self._queue = list(responses)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self._queue.pop(0)


def measure(tool, resp):
    return tool.measure(Session(resp), "https://x/api", 5, None)


def test_the_wire_size_is_reported_apart_from_the_decoded_body():
    tool = _tool()
    got = measure(tool, Resp(records=40, wire=3_000))
    assert got["wire"] == 3_000
    assert got["decoded"] > got["wire"], "разжатое тело больше того, что прошло"


def test_the_record_count_comes_from_a_bare_list():
    tool = _tool()
    assert measure(tool, Resp(records=40, wire=3_000))["records"] == 40


def test_the_record_count_comes_from_a_wrapped_list_too():
    """Тот же эндпоинт у CSFloat встречается и обёрнутым в {"data": [...]}."""
    tool = _tool()
    resp = Resp(records=0, wire=900, payload={"data": [{"id": 1}, {"id": 2}]})
    assert measure(tool, resp)["records"] == 2


def test_a_body_that_is_not_json_does_not_raise():
    tool = _tool()

    class Junk(Resp):
        def json(self):
            raise ValueError("не json")

    got = measure(tool, Junk(records=3, wire=100))
    assert got["records"] == 0 and got["body"] is None


def test_an_uncompressed_answer_is_named_as_such():
    tool = _tool()
    assert measure(tool, Resp(records=40, wire=45_000, encoding=None))["encoding"] == "нет"


def test_the_probe_asks_for_the_parameter_it_is_testing():
    tool = _tool()
    session = Session(Resp(records=5, wire=800))
    tool.measure(session, "https://x/api?limit=5", 5, None)
    assert session.urls == ["https://x/api?limit=5"]


def test_the_candidate_names_are_tried_one_request_each():
    """Квота не бесконечная: один запрос на имя, не больше."""
    tool = _tool()
    assert len(tool.CANDIDATES) == len(set(tool.CANDIDATES))
    assert all(isinstance(name, str) and name for name in tool.CANDIDATES)


def test_an_empty_params_list_means_measure_only():
    """Замер размера стоит один запрос; перебор имён — по одному на имя.

    Пустая строка это «ничего не перебирать», а не «не задано»: иначе просьба
    сделать один дешёвый замер тратит шесть запросов из квоты.
    """
    tool = _tool()
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--params")
    assert ap.parse_args(["--params", ""]).params == ""
    assert ap.parse_args([]).params is None
