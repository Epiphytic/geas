# Source of truth and drift management

## Authority order

The system has one-way authority:

1. Version-controlled ontology and controlled-vocabulary files define semantic
   meaning.
2. Version-controlled ingestion, research, and source policies define trusted
   operational defaults.
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

The exact canonical paths and reconciliation actions are declared in
`config/truth-policy.yaml`. A policy change is itself detected as canonical
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
uv run research-agent truth-snapshot \
  --root data \
  --created-by operator:example

uv run research-agent truth-check \
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
