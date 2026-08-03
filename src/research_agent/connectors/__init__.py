"""Narrow discovery and acquisition connectors."""

from research_agent.connectors.crossref import CrossrefDiscoveryConnector
from research_agent.connectors.local_file import LocalFileConnector
from research_agent.connectors.mojeek import MojeekDiscoveryConnector
from research_agent.connectors.openalex import OpenAlexDiscoveryConnector

__all__ = [
    "CrossrefDiscoveryConnector",
    "LocalFileConnector",
    "MojeekDiscoveryConnector",
    "OpenAlexDiscoveryConnector",
]
