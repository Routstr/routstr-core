import pytest

from routstr.upstream.perplexity import PerplexityUpstreamProvider


@pytest.mark.parametrize(
    ("request_headers", "expected_headers"),
    [
        (
            {},
            {
                "Authorization": "Bearer test-key",
                "accept-encoding": "gzip, deflate, br, identity",
                "X-Pplx-Integration": "routstr",
            },
        ),
        (
            {"x-pplx-integration": "custom"},
            {
                "x-pplx-integration": "custom",
                "Authorization": "Bearer test-key",
                "accept-encoding": "gzip, deflate, br, identity",
            },
        ),
    ],
)
def test_prepare_headers(
    request_headers: dict[str, str], expected_headers: dict[str, str]
) -> None:
    provider = PerplexityUpstreamProvider(api_key="test-key")

    assert provider.prepare_headers(request_headers) == expected_headers
