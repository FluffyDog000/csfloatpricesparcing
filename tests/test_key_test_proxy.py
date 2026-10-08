import importlib.util
import os

_PATH = os.path.join(os.path.dirname(__file__), "..", "tools", "key_test.py")
_spec = importlib.util.spec_from_file_location("key_test", _PATH)
key_test = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(key_test)


def test_seller_formats_become_urls():
    want = "http://u:p@1.2.3.4:6095"
    assert key_test.normalized("1.2.3.4:6095:u:p") == want
    assert key_test.normalized("u:p:1.2.3.4:6095") == want
    assert key_test.normalized("u:p@1.2.3.4:6095") == want
    assert key_test.normalized(" " + want + " ") == want


def test_numeric_password_keeps_host_first():
    assert key_test.normalized("1.2.3.4:6095:u:12345") == "http://u:12345@1.2.3.4:6095"


def test_scheme_and_empty_untouched():
    assert key_test.normalized("socks5://u:p@h.example:1080") == "socks5://u:p@h.example:1080"
    assert key_test.normalized("") == ""
