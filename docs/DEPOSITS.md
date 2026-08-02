# User deposits and deployment-level authorization

## Initial authorization boundary

The initial system assumes that the site, API, filesystem, and ontology
interface are available only to authorized users. It does not implement
per-source, per-claim, per-topic, or per-branch access control.

Consequences:

- every authorized user of a deployment may be able to retrieve every indexed
  deposit;
- `scope_label` is descriptive metadata, not an authorization decision;
- changing a handling label does not conceal or encrypt content;
- the deployment must not be exposed without suitable authentication,
  filesystem permissions, backups, and transport security;
- separate deployments or store roots are required when hard isolation is
  needed in the initial version.

## User-controlled defaults

`config/deposit-policy.yaml` supplies editable defaults for:

- workspace scope label;
- whether content should be indexed;
- whether it should be considered for ontology extraction;
- local-preferred or external-allowed model routing;
- redistribution status;
- retention policy.

Each deposit may override every default. The checked-in default is
`workspace_ungated`: the content is indexed and can contribute to the ontology
for users already authorized to access the deployment.

These fields record intent. Downstream indexers, model routers, exporters, and
retention jobs must implement their relevant fields before those automated
behaviors can be considered enforced.

## Required provenance

A deposit record always binds:

- exact content-addressed source version;
- depositor identity;
- deposit time and acquisition method;
- original filename and optional locator;
- effective user-selected defaults;
- optional license, rights basis, and provenance note;
- deposit-policy version.

Rights and redistribution metadata do not determine who may read the ontology.
They preserve enough context for later review, export decisions, and migration
to more nuanced controls if needed.

## Deposit mechanisms

The initial mechanism accepts local files and user-created exports, including
browser saves, email exports, Zotero exports, and API exports. It does not give
an agent browser cookies or authenticated sessions.

```bash
uv run research-agent deposit-add paper.pdf \
  --deposited-by user:researcher \
  --method browser_save \
  --original-locator https://publisher.example/paper \
  --rights-basis "user-provided licensed copy"
```

For an override:

```bash
uv run research-agent deposit-add internal.txt \
  --deposited-by user:operator \
  --scope-label operator_chosen_ungated \
  --model-route external_allowed \
  --redistribution-status granted
```

The immutable store currently relies on deployment-level disk protection; it
does not add application-level encryption to individual deposits.
