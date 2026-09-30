"""Diagnostic comparison ONLY: remove LoggingMiddleware from unchanged image app."""
from routstr.core.main import app
from routstr.core.middleware import LoggingMiddleware
app.user_middleware = [m for m in app.user_middleware if m.cls is not LoggingMiddleware]
