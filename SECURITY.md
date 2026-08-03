# Security model

This project treats all retrieved content and all model output as untrusted data.
No prompt, classifier, critic model, or reviewer agent is an authorization
boundary.

## Hard invariants

- Workflow transitions are defined in code and cannot be selected by a model.
- Models receive no shell, network, secret, approval, or database-write tools.
- A model cannot confirm a threat observation or lift quarantine.
- Only typed, validated, content-addressed artifacts cross process boundaries.
- The policy engine reads structured observations, never hostile source prose.
- The committer is deterministic and accepts only an approved immutable patch.
- Source content cannot supply tool names, destinations, capabilities, policy
  rules, credentials, or approval tokens.
- Query proposals reject undeclared connectors, concepts, capabilities, and
  fields; configured limits clamp model-proposed budgets.
- Local discovery and acquisition are confined to resolved operator-selected
  roots and do not follow symlinks outside them.
- Connector manifests are trusted code/configuration, while snippets and
  connector results remain untrusted records.
- Secret environment files and editor swap files are ignored. The deterministic
  loader reads only explicitly allowlisted variable names and performs no shell
  evaluation or interpolation.
- The Mojeek transport has a fixed HTTPS destination. Although Mojeek requires
  its key as a query parameter, full request URLs and upstream error bodies are
  never placed in audit records or propagated through connector exceptions.
- The Unpaywall project contact is treated like a credential: it is injected
  only by the fixed-host transport and never persisted. Returned OA URLs are
  rejected if private, local, credential-bearing, or non-HTTP(S), and vague
  license classes cannot authorize automatic acquisition.
- Remote acquisition is HTTPS-only, resolves and pins a public IPv4 address,
  disables proxy inheritance and automatic redirects, and independently
  validates every redirect. Original bytes remain quarantined. Versioned
  parsers emit inert text with size/time bounds; parsing never upgrades trust.
- Native document parsing is fail-closed behind Bubblewrap with fresh
  namespaces, no network route or content-path mounts, a cleared environment,
  dropped capabilities, closed inherited descriptors, and resource limits.
  Acquired bytes cross only through stdin. Missing host namespace support causes
  an explicit parser failure; it never enables an unsandboxed fallback.
- Structural extraction is deterministic local code over inert derived text.
  It emits typed, content-addressed ranges and cannot invoke models, tools,
  network access, secrets, or policy transitions. SQLite rebuilds independently
  verify every anchor range and exact-text hash before indexing it. Anchor
  search hits include quarantine state and deterministic threat-observation
  IDs; lexical relevance cannot silently strip poisoning context.
- Citation extraction is deterministic and tool-free. Identifier text can only
  create typed exact-range records; it cannot initiate network access, select a
  connector, establish an authoritative retraction, or mutate a claim. Exact
  projection joins use normalized identifiers or canonical locators. Fuzzy
  entity matches are never silently accepted.
- Maintained ontology bundles reject path traversal, symlinked sources, and
  source hash drift before knowledge import. Every source is scanned, including
  inspected sources that provide no accepted evidence. The maintained demo
  preserves a poisoned fixture as queryable threat data and refuses to use it
  as accepted claim evidence.
- Model extraction accepts only operator-selected leaf anchors and produces
  proposal records with no commit authority. Deterministic validation checks
  concept scope, hierarchy cycles, exact-quote occurrence, anchor membership,
  offsets, hashes, and cross-references. Extra tool or destination fields are
  rejected. Failed or invalid calls retain no raw model output.
- Canonical authority is one-way: version-controlled ontology and schemas plus
  immutable records and blobs produce truth snapshots; SQLite and Markdown are
  disposable projections. Projection data can never authorize changes to
  canonical state.
- Projection stamps bind SQLite to an exact truth snapshot and logical
  schema/row digest. Canonical drift requires a reviewed successor snapshot;
  projection drift requires discard and rebuild.
- Access to deposited content is enforced by the deployment boundary, not
  ontology records. Handling labels are advisory and do not provide
  confidentiality. Anyone authorized for a deployment may be able to query all
  indexed deposits; hard isolation currently requires a separate deployment or
  store root.
- External model clients require deterministic authorization before network
  I/O. Provider names are bound to exact HTTPS endpoints and models; local
  providers must use literal loopback addresses, redirects are rejected,
  unknown data is local-only, and source content must be explicitly marked
  `external_allowed`. Model text cannot supply or change these authorization
  inputs.
- Automatic external calls reserve worst-case usage in a transactional SQLite
  ledger before network I/O. Non-metered account exclusions affect dollar
  totals only; call caps, token limits, routing rules, and audit records remain.
- The usage ledger is authoritative only for operational budget enforcement,
  not ontology truth. Deployment permissions and backups must protect it;
  model processes and retrieved content have no direct ledger-write path.
- CLI budget overrides derive an authenticated principal from the local OS
  account and become expiring, single-use receipts bound to an exact request.
  A bare model/source-provided approval boolean is not accepted.

The current repository implements the data models, immutable store, deterministic
policy decision logic, fixed workflow transitions, typed query validation,
offline and governed network connectors, and a Bubblewrap boundary for native
document parsing. Broader model-process isolation, deployment-wide network
egress controls, a production committer, signatures, and human approval UI
remain future deployment work.

## Reporting

Do not include live secrets, raw private source data, or functioning indirect
prompt-injection payloads in a public issue. Contact the maintainers privately
before publishing sensitive details.
