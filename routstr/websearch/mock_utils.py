from datetime import datetime
from ..core.logging import get_logger
logger = get_logger(__name__)
from typing import Any
import json
from pathlib import Path

async def _save_api_response(
        response_data: dict[str, Any], query: str, provider: str
    ) -> None:
        """Save live API response to timestamped JSON file for debugging and testing.

        Args:
            response_data: The API response dictionary to save
            query: The search query (used in filename generation)
            provider: Provider name (used in filename generation)

        Note:
            Creates files in api_responses/ directory with timestamp and sanitized query
        """

        # Create responses directory if it doesn't exist
        responses_dir = Path(__file__).parent / "api_responses"
        responses_dir.mkdir(exist_ok=True)

        # Generate filename with timestamp and sanitized query
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_query = (
            "".join(c for c in query if c.isalnum() or c in (" ", "-", "_"))
            .rstrip()
            .replace(" ", "_")
        )[:60]
        if not safe_query:
            safe_query = "query"
        filename = f"{provider}_{safe_query}_{timestamp}.json"
        file_path = responses_dir / filename

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(response_data, f, indent=2, ensure_ascii=False)
            logger.info(f"API response saved to {file_path}", extra={"path": str(file_path)})
        except Exception as e:
            logger.error(
                f"Failed to save API response: {e}",
                extra={"path": str(file_path), "error": str(e)},
            )

async def _load_mock_data(file_name: str) -> dict[str, Any]:
    """Load mock API response data from local JSON file for testing purposes.

    Args:
        file_name: Name of the JSON file containing mock response data

    Returns:
        Dictionary containing mock API response data

    """
    logger.debug("Using mock data from file.")
    from pathlib import Path

    script_dir = Path(__file__).parent
    json_file_path = script_dir / f"api_responses/{file_name}"
    with open(json_file_path, "r", encoding="utf-8") as file:
        return json.load(file)