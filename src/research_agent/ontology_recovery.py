"""Central recovery boundary for managed ontology removals."""

from __future__ import annotations

from research_agent.ontology_subscriptions import recover_subscription_removals
from research_agent.ontology_trust import recover_snapshot_removals
from research_agent.user_config import UserConfigManager


def recover_managed_removals(manager: UserConfigManager) -> None:
    """Recover every removal kind before catalog listing or operational use."""
    recover_snapshot_removals(manager)
    recover_subscription_removals(manager)
