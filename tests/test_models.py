from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from research_agent.models import (
    Detector,
    DetectorKind,
    ThreatObservation,
    ThreatSeverity,
    ThreatStatus,
    ThreatTarget,
)


def test_model_detector_cannot_confirm_threat() -> None:
    with pytest.raises(ValidationError, match="model detectors may create suspected"):
        ThreatObservation(
            id="threat:1",
            target=ThreatTarget(source_version="source:1"),
            threat_type="threat:indirect-prompt-injection",
            status=ThreatStatus.CONFIRMED,
            detected_at=datetime.now(UTC),
            detector=Detector(kind=DetectorKind.MODEL, id="model:local"),
            evidence=("fragment:1",),
            severity=ThreatSeverity.HIGH,
        )


def test_external_feed_can_create_suspected_observation() -> None:
    observation = ThreatObservation(
        id="threat:1",
        target=ThreatTarget(source_version="source:1"),
        threat_type="threat:phishing",
        status=ThreatStatus.SUSPECTED,
        detected_at=datetime.now(UTC),
        detector=Detector(kind=DetectorKind.EXTERNAL_FEED, id="feed:phishtank"),
        evidence=("feed-item:1",),
        severity=ThreatSeverity.HIGH,
    )
    assert observation.status is ThreatStatus.SUSPECTED
