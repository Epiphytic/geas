# SQLite projection benchmarks

The benchmark is reproducible from the CLI:

```bash
uv run research-agent projection-benchmark --tier smoke
uv run research-agent projection-benchmark --tier standard
```

It measures the complete local path: content-addressed canonical writes, truth
snapshot inventory, atomic SQLite build and stamp, a broad FTS5 query, database
size, and process peak RSS. Synthetic claims all match the query, making query
latency intentionally less selective than normal topic retrieval.

Measurements on the development host on 2026-08-02:

| Tier | Claims | Canonical write | Snapshot | Rebuild | Median query | DB size | Peak RSS |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| smoke | 10,000 | 0.43 s | 0.06 s | 0.91 s | 10.71 ms | 10.7 MiB | 132 MiB |
| standard | 100,000 | 3.25 s | 0.60 s | 5.91 s | 103.53 ms | 107.1 MiB | 132 MiB |
| scale | 1,000,000 | 31.84 s | 6.27 s | 62.36 s | 1,068.96 ms | 1.04 GiB | 241 MiB |

An early file-per-record implementation required 65.65 seconds to durably write
10,000 claims. Content-addressed JSON record batches reduced that to 0.43
seconds. A streaming projection digest reduced 100,000-claim peak RSS from
approximately 611 MiB to 132 MiB.

These measurements support retaining SQLite for the local single-user CLI
through the configured million-claim scale tier. The scale query deliberately
matched every synthetic claim and therefore measures a worst-case global rank,
not a selective topic lookup.

No production graph-backend migration is justified for the accepted workload.
These results do not establish multi-user service or distributed-write
behavior; either requirement would reopen the workload and backend decision.
