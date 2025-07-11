import dotenv

dotenv.load_dotenv()

from .main import app as fastapi_app  # noqa

__all__ = ["fastapi_app"]
