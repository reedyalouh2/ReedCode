# Routing by cache the engine can actually reuse

I built a CPU prototype that brings hybrid-cache accounting into Dynamo's native
selection service. I also found and patched a separate stale-cache-credit case
in its draft carrier-routing stack. Both use the real Rust selection code.

## Hybrid-cache accounting

A hybrid model needs both its attention cache and the recurrent state at the
matching boundary. Counting attention blocks alone can make an unusable worker
look like the best destination.

| Qwen3.5 cache fixture | Worker A | Worker B | Selected worker |
| --- | ---: | ---: | --- |
| Attention-only estimate | 3,264 | 2,720 | A |
| Engine's reusable tokens | 0 | 2,720 | B |
| Prototype estimate | 0 | 2,720 | B |

The prototype carries cache-group metadata through registration, reads sparse
cache events over ZeroMQ, and updates selection, reservations and queued-request
refresh. Missing events and invalid state withdraw credit until it can be rebuilt.

The native suite passed **1,284 tests**. Replays with one and four indexer threads
each passed 68 lookup checks, eight worker-choice checks and 13 adverse lifecycle
cases. The fixtures use vLLM's actual CPU allocation and lookup code, with both
vLLM 0.30.0 and a pinned main revision. [Results](hybrid-results.json),
[patch](hybrid.patch), [source pins](pins.json).

The release build exposed a scaling problem in my implementation. At 65,536
tokens and 32 workers, selection added a median **34.881 ms** across 20 pairs.
It repeats prefix work across workers and groups. This needs further work before
broader deployment. [Cost results](hybrid-cost.json), [raw timings](hybrid-timings.jsonl).

This implements part of Dynamo's existing
[hybrid-router direction](https://github.com/ai-dynamo/dynamo/issues/12169).
The event contract builds on [vLLM #44488](https://github.com/vllm-project/vllm/pull/44488)
and [#57358](https://github.com/vllm-project/vllm/pull/57358).

## Stale credit during carrier resynchronization

In the [draft carrier stack](https://github.com/ai-dynamo/dynamo/pull/15555)
tested on October 7, a missing feed update or changed hub epoch invalidated the
replica's cursor but left its old cache index readable. The router could still
select a worker using cache credit it knew was out of date.

| Controlled case | Original | Fixed |
| --- | ---: | ---: |
| Credit after a sequence gap | 12 tokens | 0 tokens |
| Credit during a held-open resnapshot | 3 blocks | 0 blocks |
| Two-worker selection | A, stale | B, synced |
| Prefill booked for the selected worker | 0 tokens | 4 tokens |

The [one-file fix](carrier-fix.patch) publishes the manifest map and its validity
together. A gap suspends depth queries while retaining known cache kinds; a new
snapshot becomes queryable after its buffered updates have been reconciled.

All **six regression tests fail on the original code and pass with the fix**.
The full fixed library suite passed **1,298 tests**. One regression holds an HTTP
snapshot response open and checks the native selector during the recovery wait.
[Tests](carrier-tests.patch), [results](carrier-results.json),
[saved test summaries](test-results.txt).

## Run the tests

Requirements: Python 3.12+, Rust/Cargo 1.96.1, `patch`, and Dynamo's native build
dependencies. From the repository root:

```sh
uv run --no-project --python 3.12 python experiments/dynamo-routing-20261007/reproduce.py hybrid --output /tmp/reedcode-hybrid
uv run --no-project --python 3.12 python experiments/dynamo-routing-20261007/reproduce.py carrier --full --output /tmp/reedcode-carrier
```

The script downloads a pinned upstream source archive, checks its hash and
applies the patches in a fresh directory. The carrier run checks the exact six
failures before applying the fix. Add `--prepare-only` to check patch application
without compiling, or `--archive PATH` to use a cached source archive.

These commands run the Rust tests. The captured engine/service replay results
and release timings above are saved evidence from the original experiments.

## Limits

Both studies are CPU-only. The hybrid prototype reaches the standalone selection
service; the ordinary frontend path and live GPU serving remain unqualified. It
supports local-device full attention plus align-mode Mamba with TP=PP=DP=1 and
requires the matching sparse-event contract. The carrier finding concerns the
pinned, unmerged PR stack. Neither study measures production incident frequency,
GPU latency or serving throughput.
