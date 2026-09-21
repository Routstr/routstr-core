"""Unit tests for the pure row builders in routstr.upstream.certification.

Every builder here takes an already-fetched fact (a ``ProbeResult``, a
model, a cost datum) and turns it into a row — no network, no DB. The
tests therefore cover every verdict — ok, warn, fail — including the
failure modes that would be hard to provoke against a live upstream.
"""

from __future__ import annotations

from typing import Any

from routstr.upstream.certification import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_WARN,
    ProbeResult,
    _expected_token_msats,
    _reported_usd_cost,
    build_checklist,
    certification_row,
    cost_prompt_completion_row,
    endpoint_validity_row,
    heartbeat_row,
    models_payload_row,
    usage_capture_row,
)


def _probe(**kwargs: Any) -> ProbeResult:
    return ProbeResult(
        base_url=kwargs.get("base_url", "https://upstream.example/v1"),
        models_url=kwargs.get("models_url", "https://upstream.example/v1/models"),
        chat_url=kwargs.get("chat_url", "https://upstream.example/v1/chat/completions"),
        models_status=kwargs.get("models_status"),
        models_payload=kwargs.get("models_payload"),
        models_error=kwargs.get("models_error"),
        models_latency_ms=kwargs.get("models_latency_ms", 42.0),
        chat_status=kwargs.get("chat_status"),
        chat_payload=kwargs.get("chat_payload"),
        chat_error=kwargs.get("chat_error"),
        chat_latency_ms=kwargs.get("chat_latency_ms", 88.0),
    )


# --- endpoint_validity_row --------------------------------------------------


class TestEndpointValidity:
    def test_ok_for_https_url(self) -> None:
        row = endpoint_validity_row("https://api.example.com/v1")
        assert row["status"] == STATUS_OK
        assert row["evidence"]["scheme"] == "https"
        assert row["evidence"]["host"] == "api.example.com"

    def test_ok_for_http_url(self) -> None:
        row = endpoint_validity_row("http://localhost:8888/v1")
        assert row["status"] == STATUS_OK

    def test_fail_for_ftp_scheme(self) -> None:
        row = endpoint_validity_row("ftp://files.example.com")
        assert row["status"] == STATUS_FAIL
        assert "scheme" in row["detail"]

    def test_fail_for_empty_string(self) -> None:
        row = endpoint_validity_row("")
        assert row["status"] == STATUS_FAIL

    def test_fail_for_no_host(self) -> None:
        row = endpoint_validity_row("https://")
        assert row["status"] == STATUS_FAIL


# --- heartbeat_row ----------------------------------------------------------


class TestHeartbeat:
    def test_ok_on_2xx(self) -> None:
        row = heartbeat_row(_probe(models_status=200))
        assert row["status"] == STATUS_OK
        assert "200" in row["detail"]
        assert row["evidence"]["latency_ms"] == 42.0

    def test_ok_on_201(self) -> None:
        row = heartbeat_row(_probe(models_status=201))
        assert row["status"] == STATUS_OK

    def test_fail_on_404(self) -> None:
        row = heartbeat_row(_probe(models_status=404))
        assert row["status"] == STATUS_FAIL
        assert "404" in row["detail"]

    def test_fail_on_500(self) -> None:
        row = heartbeat_row(_probe(models_status=500))
        assert row["status"] == STATUS_FAIL

    def test_fail_on_transport_error(self) -> None:
        row = heartbeat_row(
            _probe(models_status=None, models_error="ConnectError: ...")
        )
        assert row["status"] == STATUS_FAIL
        assert "ConnectError" in row["detail"]


# --- models_payload_row -----------------------------------------------------


class TestModelsPayload:
    def test_ok_with_ids(self) -> None:
        row = models_payload_row(
            _probe(models_payload={"data": [{"id": "gpt-4o"}, {"id": "claude"}]})
        )
        assert row["status"] == STATUS_OK
        assert row["evidence"]["model_count"] == 2
        assert row["evidence"]["usable_ids"] == 2
        assert row["evidence"]["sample_ids"] == ["gpt-4o", "claude"]

    def test_fail_when_data_missing(self) -> None:
        row = models_payload_row(_probe(models_payload={"error": "not found"}))
        assert row["status"] == STATUS_FAIL
        assert "data" in row["detail"]

    def test_fail_when_data_not_list(self) -> None:
        row = models_payload_row(_probe(models_payload={"data": {"id": "oops"}}))
        assert row["status"] == STATUS_FAIL

    def test_fail_when_no_string_ids(self) -> None:
        row = models_payload_row(
            _probe(models_payload={"data": [{"name": "no-id-here"}]})
        )
        assert row["status"] == STATUS_FAIL
        assert row["evidence"]["model_count"] == 1
        assert row["evidence"]["usable_ids"] == 0

    def test_fail_when_payload_none(self) -> None:
        row = models_payload_row(
            _probe(models_payload=None, models_error="JSONDecodeError")
        )
        assert row["status"] == STATUS_FAIL

    def test_sample_ids_truncated_to_five(self) -> None:
        row = models_payload_row(
            _probe(models_payload={"data": [{"id": f"m{i}"} for i in range(10)]})
        )
        assert len(row["evidence"]["sample_ids"]) == 5
        assert row["evidence"]["model_count"] == 10


# --- usage_capture_row ------------------------------------------------------


class TestUsageCapture:
    def test_ok_with_tokens(self) -> None:
        row = usage_capture_row(
            _probe(
                chat_status=200,
                chat_payload={
                    "choices": [],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 1},
                },
            )
        )
        assert row["status"] == STATUS_OK
        assert row["evidence"]["input_tokens"] == 5
        assert row["evidence"]["output_tokens"] == 1

    def test_warn_when_no_usage_object(self) -> None:
        row = usage_capture_row(_probe(chat_status=200, chat_payload={"choices": []}))
        assert row["status"] == STATUS_WARN
        assert "usage" in row["detail"].lower()

    def test_warn_when_all_zero_tokens(self) -> None:
        row = usage_capture_row(
            _probe(
                chat_status=200,
                chat_payload={
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                },
            )
        )
        assert row["status"] == STATUS_WARN

    def test_fail_on_non_2xx(self) -> None:
        row = usage_capture_row(
            _probe(chat_status=401, chat_payload={"error": "bad key"})
        )
        assert row["status"] == STATUS_FAIL
        assert "401" in row["detail"]

    def test_fail_on_transport_error(self) -> None:
        row = usage_capture_row(_probe(chat_status=None, chat_error="TimeoutException"))
        assert row["status"] == STATUS_FAIL

    def test_fail_when_body_not_json(self) -> None:
        row = usage_capture_row(
            _probe(chat_status=200, chat_payload=None, chat_error="not json")
        )
        assert row["status"] == STATUS_FAIL

    def test_ok_with_anthropic_style_usage(self) -> None:
        """Anthropic reports input_tokens (not prompt_tokens) and caches
        additively, not as a grand total. normalize_usage handles this."""
        row = usage_capture_row(
            _probe(
                chat_status=200,
                chat_payload={
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "cache_read_input_tokens": 5,
                        "cache_creation_input_tokens": 3,
                    },
                },
            )
        )
        assert row["status"] == STATUS_OK


# --- _reported_usd_cost -----------------------------------------------------


class TestReportedUsdCost:
    def test_zero_when_no_usage(self) -> None:
        assert _reported_usd_cost({}) == 0.0
        assert _reported_usd_cost({"usage": None}) == 0.0

    def test_from_cost_details_total(self) -> None:
        payload = {"usage": {"cost_details": {"total_cost": 0.001}}}
        assert _reported_usd_cost(payload) == 0.001

    def test_from_total_cost(self) -> None:
        payload = {"usage": {"total_cost": 0.002}}
        assert _reported_usd_cost(payload) == 0.002

    def test_from_cost_field(self) -> None:
        payload = {"usage": {"cost": 0.003}}
        assert _reported_usd_cost(payload) == 0.003

    def test_zero_for_negative(self) -> None:
        payload = {"usage": {"cost": -1.0}}
        assert _reported_usd_cost(payload) == 0.0

    def test_zero_for_nan(self) -> None:
        payload = {"usage": {"cost": float("nan")}}
        assert _reported_usd_cost(payload) == 0.0

    def test_zero_for_non_numeric(self) -> None:
        payload = {"usage": {"cost": "free"}}
        assert _reported_usd_cost(payload) == 0.0


# --- _expected_token_msats --------------------------------------------------


class TestExpectedTokenMsats:
    def _pricing(self, **overrides: float) -> Any:
        from routstr.payment.models import Pricing

        pricing = Pricing(prompt=1.4e-7, completion=2.8e-7)
        return pricing.copy(update=overrides)

    def _usage(self, **kwargs: int) -> Any:
        from routstr.payment.usage import NormalizedUsage

        return NormalizedUsage(**kwargs)

    def test_basic_calculation(self) -> None:
        pricing = self._pricing()
        usage = self._usage(input_tokens=100, output_tokens=50)
        total, inp, outp = _expected_token_msats(pricing, usage)
        assert total > 0
        assert inp + outp == total  # folding invariant

    def test_zero_tokens_give_zero_total(self) -> None:
        pricing = self._pricing()
        usage = self._usage()
        total, inp, outp = _expected_token_msats(pricing, usage)
        assert total == 0
        assert inp == 0
        assert outp == 0

    def test_cache_tokens_included_in_total(self) -> None:
        pricing = self._pricing(input_cache_read=0.5e-7, input_cache_write=0.7e-7)
        usage = self._usage(
            input_tokens=10, output_tokens=5, cache_read_tokens=3, cache_write_tokens=2
        )
        total, _, _ = _expected_token_msats(pricing, usage)
        assert total > 0

    def test_input_plus_output_equals_total(self) -> None:
        """The folding invariant: visible_input = total - visible_output."""
        pricing = self._pricing(prompt=3.33e-7, completion=7.77e-7)
        usage = self._usage(input_tokens=77, output_tokens=33)
        total, inp, outp = _expected_token_msats(pricing, usage)
        assert inp + outp == total


# --- cost_prompt_completion_row ---------------------------------------------


class TestCostPromptCompletion:
    def _model(self, prompt: float = 1.4e-7, completion: float = 2.8e-7) -> Any:
        from routstr.payment.models import (
            Architecture,
            Model,
            Pricing,
            _update_model_sats_pricing,
        )

        model = Model(
            id="test-model",
            name="test-model",
            created=0,
            description="",
            context_length=8192,
            architecture=Architecture(
                modality="text",
                input_modalities=["text"],
                output_modalities=["text"],
                tokenizer="unknown",
                instruct_type=None,
            ),
            pricing=Pricing(prompt=prompt, completion=completion),
            sats_pricing=None,
            per_request_limits=None,
            top_provider=None,
            enabled=True,
            upstream_provider_id=None,
            canonical_slug=None,
        )
        return _update_model_sats_pricing(model, 0.0005)

    def _cost_data(self, total: int, inp: int, outp: int) -> Any:
        from routstr.payment.cost_calculation import CostData

        return CostData(
            base_msats=0,
            input_msats=inp,
            output_msats=outp,
            total_msats=total,
        )

    def test_ok_when_engine_matches_expected(self) -> None:
        model = self._model()
        usage_dict = {"prompt_tokens": 10, "completion_tokens": 5}
        probe = _probe(chat_status=200, chat_payload={"usage": usage_dict})

        from routstr.payment.usage import normalize_usage

        usage = normalize_usage(usage_dict)
        expected_total, expected_input, expected_output = _expected_token_msats(
            model.sats_pricing, usage
        )

        cost_data = self._cost_data(expected_total, expected_input, expected_output)
        row = cost_prompt_completion_row(
            model=model,
            probe=probe,
            cost_data=cost_data,
            provider_fee=1.0,
            sats_to_usd=0.0005,
        )
        assert row["status"] == STATUS_OK

    def test_fail_when_total_mismatches(self) -> None:
        model = self._model()
        probe = _probe(
            chat_status=200,
            chat_payload={"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        )
        cost_data = self._cost_data(total=999, inp=500, outp=499)
        row = cost_prompt_completion_row(
            model=model,
            probe=probe,
            cost_data=cost_data,
            provider_fee=1.0,
            sats_to_usd=0.0005,
        )
        assert row["status"] == STATUS_FAIL
        assert "total" in row["detail"]

    def test_fail_when_components_dont_sum(self) -> None:
        model = self._model()
        probe = _probe(
            chat_status=200,
            chat_payload={"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        )
        cost_data = self._cost_data(total=100, inp=60, outp=50)  # 60+50 != 100
        row = cost_prompt_completion_row(
            model=model,
            probe=probe,
            cost_data=cost_data,
            provider_fee=1.0,
            sats_to_usd=0.0005,
        )
        assert row["status"] == STATUS_FAIL
        assert "components" in row["detail"]

    def test_warn_when_no_usage(self) -> None:
        model = self._model()
        probe = _probe(chat_status=200, chat_payload={"choices": []})
        cost_data = self._cost_data(total=0, inp=0, outp=0)
        row = cost_prompt_completion_row(
            model=model,
            probe=probe,
            cost_data=cost_data,
            provider_fee=1.0,
            sats_to_usd=0.0005,
        )
        assert row["status"] == STATUS_WARN

    def test_warn_when_no_sats_pricing(self) -> None:
        from routstr.payment.models import Architecture, Model, Pricing

        model = Model(
            id="no-sats",
            name="no-sats",
            created=0,
            description="",
            context_length=8192,
            architecture=Architecture(
                modality="text",
                input_modalities=["text"],
                output_modalities=["text"],
                tokenizer="unknown",
                instruct_type=None,
            ),
            pricing=Pricing(prompt=1e-7, completion=2e-7),
            sats_pricing=None,
            per_request_limits=None,
            top_provider=None,
            enabled=True,
            upstream_provider_id=None,
            canonical_slug=None,
        )
        probe = _probe(
            chat_status=200,
            chat_payload={"usage": {"prompt_tokens": 5, "completion_tokens": 1}},
        )
        cost_data = self._cost_data(total=0, inp=0, outp=0)
        row = cost_prompt_completion_row(
            model=model,
            probe=probe,
            cost_data=cost_data,
            provider_fee=1.0,
            sats_to_usd=0.0005,
        )
        assert row["status"] == STATUS_WARN

    def test_warn_when_pricing_unknown(self) -> None:
        model = self._model()
        probe = _probe(
            chat_status=200,
            chat_payload={"usage": {"prompt_tokens": 5, "completion_tokens": 1}},
        )
        cost_data = self._cost_data(total=0, inp=0, outp=0)
        row = cost_prompt_completion_row(
            model=model,
            probe=probe,
            cost_data=cost_data,
            provider_fee=1.0,
            sats_to_usd=0.0005,
            pricing_known=False,
        )
        assert row["status"] == STATUS_WARN

    def test_fail_on_cost_data_error(self) -> None:
        from routstr.payment.cost_calculation import CostDataError

        model = self._model()
        probe = _probe(
            chat_status=200,
            chat_payload={"usage": {"prompt_tokens": 5, "completion_tokens": 1}},
        )
        cost_data = CostDataError(message="pricing not found", code="pricing_error")
        row = cost_prompt_completion_row(
            model=model,
            probe=probe,
            cost_data=cost_data,
            provider_fee=1.0,
            sats_to_usd=0.0005,
        )
        assert row["status"] == STATUS_FAIL
        assert "pricing not found" in row["detail"]

    def test_ok_with_usd_reported_cost(self) -> None:
        model = self._model()
        sats_to_usd = 0.0005
        provider_fee = 1.05
        reported_usd = 0.0001
        expected_total = int(
            __import__("math").ceil(reported_usd * provider_fee / sats_to_usd * 1000)
        )
        probe = _probe(
            chat_status=200,
            chat_payload={
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "cost_details": {"total_cost": reported_usd},
                },
            },
        )
        cost_data = self._cost_data(expected_total, 0, expected_total)
        row = cost_prompt_completion_row(
            model=model,
            probe=probe,
            cost_data=cost_data,
            provider_fee=provider_fee,
            sats_to_usd=sats_to_usd,
        )
        assert row["status"] == STATUS_OK
        assert row["evidence"]["basis"] == "upstream_reported_usd"


# --- build_checklist --------------------------------------------------------


class TestBuildChecklist:
    def _row(self, row_id: str, status: str) -> dict[str, Any]:
        return certification_row(row_id, status, "title", "detail")

    def test_all_ok_makes_all_goals_ok(self) -> None:
        rows = [
            self._row("endpoint.reachable", STATUS_OK),
            self._row("usage.capture", STATUS_OK),
            self._row("cost.prompt_completion", STATUS_OK),
            self._row("pricing.served_matches_configured", STATUS_OK),
            self._row("pricing.enabled_models_served", STATUS_OK),
        ]
        checklist = build_checklist(rows)
        assert len(checklist) == 4
        for item in checklist:
            assert item["status"] == STATUS_OK
            assert item["tick"] == "☑️"

    def test_one_fail_makes_its_goal_fail(self) -> None:
        rows = [
            self._row("endpoint.reachable", STATUS_FAIL),
            self._row("usage.capture", STATUS_OK),
            self._row("cost.prompt_completion", STATUS_OK),
            self._row("pricing.served_matches_configured", STATUS_OK),
            self._row("pricing.enabled_models_served", STATUS_OK),
        ]
        checklist = build_checklist(rows)
        goals = {item["goal"]: item["status"] for item in checklist}
        assert goals["heartbeat"] == STATUS_FAIL
        assert goals["usage_data"] == STATUS_OK

    def test_warn_makes_goal_warn(self) -> None:
        rows = [
            self._row("endpoint.reachable", STATUS_OK),
            self._row("usage.capture", STATUS_WARN),
            self._row("cost.prompt_completion", STATUS_OK),
            self._row("pricing.served_matches_configured", STATUS_OK),
            self._row("pricing.enabled_models_served", STATUS_OK),
        ]
        checklist = build_checklist(rows)
        goals = {item["goal"]: item["status"] for item in checklist}
        assert goals["usage_data"] == STATUS_WARN

    def test_missing_row_makes_goal_warn(self) -> None:
        rows = [self._row("endpoint.reachable", STATUS_OK)]
        checklist = build_checklist(rows)
        goals = {item["goal"]: item["status"] for item in checklist}
        assert goals["heartbeat"] == STATUS_OK
        assert goals["usage_data"] == STATUS_WARN  # row absent → warn

    def test_pricing_goal_combines_two_rows(self) -> None:
        """pricing_v1_models depends on TWO rows; one fail → goal fail."""
        rows = [
            self._row("endpoint.reachable", STATUS_OK),
            self._row("usage.capture", STATUS_OK),
            self._row("cost.prompt_completion", STATUS_OK),
            self._row("pricing.served_matches_configured", STATUS_OK),
            self._row("pricing.enabled_models_served", STATUS_FAIL),
        ]
        checklist = build_checklist(rows)
        goals = {item["goal"]: item["status"] for item in checklist}
        assert goals["pricing_v1_models"] == STATUS_FAIL

    def test_pricing_goal_ok_only_when_both_ok(self) -> None:
        rows = [
            self._row("endpoint.reachable", STATUS_OK),
            self._row("usage.capture", STATUS_OK),
            self._row("cost.prompt_completion", STATUS_OK),
            self._row("pricing.served_matches_configured", STATUS_OK),
            self._row("pricing.enabled_models_served", STATUS_OK),
        ]
        checklist = build_checklist(rows)
        goals = {item["goal"]: item["status"] for item in checklist}
        assert goals["pricing_v1_models"] == STATUS_OK

    def test_fail_takes_precedence_over_warn(self) -> None:
        rows = [
            self._row("endpoint.reachable", STATUS_FAIL),
            self._row("usage.capture", STATUS_WARN),
            self._row("cost.prompt_completion", STATUS_OK),
            self._row("pricing.served_matches_configured", STATUS_OK),
            self._row("pricing.enabled_models_served", STATUS_OK),
        ]
        checklist = build_checklist(rows)
        goals = {item["goal"]: item["status"] for item in checklist}
        assert goals["heartbeat"] == STATUS_FAIL
        assert goals["usage_data"] == STATUS_WARN
