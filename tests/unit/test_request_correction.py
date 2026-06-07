"""Unit tests for the reactive request-correction layer.

Covers the recovery path that lets a request survive a 400 where the upstream
names a single unsupported request param (e.g. newer Anthropic models
deprecating ``temperature``): the param is stripped from the JSON body and the
same upstream is retried, provider-agnostically, keyed off the error text.
"""

from __future__ import annotations

import json

from fastapi.responses import Response

from routstr.upstream.request_correction import (
    Correction,
    correct_request,
    extract_error_message,
    strip_unsupported_param,
)


def _body(**kwargs: object) -> bytes:
    return json.dumps(kwargs).encode()


class TestCorrectRequest:
    def test_strips_deprecated_temperature(self) -> None:
        body = _body(model="claude-opus-4-8", temperature=1, messages=[])
        result = correct_request(
            body, "`temperature` is deprecated for this model.", set()
        )
        assert isinstance(result, Correction)
        assert result.label == "temperature"
        decoded = json.loads(result.body)
        assert "temperature" not in decoded
        assert decoded["model"] == "claude-opus-4-8"

    def test_strips_not_supported_param(self) -> None:
        body = _body(model="m", top_p=0.9, messages=[])
        result = correct_request(body, "Parameter 'top_p' is not supported", set())
        assert result is not None
        assert result.label == "top_p"
        assert "top_p" not in json.loads(result.body)

    def test_returns_none_when_label_already_applied(self) -> None:
        body = _body(model="m", temperature=1)
        assert (
            correct_request(body, "`temperature` is deprecated", {"temperature"})
            is None
        )

    def test_returns_none_when_param_absent_from_body(self) -> None:
        body = _body(model="m", messages=[])
        assert correct_request(body, "`temperature` is deprecated", set()) is None

    def test_returns_none_when_message_does_not_match(self) -> None:
        body = _body(model="m", temperature=1)
        assert correct_request(body, "Insufficient balance", set()) is None

    def test_returns_none_on_empty_inputs(self) -> None:
        assert correct_request(b"", "`temperature` is deprecated", set()) is None
        assert correct_request(_body(temperature=1), "", set()) is None

    def test_returns_none_on_non_object_body(self) -> None:
        assert correct_request(b"[1, 2, 3]", "`temperature` is deprecated", set()) is None

    def test_deprecated_model_name_is_not_stripped_as_param(self) -> None:
        """A 'model is deprecated' error must not strip an unrelated body field.

        The regex matches the ``<token> is deprecated`` wording, but the
        ``param not in body`` guard means a deprecated *model* name (not a
        request param) yields no correction rather than a false strip.
        """
        body = _body(model="gpt-3", temperature=1, messages=[])
        assert correct_request(body, "`gpt-3` is deprecated, use gpt-4", set()) is None

    def test_streaming_400_buffered_error_is_correctable(self) -> None:
        """Streaming 400s funnel through a buffered JSON Response, so the same
        correction path applies as for non-streaming requests."""
        # Mirrors forward_upstream_error_response's buffered JSON envelope.
        resp = Response(
            content=json.dumps(
                {"error": {"message": "`temperature` is deprecated for this model"}}
            ).encode(),
            status_code=400,
        )
        body = _body(model="claude-opus-4-8", temperature=1, messages=[])
        result = correct_request(body, extract_error_message(resp), set())
        assert isinstance(result, Correction)
        assert result.label == "temperature"
        assert "temperature" not in json.loads(result.body)


class TestStripUnsupportedParam:
    def test_does_not_mutate_input(self) -> None:
        body = {"model": "m", "temperature": 1}
        result = strip_unsupported_param(body, "`temperature` is deprecated")
        assert result is not None
        new_body, param = result
        assert param == "temperature"
        assert "temperature" not in new_body
        # original untouched (immutability)
        assert body == {"model": "m", "temperature": 1}

    def test_declines_when_no_match(self) -> None:
        assert strip_unsupported_param({"temperature": 1}, "nope") is None


class TestExtractErrorMessage:
    def test_extracts_nested_error_message(self) -> None:
        resp = Response(
            content=json.dumps(
                {"error": {"message": "`temperature` is deprecated", "type": "x"}}
            ).encode(),
            status_code=400,
        )
        assert extract_error_message(resp) == "`temperature` is deprecated"

    def test_extracts_string_error(self) -> None:
        resp = Response(
            content=json.dumps({"error": "bad request"}).encode(), status_code=400
        )
        assert extract_error_message(resp) == "bad request"

    def test_extracts_top_level_message(self) -> None:
        resp = Response(
            content=json.dumps({"message": "nope"}).encode(), status_code=400
        )
        assert extract_error_message(resp) == "nope"

    def test_empty_body_returns_empty_string(self) -> None:
        assert extract_error_message(Response(status_code=400)) == ""

    def test_non_json_body_returns_preview(self) -> None:
        resp = Response(content=b"plain text error", status_code=400)
        assert extract_error_message(resp) == "plain text error"


class TestEndToEndChaining:
    def test_two_distinct_params_corrected_sequentially(self) -> None:
        """Simulates the proxy loop: each 400 fixes one param, set guards reuse."""
        body = _body(model="m", temperature=1, top_p=0.5, messages=[])
        applied: set[str] = set()

        first = correct_request(body, "`temperature` is deprecated", applied)
        assert first is not None
        body, applied = first.body, applied | {first.label}

        second = correct_request(body, "`top_p` is not supported", applied)
        assert second is not None
        body, applied = second.body, applied | {second.label}

        decoded = json.loads(body)
        assert "temperature" not in decoded and "top_p" not in decoded
        assert applied == {"temperature", "top_p"}
