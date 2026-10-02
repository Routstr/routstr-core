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
    rename_unsupported_param,
    strip_unsupported_param,
)

OPENAI_MAX_TOKENS_ERROR = (
    "Unsupported parameter: 'max_tokens' is not supported with this model. "
    "Use 'max_completion_tokens' instead."
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
        assert (
            correct_request(b"[1, 2, 3]", "`temperature` is deprecated", set()) is None
        )

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

    def test_never_strips_spend_shaping_params(self) -> None:
        # Stripping an output cap after the reservation was priced would let
        # the retry run uncapped and overcharge — the corrector must decline so
        # the upstream error propagates instead.
        for param in (
            "max_tokens",
            "max_completion_tokens",
            "max_output_tokens",
            "max_tokens_to_sample",
            "n",
            "best_of",
        ):
            body = {"model": "m", param: 4, "messages": []}
            assert (
                strip_unsupported_param(body, f"`{param}` is not supported") is None
            ), param
            # And through the full pipeline entry point.
            assert (
                correct_request(
                    json.dumps(body).encode(),
                    f"`{param}` is not supported",
                    set(),
                )
                is None
            ), param

    def test_spend_shaping_guard_is_case_insensitive(self) -> None:
        body = {"model": "m", "Max_Tokens": 4}
        assert strip_unsupported_param(body, "`Max_Tokens` is deprecated") is None


class TestRenameUnsupportedParam:
    def test_renames_max_tokens_for_openai_reasoning_models(self) -> None:
        body = {"model": "gpt-5.6-sol", "max_tokens": 256, "messages": []}
        result = rename_unsupported_param(body, OPENAI_MAX_TOKENS_ERROR)
        assert result is not None
        new_body, label = result
        assert label == "max_tokens->max_completion_tokens"
        assert new_body == {
            "model": "gpt-5.6-sol",
            "max_completion_tokens": 256,
            "messages": [],
        }

    def test_preserves_key_order(self) -> None:
        body = {"model": "m", "max_tokens": 1, "stream": True}
        result = rename_unsupported_param(body, OPENAI_MAX_TOKENS_ERROR)
        assert result is not None
        assert list(result[0]) == ["model", "max_completion_tokens", "stream"]

    def test_does_not_mutate_input(self) -> None:
        body = {"model": "m", "max_tokens": 8}
        assert rename_unsupported_param(body, OPENAI_MAX_TOKENS_ERROR) is not None
        assert body == {"model": "m", "max_tokens": 8}

    def test_renames_between_any_output_caps(self) -> None:
        caps = (
            "max_tokens",
            "max_completion_tokens",
            "max_output_tokens",
            "max_tokens_to_sample",
        )
        for param in caps:
            for replacement in caps:
                if param == replacement:
                    continue
                message = f"`{param}` is deprecated. Use `{replacement}` instead."
                result = rename_unsupported_param({param: 7}, message)
                assert result == ({replacement: 7}, f"{param}->{replacement}"), (
                    param,
                    replacement,
                )

    def test_renames_non_spend_param(self) -> None:
        message = "'functions' is deprecated. Use 'tools' instead."
        result = rename_unsupported_param({"functions": [{"name": "f"}]}, message)
        assert result == ({"tools": [{"name": "f"}]}, "functions->tools")

    def test_matches_across_quote_styles_case_and_newlines(self) -> None:
        for message in (
            'Unsupported parameter: "max_tokens" is not supported.\nUse '
            '"max_completion_tokens" instead.',
            "`max_tokens` IS UNSUPPORTED here; please USE `max_completion_tokens`"
            " INSTEAD",
            "'max_tokens' is no longer supported, use 'max_completion_tokens' instead",
        ):
            result = rename_unsupported_param({"max_tokens": 3}, message)
            assert result is not None, message
            assert result[0] == {"max_completion_tokens": 3}

    def test_refuses_renames_that_change_the_spend_bound(self) -> None:
        for param, replacement in (
            ("max_tokens", "n"),
            ("n", "best_of"),
            ("best_of", "n"),
            ("temperature", "max_tokens"),
            ("max_tokens", "temperature"),
            ("n", "max_tokens"),
        ):
            message = f"'{param}' is not supported. Use '{replacement}' instead."
            assert rename_unsupported_param({param: 2}, message) is None, (
                param,
                replacement,
            )

    def test_spend_guard_is_case_insensitive(self) -> None:
        ok = "'Max_Tokens' is not supported. Use 'MAX_COMPLETION_TOKENS' instead."
        assert rename_unsupported_param({"Max_Tokens": 4}, ok) == (
            {"MAX_COMPLETION_TOKENS": 4},
            "Max_Tokens->MAX_COMPLETION_TOKENS",
        )
        bad = "'Max_Tokens' is not supported. Use 'N' instead."
        assert rename_unsupported_param({"Max_Tokens": 4}, bad) is None

    def test_declines_when_replacement_already_present(self) -> None:
        body = {"max_tokens": 4, "max_completion_tokens": 8}
        assert rename_unsupported_param(body, OPENAI_MAX_TOKENS_ERROR) is None

    def test_declines_when_param_absent(self) -> None:
        assert rename_unsupported_param({"model": "m"}, OPENAI_MAX_TOKENS_ERROR) is None

    def test_declines_self_rename(self) -> None:
        message = "'max_tokens' is deprecated. Use 'max_tokens' instead."
        assert rename_unsupported_param({"max_tokens": 1}, message) is None

    def test_declines_unquoted_or_missing_replacement(self) -> None:
        for message in (
            "`gpt-3` is deprecated, use gpt-4 instead",
            "'max_tokens' is not supported, use max_completion_tokens instead",
            "'max_tokens' is not supported with this model.",
            "Use 'max_completion_tokens' instead.",
        ):
            assert rename_unsupported_param({"max_tokens": 1}, message) is None, message

    def test_declines_nested_only_param(self) -> None:
        body = {"reasoning": {"max_tokens": 5}}
        assert rename_unsupported_param(body, OPENAI_MAX_TOKENS_ERROR) is None


class TestCorrectRequestRename:
    def test_openai_max_tokens_error_is_renamed_not_refused(self) -> None:
        body = _body(model="gpt-5.6-sol", max_tokens=512, messages=[])
        result = correct_request(body, OPENAI_MAX_TOKENS_ERROR, set())
        assert isinstance(result, Correction)
        assert result.label == "max_tokens->max_completion_tokens"
        decoded = json.loads(result.body)
        assert "max_tokens" not in decoded
        assert decoded["max_completion_tokens"] == 512

    def test_rename_wins_over_strip_for_non_spend_param(self) -> None:
        body = _body(model="m", functions=[1])
        result = correct_request(
            body, "'functions' is deprecated. Use 'tools' instead.", set()
        )
        assert result is not None
        assert json.loads(result.body) == {"model": "m", "tools": [1]}

    def test_unsafe_rename_of_cap_still_surfaces_error(self) -> None:
        body = _body(model="m", max_tokens=5)
        assert (
            correct_request(
                body, "'max_tokens' is not supported. Use 'n' instead.", set()
            )
            is None
        )

    def test_applied_rename_does_not_repeat_or_strip_cap(self) -> None:
        body = _body(model="m", max_tokens=5)
        applied = {"max_tokens->max_completion_tokens"}
        assert correct_request(body, OPENAI_MAX_TOKENS_ERROR, applied) is None

    def test_rename_ping_pong_terminates(self) -> None:
        """An upstream that flip-flops between names cannot loop forever."""
        forward = OPENAI_MAX_TOKENS_ERROR
        backward = "'max_completion_tokens' is not supported. Use 'max_tokens' instead."
        body = _body(model="m", max_tokens=5)
        applied: set[str] = set()
        for attempt in range(10):
            message = forward if attempt % 2 == 0 else backward
            result = correct_request(body, message, applied)
            if result is None:
                break
            body, applied = result.body, applied | {result.label}
        else:
            raise AssertionError("correction loop did not terminate")
        assert applied == {
            "max_tokens->max_completion_tokens",
            "max_completion_tokens->max_tokens",
        }
        assert json.loads(body) == {"model": "m", "max_tokens": 5}

    def test_buffered_openai_error_response_is_renamed(self) -> None:
        resp = Response(
            content=json.dumps(
                {
                    "error": {
                        "message": OPENAI_MAX_TOKENS_ERROR,
                        "type": "invalid_request_error",
                        "param": "max_tokens",
                        "code": "unsupported_parameter",
                    }
                }
            ).encode(),
            status_code=400,
        )
        body = _body(model="gpt-5.6-sol", max_tokens=64, stream=True)
        result = correct_request(body, extract_error_message(resp), set())
        assert result is not None
        assert json.loads(result.body) == {
            "model": "gpt-5.6-sol",
            "max_completion_tokens": 64,
            "stream": True,
        }


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
