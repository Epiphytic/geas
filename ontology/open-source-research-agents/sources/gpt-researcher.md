# GPT Researcher source card

Observed on 2026-08-29 at default-branch commit 6f998577d547b1e54ec662dac63583aa11e3b84b.

The official repository describes GPT Researcher as an open deep-research agent for web and local research.

Its documented architecture separates a planner, parallel execution agents, source tracking, and a publisher that produces a research report.

The project documents recursive tree-like exploration with configurable breadth and depth.

At the observed commit, the retriever package includes OpenAlex and PubMed Central adapters, and the v3.6.1 release notes document scholarly-retriever configuration.

The observed release also adds URL-security handling and regression tests for unsafe retrieved destinations.

The repository identifies its software license as Apache-2.0.

The documented durable output is a cited report; this source card did not verify a maintained claim-level ontology.

## References

- https://github.com/assafelovic/gpt-researcher
- https://github.com/assafelovic/gpt-researcher/commit/6f998577d547b1e54ec662dac63583aa11e3b84b
- https://github.com/assafelovic/gpt-researcher/releases/tag/v3.6.1
- https://github.com/assafelovic/gpt-researcher/tree/6f998577d547b1e54ec662dac63583aa11e3b84b/gpt_researcher/retrievers
- https://github.com/assafelovic/gpt-researcher/blob/6f998577d547b1e54ec662dac63583aa11e3b84b/gpt_researcher/utils/url_security.py
