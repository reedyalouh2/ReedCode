# Cache preparation checks

These four workloads exercise a local cache and scheduling model. The service costs are chosen inputs. They do not estimate GPU latency or show a Dynamo speedup.

| Workload | Check |
| --- | --- |
| `quiet.json` | Ordinary decoding already caches the known prefix. Preparation should add no useful work. |
| `eviction-pressure.json` | Unrelated traffic evicts context during a tool wait. Preparation can restore it before the next request. |
| `revised-cancelled.json` | A revealed revision replaces the intended context, and another workflow is cancelled. Completed unused work remains charged. |
| `shared-prefix.json` | Two continuations share a known prefix. Cancelling one workflow leaves the other able to use the prepared block. |

Run a workload from the repository root:

```bash
python3 dynamo_headroom.py experiments/dynamo-headroom/workloads/quiet.json --output /tmp/reedcode-headroom-quiet.json
```

The output file must not already exist. Each report includes all three policies, an event log, source and workload hashes, and the model assumptions. `off` uses ordinary caching. `eager` prepares known missing blocks when compute is available. `future_timing` also knows future readiness and revision times. It can only prepare contents already revealed to it.

Block labels stand for complete synthetic token blocks. Their parent lineage determines cache identity. A tool's new output appears only in its real request; it is absent from preparation. The revised and shared cases explicitly reveal changed context during the wait. ReedCode does not yet implement that serving-side control path.

There is one serialized compute resource. Demand requests take priority at dispatch; a running preparation chunk finishes before another request can start. Cache capacity includes active reservations, and each condition starts empty. Speculation shares a fixed block budget. Full service cost is charged when work starts, including work that outlasts the observation window. Cancelled and timed-out workflows retain their full completion penalty.

The unit tests also cover a case where preparation delays unrelated traffic, prefix-lineage mismatches, hidden future content, revisions during computation, and deadlines. The [protocol](../../docs/dynamo-headroom-protocol.md) describes the GPU checks needed before drawing performance conclusions.

The [saved outputs](results/) retain every diagnostic. Quiet caching gains nothing. Both preparation policies restore evicted context in the pressure case. Future timing performs less unused preparation in the revision case, but its completion time ties eager preparation. The tests also show how an eager chunk can delay an unrelated request. These checks justify testing those mechanisms on a GPU; they establish no scheduling advantage there.
