from pathlib import Path

import pytest
from pydantic import ValidationError

from research_agent.deposits import (
    AcquisitionMethod,
    DepositManager,
    DepositOverrides,
    DepositPolicy,
    ModelRoute,
    RedistributionStatus,
)
from research_agent.store import ImmutableStore


def test_checked_in_defaults_are_workspace_ungated_and_advisory() -> None:
    policy = DepositPolicy.from_yaml(Path("config/deposit-policy.yaml"))

    assert policy.authorization_boundary == "deployment"
    assert policy.per_record_access_control is False
    assert policy.labels_are_advisory is True
    assert policy.defaults.scope_label == "workspace_ungated"
    assert policy.defaults.index_content
    assert policy.defaults.include_in_ontology


def test_deposit_records_provenance_and_applies_defaults(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF fixture")
    store = ImmutableStore(tmp_path / "data")
    store.initialize()
    result = DepositManager(
        store=store,
        policy=DepositPolicy.from_yaml(Path("config/deposit-policy.yaml")),
    ).deposit_file(
        source,
        deposited_by="user:researcher",
        acquisition_method=AcquisitionMethod.BROWSER_SAVE,
        original_locator="https://publisher.example/paper",
        rights_basis="user-provided licensed copy",
        provenance_note="Saved through the user's authenticated browser.",
    )

    assert result.source.connector_id == "connector:user-deposit"
    assert result.deposit.source_version == result.source.id
    assert result.deposit.deposited_by == "user:researcher"
    assert result.deposit.scope_label == "workspace_ungated"
    assert result.deposit.access_enforcement == "deployment_boundary_only"
    assert result.deposit.rights_basis == "user-provided licensed copy"
    record = store.record_path("deposit", result.record_hash)
    assert record.is_file()


def test_user_can_override_every_handling_default_without_creating_acl(
    tmp_path: Path,
) -> None:
    source = tmp_path / "internal.txt"
    source.write_text("internal research")
    store = ImmutableStore(tmp_path / "data")
    store.initialize()
    result = DepositManager(
        store=store,
        policy=DepositPolicy.from_yaml(Path("config/deposit-policy.yaml")),
    ).deposit_file(
        source,
        deposited_by="user:operator",
        overrides=DepositOverrides(
            scope_label="operator_chosen_ungated",
            index_content=False,
            include_in_ontology=False,
            model_route=ModelRoute.EXTERNAL_ALLOWED,
            redistribution_status=RedistributionStatus.GRANTED,
            retention_policy="retain_until_project_end",
        ),
    )

    assert result.deposit.scope_label == "operator_chosen_ungated"
    assert not result.deposit.index_content
    assert not result.deposit.include_in_ontology
    assert result.deposit.model_route is ModelRoute.EXTERNAL_ALLOWED
    assert result.deposit.redistribution_status is RedistributionStatus.GRANTED
    assert "allowed_users" not in type(result.deposit).model_fields


def test_policy_cannot_silently_enable_unimplemented_record_acls() -> None:
    policy = DepositPolicy.from_yaml(Path("config/deposit-policy.yaml"))

    with pytest.raises(ValidationError):
        DepositPolicy.model_validate(
            {
                **policy.model_dump(mode="json"),
                "per_record_access_control": True,
            }
        )


def test_missing_depositor_fails_before_content_is_archived(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("content")
    store = ImmutableStore(tmp_path / "data")
    store.initialize()
    manager = DepositManager(
        store=store,
        policy=DepositPolicy.from_yaml(Path("config/deposit-policy.yaml")),
    )

    with pytest.raises(ValueError, match="deposited_by is required"):
        manager.deposit_file(source, deposited_by=" ")

    assert not tuple(store.blob_root.rglob("*"))
    assert not tuple(store.record_root.rglob("*.json"))
