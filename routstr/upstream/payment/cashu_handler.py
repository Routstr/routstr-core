"""X-Cashu payment handling utilities."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request
from fastapi.responses import Response

from ...core import get_logger
from ...payment.helpers import create_error_response
from ...wallet import recieve_token

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


class CashuTokenError(Exception):
    """Exception raised for X-Cashu token processing errors."""

    def __init__(self, error_type: str, message: str, status_code: int = 400):
        self.error_type = error_type
        self.message = message
        self.status_code = status_code
        super().__init__(message)


async def validate_and_redeem_token(x_cashu_token: str, request: Request) -> tuple[int, str, str | None]:
    """Validate and redeem X-Cashu token.

    Args:
        x_cashu_token: The X-Cashu token to validate and redeem
        request: Original FastAPI request (for error responses)

    Returns:
        Tuple of (amount, unit, mint_url)

    Raises:
        CashuTokenError: If token validation or redemption fails
    """
    logger.info(
        "Processing X-Cashu token redemption",
        extra={
            "token_preview": x_cashu_token[:20] + "..."
            if len(x_cashu_token) > 20
            else x_cashu_token,
        },
    )

    try:
        amount, unit, mint = await recieve_token(x_cashu_token)
        logger.info(
            "X-Cashu token redeemed successfully",
            extra={"amount": amount, "unit": unit, "mint": mint},
        )
        return amount, unit, mint
    except Exception as e:
        error_message = str(e)
        logger.error(
            "X-Cashu token redemption failed",
            extra={
                "error": error_message,
                "error_type": type(e).__name__,
            },
        )

        # Determine specific error type
        if "already spent" in error_message.lower():
            raise CashuTokenError(
                "token_already_spent",
                "The provided CASHU token has already been spent",
                400
            )
        elif "invalid token" in error_message.lower():
            raise CashuTokenError(
                "invalid_token",
                "The provided CASHU token is invalid",
                400
            )
        elif "mint error" in error_message.lower():
            raise CashuTokenError(
                "mint_error",
                f"CASHU mint error: {error_message}",
                422
            )
        else:
            raise CashuTokenError(
                "cashu_error",
                f"CASHU token processing failed: {error_message}",
                400
            )


def create_cashu_error_response(
    error: CashuTokenError,
    request: Request,
    x_cashu_token: str
) -> Response:
    """Create error response for X-Cashu token errors.

    Args:
        error: The CashuTokenError that occurred
        request: Original FastAPI request
        x_cashu_token: The token that caused the error

    Returns:
        Error response with appropriate status code and message
    """
    return create_error_response(
        error.error_type,
        error.message,
        error.status_code,
        request=request,
        token=x_cashu_token,
    )