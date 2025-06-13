import dotenv

dotenv.load_dotenv()

# ---------------------------------------------------------------------------
# Compatibility patch for httpx>=0.28 used together with Starlette <0.28.
# Starlette's TestClient expects ``httpx.Client`` to accept an ``app`` argument,
# which was removed in newer httpx releases.  For the test suite we provide a
# minimal shim so ``app`` can still be passed without errors.
try:  # pragma: no cover - only executed when running tests
    import httpx

    _original_init = httpx.Client.__init__  # type: ignore[attr-defined]

    def _patched_init(self, *args, app=None, **kwargs):
        if app is not None:
            kwargs.setdefault("transport", httpx.ASGITransport(app=app))
            kwargs.pop("app", None)
        _original_init(self, *args, **kwargs)

    httpx.Client.__init__ = _patched_init  # type: ignore[assignment]
except Exception:
    pass

from .main import app as fastapi_app  # noqa


__all__ = ["fastapi_app"]
