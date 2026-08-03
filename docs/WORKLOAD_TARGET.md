# Initial workload target

## Production profile

The initial production target is a local, single-user CLI:

- one deterministic canonical writer;
- up to four research workers producing proposals or immutable artifacts;
- up to four local query readers;
- serialized writes into canonical state and operational ledgers;
- no multi-host availability or horizontal-write requirement.

Parallel research does not create multiple canonical writers. Workers may fetch,
parse, extract, and stage independently, but accepted state passes through one
deterministic committer.

## Benchmark tiers

Generated, reproducible fixtures will exercise:

| Tier | Claims | Purpose |
| --- | ---: | --- |
| Smoke | 10,000 | Pull-request correctness and regression checks |
| Standard | 100,000 | Routine local development and release checks |
| Scale | 1,000,000 | Periodic capacity and migration evidence |

Evidence, source-version, controversy, provenance, and ontology-edge fixtures
should scale proportionally rather than benchmarking an isolated flat claim
table.

## Evaluation priorities

The ordered priorities are:

1. inspectability;
2. deterministic rebuilds;
3. crash recovery;
4. portability;
5. local query latency.

This ordering permits a faster backend only when it preserves the earlier
properties or when measured product requirements justify the tradeoff.

## Graph-backend migration

SQLite remains the projection backend unless measurements show that the
accepted workload cannot be served adequately, or the product target changes
to multi-user, multi-host, or high-availability operation.

A migration proposal must include:

- reproducible fixture and hardware description;
- exact query mix and concurrency;
- latency distributions, throughput, database size, and rebuild time;
- writer queueing and lock-contention observations;
- crash/recovery and drift-check behavior;
- comparison against at least one candidate backend.

The graph-backend decision is therefore deferred evidence gathering, not an
unanswered architectural preference.
