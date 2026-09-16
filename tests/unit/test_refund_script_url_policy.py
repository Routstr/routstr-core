"""The refund helper puts a cashu token and a bearer key on the wire, so it
must not speak cleartext to a remote host."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "refund_token_to_lightning.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("refund_token_to_lightning", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "url",
    [
        "https://node.example.com",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
    ],
)
def test_accepts_https_and_loopback_http(url: str) -> None:
    assert _load().check_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://node.example.com",
        "http://192.168.1.10:8000",
        "ftp://node.example.com",
        "node.example.com",
    ],
)
def test_rejects_remote_cleartext_and_other_schemes(url: str) -> None:
    with pytest.raises(SystemExit):
        _load().check_url(url)
