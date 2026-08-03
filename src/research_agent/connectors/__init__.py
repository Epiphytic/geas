"""Narrow discovery and acquisition connectors."""

from research_agent.connectors.crossref import CrossrefDiscoveryConnector
from research_agent.connectors.local_file import LocalFileConnector
from research_agent.connectors.mojeek import MojeekDiscoveryConnector

__all__ = [
    "CrossrefDiscoveryConnector",
    "LocalFileConnector",
    "MojeekDiscoveryConnector",
]
