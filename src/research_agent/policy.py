from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml
from pydantic import Field, model_validator

from research_agent.models import (
    PolicyAction,
    PolicyDecision,
    PolicyStage,
    StrictModel,
    ThreatObservation,
    ThreatSeverity,
    ThreatStatus,
    ThreatTarget,
    content_id,
    utc_now,
)

_ACTION_PRIORITY = {
    PolicyAction.ALLOW: 0,
    PolicyAction.SANDBOX: 1,
    PolicyAction.ALLOW_METADATA_ONLY: 2,
    PolicyAction.REQUIRE_APPROVAL: 3,
    PolicyAction.QUARANTINE: 4,
    PolicyAction.DENY: 5,
}


class PolicyRule(StrictModel):
    id: str
    statuses: frozenset[ThreatStatus] = Field(min_length=1)
    severities: frozenset[ThreatSeverity] = Field(min_length=1)
    actions: dict[PolicyStage, PolicyAction]

    @model_validator(mode="after")
    def covers_every_stage(self) -> PolicyRule:
        missing = set(PolicyStage) - set(self.actions)
        if missing:
            names = ", ".join(sorted(stage.value for stage in missing))
            raise ValueError(f"policy rule {self.id!r} has no action for: {names}")
        return self


class PolicyConfig(StrictModel):
    version: int = Field(ge=1)
    default_actions: dict[PolicyStage, PolicyAction]
    rules: tuple[PolicyRule, ...]

    @model_validator(mode="after")
    def validate_config(self) -> PolicyConfig:
        missing = set(PolicyStage) - set(self.default_actions)
        if missing:
            names = ", ".join(sorted(stage.value for stage in missing))
            raise ValueError(f"default policy has no action for: {names}")
        rule_ids = [rule.id for rule in self.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("policy rule ids must be unique")
        return self


_DEFAULT_CONFIG = PolicyConfig(
    version=1,
    default_actions={
        PolicyStage.RETRIEVAL: PolicyAction.SANDBOX,
        PolicyStage.EXTRACTION: PolicyAction.SANDBOX,
        PolicyStage.COMMIT: PolicyAction.REQUIRE_APPROVAL,
    },
    rules=(
        PolicyRule(
            id="confirmed-high-threat",
            statuses=frozenset({ThreatStatus.CONFIRMED}),
            severities=frozenset({ThreatSeverity.HIGH, ThreatSeverity.CRITICAL}),
            actions={
                PolicyStage.RETRIEVAL: PolicyAction.ALLOW_METADATA_ONLY,
                PolicyStage.EXTRACTION: PolicyAction.DENY,
                PolicyStage.COMMIT: PolicyAction.DENY,
            },
        ),
        PolicyRule(
            id="confirmed-threat",
            statuses=frozenset({ThreatStatus.CONFIRMED}),
            severities=frozenset({ThreatSeverity.LOW, ThreatSeverity.MEDIUM}),
            actions={
                PolicyStage.RETRIEVAL: PolicyAction.QUARANTINE,
                PolicyStage.EXTRACTION: PolicyAction.QUARANTINE,
                PolicyStage.COMMIT: PolicyAction.DENY,
            },
        ),
        PolicyRule(
            id="suspected-threat",
            statuses=frozenset({ThreatStatus.SUSPECTED}),
            severities=frozenset(ThreatSeverity),
            actions={
                PolicyStage.RETRIEVAL: PolicyAction.QUARANTINE,
                PolicyStage.EXTRACTION: PolicyAction.QUARANTINE,
                PolicyStage.COMMIT: PolicyAction.DENY,
            },
        ),
        PolicyRule(
            id="remediated-threat",
            statuses=frozenset({ThreatStatus.REMEDIATED}),
            severities=frozenset(ThreatSeverity),
            actions={
                PolicyStage.RETRIEVAL: PolicyAction.SANDBOX,
                PolicyStage.EXTRACTION: PolicyAction.SANDBOX,
                PolicyStage.COMMIT: PolicyAction.REQUIRE_APPROVAL,
            },
        ),
    ),
)


class PolicyEngine:
    """A deterministic reference monitor. It never reads source prose."""

    def __init__(self, config: PolicyConfig | None = None) -> None:
        self.config = config or _DEFAULT_CONFIG
        self.version = f"source-policy/{self.config.version}"

    @classmethod
    def from_yaml(cls, path: Path) -> PolicyEngine:
        value = yaml.safe_load(path.read_text())
        return cls(PolicyConfig.model_validate(value))

    def evaluate(
        self,
        *,
        target: ThreatTarget,
        workflow_id: str,
        stage: PolicyStage,
        observations: Iterable[ThreatObservation],
    ) -> PolicyDecision:
        relevant = tuple(
            observation
            for observation in observations
            if observation.target.source_version == target.source_version
            and observation.status is not ThreatStatus.FALSE_POSITIVE
        )

        candidates: list[tuple[PolicyAction, str]] = [
            (self.config.default_actions[stage], "default-untrusted")
        ]
        for observation in relevant:
            for rule in self.config.rules:
                if observation.status in rule.statuses and observation.severity in rule.severities:
                    candidates.append((rule.actions[stage], rule.id))

        action, _ = max(candidates, key=lambda item: _ACTION_PRIORITY[item[0]])
        rule_ids = tuple(
            sorted({rule for candidate_action, rule in candidates if candidate_action is action})
        )
        payload = {
            "target": target.model_dump(mode="json"),
            "workflow_id": workflow_id,
            "stage": stage,
            "action": action,
            "rules": rule_ids,
            "observations": sorted(item.id for item in relevant),
            "engine_version": self.version,
        }
        return PolicyDecision(
            id=content_id("policy-decision", payload),
            target=target,
            workflow_id=workflow_id,
            stage=stage,
            action=action,
            rule_ids=rule_ids,
            observation_ids=tuple(sorted(item.id for item in relevant)),
            decided_at=utc_now(),
            engine_version=self.version,
        )
