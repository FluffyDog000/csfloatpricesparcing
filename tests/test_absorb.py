"""Размер опроса на проводе — единственное, за что платит прокси.

Взять его можно только до того, как тело прочитано, и только не распаковывая:
CSFloat отвечает кусками (chunked), а счётчик urllib3 такие ответы не считает
вовсе — он остаётся на нуле. Именно так отчёт, задуманный как замена
`len(resp.content)`, начал печатать ровно её, дважды и под разными подписями.

Тело при этом должно вернуться в ответ целым: весь код ниже по течению —
проверка на заглушку Cloudflare, разбор JSON, сообщения об отказах — читает
его как обычно и о подмене не знает.
"""
import gzip
import json
import socket
import threading
import zlib

import pytest
import requests

from src.csfloat_client import absorb, decompress

PLAIN = json.dumps([{"id": i, "pad": "повторяющийся текст " * 20}
                    for i in range(40)]).encode()


def chunked_server(body: bytes, encoding: str | None, chunk: int = 1024):
    """Отдаёт тело кусками, как это делает CSFloat. Возвращает порт."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)

    def serve():
        conn, _ = sock.accept()
        try:
            conn.recv(65536)
            head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            if encoding:
                head += b"Content-Encoding: " + encoding.encode() + b"\r\n"
            conn.sendall(head + b"Transfer-Encoding: chunked\r\n\r\n")
            for i in range(0, len(body), chunk):
                part = body[i:i + chunk]
                conn.sendall(b"%x\r\n" % len(part) + part + b"\r\n")
            conn.sendall(b"0\r\n\r\n")
        finally:
            conn.close()
            sock.close()

    threading.Thread(target=serve, daemon=True).start()
    return sock.getsockname()[1]


def fetch(body, encoding):
    port = chunked_server(body, encoding)
    resp = requests.get(f"http://127.0.0.1:{port}/", timeout=10, stream=True)
    return resp, absorb(resp)


def test_a_chunked_gzip_answer_is_measured_compressed():
    packed = gzip.compress(PLAIN, 6)
    resp, measured = fetch(packed, "gzip")
    assert measured == len(packed)
    assert measured < len(PLAIN) / 5, "сжатие обязано быть видно в замере"


def test_the_body_comes_back_whole_and_decoded():
    """Всё ниже по течению читает тело как всегда."""
    resp, _ = fetch(gzip.compress(PLAIN, 6), "gzip")
    assert resp.content == PLAIN
    assert len(resp.json()) == 40
    assert resp.text.startswith("[{")


def test_brotli_is_undone_like_gzip():
    brotli = pytest.importorskip("brotli")
    packed = brotli.compress(PLAIN, quality=5)
    resp, measured = fetch(packed, "br")
    assert measured == len(packed)
    assert resp.content == PLAIN


def test_an_uncompressed_chunked_answer_measures_its_own_length():
    resp, measured = fetch(PLAIN, None)
    assert measured == len(PLAIN)
    assert resp.content == PLAIN


def test_a_header_naming_a_codec_the_body_does_not_use_costs_nothing():
    """Так делает прокси, распаковавший ответ и не снявший заголовок.

    Распаковка не удалась, тело кладётся как пришло — и оказывается обычным
    JSON, так что опрос доходит целым. Решать, что он потерян, не этому
    помощнику."""
    resp, measured = fetch(PLAIN, "br")
    assert measured == len(PLAIN)
    assert len(resp.json()) == 40, "опрос не должен теряться из-за заголовка"


def test_a_body_that_is_neither_compressed_nor_json_reaches_the_parser():
    """Заглушка Cloudflare приходит с кодом 200 — её проверяет код выше."""
    resp, measured = fetch(b"<html>Just a moment...</html>", "gzip")
    assert measured == len(b"<html>Just a moment...</html>")
    assert resp.text.startswith("<html>")
    with pytest.raises(ValueError):
        resp.json()


def test_a_stand_in_response_without_a_stream_still_reports_something():
    """Подставной ответ в тестах потока не имеет — замер не должен падать."""

    class Fake:
        headers = {"Content-Length": "4242"}
        content = b"{}"

    assert absorb(Fake()) == 4242


def test_deflate_in_both_shapes():
    """С zlib-обёрткой и без неё — встречаются оба."""
    assert decompress(zlib.compress(PLAIN), "deflate") == PLAIN
    packer = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    bare = packer.compress(PLAIN) + packer.flush()
    assert decompress(bare, "deflate") == PLAIN


def test_an_unknown_codec_is_handed_back_untouched():
    assert decompress(b"abc", "хитрый-кодек") == b"abc"
    assert decompress(b"abc", "") == b"abc"
