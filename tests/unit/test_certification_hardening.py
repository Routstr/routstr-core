"""Regression tests for defects found by independent adversarial testing.

Each test here pins a specific failure mode that was found and fixed while
building the certification harness. They are grouped by the defect they
guard, not by the function under test, because the point of each one is the
bug it prevents from coming back.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from routstr.payment.usage import parse_token_count
from routstr.upstream.certification import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_WARN,
    ProbeResult,
    _as_price,
    _expected_token_msats,
    _reported_usd_cost,
    certification_row,
    endpoint_validity_row,
    models_payload_row,
    safe_row,
    usage_capture_row,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _probe(**kwargs: Any) -> ProbeResult:
    return ProbeResult(
        base_url=kwargs.get("base_url", "https://upstream.example/v1"),
        models_url=kwargs.get("models_url", "https://upstream.example/v1/models"),
        chat_url=kwargs.get("chat_url", "https://upstream.example/v1/chat/completions"),
        models_status=kwargs.get("models_status"),
        models_payload=kwargs.get("models_payload"),
        models_error=kwargs.get("models_error"),
        chat_status=kwargs.get("chat_status"),
        chat_payload=kwargs.get("chat_payload"),
        chat_error=kwargs.get("chat_error"),
    )


# Regression: a non-finite token count crashed the billing path.
# ``json.loads`` accepts the bare ``Infinity``/``NaN`` literals, so an
# upstream can put them on the wire; ``int(inf)`` raised OverflowError and
# ``int(nan)`` raised ValueError inside ``parse_token_count``.


class TestNonFiniteTokenCounts:
    @pytest.mark.parametrize(
        "value",
        [
            float("inf"),
            float("-inf"),
            float("nan"),
            1e999,
            "Infinity",
            "NaN",
            "-Infinity",
            "1e999",
        ],
    )
    def test_parse_token_count_rejects_non_finite(self, value: Any) -> None:
        assert parse_token_count(value) == 0

    def test_parse_token_count_still_parses_ordinary_values(self) -> None:
        assert parse_token_count(42) == 42
        assert parse_token_count("42") == 42
        assert parse_token_count(42.9) == 42
        assert parse_token_count("42.9") == 42
        assert parse_token_count(True) == 0
        assert parse_token_count(-5) == 0
        assert parse_token_count("not a number") == 0
        assert parse_token_count(None) == 0

    def test_usage_row_survives_infinite_tokens(self) -> None:
        row = usage_capture_row(
            _probe(
                chat_status=200,
                chat_payload={
                    "usage": {
                        "prompt_tokens": float("inf"),
                        "completion_tokens": float("nan"),
                    }
                },
            )
        )
        # Both counts collapse to 0, which is the "nothing to bill on" case.
        assert row["status"] == STATUS_WARN

    def test_usage_row_survives_infinite_tokens_in_a_string(self) -> None:
        row = usage_capture_row(
            _probe(
                chat_status=200,
                chat_payload={"usage": {"prompt_tokens": "Infinity"}},
            )
        )
        assert row["status"] == STATUS_WARN


# Regression: ``certification_row`` stored non-dict evidence verbatim, so the
# row contract ("evidence is always a dict") held only by caller discipline.


class TestEvidenceContract:
    @pytest.mark.parametrize("evidence", [None, [1, 2], "text", 42, (1, 2)])
    def test_evidence_is_always_a_dict(self, evidence: Any) -> None:
        row = certification_row("x", STATUS_OK, "t", "d", evidence)
        assert isinstance(row["evidence"], dict)

    def test_evidence_dict_is_passed_through(self) -> None:
        row = certification_row("x", STATUS_OK, "t", "d", {"a": 1})
        assert row["evidence"] == {"a": 1}


# Regression: ``http://:8080/v1`` was certified as a valid endpoint because
# ``netloc`` is truthy for a hostless authority.


class TestEndpointValidity:
    @pytest.mark.parametrize(
        "url",
        ["http://:8080/v1", "https://:443", "http://", "https://"],
    )
    def test_hostless_authority_is_rejected(self, url: str) -> None:
        row = endpoint_validity_row(url)
        assert row["status"] == STATUS_FAIL, url

    @pytest.mark.parametrize(
        "url",
        [
            "https://api.example.com/v1",
            "http://localhost:8888/v1",
            "http://127.0.0.1:8080",
            "https://[::1]:8080/v1",
        ],
    )
    def test_real_hosts_are_accepted(self, url: str) -> None:
        row = endpoint_validity_row(url)
        assert row["status"] == STATUS_OK, url


# Regression: the payload builders called ``.get()`` on whatever they were
# given, so a wrong-typed body raised AttributeError instead of producing a
# verdict.


class TestPayloadTypeGuards:
    @pytest.mark.parametrize("payload", [[1, 2], "text", 42, ("a",)])
    def test_models_payload_row_handles_non_dict(self, payload: Any) -> None:
        row = models_payload_row(_probe(models_payload=payload))
        assert row["status"] == STATUS_FAIL

    @pytest.mark.parametrize("payload", [[1, 2], "text", 42, ("a",)])
    def test_usage_row_handles_non_dict_chat_payload(self, payload: Any) -> None:
        row = usage_capture_row(_probe(chat_status=200, chat_payload=payload))
        assert row["status"] == STATUS_FAIL

    def test_models_payload_with_non_dict_entries(self) -> None:
        row = models_payload_row(
            _probe(models_payload={"data": [None, 42, "string", {}]})
        )
        assert row["status"] == STATUS_FAIL
        assert row["evidence"]["model_count"] == 4
        assert row["evidence"]["usable_ids"] == 0


# Regression: an empty-string id was counted as "usable" by the payload row but
# rejected by the CLI's discovery path — the two disagreed on one response.


class TestModelIdAgreement:
    def test_empty_string_id_is_not_usable(self) -> None:
        row = models_payload_row(_probe(models_payload={"data": [{"id": ""}]}))
        assert row["status"] == STATUS_FAIL
        assert row["evidence"]["usable_ids"] == 0

    def test_one_usable_id_among_empties_is_ok(self) -> None:
        row = models_payload_row(
            _probe(models_payload={"data": [{"id": ""}, {"id": "real-model"}]})
        )
        assert row["status"] == STATUS_OK
        assert row["evidence"]["usable_ids"] == 1


# Regression: the independent cost re-derivation disagreed with the engine on
# coercion (numeric strings, booleans), manufacturing false failures.


class TestReportedCostCoercionParity:
    def test_numeric_string_cost_is_read(self) -> None:
        assert _reported_usd_cost({"usage": {"cost": "0.001"}}) == pytest.approx(0.001)

    def test_boolean_cost_is_rejected(self) -> None:
        # ``True`` is an int in Python and would read as $1.00 per token.
        assert _reported_usd_cost({"usage": {"cost": True}}) == 0.0

    def test_non_finite_cost_is_rejected(self) -> None:
        assert _reported_usd_cost({"usage": {"cost": float("inf")}}) == 0.0
        assert _reported_usd_cost({"usage": {"cost": float("nan")}}) == 0.0

    def test_negative_cost_is_rejected(self) -> None:
        assert _reported_usd_cost({"usage": {"cost": -1.0}}) == 0.0

    def test_cost_details_wins_over_cost(self) -> None:
        payload = {"usage": {"cost": 0.5, "cost_details": {"total_cost": 0.001}}}
        assert _reported_usd_cost(payload) == pytest.approx(0.001)


# Regression: ``_expected_token_msats`` ran ``math.ceil`` on a non-finite sum,
# raising an opaque error instead of a describable one.


class TestNonFinitePricing:
    class _SatsPricing:
        def __init__(self, **kwargs: Any) -> None:
            self.prompt = kwargs.get("prompt", 1.0)
            self.completion = kwargs.get("completion", 1.0)
            self.input_cache_read = kwargs.get("input_cache_read", 0.0)
            self.input_cache_write = kwargs.get("input_cache_write", 0.0)

    class _Usage:
        input_tokens = 10
        output_tokens = 5
        cache_read_tokens = 0
        cache_write_tokens = 0

    def test_infinite_rate_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="non-finite"):
            _expected_token_msats(self._SatsPricing(prompt=float("inf")), self._Usage())

    def test_nan_rate_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="non-finite"):
            _expected_token_msats(
                self._SatsPricing(completion=float("nan")), self._Usage()
            )

    def test_finite_rates_still_work(self) -> None:
        total, inp, outp = _expected_token_msats(
            self._SatsPricing(prompt=1.4e-7, completion=2.8e-7), self._Usage()
        )
        assert total == inp + outp


# Regression: a row builder raising escaped as a 500 from the admin endpoint.


class TestSafeRow:
    def test_a_raising_builder_becomes_a_fail_row(self) -> None:
        def boom() -> dict[str, Any]:
            raise RuntimeError("hostile payload")

        row = safe_row("x.row", "Title", boom)
        assert row["status"] == STATUS_FAIL
        assert "RuntimeError" in row["detail"]
        assert isinstance(row["evidence"], dict)

    def test_a_working_builder_passes_through(self) -> None:
        row = safe_row(
            "x.row", "Title", lambda: certification_row("x.row", STATUS_OK, "T", "D")
        )
        assert row["status"] == STATUS_OK


# Regression: explicit ``--prompt-price`` bypassed validation, so a negative
# rate could be fed into the cost engine.


class TestExplicitPriceValidation:
    def test_negative_price_is_rejected(self) -> None:
        assert _as_price(-1.0) is None

    def test_non_finite_price_is_rejected(self) -> None:
        assert _as_price(float("inf")) is None
        assert _as_price(float("nan")) is None

    def test_boolean_price_is_rejected(self) -> None:
        assert _as_price(True) is None

    def test_zero_is_a_valid_price(self) -> None:
        # Free is a real price.
        assert _as_price(0.0) == 0.0

    def test_numeric_string_is_accepted(self) -> None:
        assert _as_price("1e-7") == pytest.approx(1e-7)


# Regression: the standalone CLI was dead on arrival — ``sats_usd_price()``
# raises in a fresh process because the module global is only populated by
# the app's lifespan task. These run the CLI as a subprocess so the fresh
# process is the thing under test.


def _run_cli(*args: str, timeout: float = 90.0) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.setdefault("ROUTSTR_SECRET_KEY", "l_Tkp-7xmjcQ-IFhr6qhILrU8HPRbEmYMrfSbo_5srU=")
    return subprocess.run(
        [sys.executable, "-m", "routstr.upstream.certification", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.mark.slow
class TestCliFreshProcess:
    def test_cli_emits_a_report_instead_of_dying(self, tmp_path: Path) -> None:
        """The regression: this used to raise 'SATS price not initialized'."""
        out = tmp_path / "report.json"
        result = _run_cli(
            "--url",
            "http://localhost:1/v1",
            "--timeout",
            "1",
            "--json-out",
            str(out),
        )
        assert "SATS price not initialized" not in result.stderr
        assert out.exists(), result.stderr

    def test_json_out_is_strictly_parseable(self, tmp_path: Path) -> None:
        out = tmp_path / "report.json"
        _run_cli(
            "--url", "http://localhost:1/v1", "--timeout", "1", "--json-out", str(out)
        )
        document = json.loads(out.read_text(encoding="utf-8"))
        assert isinstance(document, list) and document
        assert len(document[0]["rows"]) == 5
        assert len(document[0]["checklist"]) == 4

    def test_dead_host_exits_non_zero(self) -> None:
        result = _run_cli("--url", "http://localhost:1/v1", "--timeout", "1")
        assert result.returncode == 1

    def test_negative_explicit_price_is_rejected(self, tmp_path: Path) -> None:
        out = tmp_path / "report.json"
        _run_cli(
            "--url",
            "http://localhost:1/v1",
            "--timeout",
            "1",
            "--model",
            "m",
            "--prompt-price",
            "-1",
            "--json-out",
            str(out),
        )
        document = json.loads(out.read_text(encoding="utf-8"))
        assert document[0]["target"]["prompt_price_usd"] is None

    def test_checklist_uses_the_documented_ticks(self) -> None:
        result = _run_cli("--url", "http://localhost:1/v1", "--timeout", "1")
        assert "❌" in result.stdout
        assert "Heartbeat" in result.stdout
