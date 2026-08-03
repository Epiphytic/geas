"""Narrow discovery and acquisition connectors."""

from research_agent.connectors.crossref import CrossrefDiscoveryConnector
from research_agent.connectors.europe_pmc import EuropePmcDiscoveryConnector
from research_agent.connectors.local_file import LocalFileConnector
from research_agent.connectors.mojeek import MojeekDiscoveryConnector
from research_agent.connectors.openalex import OpenAlexDiscoveryConnector
from research_agent.connectors.unpaywall import UnpaywallResolver

__all__ = [
    "CrossrefDiscoveryConnector",
    "EuropePmcDiscoveryConnector",
    "LocalFileConnector",
    "MojeekDiscoveryConnector",
    "OpenAlexDiscoveryConnector",
    "UnpaywallResolver",
]
