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

The managed `deposit-policy.yaml` in the user configuration root supplies
editable defaults (with `config/deposit-policy.yaml` as its packaged template)
for:

- workspace scope label;
- whether content should be indexed;
- whether it should be considered for ontology extraction;
- local-preferred or external-allowed model routing;
- redistribution status;
- permission to archive, quote, transform, or redistribute the original;
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

## Unknown and known metadata

The default is explicitly unknown for:

- authorship;
- license;
- usage conditions;
- rights basis and source provenance;
- redistribution status;
- archive, quotation, transformation, and original-content redistribution
  permissions.

Unknown means “not yet established,” not prohibited. When supplied, authors,
license, conditions, rights basis, and provenance are stored with a `declared`
status. Permission states are independently `unknown`, `allowed`, or
`not_allowed`; a license string does not silently infer permissions. The legacy
redistribution status and `redistribute_original` permission are kept
deterministically synchronized and contradictory values are rejected.

```bash
uv run geas deposit-add paper.pdf \
  --deposited-by user:researcher \
  --author "Ada Example" \
  --author "Lin Example" \
  --license CC-BY-4.0 \
  --usage-condition "Attribution required" \
  --quote-permission allowed \
  --redistribute-original-permission allowed
```

## Nostr signature evidence

The deposit command accepts repeatable Nostr event files:

```bash
uv run geas deposit-add dataset.csv \
  --deposited-by user:researcher \
  --nostr-ownership-event ownership-event.json
```

Authorship and publication evidence use `--nostr-authorship-event` and
`--nostr-publication-event`. The verifier fails closed unless:

1. the event ID matches the exact NIP-01 serialization;
2. its BIP-340 Schnorr signature verifies against its Nostr public key;
3. it is a NIP-94 kind `1063` file-metadata event; and
4. an `x` or `ox` tag equals the deposited file's SHA-256 hash.

The complete event, claimed relation, binding tag, file hash, and verification
method are preserved in the immutable deposit record. This is cryptographic
evidence that a Nostr key signed a file-bound event. It is not, by itself,
proof of the signer's civil identity, copyright ownership, or legal authority.

## Deposit mechanisms

The initial mechanism accepts local files and user-created exports, including
browser saves, email exports, Zotero exports, and API exports. It does not give
an agent browser cookies or authenticated sessions.

```bash
uv run geas deposit-add paper.pdf \
  --deposited-by user:researcher \
  --method browser_save \
  --original-locator https://publisher.example/paper \
  --rights-basis "user-provided licensed copy"
```

For an override:

```bash
uv run geas deposit-add internal.txt \
  --deposited-by user:operator \
  --scope-label operator_chosen_ungated \
  --model-route external_allowed \
  --redistribution-status granted
```

The immutable store currently relies on deployment-level disk protection; it
does not add application-level encryption to individual deposits.
