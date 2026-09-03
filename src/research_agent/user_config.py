from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

import yaml
from pydantic import Field, PrivateAttr, ValidationError, field_validator, model_validator

from research_agent.agent_skills import BuiltinSkillReceipt, install_builtin_geas_skill
from research_agent.bootstrap_models import (
    BootstrapConfigMutationReceipt,
    BootstrapGrantMutationReceipt,
    BootstrapGrantOwnershipReceipt,
)
from research_agent.capabilities import (
    Capability,
    CapabilityGrant,
    CapabilityResources,
    CapabilitySubject,
)
from research_agent.models import StrictModel, canonical_json
from research_agent.ontology_config import OntologyBuildDefaults
from research_agent.ontology_subscriptions import (
    NormalizedProfile,
    OntologyFreshnessConfig,
    OntologySubscription,
)
from research_agent.ontology_trust import InstalledOntologySnapshot, TrustRule
from research_agent.paths import geas_config_home
from research_agent.removal_journal import validate_removal_journal_namespace

if os.name == "nt":
    import msvcrt
else:
    import fcntl

DEFAULT_ONTOLOGY_REPOSITORY = "https://github.com/liamhelmer-bel/ontologies.git"
DEFAULT_CONFIG_FILENAMES = (
    "providers.toml",
    "source-policy.yaml",
    "research-policy.yaml",
    "truth-policy.yaml",
    "deposit-policy.yaml",
    "model-policy.yaml",
    "budget-policy.yaml",
    "workload-policy.yaml",
    "query-vocabulary.yaml",
)
_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_CONFIG_LOCKS_GUARD = threading.Lock()
_CONFIG_LOCKS: dict[str, threading.RLock] = {}
_CONFIG_LOCK_DEPTH = threading.local()


def _compatibility_grant(rule: TrustRule) -> CapabilityGrant:
    """Expose legacy repository trust as read-only, non-delegable authority."""
    return CapabilityGrant(
        decision=rule.decision,
        subject=CapabilitySubject(
            repository=rule.repository,
            refs=rule.refs,
            paths=(
                "*"
                if rule.paths == "*"
                else tuple(path.as_posix() for path in rule.paths)
            ),
            bundle_sha256=rule.bundle_sha256,
        ),
        capabilities=(Capability.REPOSITORY_READ,),
        delegable_capabilities=(),
        resources=CapabilityResources(),
        max_delegation_depth=0,
        expires_at=None,
        created_at=rule.created_at,
        created_via=rule.created_via,
    )


class ManagedDefaultFile(StrictModel):
    installed_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ManagedDefaultsState(StrictModel):
    version: Literal[1] = 1
    files: dict[str, ManagedDefaultFile] = Field(default_factory=dict)


class _BootstrapGrantJournal(StrictModel):
    """Private write-ahead record for one exact grant mutation."""

    version: Literal[1] = 1
    phase: Literal["prepared", "applied", "completed"]
    operation_key: str = Field(
        pattern=(
            r"^repository-bootstrap-(?:operation|update-operation|removal-operation):"
            r"sha256:[0-9a-f]{64}$"
        )
    )
    profile_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    bootstrap_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    action: Literal["record", "replace", "remove"]
    owner_operation_key: str = Field(
        pattern=(
            r"^repository-bootstrap-(?:operation|update-operation|removal-operation):"
            r"sha256:[0-9a-f]{64}$"
        )
    )
    old_grant_id: str | None = Field(
        default=None, pattern=r"^capability-grant:sha256:[0-9a-f]{64}$"
    )
    new_grant_id: str | None = Field(
        default=None, pattern=r"^capability-grant:sha256:[0-9a-f]{64}$"
    )
    before_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt: BootstrapGrantMutationReceipt | None = None

    @model_validator(mode="after")
    def receipt_matches_phase(self) -> _BootstrapGrantJournal:
        if (self.phase == "completed") != (self.receipt is not None):
            raise ValueError("grant journal completion and receipt must agree")
        if self.receipt is not None and (
            self.receipt.operation_key != self.operation_key
            or self.receipt.profile_name != self.profile_name
            or self.receipt.bootstrap_name != self.bootstrap_name
            or self.receipt.action != self.action
            or self.receipt.old_grant_id != self.old_grant_id
            or self.receipt.new_grant_id != self.new_grant_id
        ):
            raise ValueError("grant journal receipt does not match its intent")
        return self


class DefaultConfigReceipt(StrictModel):
    root: str
    installed: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    preserved: tuple[str, ...] = ()
    review_candidates: tuple[str, ...] = ()


class SecretSource(StrictModel):
    path: Path
    format: Literal["dotenv", "yaml", "json"] = "dotenv"

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: Path) -> Path:
        if value.is_absolute() or ".." in value.parts:
            raise ValueError("secret source paths must be config-relative")
        return value


class OntologyGitConfig(StrictModel):
    url: str = Field(min_length=1)
    branch: str = Field(default="main", pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
    remote: str = Field(default="origin", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    pull_before_update: bool = False
    push_on_update: bool = False

    @property
    def active_ref(self) -> str:
        return f"refs/heads/{self.branch}"

    @field_validator("url")
    @classmethod
    def url_has_no_embedded_credentials(cls, value: str) -> str:
        if any(character in value for character in ("\n", "\r", "\x00")):
            raise ValueError("ontology Git URL contains control characters")
        if "://" in value and "@" in value.partition("://")[2].partition("/")[0]:
            raise ValueError("ontology Git URLs cannot embed credentials")
        if value.startswith(("file:", "/", "./", "../")):
            raise ValueError("ontology Git URL must be a remote repository")
        return value


class GeasProfile(StrictModel):
    ontology_directory: Path = Path("ontologies")
    secret_sources: tuple[SecretSource, ...] = (
        SecretSource(path=Path("secrets/common.env"), format="dotenv"),
    )
    ontology_git: OntologyGitConfig | None = None
    subscriptions: dict[str, OntologySubscription] = Field(default_factory=dict)
    trust_rules: tuple[TrustRule, ...] = ()
    capability_grants: tuple[CapabilityGrant, ...] = ()
    installed_ontologies: tuple[InstalledOntologySnapshot, ...] = ()

    @field_validator("ontology_directory")
    @classmethod
    def ontology_directory_is_relative(cls, value: Path) -> Path:
        if value.is_absolute() or ".." in value.parts:
            raise ValueError("profile ontology_directory must be config-relative")
        return value

    @model_validator(mode="after")
    def trust_selectors_are_unique(self) -> GeasProfile:
        NormalizedProfile(subscriptions=self.normalized_subscriptions())
        selectors = [
            (
                rule.repository,
                rule.refs,
                rule.paths,
                rule.bundle_sha256,
            )
            for rule in self.trust_rules
        ]
        if len(selectors) != len(set(selectors)):
            raise ValueError("duplicate trust rule selectors")
        grant_selectors = [
            (
                grant.subject.repository,
                grant.subject.refs,
                grant.subject.paths,
                grant.subject.bundle_sha256,
                grant.capabilities,
                grant.delegable_capabilities,
                grant.resources.model_dump_json(),
            )
            for grant in self.capability_grants
        ]
        if len(grant_selectors) != len(set(grant_selectors)):
            raise ValueError("duplicate capability grant selectors")
        snapshots = [
            (snapshot.name, snapshot.bundle_sha256) for snapshot in self.installed_ontologies
        ]
        if len(snapshots) != len(set(snapshots)):
            raise ValueError("duplicate installed ontology snapshot")
        return self

    def effective_capability_grants(self) -> tuple[CapabilityGrant, ...]:
        if self.capability_grants:
            return self.capability_grants
        return tuple(_compatibility_grant(rule) for rule in self.trust_rules)

    def normalized_subscriptions(
        self,
        *,
        freshness: OntologyFreshnessConfig | None = None,
    ) -> dict[str, OntologySubscription]:
        subscriptions = dict(self.subscriptions)
        if self.ontology_git is not None and "primary" not in subscriptions:
            subscriptions["primary"] = OntologySubscription(
                url=self.ontology_git.url,
                active_ref=self.ontology_git.active_ref,
                checkout=self.ontology_directory,
                catalog=Path("geas.yaml"),
                remote=self.ontology_git.remote,
                pull_before_update=self.ontology_git.pull_before_update,
                push_on_update=self.ontology_git.push_on_update,
                freshness=freshness or OntologyFreshnessConfig(),
            )
        return dict(sorted(subscriptions.items()))


class GeasUserConfig(StrictModel):
    version: Literal[1, 2] = 1
    default_profile: str = "default"
    ontology_freshness: OntologyFreshnessConfig = Field(default_factory=OntologyFreshnessConfig)
    ontology_defaults: OntologyBuildDefaults = Field(default_factory=OntologyBuildDefaults)
    profiles: dict[str, GeasProfile]
    _source_config_sha256: str | None = PrivateAttr(default=None)

    @field_validator("default_profile")
    @classmethod
    def default_profile_is_safe(cls, value: str) -> str:
        if not _PROFILE_NAME.fullmatch(value):
            raise ValueError("default profile name is invalid")
        return value

    @model_validator(mode="after")
    def profiles_are_named_and_default_exists(self) -> GeasUserConfig:
        if self.default_profile not in self.profiles:
            raise ValueError("default_profile is absent from profiles")
        invalid = sorted(name for name in self.profiles if not _PROFILE_NAME.fullmatch(name))
        if invalid:
            raise ValueError(f"invalid profile names: {', '.join(invalid)}")
        checkouts: list[tuple[str, Path]] = []
        for profile_name, profile in sorted(self.profiles.items()):
            if self.version == 1 and profile.capability_grants:
                raise ValueError("version 1 configuration cannot contain capability_grants")
            if self.version == 2 and profile.trust_rules:
                raise ValueError("version 2 configuration cannot contain trust_rules")
            normalized = profile.normalized_subscriptions(freshness=self.ontology_freshness)
            for subscription_name, subscription in normalized.items():
                validate_removal_journal_namespace(subscription.checkout)
                identity = f"{profile_name}/{subscription_name}"
                for existing_identity, existing_checkout in checkouts:
                    if (
                        subscription.checkout == existing_checkout
                        or subscription.checkout.is_relative_to(existing_checkout)
                        or existing_checkout.is_relative_to(subscription.checkout)
                    ):
                        raise ValueError(
                            "subscription checkouts overlap across profiles: "
                            f"{existing_identity!r}={existing_checkout.as_posix()!r}, "
                            f"{identity!r}={subscription.checkout.as_posix()!r}"
                        )
                checkouts.append((identity, subscription.checkout))
        return self

    @classmethod
    def default(cls) -> GeasUserConfig:
        return cls(
            profiles={
                "default": GeasProfile(
                    ontology_git=OntologyGitConfig(url=DEFAULT_ONTOLOGY_REPOSITORY),
                )
            }
        )

    @classmethod
    def from_yaml(cls, path: Path) -> GeasUserConfig:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    def explicit_yaml(self) -> str:
        return yaml.safe_dump(
            self.explicit_dict(),
            sort_keys=False,
            allow_unicode=True,
        )

    def explicit_dict(self) -> dict[str, object]:
        value = self.model_dump(mode="json", exclude_none=False)
        for profile in value["profiles"].values():  # type: ignore[union-attr]
            if self.version == 1:
                profile.pop("capability_grants", None)
            else:
                profile.pop("trust_rules", None)
        return value

    def profile(self, name: str | None = None) -> tuple[str, GeasProfile]:
        selected = name or self.default_profile
        try:
            return selected, self.profiles[selected]
        except KeyError:
            raise ValueError(f"unknown Geas profile: {selected}") from None

    def normalized_profile(self, name: str | None = None) -> NormalizedProfile:
        _, profile = self.profile(name)
        return NormalizedProfile(
            subscriptions=profile.normalized_subscriptions(freshness=self.ontology_freshness)
        )


class UserConfigManager:
    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or geas_config_home() / "config.yaml").expanduser().resolve()
        self.root = self.path.parent
        self.last_defaults_receipt: DefaultConfigReceipt | None = None
        self.last_builtin_skill_receipt: BuiltinSkillReceipt | None = None

    def load(self) -> GeasUserConfig:
        value, config = self._validated_config_bytes()
        return self._bind_config_identity(config, value)

    def load_or_create(self, *, update_defaults: bool = False) -> GeasUserConfig:
        with self._config_lock():
            if self.path.exists():
                source = self.path.read_bytes()
                raw = yaml.safe_load(source)
                config = GeasUserConfig.model_validate(raw)
                self.validate_subscription_layout(config)
                explicit = config.explicit_dict()
                if _fill_missing(raw, explicit):
                    source = yaml.safe_dump(
                        raw,
                        sort_keys=False,
                        allow_unicode=True,
                    ).encode()
                    _atomic_write(
                        self.path,
                        source,
                    )
            else:
                config = GeasUserConfig.default()
                self.validate_subscription_layout(config)
                source = config.explicit_yaml().encode()
                _atomic_write(self.path, source)
            config = self._bind_config_identity(config, source)
        self._ensure_secret_scaffold()
        self.last_defaults_receipt = self.install_defaults(update=update_defaults)
        return config

    def install_defaults(self, *, update: bool = False) -> DefaultConfigReceipt:
        """Install packaged policy defaults without overwriting operator changes."""
        self.root.mkdir(parents=True, exist_ok=True)
        state_path = self.root / "defaults-state.json"
        if state_path.is_symlink():
            raise ValueError("managed-default state cannot be a symbolic link")
        state = self._load_defaults_state(state_path)
        installed: list[str] = []
        updated: list[str] = []
        unchanged: list[str] = []
        preserved: list[str] = []
        candidates: list[str] = []
        next_files = dict(state.files)

        for filename in DEFAULT_CONFIG_FILENAMES:
            template = default_config_path(filename).read_bytes()
            template_hash = _sha256(template)
            destination = self.policy_path(filename)
            if destination.is_symlink():
                raise ValueError(f"managed config cannot be a symbolic link: {destination}")
            previous = state.files.get(filename)
            if not destination.exists():
                _atomic_write(destination, template)
                installed.append(filename)
                next_files[filename] = ManagedDefaultFile(
                    installed_sha256=template_hash,
                    template_sha256=template_hash,
                )
                continue
            if not destination.is_file():
                raise ValueError(f"managed config must be a regular file: {destination}")

            current_hash = _sha256(destination.read_bytes())
            if current_hash == template_hash:
                unchanged.append(filename)
                next_files[filename] = ManagedDefaultFile(
                    installed_sha256=template_hash,
                    template_sha256=template_hash,
                )
                self._remove_candidate(destination)
                continue
            if (
                previous is not None
                and previous.installed_sha256 is not None
                and current_hash == previous.installed_sha256
                and update
            ):
                _atomic_write(destination, template)
                updated.append(filename)
                next_files[filename] = ManagedDefaultFile(
                    installed_sha256=template_hash,
                    template_sha256=template_hash,
                )
                self._remove_candidate(destination)
                continue

            preserved.append(filename)
            candidate = destination.with_name(f"{destination.name}.new")
            if candidate.is_symlink():
                raise ValueError(f"managed config candidate cannot be a symlink: {candidate}")
            if not candidate.exists() or candidate.read_bytes() != template:
                _atomic_write(candidate, template)
            candidates.append(candidate.name)
            next_files[filename] = ManagedDefaultFile(
                installed_sha256=(previous.installed_sha256 if previous is not None else None),
                template_sha256=template_hash,
            )

        next_state = ManagedDefaultsState(files=next_files)
        _atomic_write(
            state_path,
            json.dumps(next_state.model_dump(mode="json"), indent=2, sort_keys=True).encode()
            + b"\n",
        )
        return DefaultConfigReceipt(
            root=str(self.root),
            installed=tuple(installed),
            updated=tuple(updated),
            unchanged=tuple(unchanged),
            preserved=tuple(preserved),
            review_candidates=tuple(candidates),
        )

    def install_builtin_skill(
        self,
        *,
        home: Path | None = None,
        which: Callable[[str], str | None] = shutil.which,
    ) -> BuiltinSkillReceipt:
        """Install the generic packaged skill after configuration is valid."""
        receipt = install_builtin_geas_skill(
            config_root=self.root,
            home=home or Path.home(),
            which=which,
        )
        self.last_builtin_skill_receipt = receipt
        return receipt

    def policy_path(self, filename: str) -> Path:
        if filename not in DEFAULT_CONFIG_FILENAMES:
            raise ValueError(f"unknown managed Geas config file: {filename}")
        return self._confined(Path(filename))

    @staticmethod
    def _load_defaults_state(path: Path) -> ManagedDefaultsState:
        if not path.exists():
            return ManagedDefaultsState()
        try:
            return ManagedDefaultsState.model_validate_json(path.read_text())
        except ValueError as error:
            raise ValueError(f"invalid managed-default state: {path}") from error

    @staticmethod
    def _remove_candidate(destination: Path) -> None:
        candidate = destination.with_name(f"{destination.name}.new")
        if candidate.is_symlink():
            raise ValueError(f"managed config candidate cannot be a symlink: {candidate}")
        if candidate.exists():
            candidate.unlink()

    def _ensure_secret_scaffold(self) -> None:
        secrets = self.root / "secrets"
        secrets.mkdir(parents=True, exist_ok=True)
        secrets.chmod(0o700)
        ignore = secrets / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n!.gitignore\n")

    def profile(
        self,
        name: str | None = None,
        *,
        create: bool = False,
    ) -> tuple[str, GeasProfile]:
        config = self.load_or_create() if create else self.load()
        return config.profile(name)

    def config_sha256(self) -> str:
        """Return the exact identity of the current validated config bytes."""
        value, _config = self._validated_config_bytes()
        return _sha256(value)

    def mutate_profile_expected(
        self,
        *,
        operation_key: str,
        profile_name: str,
        bootstrap_name: str,
        kind: Literal[
            "grant_record",
            "grant_replace",
            "grant_remove",
            "subscription_ensure",
            "subscription_replace",
            "subscription_remove",
        ],
        expected_config_sha256: str,
        mutate: Callable[[GeasProfile], GeasProfile],
        upgrade_version: bool = False,
        applied_state_path: Path | None = None,
        applied_state: _BootstrapGrantJournal | None = None,
    ) -> BootstrapConfigMutationReceipt:
        """Lock and replace one profile only when exact config bytes still match."""
        if (applied_state_path is None) != (applied_state is None):
            raise ValueError("applied state path and value must be provided together")
        if applied_state_path is not None:
            assert applied_state is not None
            try:
                relative_applied_state = applied_state_path.relative_to(self.root)
            except ValueError as error:
                raise ValueError("applied state path escapes the config root") from error
            if self._confined_state_path(relative_applied_state) != applied_state_path:
                raise ValueError("applied state path is not canonical")
            expected_action = {
                "grant_record": "record",
                "grant_replace": "replace",
                "grant_remove": "remove",
            }.get(kind)
            if (
                expected_action is None
                or applied_state.phase != "applied"
                or applied_state.operation_key != operation_key
                or applied_state.profile_name != profile_name
                or applied_state.bootstrap_name != bootstrap_name
                or applied_state.action != expected_action
                or applied_state.before_config_sha256 != expected_config_sha256
                or applied_state_path
                != self._grant_journal_path(profile_name, bootstrap_name, operation_key)
            ):
                raise ValueError("applied grant state does not match its scoped mutation")
        BootstrapConfigMutationReceipt(
            operation_key=operation_key,
            profile_name=profile_name,
            bootstrap_name=bootstrap_name,
            kind=kind,
            before_config_sha256=expected_config_sha256,
            after_config_sha256=expected_config_sha256,
        )
        with self._config_lock():
            if applied_state_path is not None:
                assert applied_state is not None
                prepared_state = applied_state.model_copy(update={"phase": "prepared"})
                expected_prepared = (
                    canonical_json(prepared_state.model_dump(mode="json")) + b"\n"
                )
                if self._load_bootstrap_state(applied_state_path) != expected_prepared:
                    raise RuntimeError(
                        "bootstrap grant journal changed before scoped mutation"
                    )
            before, config = self._validated_config_bytes()
            before_sha256 = _sha256(before)
            if before_sha256 != expected_config_sha256:
                raise RuntimeError("Geas user config changed before scoped mutation")
            _updated, after = self._profile_mutation_bytes(
                config,
                profile_name=profile_name,
                mutate=mutate,
                upgrade_version=upgrade_version,
            )
            if self.path.read_bytes() != before:
                raise RuntimeError("Geas user config changed before atomic replacement")
            if (
                applied_state is not None
                and applied_state.after_config_sha256 != _sha256(after)
            ):
                raise ValueError("applied grant state does not match config after-state")
            if after != before:
                _atomic_write(self.path, after)
            if applied_state_path is not None:
                assert applied_state is not None
                self._write_bootstrap_state(applied_state_path, applied_state)
            return BootstrapConfigMutationReceipt(
                operation_key=operation_key,
                profile_name=profile_name,
                bootstrap_name=bootstrap_name,
                kind=kind,
                before_config_sha256=before_sha256,
                after_config_sha256=_sha256(after),
            )

    def _profile_mutation_bytes(
        self,
        config: GeasUserConfig,
        *,
        profile_name: str,
        mutate: Callable[[GeasProfile], GeasProfile],
        upgrade_version: bool,
    ) -> tuple[GeasUserConfig, bytes]:
        writable = _upgrade_config_to_v2(config) if upgrade_version else config
        if writable.version == 2 and not upgrade_version:
            raise ValueError("writing version 2 capability grants requires upgrade_version=True")
        _, profile = writable.profile(profile_name)
        updated_profile = GeasProfile.model_validate(mutate(profile).model_dump(mode="python"))
        updated = GeasUserConfig.model_validate(
            writable.model_copy(
                update={
                    "profiles": {
                        **writable.profiles,
                        profile_name: updated_profile,
                    }
                }
            ).model_dump(mode="python")
        )
        self.validate_subscription_layout(updated)
        return updated, updated.explicit_yaml().encode()

    @staticmethod
    def _bind_config_identity(config: GeasUserConfig, value: bytes) -> GeasUserConfig:
        object.__setattr__(config, "_source_config_sha256", _sha256(value))
        return config

    def record_bootstrap_grant(
        self,
        *,
        operation_key: str,
        profile_name: str,
        bootstrap_name: str,
        grant: CapabilityGrant,
    ) -> BootstrapGrantMutationReceipt:
        """Record one absent grant without adopting equivalent operator state."""
        self._validate_bootstrap_grant_selector(
            operation_key, profile_name, bootstrap_name, "grant_record"
        )
        validated_grant = CapabilityGrant.model_validate(grant.model_dump(mode="python"))
        journal_path = self._grant_journal_path(
            profile_name, bootstrap_name, operation_key
        )
        journal = self._load_grant_journal(journal_path)
        if journal is not None:
            self._validate_grant_journal(
                journal,
                operation_key=operation_key,
                profile_name=profile_name,
                bootstrap_name=bootstrap_name,
                action="record",
                old_grant_id=None,
                new_grant_id=validated_grant.id,
            )
            return self._resume_grant_record(journal_path, journal, validated_grant)

        active = self._load_grant_ownership(profile_name, bootstrap_name)
        if active is not None:
            raise ValueError("bootstrap grant name is already owned by another operation")
        before, config = self._validated_config_bytes()
        before_sha256 = _sha256(before)
        writable = _upgrade_config_to_v2(config)
        _, profile = writable.profile(profile_name)
        if any(item.id == validated_grant.id for item in profile.capability_grants):
            raise ValueError("refusing to adopt a pre-existing capability grant")

        def append_grant(current: GeasProfile) -> GeasProfile:
            return current.model_copy(
                update={"capability_grants": (*current.capability_grants, validated_grant)}
            )

        _updated, after = self._profile_mutation_bytes(
            config,
            profile_name=profile_name,
            mutate=append_grant,
            upgrade_version=True,
        )
        prepared = _BootstrapGrantJournal(
            phase="prepared",
            operation_key=operation_key,
            profile_name=profile_name,
            bootstrap_name=bootstrap_name,
            action="record",
            owner_operation_key=operation_key,
            old_grant_id=None,
            new_grant_id=validated_grant.id,
            before_config_sha256=before_sha256,
            after_config_sha256=_sha256(after),
        )
        self._write_bootstrap_state(journal_path, prepared)
        return self._resume_grant_record(journal_path, prepared, validated_grant)

    def _resume_grant_record(
        self,
        journal_path: Path,
        journal: _BootstrapGrantJournal,
        grant: CapabilityGrant,
    ) -> BootstrapGrantMutationReceipt:
        if journal.phase == "completed":
            assert journal.receipt is not None
            self._assert_live_grant_ownership(journal.receipt, grant)
            return journal.receipt
        current_sha256 = self.config_sha256()
        if journal.phase == "prepared":
            if current_sha256 == journal.after_config_sha256:
                raise RuntimeError(
                    "matching grant config has no applied marker for this operation"
                )
            if current_sha256 != journal.before_config_sha256:
                raise RuntimeError("Geas user config changed during bootstrap grant recovery")

            def append_grant(profile: GeasProfile) -> GeasProfile:
                if any(item.id == grant.id for item in profile.capability_grants):
                    raise ValueError("refusing to adopt a pre-existing capability grant")
                return profile.model_copy(
                    update={"capability_grants": (*profile.capability_grants, grant)}
                )

            applied = journal.model_copy(update={"phase": "applied"})
            mutation = self.mutate_profile_expected(
                operation_key=journal.operation_key,
                profile_name=journal.profile_name,
                bootstrap_name=journal.bootstrap_name,
                kind="grant_record",
                expected_config_sha256=journal.before_config_sha256,
                mutate=append_grant,
                upgrade_version=True,
                applied_state_path=journal_path,
                applied_state=applied,
            )
            if mutation.after_config_sha256 != journal.after_config_sha256:
                raise RuntimeError("bootstrap grant config result changed during mutation")
            journal = applied
        elif journal.phase == "applied":
            if current_sha256 != journal.after_config_sha256:
                raise RuntimeError("Geas user config changed after bootstrap grant application")
            mutation = BootstrapConfigMutationReceipt(
                operation_key=journal.operation_key,
                profile_name=journal.profile_name,
                bootstrap_name=journal.bootstrap_name,
                kind="grant_record",
                before_config_sha256=journal.before_config_sha256,
                after_config_sha256=journal.after_config_sha256,
            )
        else:
            raise ValueError("bootstrap grant journal phase is invalid")
        self._assert_grant_present(journal.profile_name, grant.id)
        ownership = BootstrapGrantOwnershipReceipt(
            owner_operation_key=journal.owner_operation_key,
            operation_key=journal.operation_key,
            profile_name=journal.profile_name,
            bootstrap_name=journal.bootstrap_name,
            grant_id=grant.id,
            config_mutation=mutation,
        )
        receipt = BootstrapGrantMutationReceipt(
            operation_key=journal.operation_key,
            profile_name=journal.profile_name,
            bootstrap_name=journal.bootstrap_name,
            action="record",
            old_grant_id=None,
            new_grant_id=grant.id,
            config_mutation=mutation,
            ownership=ownership,
        )
        self._write_bootstrap_state(
            self._grant_ownership_path(journal.profile_name, journal.bootstrap_name),
            ownership,
        )
        self._write_bootstrap_state(
            journal_path,
            journal.model_copy(update={"phase": "completed", "receipt": receipt}),
        )
        return receipt

    def replace_bootstrap_grant(
        self,
        *,
        operation_key: str,
        profile_name: str,
        bootstrap_name: str,
        ownership: BootstrapGrantOwnershipReceipt,
        old_grant: CapabilityGrant,
        new_grant: CapabilityGrant | None,
    ) -> BootstrapGrantMutationReceipt:
        """Atomically replace one exactly owned grant, including replacement by none."""
        self._validate_bootstrap_grant_selector(
            operation_key, profile_name, bootstrap_name, "grant_replace"
        )
        old = CapabilityGrant.model_validate(old_grant.model_dump(mode="python"))
        new = (
            None
            if new_grant is None
            else CapabilityGrant.model_validate(new_grant.model_dump(mode="python"))
        )
        if ownership.profile_name != profile_name or ownership.bootstrap_name != bootstrap_name:
            raise ValueError("bootstrap grant ownership selector changed")
        if ownership.grant_id != old.id:
            raise ValueError("bootstrap grant ownership does not match the exact old grant")
        journal_path = self._grant_journal_path(
            profile_name, bootstrap_name, operation_key
        )
        journal = self._load_grant_journal(journal_path)
        if journal is not None:
            self._validate_grant_journal(
                journal,
                operation_key=operation_key,
                profile_name=profile_name,
                bootstrap_name=bootstrap_name,
                action="replace",
                old_grant_id=old.id,
                new_grant_id=None if new is None else new.id,
            )
            if journal.owner_operation_key != ownership.owner_operation_key:
                raise ValueError("bootstrap grant replacement owner changed")
            return self._resume_grant_replace(journal_path, journal, ownership, old, new)

        active = self._load_grant_ownership(profile_name, bootstrap_name)
        if active != ownership:
            raise ValueError("bootstrap grant is not owned by the expected operation")
        self._assert_grant_present(profile_name, old.id)
        before, config = self._validated_config_bytes()

        def replace_grant(profile: GeasProfile) -> GeasProfile:
            matches = tuple(item for item in profile.capability_grants if item.id == old.id)
            if len(matches) != 1:
                raise RuntimeError("exact old bootstrap grant changed before replacement")
            retained = tuple(item for item in profile.capability_grants if item.id != old.id)
            return profile.model_copy(
                update={"capability_grants": retained + (() if new is None else (new,))}
            )

        _updated, after = self._profile_mutation_bytes(
            config,
            profile_name=profile_name,
            mutate=replace_grant,
            upgrade_version=True,
        )
        prepared = _BootstrapGrantJournal(
            phase="prepared",
            operation_key=operation_key,
            profile_name=profile_name,
            bootstrap_name=bootstrap_name,
            action="replace",
            owner_operation_key=ownership.owner_operation_key,
            old_grant_id=old.id,
            new_grant_id=None if new is None else new.id,
            before_config_sha256=_sha256(before),
            after_config_sha256=_sha256(after),
        )
        self._write_bootstrap_state(journal_path, prepared)
        return self._resume_grant_replace(journal_path, prepared, ownership, old, new)

    def _resume_grant_replace(
        self,
        journal_path: Path,
        journal: _BootstrapGrantJournal,
        prior_ownership: BootstrapGrantOwnershipReceipt,
        old_grant: CapabilityGrant,
        new_grant: CapabilityGrant | None,
    ) -> BootstrapGrantMutationReceipt:
        if journal.phase == "completed":
            assert journal.receipt is not None
            self._assert_completed_grant_replacement(journal.receipt, new_grant)
            return journal.receipt
        current_sha256 = self.config_sha256()
        if journal.phase == "prepared":
            if current_sha256 == journal.after_config_sha256:
                raise RuntimeError(
                    "matching grant config has no applied marker for this operation"
                )
            if current_sha256 != journal.before_config_sha256:
                raise RuntimeError(
                    "Geas user config changed during bootstrap grant replacement"
                )
            if self._load_grant_ownership(journal.profile_name, journal.bootstrap_name) != (
                prior_ownership
            ):
                raise ValueError("bootstrap grant ownership changed before replacement")

            def replace_grant(profile: GeasProfile) -> GeasProfile:
                matches = tuple(
                    item for item in profile.capability_grants if item.id == old_grant.id
                )
                if len(matches) != 1:
                    raise RuntimeError("exact old bootstrap grant changed before replacement")
                retained = tuple(
                    item for item in profile.capability_grants if item.id != old_grant.id
                )
                return profile.model_copy(
                    update={
                        "capability_grants": retained
                        + (() if new_grant is None else (new_grant,))
                    }
                )

            applied = journal.model_copy(update={"phase": "applied"})
            mutation = self.mutate_profile_expected(
                operation_key=journal.operation_key,
                profile_name=journal.profile_name,
                bootstrap_name=journal.bootstrap_name,
                kind="grant_replace",
                expected_config_sha256=journal.before_config_sha256,
                mutate=replace_grant,
                upgrade_version=True,
                applied_state_path=journal_path,
                applied_state=applied,
            )
            if mutation.after_config_sha256 != journal.after_config_sha256:
                raise RuntimeError("bootstrap grant replacement config identity changed")
            journal = applied
        elif journal.phase == "applied":
            if current_sha256 != journal.after_config_sha256:
                raise RuntimeError(
                    "Geas user config changed after bootstrap grant replacement"
                )
            mutation = BootstrapConfigMutationReceipt(
                operation_key=journal.operation_key,
                profile_name=journal.profile_name,
                bootstrap_name=journal.bootstrap_name,
                kind="grant_replace",
                before_config_sha256=journal.before_config_sha256,
                after_config_sha256=journal.after_config_sha256,
            )
        else:
            raise ValueError("bootstrap grant replacement journal phase is invalid")
        self._assert_grant_absent(journal.profile_name, old_grant.id)
        resulting_ownership: BootstrapGrantOwnershipReceipt | None = None
        if new_grant is not None:
            self._assert_grant_present(journal.profile_name, new_grant.id)
            resulting_ownership = BootstrapGrantOwnershipReceipt(
                owner_operation_key=journal.owner_operation_key,
                operation_key=journal.operation_key,
                profile_name=journal.profile_name,
                bootstrap_name=journal.bootstrap_name,
                grant_id=new_grant.id,
                config_mutation=mutation,
            )
            self._write_bootstrap_state(
                self._grant_ownership_path(journal.profile_name, journal.bootstrap_name),
                resulting_ownership,
            )
        else:
            self._remove_exact_bootstrap_state(
                self._grant_ownership_path(journal.profile_name, journal.bootstrap_name),
                prior_ownership,
            )
        receipt = BootstrapGrantMutationReceipt(
            operation_key=journal.operation_key,
            profile_name=journal.profile_name,
            bootstrap_name=journal.bootstrap_name,
            action="replace",
            old_grant_id=old_grant.id,
            new_grant_id=None if new_grant is None else new_grant.id,
            config_mutation=mutation,
            ownership=resulting_ownership,
        )
        self._write_bootstrap_state(
            journal_path,
            journal.model_copy(update={"phase": "completed", "receipt": receipt}),
        )
        return receipt

    def _assert_completed_grant_replacement(
        self,
        receipt: BootstrapGrantMutationReceipt,
        new_grant: CapabilityGrant | None,
    ) -> None:
        if new_grant is None:
            if self._load_grant_ownership(receipt.profile_name, receipt.bootstrap_name) is not None:
                raise ValueError("removed bootstrap grant retained ownership state")
            assert receipt.old_grant_id is not None
            self._assert_grant_absent(receipt.profile_name, receipt.old_grant_id)
            return
        self._assert_live_grant_ownership(receipt, new_grant)

    def remove_bootstrap_grant(
        self,
        *,
        operation_key: str,
        profile_name: str,
        bootstrap_name: str,
        ownership: BootstrapGrantOwnershipReceipt,
        grant: CapabilityGrant,
    ) -> BootstrapGrantMutationReceipt:
        """Remove one exact owned grant and retain a stable-keyed tombstone journal."""
        self._validate_bootstrap_grant_selector(
            operation_key, profile_name, bootstrap_name, "grant_remove"
        )
        validated = CapabilityGrant.model_validate(grant.model_dump(mode="python"))
        if (
            ownership.profile_name != profile_name
            or ownership.bootstrap_name != bootstrap_name
            or ownership.grant_id != validated.id
        ):
            raise ValueError("bootstrap grant ownership does not match removal request")
        journal_path = self._grant_journal_path(
            profile_name, bootstrap_name, operation_key
        )
        journal = self._load_grant_journal(journal_path)
        if journal is not None:
            self._validate_grant_journal(
                journal,
                operation_key=operation_key,
                profile_name=profile_name,
                bootstrap_name=bootstrap_name,
                action="remove",
                old_grant_id=validated.id,
                new_grant_id=None,
            )
            if journal.owner_operation_key != ownership.owner_operation_key:
                raise ValueError("bootstrap grant removal owner changed")
            return self._resume_grant_removal(journal_path, journal, ownership, validated)
        if self._load_grant_ownership(profile_name, bootstrap_name) != ownership:
            raise ValueError("bootstrap grant is not owned by the expected operation")
        self._assert_grant_present(profile_name, validated.id)
        before, config = self._validated_config_bytes()

        def remove_grant(profile: GeasProfile) -> GeasProfile:
            matches = tuple(item for item in profile.capability_grants if item.id == validated.id)
            if len(matches) != 1:
                raise RuntimeError("exact owned bootstrap grant changed before removal")
            return profile.model_copy(
                update={
                    "capability_grants": tuple(
                        item for item in profile.capability_grants if item.id != validated.id
                    )
                }
            )

        _updated, after = self._profile_mutation_bytes(
            config,
            profile_name=profile_name,
            mutate=remove_grant,
            upgrade_version=True,
        )
        prepared = _BootstrapGrantJournal(
            phase="prepared",
            operation_key=operation_key,
            profile_name=profile_name,
            bootstrap_name=bootstrap_name,
            action="remove",
            owner_operation_key=ownership.owner_operation_key,
            old_grant_id=validated.id,
            new_grant_id=None,
            before_config_sha256=_sha256(before),
            after_config_sha256=_sha256(after),
        )
        self._write_bootstrap_state(journal_path, prepared)
        return self._resume_grant_removal(journal_path, prepared, ownership, validated)

    def _resume_grant_removal(
        self,
        journal_path: Path,
        journal: _BootstrapGrantJournal,
        ownership: BootstrapGrantOwnershipReceipt,
        grant: CapabilityGrant,
    ) -> BootstrapGrantMutationReceipt:
        if journal.phase == "completed":
            assert journal.receipt is not None
            if self._load_grant_ownership(journal.profile_name, journal.bootstrap_name) is not None:
                raise ValueError("removed bootstrap grant retained ownership state")
            self._assert_grant_absent(journal.profile_name, grant.id)
            return journal.receipt
        current_sha256 = self.config_sha256()
        if journal.phase == "prepared":
            if current_sha256 == journal.after_config_sha256:
                raise RuntimeError(
                    "matching grant config has no applied marker for this operation"
                )
            if current_sha256 != journal.before_config_sha256:
                raise RuntimeError(
                    "Geas user config changed during bootstrap grant removal"
                )
            if (
                self._load_grant_ownership(
                    journal.profile_name, journal.bootstrap_name
                )
                != ownership
            ):
                raise ValueError("bootstrap grant ownership changed before removal")

            def remove_grant(profile: GeasProfile) -> GeasProfile:
                matches = tuple(item for item in profile.capability_grants if item.id == grant.id)
                if len(matches) != 1:
                    raise RuntimeError("exact owned bootstrap grant changed before removal")
                return profile.model_copy(
                    update={
                        "capability_grants": tuple(
                            item for item in profile.capability_grants if item.id != grant.id
                        )
                    }
                )

            applied = journal.model_copy(update={"phase": "applied"})
            mutation = self.mutate_profile_expected(
                operation_key=journal.operation_key,
                profile_name=journal.profile_name,
                bootstrap_name=journal.bootstrap_name,
                kind="grant_remove",
                expected_config_sha256=journal.before_config_sha256,
                mutate=remove_grant,
                upgrade_version=True,
                applied_state_path=journal_path,
                applied_state=applied,
            )
            if mutation.after_config_sha256 != journal.after_config_sha256:
                raise RuntimeError("bootstrap grant removal config identity changed")
            journal = applied
        elif journal.phase == "applied":
            if current_sha256 != journal.after_config_sha256:
                raise RuntimeError(
                    "Geas user config changed after bootstrap grant removal"
                )
            mutation = BootstrapConfigMutationReceipt(
                operation_key=journal.operation_key,
                profile_name=journal.profile_name,
                bootstrap_name=journal.bootstrap_name,
                kind="grant_remove",
                before_config_sha256=journal.before_config_sha256,
                after_config_sha256=journal.after_config_sha256,
            )
        else:
            raise ValueError("bootstrap grant removal journal phase is invalid")
        self._assert_grant_absent(journal.profile_name, grant.id)
        active_path = self._grant_ownership_path(journal.profile_name, journal.bootstrap_name)
        active = self._load_grant_ownership(journal.profile_name, journal.bootstrap_name)
        if active is not None:
            if active != ownership:
                raise ValueError("bootstrap grant ownership changed before state removal")
            self._remove_exact_bootstrap_state(active_path, ownership)
        receipt = BootstrapGrantMutationReceipt(
            operation_key=journal.operation_key,
            profile_name=journal.profile_name,
            bootstrap_name=journal.bootstrap_name,
            action="remove",
            old_grant_id=grant.id,
            new_grant_id=None,
            config_mutation=mutation,
            ownership=None,
        )
        self._write_bootstrap_state(
            journal_path,
            journal.model_copy(update={"phase": "completed", "receipt": receipt}),
        )
        return receipt

    def _assert_grant_absent(self, profile_name: str, grant_id: str) -> None:
        config = self.load()
        _, profile = config.profile(profile_name)
        if any(item.id == grant_id for item in profile.capability_grants):
            raise RuntimeError("removed bootstrap grant is still present")

    def _assert_grant_present(self, profile_name: str, grant_id: str) -> None:
        config = self.load()
        _, profile = config.profile(profile_name)
        matches = tuple(item for item in profile.capability_grants if item.id == grant_id)
        if len(matches) != 1:
            raise RuntimeError("owned bootstrap grant is missing or ambiguous")

    def _assert_live_grant_ownership(
        self,
        receipt: BootstrapGrantMutationReceipt,
        grant: CapabilityGrant,
    ) -> None:
        if receipt.ownership is None or receipt.new_grant_id != grant.id:
            raise ValueError("bootstrap grant receipt does not match the requested grant")
        active = self._load_grant_ownership(receipt.profile_name, receipt.bootstrap_name)
        if active != receipt.ownership:
            raise ValueError("bootstrap grant ownership state changed")
        self._assert_grant_present(receipt.profile_name, grant.id)

    def _grant_journal_path(
        self, profile_name: str, bootstrap_name: str, operation_key: str
    ) -> Path:
        digest = operation_key.rsplit(":", 1)[-1]
        relative = (
            Path("repository-bootstrap")
            / "grant-mutations"
            / profile_name
            / bootstrap_name
            / f"{digest}.json"
        )
        return self._confined_state_path(relative)

    @staticmethod
    def _validate_bootstrap_grant_selector(
        operation_key: str,
        profile_name: str,
        bootstrap_name: str,
        kind: Literal["grant_record", "grant_replace", "grant_remove"],
    ) -> None:
        BootstrapConfigMutationReceipt(
            operation_key=operation_key,
            profile_name=profile_name,
            bootstrap_name=bootstrap_name,
            kind=kind,
            before_config_sha256="0" * 64,
            after_config_sha256="0" * 64,
        )

    def _grant_ownership_path(self, profile_name: str, bootstrap_name: str) -> Path:
        return self._confined_state_path(
            Path("repository-bootstrap")
            / "grant-ownership"
            / profile_name
            / f"{bootstrap_name}.json"
        )

    def _load_grant_journal(self, path: Path) -> _BootstrapGrantJournal | None:
        value = self._load_bootstrap_state(path)
        if value is None:
            return None
        try:
            return _BootstrapGrantJournal.model_validate_json(value)
        except ValueError as error:
            raise ValueError("bootstrap grant mutation journal is invalid") from error

    def _load_grant_ownership(
        self, profile_name: str, bootstrap_name: str
    ) -> BootstrapGrantOwnershipReceipt | None:
        value = self._load_bootstrap_state(
            self._grant_ownership_path(profile_name, bootstrap_name)
        )
        if value is None:
            return None
        try:
            return BootstrapGrantOwnershipReceipt.model_validate_json(value)
        except ValueError as error:
            raise ValueError("bootstrap grant ownership state is invalid") from error

    @staticmethod
    def _validate_grant_journal(
        journal: _BootstrapGrantJournal,
        *,
        operation_key: str,
        profile_name: str,
        bootstrap_name: str,
        action: Literal["record", "replace", "remove"],
        old_grant_id: str | None,
        new_grant_id: str | None,
    ) -> None:
        if (
            journal.operation_key != operation_key
            or journal.profile_name != profile_name
            or journal.bootstrap_name != bootstrap_name
            or journal.action != action
            or journal.old_grant_id != old_grant_id
            or journal.new_grant_id != new_grant_id
        ):
            raise ValueError("bootstrap grant operation conflicts with its journal")

    def _confined_state_path(self, relative: Path) -> Path:
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("bootstrap state path must be normalized and relative")
        candidate = self.root / relative
        try:
            candidate.relative_to(self.root)
        except ValueError as error:
            raise ValueError("bootstrap state path escapes the config root") from error
        _reject_symlink_ancestry(candidate.parent)
        return candidate

    @staticmethod
    def _load_bootstrap_state(path: Path) -> bytes | None:
        _reject_symlink_ancestry(path.parent)
        if path.is_symlink():
            raise ValueError("bootstrap state must be a regular file")
        if not path.exists():
            return None
        if not path.is_file():
            raise ValueError("bootstrap state must be a regular file")
        return path.read_bytes()

    @staticmethod
    def _write_bootstrap_state(path: Path, value: StrictModel) -> None:
        _reject_symlink_ancestry(path.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        _reject_symlink_ancestry(path.parent)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("bootstrap state must be a regular file")
        _atomic_write(path, canonical_json(value.model_dump(mode="json")) + b"\n")

    @staticmethod
    def _remove_exact_bootstrap_state(path: Path, expected: StrictModel) -> None:
        value = UserConfigManager._load_bootstrap_state(path)
        expected_bytes = canonical_json(expected.model_dump(mode="json")) + b"\n"
        if value != expected_bytes:
            raise ValueError("bootstrap ownership state changed before removal")
        path.unlink()
        _fsync_directory(path.parent)

    def replace(
        self,
        config: GeasUserConfig,
        *,
        upgrade_version: bool = False,
        expected_config_sha256: str | None = None,
    ) -> None:
        """CAS-replace trusted config from an exact loaded or explicit identity."""
        bound_identity = config._source_config_sha256
        with self._config_lock():
            validated = GeasUserConfig.model_validate(config.model_dump(mode="python"))
            if upgrade_version and validated.version == 1:
                validated = GeasUserConfig.model_validate(
                    {
                        **validated.model_dump(mode="python"),
                        "version": 2,
                        "profiles": {
                            name: {
                                **profile.model_dump(mode="python"),
                                "trust_rules": (),
                                "capability_grants": profile.effective_capability_grants(),
                            }
                            for name, profile in validated.profiles.items()
                        },
                    }
                )
            elif validated.version == 2 and not upgrade_version:
                raise ValueError(
                    "writing version 2 capability grants requires upgrade_version=True"
                )
            self.validate_subscription_layout(validated)
            if expected_config_sha256 is not None and not re.fullmatch(
                r"[0-9a-f]{64}", expected_config_sha256
            ):
                raise ValueError("expected Geas user config identity is invalid")
            if (
                expected_config_sha256 is not None
                and bound_identity is not None
                and expected_config_sha256 != bound_identity
            ):
                raise ValueError("loaded and explicit Geas config identities disagree")
            expected = expected_config_sha256 or bound_identity
            if self.path.is_symlink():
                raise ValueError("Geas user config cannot be a symbolic link")
            rendered = validated.explicit_yaml().encode()
            if self.path.exists():
                before, _current = self._validated_config_bytes()
                before_sha256 = _sha256(before)
                if expected is None and before != rendered:
                    raise RuntimeError(
                        "Geas user config replacement requires an exact loaded identity"
                    )
                if expected is not None and before_sha256 != expected:
                    raise RuntimeError(
                        "Geas user config changed before atomic replacement"
                    )
                if before != rendered:
                    _atomic_write(self.path, rendered)
            else:
                if expected is not None:
                    raise RuntimeError(
                        "Geas user config changed before atomic replacement"
                    )
                _atomic_write(self.path, rendered)
            self._bind_config_identity(config, rendered)

    def _validated_config_bytes(self) -> tuple[bytes, GeasUserConfig]:
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError(f"Geas user config does not exist or is unsafe: {self.path}")
        value = self.path.read_bytes()
        try:
            raw = yaml.safe_load(value)
        except yaml.YAMLError as error:
            raise ValueError("invalid Geas user configuration bytes") from error
        try:
            config = GeasUserConfig.model_validate(raw)
        except ValidationError as error:
            messages = "; ".join(
                str(item["msg"])
                for item in error.errors(include_url=False, include_input=False)
            )
            raise ValueError(f"invalid Geas user configuration: {messages}") from error
        self.validate_subscription_layout(config)
        return value, config

    @contextmanager
    def _config_lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        lock_key = os.fspath(lock_path)
        with _CONFIG_LOCKS_GUARD:
            process_lock = _CONFIG_LOCKS.setdefault(lock_key, threading.RLock())
        with process_lock:
            depths = getattr(_CONFIG_LOCK_DEPTH, "paths", None)
            if depths is None:
                depths = {}
                _CONFIG_LOCK_DEPTH.paths = depths
            depth = depths.get(lock_key, 0)
            depths[lock_key] = depth + 1
            try:
                if lock_path.is_symlink():
                    raise ValueError("Geas user config lock cannot be a symbolic link")
                if depth:
                    yield
                else:
                    with _exclusive_file_lock(lock_path):
                        yield
            finally:
                if depth:
                    depths[lock_key] = depth
                else:
                    depths.pop(lock_key, None)

    def restore_bytes(
        self,
        value: bytes,
        *,
        expected_config_sha256: str | None,
    ) -> None:
        """Atomically restore an exact previously validated configuration snapshot."""
        with self._config_lock():
            try:
                restored = GeasUserConfig.model_validate(yaml.safe_load(value))
                self.validate_subscription_layout(restored)
            except (ValueError, yaml.YAMLError) as error:
                raise ValueError(
                    "cannot restore invalid Geas user configuration bytes"
                ) from error
            if expected_config_sha256 is not None and not re.fullmatch(
                r"[0-9a-f]{64}", expected_config_sha256
            ):
                raise ValueError("expected Geas user config identity is invalid")
            if self.path.is_symlink():
                raise ValueError("Geas user config cannot be a symbolic link")
            if self.path.exists():
                before, _current = self._validated_config_bytes()
                if (
                    expected_config_sha256 is None
                    or _sha256(before) != expected_config_sha256
                ):
                    raise RuntimeError(
                        "Geas user config changed before atomic restoration"
                    )
            elif expected_config_sha256 is not None:
                raise RuntimeError("Geas user config changed before atomic restoration")
            _atomic_write(self.path, value)

    def ontology_root(self, profile: GeasProfile) -> Path:
        return self._confined(profile.ontology_directory)

    def subscription_checkout(self, subscription: OntologySubscription) -> Path:
        return self._confined_subscription_path(subscription.checkout)

    def validate_subscription_layout(self, config: GeasUserConfig) -> None:
        """Reject filesystem aliases and overlaps without following symlinks."""
        resolved: list[tuple[str, Path]] = []
        for profile_name, profile in sorted(config.profiles.items()):
            normalized = profile.normalized_subscriptions(freshness=config.ontology_freshness)
            for name, subscription in normalized.items():
                validate_removal_journal_namespace(subscription.checkout)
                checkout = self._confined_subscription_path(subscription.checkout)
                canonical = checkout.resolve()
                self._confined_subscription_path(subscription.checkout)
                identity = f"{profile_name}/{name}"
                for sibling_identity, sibling in resolved:
                    if (
                        canonical == sibling
                        or canonical.is_relative_to(sibling)
                        or sibling.is_relative_to(canonical)
                    ):
                        raise ValueError(
                            "subscriptions resolve to the same checkout or overlap: "
                            f"{sibling_identity!r}, {identity!r}"
                        )
                resolved.append((identity, canonical))

    def validate_subscription_removal(
        self,
        config: GeasUserConfig,
        *,
        profile_name: str,
        subscription_name: str,
        expected_checkout: Path,
    ) -> None:
        """Prove no configured checkout aliases or overlaps a removal target."""
        self.validate_subscription_layout(config)
        _, selected_profile = config.profile(profile_name)
        try:
            selected = selected_profile.normalized_subscriptions(
                freshness=config.ontology_freshness
            )[subscription_name]
        except KeyError:
            raise ValueError(f"unknown ontology subscription: {subscription_name}") from None
        checkout = self._confined_subscription_path(selected.checkout)
        if checkout != expected_checkout:
            raise RuntimeError("subscription checkout identity changed before removal")
        canonical = checkout.resolve()
        self._confined_subscription_path(selected.checkout)
        for other_profile_name, profile in sorted(config.profiles.items()):
            normalized = profile.normalized_subscriptions(freshness=config.ontology_freshness)
            for other_name, subscription in normalized.items():
                if other_profile_name == profile_name and other_name == subscription_name:
                    continue
                other_checkout = self._confined_subscription_path(subscription.checkout)
                other_canonical = other_checkout.resolve()
                self._confined_subscription_path(subscription.checkout)
                if (
                    canonical == other_canonical
                    or canonical.is_relative_to(other_canonical)
                    or other_canonical.is_relative_to(canonical)
                ):
                    raise ValueError(
                        "subscription removal checkout overlaps checkout referenced by "
                        f"{other_profile_name}/{other_name!s}"
                    )

    def secret_paths(self, profile: GeasProfile) -> tuple[tuple[Path, str], ...]:
        return tuple(
            (self._confined(source.path), source.format) for source in profile.secret_sources
        )

    def _confined(self, relative: Path) -> Path:
        resolved = (self.root / relative).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("Geas profile path escapes the user config directory")
        return resolved

    def _confined_subscription_path(self, relative: Path) -> Path:
        candidate = self.root / relative
        current = self.root
        for component in relative.parts:
            current /= component
            if current.is_symlink():
                raise ValueError(f"subscription checkout contains a symbolic link: {current}")
            if current.exists() and not current.is_dir():
                raise ValueError(f"subscription checkout component is not a directory: {current}")
        return candidate


def default_config_path(filename: str) -> Path:
    if filename not in DEFAULT_CONFIG_FILENAMES:
        raise ValueError(f"unknown packaged Geas config file: {filename}")
    path = Path(__file__).parent / "default_config" / filename
    if not path.is_file():
        raise ValueError(f"packaged Geas config is missing: {filename}")
    return path


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _upgrade_config_to_v2(config: GeasUserConfig) -> GeasUserConfig:
    if config.version == 2:
        return config
    return GeasUserConfig.model_validate(
        {
            **config.model_dump(mode="python"),
            "version": 2,
            "profiles": {
                name: {
                    **profile.model_dump(mode="python"),
                    "trust_rules": (),
                    "capability_grants": profile.effective_capability_grants(),
                }
                for name, profile in config.profiles.items()
            },
        }
    )


def _fill_missing(target: object, defaults: object) -> bool:
    """Materialize only absent config defaults without changing operator values."""
    if not isinstance(target, dict) or not isinstance(defaults, dict):
        return False
    changed = False
    for key, value in defaults.items():
        if key not in target:
            target[key] = value
            changed = True
        else:
            changed = _fill_missing(target[key], value) or changed
    return changed


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    try:
        with temporary.open("xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _exclusive_file_lock(path: Path):
    with path.open("a+b") as lock:
        if os.name == "nt":
            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reject_symlink_ancestry(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("bootstrap state must not traverse symbolic links")
