# Source of truth and drift management

## Authority order

The system has one-way authority:

1. Version-controlled ontology and controlled-vocabulary files define semantic
   meaning.
2. Version-controlled ingestion, research, model-use, budget, deposit, and
   source policies define trusted operational defaults.
3. Version-controlled Pydantic schema source defines operational record
   envelopes and validation.
4. Immutable, content-addressed JSON records and source blobs preserve knowledge
   history and evidence.
5. A `TruthSnapshot` binds the exact policy, ontology, schemas, records, and
   blobs into one content-derived state.
6. SQLite and generated Markdown are disposable projections of that snapshot.
7. Reports and model answers are transient views of a selected projection.

Later layers cannot silently modify earlier layers. In particular, SQLite is
never reconciled back into ontology files or immutable records.

Checked-in source intent declares desired coverage, refresh cadence, and
bounded acquisition work. It is data, not authority. Current locally trusted
capability decisions, connector policy, storage rights, threat policy, model
policy, and budgets must authorize each side effect. Repository content and
retrieved sources cannot grant, widen, or replace those decisions.

Discovery observations, request receipts, access constraints, source-work
checkpoints, source versions, and parsed derivations are immutable operational
history. A completed phase may be resumed only through its exact predecessor
chain and source identity. Fetched bytes remain untrusted even after their hash
and provenance are recorded. Extraction over eligible, untainted anchors is
proposal-only; neither the source nor the model can select policy, write
canonical knowledge, or publish a branch.

An ontology Git repository may contain an `artifacts.yaml` manifest pointing
to content-addressed private release assets. This is a distribution and cache
mechanism only. The manifest records input revisions, hashes, sizes, and an
explicit storage-rights basis; it does not make SQLite or generated Markdown
canonical. Hydration verifies the remote digest, local SHA-256, SQLite
integrity, and embedded projection metadata. Drift is still repaired by
discarding or rebuilding the projection, never by promoting its rows.

Maintained bundle YAML and Markdown source cards under `ontology/` are
canonical workspace inputs. Bundle imports create immutable source,
provenance, structural, citation, claim, controversy, gap, threat, and receipt
records. Changing a card without updating its declared SHA-256 fails before
import; changing the card or declaration also changes the truth snapshot.

Validated model extraction output is canonical audit history only as a
quarantined proposal. Its `review_state` is fixed to `proposed`, its commit
authority is `none_proposal_only`, and SQLite proposal search cannot promote it
into accepted knowledge.

A version-controlled promotion manifest is the sole bridge from such a
proposal to accepted records. It has authority only when its exact bytes are
read from its declared canonical local Git branch and pass deterministic
verification. GitHub pull requests, GitLab merge requests, Radicle patches,
reviews, and repository automation are transport and governance layers; their
API state is not read as ontology truth. See
[`PROMOTIONS.md`](PROMOTIONS.md).

A repository update receipt can prove which deterministic files were created,
which capability decisions authorized publication, and how to resume or remove
owned outputs. It cannot prove a claim true. Likewise, a successful pull
request or GitHub App merge does not promote model proposals by itself.

### Operational ledger exception

“SQLite is disposable” applies to ontology/query projections. The separate
`usage.sqlite` database is authoritative operational state for budget
reservations and settlements. It has no authority over ontology meaning,
claims, evidence, or truth snapshots and is never reconciled into them.

The usage ledger must be protected and backed up as deployment state. A pending
reservation remains charged conservatively after a failed or interrupted
request. Agents and model processes receive no direct database-write authority;
only the deterministic budget component may reserve or settle usage.

The exact canonical paths and reconciliation actions are declared in
the managed `truth-policy.yaml`; `config/truth-policy.yaml` is its tracked
packaged template and maintained-workspace input. A policy change is itself detected as canonical
drift.

## Truth snapshots

A truth snapshot inventories:

- canonical ontology and vocabulary files;
- operational schema source files;
- canonical hashes and stored-byte hashes for immutable JSON records;
- content hashes for source blobs;
- the truth-policy hash;
- predecessor, creator, timestamp, and builder version.

Truth-snapshot records are excluded from their own inventory. A corrupt record
or blob whose filename does not match its canonical content fails verification
closed.

Capture and check a snapshot:

```bash
uv run geas truth-snapshot \
  --root data \
  --created-by operator:example

uv run geas truth-check \
  data/records/truth-snapshot/aa/example.json \
  --root data
```

`truth-check` exits with status 2 when files, records, blobs, schemas, or policy
have been added, removed, or changed relative to the selected snapshot.

## SQLite projection stamps

After a projection builder completes its transaction, it stamps SQLite with:

- truth-snapshot ID and logical state digest;
- projection schema and builder versions;
- a deterministic digest of SQLite schema objects and logical row contents;
- stamp time.

`projection-check` independently recalculates canonical and SQLite state. It
distinguishes:

- missing or unstamped projection;
- projection built from an older truth snapshot;
- SQLite rows or schema mutated after the stamp;
- canonical ontology or records changed after the snapshot.

The only permitted repairs are:

| Condition | Required action |
| --- | --- |
| Canonical state changed intentionally | Review it, create a successor truth snapshot, rebuild |
| Canonical file or record changed unexpectedly | Stop and investigate; do not bless the database |
| Projection missing, stale, or mutated | Discard and rebuild SQLite from the selected snapshot |
| Projection clean | Continue |

The drift sentinel exists now. The complete M4 projection builder and migrations
will call the same stamp only after all typed tables and indexes are built.
