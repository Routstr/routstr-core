"""Bodies the proxy cannot parse as a JSON object are client errors, not 500s."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from routstr.core.main import app
from routstr.proxy import parse_request_body_json

BOUNDARY = "routstr-test"
MULTIPART_BODY = (
    (
        f"--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="model"\r\n\r\n'
        "qwen-edit-uncensored\r\n"
        f"--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="image"; filename="in.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode()
    + b"\x89PNG\r\n\x1a\n\x00\x00\xff\xfe"
    + f"\r\n--{BOUNDARY}--\r\n".encode()
)


def test_non_utf8_body_is_invalid_json() -> None:
    with pytest.raises(HTTPException) as exc_info:
        parse_request_body_json(b"\x89PNG\xff\xfe", "v1/images/edits")

    assert exc_info.value.status_code == 400


@pytest.mark.parametrize("body", [b"[]", b'"model"', b"42", b"null"])
def test_json_that_is_not_an_object_is_rejected(body: bytes) -> None:
    with pytest.raises(HTTPException) as exc_info:
        parse_request_body_json(body, "v1/chat/completions")

    assert exc_info.value.status_code == 400


def test_multipart_body_is_unsupported_media_type() -> None:
    with pytest.raises(HTTPException) as exc_info:
        parse_request_body_json(
            MULTIPART_BODY,
            "v1/images/edits",
            f"multipart/form-data; boundary={BOUNDARY}",
        )

    assert exc_info.value.status_code == 415


def test_multipart_image_edit_is_refused_with_cors_headers() -> None:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/v1/images/edits",
        content=MULTIPART_BODY,
        headers={
            "content-type": f"multipart/form-data; boundary={BOUNDARY}",
            "authorization": "Bearer sk-test",
            "origin": "https://chat.example",
        },
    )

    assert response.status_code == 415
    # A 500 from the outer error handler has no CORS headers; browsers then
    # report only "Failed to fetch".
    assert "access-control-allow-origin" in response.headers
