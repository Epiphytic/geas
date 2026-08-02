# Maintained intelligence on hostile or misleading information sources

**Reviewed:** 2026-08-02

There is no maintained, open, neutral repository that can safely answer “is
this source poisoned?” as one permanent boolean. The useful sources divide into
behavior vocabularies, incident and fact-check exchanges, technical threat
feeds, community assessments, and regulatory findings.

The machine-readable inventory is
[`intelligence/sources.yaml`](../intelligence/sources.yaml). Every connector
must import upstream entries as attributed `ThreatObservation` or `Claim`
records. A feed match is never itself a canonical policy decision.

## Highest-value integrations

1. **DISARM Framework** — the strongest open, maintained vocabulary for
   influence-operation tactics and techniques. It is CC-BY-SA and designed for
   interoperable intelligence, but is not a domain list.
2. **DISINFOX** — an actively developed STIX2/DISARM exchange platform. It is a
   promising incident connector when a governed API instance and data license
   are available.
3. **Data Commons ClaimReview feed** — structured, attributed fact-check
   records. The feed is updated daily, but rating scales and publisher judgments
   must remain separate.
4. **URLhaus and PhishTank** — strong technical evidence about exact malicious
   or phishing URLs. Scope observations to URL and source version because
   legitimate hosts can be compromised.
5. **FDA and FTC collections** — primary regulatory evidence for misleading
   health and marketing claims. Preserve whether a record is an allegation,
   warning, settlement, final order, remediation, or closed matter.

## Useful secondary integrations

- MISP taxonomies and warning lists provide interoperability and
  false-positive context.
- Spamhaus DBL provides time-sensitive domain reputation categories under
  explicit fair-use terms.
- uBlock Origin uAssets and The Block List Project provide actively maintained
  technical filter observations.
- Wikipedia’s Perennial Sources list is an unusually active, versioned
  community source assessment. It is valuable evidence of editorial consensus
  and controversy, not an authorization rule.
- Open Feedback provides a relevant source/claim review graph, but automated
  access requires agreement with the provider.

## Sources that should not drive live blocking

Many “fake news source” repositories on GitHub are static datasets built for
machine-learning papers. Their labels collapse satire, clickbait, political
bias, fabrication, and low-quality reporting, often without current status or
appeal history. They are useful test fixtures, not live intelligence.

Likewise, the `misinformation-website-label` entry in MISP’s actively maintained
taxonomy repository derives from a 2019 domain list. Maintenance of the wrapper
does not make the underlying judgments current.

## Required connector behavior

Each imported item must retain:

- upstream source and stable upstream identifier;
- retrieval time and upstream publication/verification time;
- exact URL, domain, source version, or fragment scope;
- upstream category and original textual rating;
- license and access terms;
- upstream evidence and appeal/remediation links;
- connector version and content hash;
- expiration or next-refresh time.

Connectors may create observations only. A deterministic policy engine evaluates
those observations at workflow boundaries. Model classifiers may add
`suspected` observations but cannot confirm a threat, change workflow state, or
lift quarantine.

## Primary references

- [DISARM Red Framework](https://www.disarm.foundation/framework)
- [DISARM master repository](https://github.com/DISARMFoundation/DISARMframeworks)
- [DISINFOX](https://github.com/CyberDataLab/disinfox)
- [Data Commons fact-check download](https://datacommons.org/factcheck/download)
- [Google Fact Check Tools API](https://developers.google.com/fact-check/tools/api/)
- [Open Feedback](https://open.feedback.org/)
- [MISP taxonomies](https://github.com/MISP/misp-taxonomies)
- [MISP warning lists](https://misp.github.io/misp-warninglists/)
- [URLhaus API](https://urlhaus.abuse.ch/api/)
- [PhishTank developer feed](https://phishtank.org/developer_info.php)
- [Spamhaus DBL](https://www.spamhaus.org/blocklists/domain-blocklist/)
- [uBlock Origin uAssets](https://github.com/uBlockOrigin/uAssets)
- [FDA Health Fraud Product Database](https://www.fda.gov/consumers/health-fraud-scams/health-fraud-product-database)
- [FTC Cases and Proceedings](https://www.ftc.gov/legal-library/browse/cases-proceedings)
