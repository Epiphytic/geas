# Persistent, ontology-backed research agents

**Investigation date:** 2026-08-02

**Goal:** A broad, precise, searchable, continuously maintainable understanding
of a topic, including provenance, uncertainty, dissent, freshness, and
non-web sources.

## Executive conclusion

No current open-source system fully satisfies the goal.

The closest components solve different slices:

- **Co-STORM** is the closest model for breadth-first topic exploration and a
  hierarchical “mind map,” but its hierarchy is an LLM-curated workspace, not a
  formally validated, temporal ontology.
- **Graphiti** is the closest off-the-shelf dynamic agent memory. It has
  incremental updates, provenance episodes, custom types, temporal validity, and
  graph traversal, but its core representation is an automatically extracted
  property graph and its default retrieval is still hybrid vector/BM25/graph
  retrieval.
- **OntoGPT** is the best mature component for schema-constrained, ontology-
  grounded extraction. It is an extraction tool rather than a complete curation,
  search, and maintenance system.
- **PaperQA2** and **OpenScholar** demonstrate strong scientific retrieval and
  evidence synthesis, but their primary artifact is an answer or review rather
  than an evolving, formal knowledge substrate.
- **Microsoft GraphRAG** and **HippoRAG 2** improve breadth and multi-hop
  retrieval, respectively, but remain retrieval architectures. They do not
  provide the epistemic and ontology-governance model required here.
- **Open Knowledge Format (OKF)** is a particularly relevant new presentation
  format: Markdown plus YAML frontmatter, Git-friendly hierarchy, provenance,
  freshness, and progressive disclosure. It is an excellent *projection* for
  agents, but is not by itself a sufficiently formal claim store.
- **Semble** is useful as an optional local agent-facing search index over the
  generated Markdown. It is not a non-vector alternative: it combines static
  embeddings and BM25 with reciprocal-rank fusion.

The recommended design is therefore a small composition, not a bet on one
framework:

1. A **content-addressed source archive** retains raw evidence and versions.
2. A **claim-centric, bi-temporal knowledge graph** records what each source
   says without prematurely declaring it universally true.
3. A separately versioned **ontology** defines concepts, relations, constraints,
   aliases, and the hierarchical topic map.
4. **Deterministic query engines**—SPARQL, graph traversal, exact lookup, and
   BM25/full-text—provide the authoritative search path.
5. An **OKF-like Markdown bundle** is generated for progressive, agent-friendly
   reading and can optionally be indexed by Semble.
6. LLM workflows act as untrusted **research and curation proposal generators**.
   Schema validation, provenance rules, permissions, and review gates decide
   what is committed.

This is deliberately “RAG optional.” Embeddings may help discover candidate
sources or concepts, but no fact is accepted, rejected, or answered solely
because of vector proximity.

## 1. Clarifying the retrieval requirement

“RAG” is an architectural pattern, not one retrieval algorithm. Vector RAG
usually embeds chunks into a lossy numerical representation and ranks by
similarity. BM25 also ranks, but it operates on an inspectable inverted index.
SPARQL, SQL, and graph traversal evaluate explicit predicates and paths.

Semble itself splits files into chunks, scores them using static Model2Vec
embeddings and BM25, fuses the rankings, and applies code-aware signals
([implementation description](https://github.com/MinishLab/semble#how-it-works)).
It is fast, local, reproducible when its version and index configuration are
pinned, and agent-friendly through MCP. It is still embedding-based semantic
retrieval.

The useful requirement is therefore not “no quantization anywhere.” It is:

- canonical knowledge must not be a vector index;
- queries with exact semantics must have exact execution;
- lossy search may propose candidates but must not decide truth;
- every result must resolve to typed claims and primary evidence;
- ranking must expose its method and allow exhaustive or bounded-complete modes.

Arbitrary natural language cannot be mapped to a formal query with both complete
coverage and guaranteed deterministic semantics unless the language is
constrained. Offer three query modes:

1. **Typed tools** for reliable agent use, such as
   `claims_about(concept, predicate, as_of, stance)` and
   `controversies(topic)`.
2. **A controlled natural-language grammar** for common query templates,
   compiled deterministically to SPARQL.
3. **An LLM query compiler** for open-ended questions. It produces a typed query
   plan, never executes writes, and returns that plan with the result. Execution
   and authorization remain deterministic. Ambiguous plans produce alternatives
   or a clarification, not a guessed answer.

## 2. What the state of the art actually covers

### 2.1 Research and knowledge-curation agents

| System | Strongest contribution | Persistent artifact | Important limitation for this goal |
|---|---|---|---|
| [STORM / Co-STORM](https://github.com/stanford-oval/storm) | Multi-perspective questioning, broad research, dynamic hierarchical mind map, human collaboration | Outline/mind map, collected references, report | The hierarchy is generated curation, not a formal ontology with constraints, claim identity, temporal validity, or robust incremental merging |
| [PaperQA2](https://github.com/future-house/paper-qa) | Iterative scientific-paper search, metadata reconciliation, evidence gathering, contradiction-oriented scientific workflows | Local index, cached papers and answers | High-accuracy agentic RAG; does not maintain a general ontology or claim lifecycle |
| [OpenScholar](https://www.nature.com/articles/s41586-025-10072-4) | Large-scale scientific literature retrieval and cited synthesis; strong ScholarQABench results | Retrieval corpus and generated synthesis | Answer-centric and passage-retrieval-centric |
| Commercial/open deep-research agents | Long-horizon web search and report synthesis | Usually a report and citations | Weak persistence and reuse; dynamic web search makes reproduction difficult |

The research-agent frontier is strong at finding and synthesizing information,
but its benchmarks emphasize final-answer accuracy, report quality, and citation
accuracy. [BrowseComp](https://openai.com/index/browsecomp/) measures difficult
web fact finding, while
[DeepResearch Bench](https://arxiv.org/abs/2506.11763) evaluates PhD-level
reports and citation behavior. BrowseComp-Plus explicitly notes that live,
opaque web search harms reproducibility and introduces a fixed corpus to
separate retriever and agent quality
([paper](https://arxiv.org/abs/2508.06600)). These are valuable evaluations, but
they do not test whether a system maintains a coherent ontology for years.

### 2.2 Graph retrieval and agent memory

| System | Strongest contribution | Retrieval/update model | Important limitation for this goal |
|---|---|---|---|
| [Graphiti](https://github.com/getzep/graphiti) | Incremental temporal context graphs, provenance episodes, custom types, fact validity windows, MCP | Hybrid semantic, BM25, graph traversal; incremental LLM extraction and invalidation | Extraction and contradiction handling depend materially on models; property-graph facts are not a complete scholarly dissent model |
| [Microsoft GraphRAG](https://microsoft.github.io/graphrag/) | Entity graph plus hierarchical community detection and summaries for global questions | Batch indexing; community summaries and local/global search | Expensive LLM-derived summaries are lossy; primarily static and report/retrieval oriented |
| [HippoRAG / HippoRAG 2](https://github.com/OSU-NLP-Group/HippoRAG) | Associative, multi-hop retrieval using a graph and Personalized PageRank | Offline graph construction, online graph ranking and passage retrieval | Still RAG; entry-point extraction and graph construction remain model-dependent; not ontology governance |
| [Cognee](https://github.com/topoteretes/cognee) | Broad memory control plane combining graph/vector ingestion, ontology grounding, traceability, and isolation | Multi-store memory pipelines | Attractive integration layer, but less formal and inspectable than a standards-first claim ledger |

Graphiti is the most relevant implementation to study or reuse selectively. Its
README describes entities, temporal fact edges, provenance episodes, custom
types, learned or prescribed ontology, and hybrid retrieval. The associated
[Zep paper](https://arxiv.org/abs/2501.13956) formalizes bi-temporal agent
memory. However, “a newer edge invalidates an older edge” is not generally the
right rule for research. Two studies can disagree while both remain valid
reports of their methods and observations. Research claims need attribution,
scope, population, method, and explicit argument relations—not only a current
truth interval.

GraphRAG builds a knowledge graph, runs hierarchical community detection, and
uses LLMs to summarize communities
([dataflow](https://microsoft.github.io/graphrag/index/default_dataflow/)).
That hierarchy is useful for orientation, but generated summaries must be
treated as cached views rather than canonical knowledge.

HippoRAG’s contribution is structural retrieval: knowledge graph nodes and
Personalized PageRank improve associative multi-hop retrieval
([paper](https://arxiv.org/abs/2405.14831)). It is worth borrowing for an
optional “related evidence” ranking mode, but exact graph patterns should remain
available.

### 2.3 Ontology learning and graph construction

| System/research | Strongest contribution | Appropriate use here |
|---|---|---|
| [OntoGPT](https://github.com/monarch-initiative/ontogpt) / SPIRES | Structured extraction using LinkML templates and ontology grounding | Primary candidate for a schema-constrained extraction prototype |
| [KGGen](https://github.com/stair-lab/kg-gen) | General text-to-KG extraction and entity clustering; MINE benchmark | Discovery/proposal generation and extractor comparison, not direct canonical writes |
| [AutoSchemaKG](https://arxiv.org/abs/2505.23628) | Web-scale joint extraction and dynamic schema induction | Evidence that schema induction scales; proposals for ontology expansion, never silent schema mutation |
| [LLMs4OL](https://arxiv.org/abs/2409.10146) | Benchmarks ontology learning tasks with LLMs | Useful evaluation framing for taxonomy discovery and relation extraction |

The important architectural lesson is that ontology induction and ontology
governance are different operations. A model can identify repeated candidate
concepts or relations. It should not silently decide that two terms are
equivalent, change a relation’s meaning, or rewrite historical data under a new
schema.

### 2.4 Formal knowledge representation

The standards stack remains more suitable for the canonical layer than the
typical LLM knowledge-graph framework:

- **RDF datasets and named graphs** provide globally identified nodes and a way
  to separate graphs. RDF 1.2 adds triple terms, although it was still a W3C
  Candidate Recommendation at the investigation date
  ([RDF 1.2 Concepts](https://www.w3.org/TR/rdf12-concepts/)).
- **SPARQL** provides deterministic pattern, path, aggregation, and federated
  queries ([SPARQL 1.2](https://www.w3.org/TR/sparql12-query/)).
- **SHACL** validates RDF graph structure and constraints
  ([W3C Recommendation](https://www.w3.org/TR/shacl/)).
- **OWL 2** supplies formal vocabulary semantics and bounded reasoning profiles
  ([overview](https://www.w3.org/TR/owl2-overview/)). Use a tractable profile
  such as OWL 2 RL rather than unconstrained reasoning.
- **PROV-O** represents entities, activities, agents, and derivation
  ([W3C Recommendation](https://www.w3.org/TR/prov-o/)).
- **SKOS** is appropriate for the navigational concept hierarchy, preferred
  labels, aliases, broader/narrower links, and mappings.
- **Web Annotation** can identify exact text fragments or other regions that
  support a claim
  ([W3C model](https://www.w3.org/TR/annotation-model/)).
- **Nanopublication** provides a useful pattern: separate assertion, provenance,
  and publication-information graphs
  ([guidelines](https://nanopub.net/docs/)).
- **CiTO** types citation intent, including support, extension, agreement, and
  disagreement ([ontology](https://www.sparontologies.net/ontologies/cito)).
- **AIF** models information, inference, conflict, and preference nodes
  ([specification](https://www.arg-tech.org/wp-content/uploads/2011/09/aif-spec.pdf)).

[LinkML](https://linkml.io/) is a pragmatic authoring layer over this stack. A
single YAML schema can generate JSON Schema, Python/Pydantic classes, RDF,
JSON-LD, OWL, SHACL, SQL, TypeDB, and documentation artifacts. Its validation
model is explicit and machine-executable
([validation specification](https://linkml.io/linkml-model/latest/docs/specification/05validation/)).
It makes the canonical records pleasant to review in Git without giving up RDF
interoperability.

## 3. Recommended architecture

### 3.1 Core principle: store assertions, not “the truth”

The canonical graph must not directly assert every extracted triple as a
universally true fact. Instead, make `Claim` a first-class record.

```text
SourceVersion ──contains──> EvidenceFragment
                                 │
                                 ▼
Claim ──hasSubject──────────> Entity/Concept
  │   ──hasPredicate────────> Relation
  │   ──hasObject───────────> Entity/Value/Proposition
  │   ──supportedBy─────────> EvidenceFragment
  │   ──assertedBy──────────> Person/Organization/Source
  │   ──supports/attacks────> Claim
  │   ──scopedTo────────────> Context/Method/Population/Jurisdiction
  └── valid time + transaction time + epistemic status
```

A minimal claim record should include:

```yaml
id: claim:sha256:...
subject: concept:...
predicate: relation:...
object: concept:...        # or a typed literal / nested proposition
qualifiers:
  population: ...
  method: ...
  jurisdiction: ...
  conditions: ...
stance: asserts            # asserts | denies | questions | reports
epistemic_status: observed # observed | inferred | hypothesized | consensus
valid_from: null
valid_until: null
recorded_at: 2026-08-02T00:00:00Z
asserted_by: agent/person/org/source identifier
evidence:
  - source_version: source:sha256:...
    selector:
      type: text_quote
      exact: "short exact evidence span"
      prefix: "..."
      suffix: "..."
extraction:
  method: model-assisted
  model: pinned provider/model/version
  prompt_hash: sha256:...
  confidence: 0.82
review:
  state: proposed           # proposed | accepted | rejected | superseded
```

Keep extraction confidence separate from truth or source credibility. A model
can be highly confident that a source makes a poorly supported claim.

### 3.2 Five distinct layers

#### Layer A — source vault

Store immutable `SourceVersion` objects:

- original bytes or an allowed archival representation;
- content hash, MIME type, acquisition time, canonical locator, license;
- tool and connector used to retrieve it;
- source author/publisher and publication dates;
- parser version and derived plain text;
- trust-zone and access-control labels;
- predecessor/successor versions and retraction/correction state.

Web pages, PDFs, Git repositories, local files, email exports, databases, APIs,
Zotero libraries, lab instruments, and MCP tools all enter through the same
versioned envelope. Connectors do not write claims directly.

#### Layer B — claim and evidence ledger

Store atomic claims, exact evidence selectors, provenance, contexts, time, and
argument relations. Prefer append-only operations:

- corrections supersede records but do not erase history;
- source changes create a new `SourceVersion`;
- entity merges preserve aliases and tombstones;
- inferred claims are stored separately from source-asserted claims;
- generated summaries cite the exact claim IDs from which they were derived.

Use a partial open-world model. Absence usually means “unknown,” not “false.”
Where a collection is known complete—such as all releases in an authoritative
registry—attach an explicit completeness assertion. The distinction is
important; completeness and negation in open-world knowledge bases remain a
substantial research problem
([survey](https://arxiv.org/abs/2305.05403)).

#### Layer C — ontology and constraints

Version independently:

- stable IDs for concepts and relations;
- SKOS preferred labels, aliases, definitions, broader/narrower links;
- relation domain/range, inverse, symmetry, transitivity, and cardinality;
- allowed claim qualifiers by relation;
- entity-resolution rules;
- SHACL shapes and LinkML schema;
- migration rules between ontology versions;
- competency questions and their executable SPARQL tests.

Begin with a small upper schema:

`Topic`, `Concept`, `Entity`, `Event`, `Method`, `Artifact`, `Source`,
`SourceVersion`, `EvidenceFragment`, `Claim`, `Argument`, `Agent`,
`Organization`, `Place`, `TimeInterval`, and `ResearchQuestion`.

Let domain concepts grow under those types. Do not attempt a complete upper
ontology before the first useful queries work.

#### Layer D — deterministic indexes and query service

Build disposable projections from the canonical ledger:

- SPARQL index for graph patterns and property paths;
- exact ID, alias, DOI, URL, and identifier lookup;
- BM25/full-text index over labels, definitions, claims, and evidence;
- citation and argument graph traversal;
- optional PageRank/PPR for “related/important” discovery;
- optional embeddings for recall-oriented candidate discovery;
- generated topic pages and indexes.

Every query response should label its guarantee:

- `exact`: exhaustive over a declared structured predicate/path;
- `lexical-ranked`: deterministic top-k over a pinned index;
- `graph-ranked`: deterministic traversal/ranking over a declared snapshot;
- `semantic-candidate`: embedding-derived and potentially lossy;
- `synthesized`: model-generated from returned claim IDs.

The service should return structured results first and prose second:

```json
{
  "query_plan": {"kind": "controversies", "topic": "concept:..."},
  "snapshot": "kg:commit:...",
  "coverage": {"mode": "exhaustive", "matched_claims": 19},
  "claims": [],
  "evidence": [],
  "conflicts": [],
  "unknowns": [],
  "rendered_context": "bounded Markdown for an agent"
}
```

#### Layer E — agent-friendly knowledge bundle

Generate a hierarchical Markdown bundle inspired by
[OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf).
OKF’s useful properties are plain Markdown plus frontmatter, Git diffs, trust
and freshness fields, hierarchical indexes, and cross-links. Its authors
explicitly position it as agent- and human-readable with progressive disclosure.

Suggested layout:

```text
knowledge/
  index.md
  topics/<topic>/index.md
  topics/<topic>/concepts/<concept>.md
  topics/<topic>/questions/<question>.md
  topics/<topic>/controversies/<controversy>.md
  topics/<topic>/gaps.md
  sources/<source-id>.md
  ontology/
    schema.yaml
    shapes.ttl
    concepts/
  snapshots/<date>/...
```

Concept pages are materialized views, not hand-maintained truth. Each page
contains:

- definition and ontology position;
- aliases and identifiers;
- current supported claims;
- important historical claims;
- explicit disagreements and why they may differ;
- open questions and coverage gaps;
- source and freshness summary;
- stable claim IDs and exact query examples;
- links to children, parents, related concepts, methods, and evidence.

Semble can index this bundle for low-latency natural-language discovery. Because
it is a derived view, re-indexing or changing its model cannot alter the
canonical knowledge.

## 4. Research and maintenance workflows

Use agent *roles* with narrow permissions. They can initially be deterministic
workflows using one model; separate autonomous agents are not required.

### 4.1 Topic bootstrap

1. A user defines scope, exclusions, intended decisions, and initial competency
   questions.
2. The planner expands them into facets: definitions, history, mechanisms,
   methods, evidence, actors, alternatives, critiques, safety, regulation, and
   unresolved questions.
3. The scout searches multiple source classes and follows citation/reference
   trails.
4. Sources are archived and deduplicated.
5. The extractor proposes entities, claims, evidence spans, qualifiers, and
   candidate ontology terms.
6. Deterministic validation rejects malformed or ungrounded proposals.
7. The curator resolves identities and creates a reviewable graph patch.
8. Accepted patches update the canonical ledger and regenerate projections.

Co-STORM’s multi-perspective questioning is an excellent bootstrap strategy.
Use its outline as proposed competency questions and taxonomy branches, not as
the ontology itself.

### 4.2 Incremental update

For each monitored source:

1. Fetch metadata or use conditional requests/change streams.
2. Hash canonicalized content and create a new source version only when changed.
3. Diff old and new versions.
4. Re-extract only affected evidence regions plus enough surrounding context.
5. Propose new claims and explicit supersession/withdrawal links.
6. Re-run entity resolution, SHACL validation, and competency-query regression
   tests.
7. Commit a patch with a machine-readable change summary.
8. Invalidate and rebuild only affected materialized views and indexes.

Never delete all claims originating in the old version. Preserve what the old
source said and alter its current/retracted status.

### 4.3 Gap filling

“Complete” is meaningless without a declared scope. Represent a topic’s research
contract:

```yaml
topic: concept:...
facets: [definitions, mechanisms, evidence, alternatives, critiques]
required_source_classes:
  peer_reviewed: 5
  primary_documentation: 2
  critical_or_negative: 2
competency_questions:
  - id: cq:...
    question: "Which methods claim to solve X, under what assumptions?"
    query: queries/cq-001.rq
freshness:
  default_days: 90
  fast_moving_branches:
    - concept:...
```

The gap auditor computes:

- unanswered competency questions;
- ontology classes with no or few instances;
- relation/domain combinations expected by shapes but absent;
- concepts with only one source or one source family;
- claims with no primary evidence;
- claims supported only by generated or secondary material;
- branches whose freshness deadline has expired;
- entities with unresolved identity candidates;
- controversies represented by only one side;
- unexplained high-centrality nodes or disconnected components;
- search queries that repeatedly return no exact results;
- source-citation frontiers not yet traversed.

An agent can then be prompted: “Fill gaps with highest decision value and lowest
evidence diversity.” It receives explicit gap records, a search budget, allowed
source types, and no direct canonical-write permission.

### 4.4 Frontier refresh

Maintain standing queries per concept and source type:

- RSS/Atom and release feeds;
- Crossref, OpenAlex, Semantic Scholar, PubMed, arXiv, and domain registries;
- Git tags/releases/issues;
- standards organizations and regulator feeds;
- saved web queries;
- database change streams and APIs;
- watched filesystem, Zotero, and document-management collections.

Rank candidates using deterministic features where possible: date, source class,
citation link, ontology/entity match, lexical match, and novelty of identifiers.
An LLM may classify relevance after retrieval, but its decision is logged and
reversible.

## 5. Dissent, controversy, and uncertainty

A controversy is not just two opposite triples. Often studies differ in:

- population or sample;
- operational definition;
- baseline and comparator;
- method or measurement;
- time period or jurisdiction;
- causal versus correlational wording;
- evidentiary standard;
- value judgment rather than empirical observation.

Represent at least these relations between claims:

`supports`, `partially_supports`, `attacks`, `contradicts`, `qualifies`,
`refines`, `uses_method_from`, `replicates`, `fails_to_replicate`,
`supersedes`, `retracts`, and `independent_of`.

Conflict detection should be a proposal pipeline:

1. deterministically group claims with the same normalized
   subject/predicate/context dimensions;
2. detect polarity, incompatible values, or mutually exclusive ontology terms;
3. use an LLM or rules to propose an argument relation and explain the mismatch;
4. require exact evidence on both sides;
5. preserve the unresolved cluster even when no adjudication is possible.

Render a controversy with:

- the neutral question;
- each position as attributed claims;
- best direct evidence and strongest counterevidence;
- scope/method differences;
- source independence and possible conflicts of interest;
- current consensus *as another attributed claim*;
- what observation would discriminate between positions;
- last search date and known coverage limitations.

This follows the spirit of nanopublications, CiTO, and AIF. It also avoids a
known weakness of ordinary RDF assertions: placing a disputed triple in the
base graph can accidentally make it available for entailment as though the
system endorses it. Claim content should be quoted/reified or isolated in an
assertion graph and only promoted into a “currently accepted” view under an
explicit policy.

## 6. Basic resistance to tampering and prompt injection

External content is hostile data, not an instruction channel. This must be an
architectural rule, not only a prompt.

NIST describes agent hijacking as a failure to separate trusted instructions
from untrusted external data
([technical note](https://www.nist.gov/news-events/news/2025/01/technical-blog-strengthening-ai-agent-hijacking-evaluations)).
[CaMeL](https://arxiv.org/abs/2503.18813) goes further by extracting control and
data flows from the trusted query so retrieved content cannot alter program
flow. OWASP cautions that there is no known foolproof prompt-only prevention
([LLM01](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)).

Minimum controls:

- **Control/data separation:** research plans come only from a trusted task
  object. Retrieved bytes are always labeled `untrusted_content`.
- **Least privilege:** the crawler can fetch but cannot write claims; the
  extractor sees content but has no network, secrets, shell, or database-write
  capability; the committer sees typed proposals, not raw hostile prose.
- **Typed intermediate representation:** models emit a closed schema. Unknown
  fields, tool requests embedded in data, and ontology terms outside the allowed
  set are rejected.
- **No model-generated arbitrary queries for writes:** use parameterized
  operations or a small patch language; enforce graph and tenant scope.
- **Staging and review:** all LLM changes are proposals in a quarantine graph or
  Git branch. SHACL/LinkML validation, provenance checks, and policy tests gate
  merge.
- **Source integrity:** content hashes, acquisition metadata, version links,
  optional signatures, and an append-only audit log.
- **Taint propagation:** claims and summaries retain the trust classification
  of their sources. Generated text never becomes a trusted instruction.
- **Action firewall:** tool calls must match the user task, allowlist, and
  capability token. High-impact external actions require independent approval.
- **Output encoding:** escape content for Markdown, HTML, SQL, SPARQL, shell, and
  downstream agents. Do not let a stored string become executable syntax.
- **Supply-chain controls:** pin connectors, MCP servers, parsers, prompts,
  models, and ontology versions; restrict egress and credentials.
- **Memory hygiene:** injection-like content may be archived as evidence but is
  never copied into system prompts or durable agent instructions.
- **Poisoning registry:** detected or suspected hostile content is recorded as
  attributed, reviewable threat intelligence against the exact source version,
  fragment, connector, or observed behavior. The registry is queried by the
  deterministic policy engine before retrieval, extraction, or commit.
- **Adversarial tests:** seed web pages, PDFs, metadata fields, tool
  descriptions, and source documents with indirect injections and verify that
  no unauthorized control-flow or write occurs.

Prompt marking or “ignore instructions in sources” remains useful defense in
depth. Microsoft’s spotlighting experiments reduced attack success
substantially in their setting
([paper](https://www.microsoft.com/en-us/research/publication/defending-against-indirect-prompt-injection-attacks-with-spotlighting/)),
but marking should not be the authorization boundary.

### Poisoned-source and hostile-content registry

Maintain poisoning information as part of the knowledge model, but keep it in a
security-controlled named graph rather than mixing it into ordinary topic
assertions. A source is not simply `poisoned: true`. Record a first-class
`ThreatObservation` with:

```yaml
id: threat:sha256:...
target:
  source_version: source:sha256:...
  evidence_fragment: fragment:sha256:...  # optional
threat_type: indirect_prompt_injection
status: suspected            # suspected | confirmed | remediated | false_positive
detected_at: 2026-08-02T00:00:00Z
detector:
  kind: deterministic_rule   # deterministic_rule | sandbox_observation | human | model
  id: detector:...
evidence:
  - fragment: fragment:sha256:...
observed_behavior:
  attempted_action: "..."
  policy_rule: policy:no-untrusted-tool-instructions
severity: high
scope:
  affects_versions: [source:sha256:...]
  affects_connector: null
review:
  state: confirmed
  reviewer: person:...
supersedes: null
```

Separate four concepts:

- `ThreatObservation`: attributed evidence that suspicious behavior or content
  was observed.
- `ThreatAssessment`: a policy or reviewer conclusion based on one or more
  observations.
- `SourceReputation`: a derived, time-scoped view; never the only stored fact.
- `PolicyDecision`: the deterministic action taken for a particular workflow,
  such as `allow_metadata_only`, `sandbox`, `quarantine`, or `deny`.

This avoids permanently condemning a domain because one page, version, comment,
advertisement, dependency, or compromised connector was hostile. It also allows
the system to represent dissent between detectors: one assessment may mark a
fragment as injection while another records a false positive, with both linked
to their evidence.

The policy engine—not an agent—evaluates the registry before every boundary:

1. Resolve the exact content hash, source version, connector, and trust zone.
2. Execute fixed policy rules over confirmed and suspected assessments.
3. Restrict capabilities before any model sees the content.
4. Record the resulting policy decision in the audit log.
5. Require a separately authorized process to change an assessment or override
   a quarantine.

Never let content clear its own status, and never let a model that read the
content approve an override. Model-based detectors may create only `suspected`
observations. Confirmation requires a deterministic behavioral signal or an
authorized human review. Raw injection text remains quarantined evidence and is
not reproduced in agent-facing concept pages; those pages receive a safe,
structured threat summary and stable observation IDs.

## 7. Concrete technology choices

### Recommended MVP stack

| Concern | Choice | Why |
|---|---|---|
| Canonical authoring schema | LinkML in YAML | Reviewable, generates validators and semantic artifacts |
| Canonical records | Git-versioned YAML/JSONL plus immutable source blobs | Transparent diffs, rollback, no database lock-in |
| Semantic projection | RDF datasets/named graphs | Mature provenance and ontology standards; SPARQL |
| Embedded graph query | Oxigraph or another standards-compliant RDF store | Local, deterministic SPARQL; easy rebuild from canonical records |
| Constraints | LinkML validator + SHACL | Reject malformed claims and graph patches |
| Hierarchy | SKOS concepts plus generated directory indexes | Polyhierarchy without pretending the domain is a strict tree |
| Exact/lexical retrieval | SQLite FTS5 or Tantivy BM25 plus exact identifier tables | Local, inspectable, reproducible, fast |
| Agent view | OKF-inspired Markdown bundle | Progressive disclosure and broad tool compatibility |
| Optional discovery | Semble over the Markdown bundle | Excellent low-token MCP interface; derived, not authoritative |
| Research orchestration | Explicit state machine/work queue | Reproducible, resumable, narrow permissions |
| Extraction | OntoGPT-style structured extraction | Ontology grounding and typed output |
| API to agents | MCP plus ordinary HTTP/CLI | Typed tools for agents, simple debugging for humans |

For a larger shared deployment, move immutable blobs to object storage, canonical
records to PostgreSQL or a ledgered event store, and SPARQL to a production
triplestore. Keep the same schemas and export format.

### What not to use as the sole substrate

- **A vector database:** cannot express exact graph constraints, provenance, or
  exhaustive conflict queries.
- **Plain Markdown only:** excellent for reading and Git, weak for invariants,
  atomic claims, joins, and concurrent updates.
- **A property graph only:** pragmatic and fast, but makes standards-based
  provenance and ontology exchange harder. It is reasonable if Graphiti reuse
  is more important than semantic-web interoperability.
- **Generated community summaries as truth:** useful caches, but lossy and hard
  to update locally.
- **Automatic ontology induction with direct writes:** creates semantic drift
  that later queries cannot reliably interpret.
- **One highly privileged “research agent”:** unnecessarily combines hostile
  content, secrets, network access, and durable memory writes.

## 8. Agent-facing query contract

Prefer several precise tools over one vague `search`:

```text
resolve_concept(text, ontology_version)
describe_concept(id, depth, as_of, include_disputed)
find_claims(subject?, predicate?, object?, qualifiers?, status?, as_of?)
trace_claim(claim_id)
find_evidence(claim_id, primary_only?, independent_sources?)
find_paths(from_id, to_id, allowed_relations, max_hops)
list_children(concept_id, depth)
list_controversies(topic_id, unresolved_only?)
compare_positions(controversy_id)
list_threat_observations(target_id?, threat_type?, status?)
get_source_policy(source_version_id, workflow_id)
list_gaps(topic_id, gap_type?, priority?)
search_lexical(query, fields, filters, top_k)
search_candidates_semantic(query, filters, top_k)  # explicitly lossy
compile_question(question)                         # returns plan, no execution
execute_read_plan(validated_plan)
propose_research(gap_ids, budgets, source_policy)
propose_patch(extraction_batch_id)
```

Every response includes snapshot ID, ontology version, query mode, truncation,
result count, provenance, and freshness. If a tool returns top-k rather than all
matches, that fact must be machine-readable.

## 9. Implementation sequence

### Phase 0 — one-week spike

- Pick one bounded, contested, fast-changing topic.
- Write 20–30 competency questions.
- Define the minimal LinkML schema for sources, evidence, claims, concepts,
  arguments, and gaps.
- Hand-curate 30 sources and 100 claims.
- Generate Markdown concept and controversy pages.
- Demonstrate exact SPARQL, BM25, and agent-tool queries.

**Exit criterion:** an independent agent answers ten queries using only typed
tools, cites exact evidence, distinguishes unknown from false, and exposes both
sides of at least two controversies.

### Phase 1 — ingestion and proposal pipeline

- Add web, PDF, filesystem, Git, Zotero, and one structured API connector.
- Implement content hashing, parsing, evidence selectors, and version diffs.
- Add schema-constrained extraction and entity-resolution proposals.
- Gate every patch with LinkML/SHACL and provenance tests.

**Exit criterion:** re-ingesting unchanged sources is idempotent; changed sources
produce a small reviewable diff; no claim without evidence enters accepted
state.

### Phase 2 — maintenance and gaps

- Add competency-question regression queries.
- Materialize gap records and coverage dashboards.
- Add standing searches and per-branch freshness deadlines.
- Add claim-conflict proposal and controversy rendering.

**Exit criterion:** the system can explain what it does not know, why a branch is
stale, and what search would most improve coverage.

### Phase 3 — security and evaluation

- Split crawler, extractor, curator, query, and committer capabilities.
- Add taint labels, egress policies, staged commits, and audit events.
- Build an indirect prompt-injection test corpus.
- Measure claim precision/recall, evidence entailment, entity resolution,
  ontology consistency, query completeness, freshness, and controversy balance.

**Exit criterion:** hostile source content cannot cause a tool call or canonical
write outside the trusted workflow; all accepted records have replayable
lineage.

### Phase 4 — scale only after quality

- Introduce queues, incremental materialized views, shared storage, and
  concurrency control.
- Benchmark a production triplestore versus the embedded store.
- Add optional semantic candidate discovery and PPR only where measured recall
  improves.

## 10. Evaluation framework

Do not evaluate only final answer quality. Maintain gold sets and regression
tests at each boundary:

| Layer | Metrics |
|---|---|
| Source discovery | recall at budget, primary-source share, source diversity, frontier coverage |
| Parsing | text/table/metadata fidelity, selector stability across versions |
| Extraction | entity/relation precision and recall, evidence entailment, qualifier accuracy |
| Entity resolution | merge precision, split recall, unresolved rate |
| Ontology | competency-question coverage, SHACL conformance, consistency, orphan and cycle checks |
| Retrieval | exact-query completeness, BM25 nDCG/recall, multi-hop path recall, latency, reproducibility |
| Epistemics | unsupported-claim rate, conflict precision/recall, controversy-side coverage, calibration |
| Maintenance | staleness, time-to-detect, time-to-integrate, idempotence, diff locality |
| Agent output | citation correctness, claim-ID coverage, unsupported synthesis rate |
| Security | unauthorized action/write rate under injection, taint-loss rate, secret exposure, benign utility |

Evaluate against snapshot-fixed corpora as well as live-source runs. Snapshot
tests make retrieval regressions reproducible; live runs test freshness and
connector behavior.

## 11. Key design decisions

1. **Use a polyhierarchy, not a tree.** A directory gives one convenient
   navigation path; SKOS broader/narrower and typed cross-links retain the actual
   graph.
2. **Claims are immutable observations about what was asserted.** Acceptance,
   validity, and consensus are separate mutable views.
3. **The ontology is code.** Version it, test it, review it, and migrate data
   explicitly.
4. **Generated prose is a cache.** It must be reproducible from claim IDs and
   can always be discarded.
5. **Natural-language query planning is optional and inspectable.** Typed query
   tools are the stable interface between agents and knowledge.
6. **Completeness is scoped metadata.** Never imply that an open-world topic is
   globally complete.
7. **Multiple narrow roles beat a privileged swarm.** Agent count is not a
   measure of research quality; separation of duties improves security and
   auditability.
8. **Use embeddings only as an additional recall channel.** They should neither
   define ontology membership nor adjudicate claims.

## 12. Bottom line

The target system is best understood as a **version-controlled epistemic
database with research agents attached**, not as a RAG application.

The most promising synthesis is:

- Co-STORM-like perspective and gap exploration;
- OntoGPT-like typed extraction;
- Graphiti-like temporal and episodic provenance, adapted so dissent is
  preserved rather than overwritten;
- LinkML + RDF/SPARQL + SHACL for explicit semantics and validation;
- nanopublication/AIF/CiTO-inspired claim and controversy modeling;
- an OKF-like Markdown hierarchy for agent consumption;
- BM25, exact lookup, and graph queries as authoritative retrieval;
- Semble or embeddings as optional discovery indexes;
- CaMeL-inspired separation of trusted control flow from hostile content.

That combination directly supports persistent expansion, precise search,
historical queries, gap-driven research, non-web tools, controversy, and basic
tamper resistance while keeping every important decision inspectable and
reversible.
