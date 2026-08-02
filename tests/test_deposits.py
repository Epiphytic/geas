import hashlib
from pathlib import Path

import pytest
from coincurve import PrivateKey, PublicKeyXOnly
from pydantic import ValidationError

from research_agent.deposits import (
    AcquisitionMethod,
    DepositManager,
    DepositOverrides,
    DepositPolicy,
    InformationStatus,
    ModelRoute,
    NostrClaim,
    NostrEvent,
    PermissionStatus,
    RedistributionStatus,
    UsagePermissionOverrides,
    nostr_event_id,
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
    assert policy.defaults.redistribution_status is RedistributionStatus.UNKNOWN
    assert all(
        status is PermissionStatus.UNKNOWN
        for status in policy.defaults.usage_permissions.model_dump().values()
    )


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
    assert result.deposit.rights_basis_status is InformationStatus.DECLARED
    assert result.deposit.authorship_status is InformationStatus.UNKNOWN
    assert result.deposit.license_status is InformationStatus.UNKNOWN
    assert result.deposit.usage_conditions_status is InformationStatus.UNKNOWN
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
            usage_permissions=UsagePermissionOverrides(
                archive=PermissionStatus.ALLOWED,
                redistribute_original=PermissionStatus.ALLOWED,
            ),
            retention_policy="retain_until_project_end",
        ),
    )

    assert result.deposit.scope_label == "operator_chosen_ungated"
    assert not result.deposit.index_content
    assert not result.deposit.include_in_ontology
    assert result.deposit.model_route is ModelRoute.EXTERNAL_ALLOWED
    assert result.deposit.redistribution_status is RedistributionStatus.GRANTED
    assert result.deposit.usage_permissions.archive is PermissionStatus.ALLOWED
    assert result.deposit.usage_permissions.quote is PermissionStatus.UNKNOWN
    assert "allowed_users" not in type(result.deposit).model_fields


def test_known_authors_license_and_usage_conditions_are_recorded(tmp_path: Path) -> None:
    source = tmp_path / "paper.txt"
    source.write_text("research")
    store = ImmutableStore(tmp_path / "data")
    store.initialize()

    result = DepositManager(
        store=store,
        policy=DepositPolicy.from_yaml(Path("config/deposit-policy.yaml")),
    ).deposit_file(
        source,
        deposited_by="user:researcher",
        authors=("Ada Example", "Lin Example"),
        license="CC-BY-4.0",
        usage_conditions=("Attribution required",),
    )

    assert result.deposit.authors == ("Ada Example", "Lin Example")
    assert result.deposit.authorship_status is InformationStatus.DECLARED
    assert result.deposit.license == "CC-BY-4.0"
    assert result.deposit.license_status is InformationStatus.DECLARED
    assert result.deposit.usage_conditions == ("Attribution required",)
    assert result.deposit.usage_conditions_status is InformationStatus.DECLARED


def _signed_nip94_event(content: bytes) -> NostrEvent:
    private_key = PrivateKey(b"\x01" * 32)
    public_key = PublicKeyXOnly.from_secret(private_key.secret).format().hex()
    values = {
        "id": "0" * 64,
        "pubkey": public_key,
        "created_at": 1_700_000_000,
        "kind": 1063,
        "tags": [["x", hashlib.sha256(content).hexdigest()]],
        "content": "Ownership claim for deposited fixture",
        "sig": "0" * 128,
    }
    unsigned = NostrEvent.model_validate(values)
    event_id = nostr_event_id(unsigned)
    values["id"] = event_id
    values["sig"] = private_key.sign_schnorr(bytes.fromhex(event_id), aux_randomness=None).hex()
    return NostrEvent.model_validate(values)


def test_verified_nostr_event_is_preserved_as_file_bound_ownership_evidence(
    tmp_path: Path,
) -> None:
    content = b"signed research data"
    source = tmp_path / "signed.txt"
    source.write_bytes(content)
    event = _signed_nip94_event(content)
    store = ImmutableStore(tmp_path / "data")
    store.initialize()

    result = DepositManager(
        store=store,
        policy=DepositPolicy.from_yaml(Path("config/deposit-policy.yaml")),
    ).deposit_file(
        source,
        deposited_by="user:researcher",
        nostr_evidence=((event, NostrClaim.OWNERSHIP),),
    )

    evidence = result.deposit.nostr_signature_evidence[0]
    assert evidence.claimed_relation is NostrClaim.OWNERSHIP
    assert evidence.bound_content_sha256 == hashlib.sha256(content).hexdigest()
    assert evidence.event_id_verified
    assert evidence.signature_verified
    assert evidence.content_binding_verified


def test_nostr_evidence_must_bind_the_exact_file_before_archival(tmp_path: Path) -> None:
    source = tmp_path / "different.txt"
    source.write_bytes(b"different content")
    event = _signed_nip94_event(b"signed content")
    store = ImmutableStore(tmp_path / "data")
    store.initialize()
    manager = DepositManager(
        store=store,
        policy=DepositPolicy.from_yaml(Path("config/deposit-policy.yaml")),
    )

    with pytest.raises(ValueError, match="does not bind"):
        manager.deposit_file(
            source,
            deposited_by="user:researcher",
            nostr_evidence=((event, NostrClaim.OWNERSHIP),),
        )

    assert not tuple(store.blob_root.rglob("*"))
    assert not tuple(store.record_root.rglob("*.json"))


def test_invalid_nostr_signature_fails_before_archival(tmp_path: Path) -> None:
    content = b"signed content"
    source = tmp_path / "signed.txt"
    source.write_bytes(content)
    valid_event = _signed_nip94_event(content)
    invalid_event = NostrEvent.model_validate(
        {**valid_event.model_dump(mode="json"), "sig": "0" * 128}
    )
    store = ImmutableStore(tmp_path / "data")
    store.initialize()
    manager = DepositManager(
        store=store,
        policy=DepositPolicy.from_yaml(Path("config/deposit-policy.yaml")),
    )

    with pytest.raises(ValueError, match="signature is invalid"):
        manager.deposit_file(
            source,
            deposited_by="user:researcher",
            nostr_evidence=((invalid_event, NostrClaim.OWNERSHIP),),
        )

    assert not tuple(store.blob_root.rglob("*"))
    assert not tuple(store.record_root.rglob("*.json"))


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
