"""Трафик считается по проводу, а не по разжатому телу.

CSFloat отдаёт JSON сжатым. `len(resp.content)` — это уже разжатое тело, и
прогноз, построенный на нём, покупает гигабайты, которых работа не требует.
Метрический прокси выставляет счёт за то, что реально прошло по каналу.
"""
import pytest

from src.csfloat_client import wire_bytes


class Raw:
    def __init__(self, counted):
        self._counted = counted

    def tell(self):
        if self._counted is None:
            raise OSError("поток не считает")
        return self._counted


class Resp:
    def __init__(self, body=b"", counted=None, headers=None, raw=True):
        self.content = body
        self.headers = headers or {}
        if raw:
            self.raw = Raw(counted)


def test_the_servers_own_count_wins_over_the_decoded_body():
    """Content-Length — это длина сжатого тела, названная самим сервером."""
    resp = Resp(body=b"x" * 60_000, counted=0,
                headers={"Content-Length": "6500"})
    assert wire_bytes(resp) == 6_500


def test_the_stream_counter_is_used_when_no_length_was_declared():
    """Ответ кусками (chunked) заголовка не несёт, счётчик потока есть."""
    resp = Resp(body=b"x" * 60_000, counted=7_000)
    assert wire_bytes(resp) == 7_000


def test_a_stream_counter_stuck_at_zero_does_not_pass_for_a_measurement():
    """Ровно это и случилось живьём: счётчик вернул ноль, замер молча стал
    длиной разжатого тела, и обе графы в отчёте показали одно число."""
    resp = Resp(body=b"x" * 60_000, counted=0)
    assert wire_bytes(resp) == 60_000, "падаем в тело, но не притворяемся"


def test_a_stream_that_refuses_to_count_does_not_break_the_read():
    resp = Resp(body=b"x" * 900, counted=None)
    assert wire_bytes(resp) == 900


def test_a_response_without_a_raw_stream_falls_back_to_the_body():
    """Подставной ответ в тестах ничего из этого не реализует."""
    resp = Resp(body=b"x" * 120, raw=False)
    assert wire_bytes(resp) == 120


def test_a_junk_content_length_is_ignored_rather_than_raised():
    resp = Resp(body=b"x" * 400, counted=0, headers={"Content-Length": "какой-то"})
    assert wire_bytes(resp) == 400


def test_the_measurement_is_never_absent():
    """Лучше неточная цифра, чем пустая графа в отчёте о трафике."""
    assert wire_bytes(Resp(body=b"", counted=0)) == 0


@pytest.mark.parametrize("counted", [1, 12_345])
def test_any_nonzero_count_is_trusted(counted):
    resp = Resp(body=b"x" * 999_999, counted=counted)
    assert wire_bytes(resp) == counted
