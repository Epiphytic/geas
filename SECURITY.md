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
- External providers are opt-in. The local DeepSeek endpoint is the default.

The current repository implements the data models, immutable store, deterministic
policy decision logic, fixed workflow transitions, typed query validation, and
the offline local connector. Process/container isolation, network egress
controls, hardened network connectors, a production committer, signatures, and
human approval UI remain future deployment work.

## Reporting

Do not include live secrets, raw private source data, or functioning indirect
prompt-injection payloads in a public issue. Contact the maintainers privately
before publishing sensitive details.
