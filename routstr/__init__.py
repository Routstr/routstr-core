"""Routstr application package."""

import os

# cashu's settings loader reads the nearest .env with ``override=True`` during
# import. A library must not replace the node's already-configured process
# environment (notably CASHU_MINTS and networking settings), so contain that
# side effect while importing the application graph.
_environment_before_import = dict(os.environ)
try:
    from .core.main import app as fastapi_app
finally:
    os.environ.clear()
    os.environ.update(_environment_before_import)
    del _environment_before_import

__all__ = ["fastapi_app"]
