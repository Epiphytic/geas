from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from research_agent.models import (
    Detector,
    DetectorKind,
    PolicyAction,
    PolicyStage,
    ThreatObservation,
    ThreatSeverity,
    ThreatStatus,
    ThreatTarget,
)
from research_agent.policy import PolicyConfig, PolicyEngine


def _observation(status: ThreatStatus, severity: ThreatSeverity) -> ThreatObservation:
    return ThreatObservation(
        id=f"threat:{status}:{severity}",
        target=ThreatTarget(source_version="source:1"),
        threat_type="threat:indirect-prompt-injection",
        status=status,
        detected_at=datetime.now(UTC),
        detector=Detector(kind=DetectorKind.DETERMINISTIC_RULE, id="rule:1"),
        evidence=("fragment:1",),
        severity=severity,
    )


def test_confirmed_high_threat_denies_extraction() -> None:
    decision = PolicyEngine().evaluate(
        target=ThreatTarget(source_version="source:1"),
        workflow_id="workflow:1",
        stage=PolicyStage.EXTRACTION,
        observations=[_observation(ThreatStatus.CONFIRMED, ThreatSeverity.HIGH)],
    )
    assert decision.action is PolicyAction.DENY
    assert decision.rule_ids == ("confirmed-high-threat",)


def test_false_positive_does_not_weaken_default_sandbox() -> None:
    decision = PolicyEngine().evaluate(
        target=ThreatTarget(source_version="source:1"),
        workflow_id="workflow:1",
        stage=PolicyStage.RETRIEVAL,
        observations=[_observation(ThreatStatus.FALSE_POSITIVE, ThreatSeverity.HIGH)],
    )
    assert decision.action is PolicyAction.SANDBOX


def test_unrelated_observation_is_ignored() -> None:
    observation = _observation(ThreatStatus.CONFIRMED, ThreatSeverity.CRITICAL)
    decision = PolicyEngine().evaluate(
        target=ThreatTarget(source_version="source:other"),
        workflow_id="workflow:1",
        stage=PolicyStage.EXTRACTION,
        observations=[observation],
    )
    assert decision.action is PolicyAction.SANDBOX


def test_checked_in_policy_matches_reference_behavior() -> None:
    engine = PolicyEngine.from_yaml(Path("config/source-policy.yaml"))
    decision = engine.evaluate(
        target=ThreatTarget(source_version="source:1"),
        workflow_id="workflow:1",
        stage=PolicyStage.EXTRACTION,
        observations=[_observation(ThreatStatus.CONFIRMED, ThreatSeverity.HIGH)],
    )
    assert decision.action is PolicyAction.DENY


def test_policy_requires_explicit_action_for_every_stage() -> None:
    with pytest.raises(ValidationError, match="default policy has no action"):
        PolicyConfig.model_validate(
            {
                "version": 1,
                "default_actions": {"retrieval": "sandbox"},
                "rules": [],
            }
        )
