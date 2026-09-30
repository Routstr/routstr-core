import pytest

from routstr.upstream.messages_dispatch import collapse_litellm_message


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("You have no credits remaining.", "You have no credits remaining."),
        (
            "litellm.MidStreamFallbackError: litellm.APIError: No credits. "
            "Original exception: MidStreamFallbackError: No credits. "
            "Original exception: APIError: litellm.APIError: No credits.",
            "No credits.",
        ),
        ("x" * 301, "x" * 299 + "…"),
    ],
)
def test_collapse_litellm_message(message: str, expected: str) -> None:
    assert collapse_litellm_message(message) == expected
