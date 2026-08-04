from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from research_agent.budget import (
    BillingBasis,
    BudgetPolicy,
    BudgetTreatment,
    ServiceKind,
    UsageLedger,
)


def _policy() -> BudgetPolicy:
    return BudgetPolicy.from_yaml(Path("config/budget-policy.yaml"))


def test_automatic_envelope_supports_bounded_128k_oneshots() -> None:
    envelope = _policy().automatic_envelope

    assert envelope.max_cost_microusd_per_call == 250_000
    assert envelope.max_cost_microusd_per_run == 2_000_000
    assert envelope.max_cost_microusd_per_day == 5_000_000
    assert envelope.max_cost_microusd_per_month == 25_000_000
    assert envelope.max_calls_per_run == 10
    assert envelope.max_input_tokens_per_call == 32_000
    assert envelope.max_output_tokens_per_call == 131_072


def test_unknown_accounting_fails_closed(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite")

    with pytest.raises(ValueError, match="accounting is unknown"):
        ledger.reserve_model(
            policy=_policy(),
            provider="unknown",
            model="unknown",
            run_id="run:test",
            request_key="request",
            input_tokens=100,
            output_tokens=100,
            human_approved=False,
        )


def test_counted_cost_is_reserved_and_settled(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite")
    policy = _policy()
    reservation = ledger.reserve_model(
        policy=policy,
        provider="openai",
        model="gpt-5.2",
        run_id="run:test",
        request_key="request",
        input_tokens=10_000,
        output_tokens=1_000,
        human_approved=False,
        now=datetime(2026, 8, 2, tzinfo=UTC),
    )

    assert reservation.budget_treatment is BudgetTreatment.COUNTED
    assert reservation.cost_microusd_reserved == 31_500
    settlement = ledger.settle_model(
        reservation,
        policy=policy,
        input_tokens=5_000,
        output_tokens=500,
    )
    assert settlement.status == "settled"
    assert settlement.cost_microusd_actual == 15_750


def test_missing_provider_usage_charges_full_reservation(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite")
    policy = _policy()
    reservation = ledger.reserve_model(
        policy=policy,
        provider="openai",
        model="gpt-5.2",
        run_id="run:test",
        request_key="request",
        input_tokens=10_000,
        output_tokens=1_000,
        human_approved=False,
    )

    settlement = ledger.settle_model(
        reservation,
        policy=policy,
        input_tokens=None,
        output_tokens=None,
    )
    assert settlement.status == "settled_estimate"
    assert settlement.cost_microusd_actual is None


def test_excluded_subscription_still_consumes_call_cap(tmp_path: Path) -> None:
    raw = _policy().model_dump(mode="json")
    raw["accounts"][0].update(
        {
            "billing_basis": BillingBasis.SUBSCRIPTION_INCLUDED,
            "budget_treatment": BudgetTreatment.EXCLUDED_FROM_COST,
            "accounting_note": "Operator-confirmed subscription allowance",
            "input_cost_microusd_per_million_tokens": None,
            "output_cost_microusd_per_million_tokens": None,
        }
    )
    policy = BudgetPolicy.model_validate(raw)
    ledger = UsageLedger(tmp_path / "usage.sqlite")

    for index in range(10):
        reservation = ledger.reserve_model(
            policy=policy,
            provider="openai",
            model="gpt-5.2",
            run_id="run:subscription",
            request_key=f"request:{index}",
            input_tokens=100,
            output_tokens=100,
            human_approved=False,
        )
        assert reservation.cost_microusd_reserved == 0

    with pytest.raises(ValueError, match="call count"):
        ledger.reserve_model(
            policy=policy,
            provider="openai",
            model="gpt-5.2",
            run_id="run:subscription",
            request_key="request:overflow",
            input_tokens=100,
            output_tokens=100,
            human_approved=False,
        )


def test_metered_account_cannot_be_excluded_from_cost() -> None:
    raw = _policy().model_dump(mode="json")
    raw["accounts"][0]["budget_treatment"] = BudgetTreatment.EXCLUDED_FROM_COST

    with pytest.raises(ValidationError, match="metered accounts cannot be excluded"):
        BudgetPolicy.model_validate(raw)


def test_enterprise_search_account_can_be_excluded_without_token_pricing() -> None:
    raw = _policy().model_dump(mode="json")
    raw["accounts"].append(
        {
            "service": ServiceKind.SEARCH,
            "provider": "enterprise-search",
            "model": None,
            "billing_basis": BillingBasis.ENTERPRISE_COMMIT,
            "budget_treatment": BudgetTreatment.EXCLUDED_FROM_COST,
            "accounting_note": "Covered by enterprise search commitment",
            "input_cost_microusd_per_million_tokens": None,
            "output_cost_microusd_per_million_tokens": None,
            "unit_cost_microusd": None,
        }
    )

    policy = BudgetPolicy.model_validate(raw)
    account = policy.account(ServiceKind.SEARCH, "enterprise-search", None)
    assert account.budget_treatment is BudgetTreatment.EXCLUDED_FROM_COST


def test_token_caps_apply_even_to_human_approved_calls(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite")

    with pytest.raises(ValueError, match="input token"):
        ledger.reserve_model(
            policy=_policy(),
            provider="openai",
            model="gpt-5.2",
            run_id="run:test",
            request_key="request",
            input_tokens=32_001,
            output_tokens=100,
            human_approved=True,
        )


def test_daily_cost_limit_is_shared_across_runs(tmp_path: Path) -> None:
    raw = _policy().model_dump(mode="json")
    raw["automatic_envelope"]["max_cost_microusd_per_day"] = 50_000
    policy = BudgetPolicy.model_validate(raw)
    ledger = UsageLedger(tmp_path / "usage.sqlite")
    timestamp = datetime(2026, 8, 2, tzinfo=UTC)
    ledger.reserve_model(
        policy=policy,
        provider="openai",
        model="gpt-5.2",
        run_id="run:one",
        request_key="one",
        input_tokens=10_000,
        output_tokens=1_000,
        human_approved=False,
        now=timestamp,
    )

    with pytest.raises(ValueError, match="daily limit"):
        ledger.reserve_model(
            policy=policy,
            provider="openai",
            model="gpt-5.2",
            run_id="run:two",
            request_key="two",
            input_tokens=10_000,
            output_tokens=1_000,
            human_approved=False,
            now=timestamp,
        )


def test_concurrent_reservations_cannot_overspend_call_limit(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite")
    policy = _policy()

    def reserve(index: int) -> bool:
        try:
            ledger.reserve_model(
                policy=policy,
                provider="zai",
                model="glm-5.2",
                run_id="run:concurrent",
                request_key=f"request:{index}",
                input_tokens=100,
                output_tokens=100,
                human_approved=False,
            )
        except ValueError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=11) as pool:
        results = tuple(pool.map(reserve, range(11)))

    assert sum(results) == 10


def test_search_usage_is_reserved_and_settled_from_provider_cost(
    tmp_path: Path,
) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite")
    reservation = ledger.reserve_search(
        policy=_policy(),
        provider="openalex",
        run_id="run:openalex",
        request_key="page:0",
        human_approved=False,
        max_calls_per_run=10,
        max_cost_microusd_per_day=1_000_000,
        now=datetime(2026, 8, 2, tzinfo=UTC),
    )

    assert reservation.service is ServiceKind.SEARCH
    assert reservation.cost_microusd_reserved == 1_000
    settlement = ledger.settle_search(reservation, cost_microusd=1_000)
    assert settlement.status == "settled"
    assert settlement.cost_microusd_actual == 1_000


def test_provider_search_call_and_daily_limits_fail_closed(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite")
    policy = _policy()
    timestamp = datetime(2026, 8, 2, tzinfo=UTC)
    ledger.reserve_search(
        policy=policy,
        provider="openalex",
        run_id="run:one",
        request_key="page:0",
        human_approved=False,
        max_calls_per_run=1,
        max_cost_microusd_per_day=1_000,
        now=timestamp,
    )

    with pytest.raises(ValueError, match="call count"):
        ledger.reserve_search(
            policy=policy,
            provider="openalex",
            run_id="run:one",
            request_key="page:1",
            human_approved=False,
            max_calls_per_run=1,
            max_cost_microusd_per_day=1_000,
            now=timestamp,
        )
    with pytest.raises(ValueError, match="provider daily"):
        ledger.reserve_search(
            policy=policy,
            provider="openalex",
            run_id="run:two",
            request_key="page:0",
            human_approved=False,
            max_calls_per_run=1,
            max_cost_microusd_per_day=1_000,
            now=timestamp,
        )


def test_search_cost_overrun_is_recorded_for_output_rejection(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite")
    reservation = ledger.reserve_search(
        policy=_policy(),
        provider="openalex",
        run_id="run:openalex",
        request_key="page:0",
        human_approved=False,
        max_calls_per_run=10,
        max_cost_microusd_per_day=1_000_000,
    )

    settlement = ledger.settle_search(reservation, cost_microusd=1_001)

    assert settlement.status == "overrun"
