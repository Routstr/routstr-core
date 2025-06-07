import importlib
import os
from unittest.mock import patch

import pytest

import router.settings as settings


def test_require_env_returns_value():
    with patch.dict(os.environ, {"UPSTREAM_BASE_URL": "http://example.com", "RECEIVE_LN_ADDRESS": "lnaddr", "NSEC": "nsec"}, clear=True):
        importlib.reload(settings)
        assert settings.require_env("UPSTREAM_BASE_URL") == "http://example.com"


def test_missing_required_env_raises_error():
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError):
            importlib.reload(settings)
